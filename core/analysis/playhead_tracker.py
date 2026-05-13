"""
PlayheadTracker — a faithful Python port of beat-link's TimeFinder algorithm.

Reference: https://github.com/Deep-Symmetry/beat-link/blob/main/src/main/java/
           org/deepsymmetry/beatlink/data/TimeFinder.java

Key differences from naive approaches
--------------------------------------
* Status-packet updates are treated as NON-definitive (inferred).
  Only beat packets produce definitive anchors.
* Position is interpolated from the LAST definitive (beat) anchor, not from the
  last status packet.  The status beat-number is used purely as a sanity-check:
  if interpolated position maps to a beat that is more than 1 beat away from the
  reported beat-number we correct to timeOfBeat(beatNumber).
* When the player stops, position is frozen at the last known ms value.
* Reverse playback: beat packets are never sent while playing backwards, so
  position must be inferred as (anchor_ms - elapsed * pitch).
* Loop handling: no explicit loop tracking needed — the beat-number sanity check
  above naturally catches and corrects loop-wraps within 1 status packet cycle.
"""

from __future__ import annotations

import logging
import time
from core.analysis.beat_grid import TrackBeatGrid
from core.devices.player_state import PlayerState, PlayStateRaw

log = logging.getLogger(__name__)

_BEAT_PLAYING_GRACE_S = 0.75
_PRECISE_HOLD_S = 0.25
_MAX_BEAT_PHASE_CORRECTION_MS = 80.0
_MAX_BACKSTEP_MS = 5.0
_PLAY_START_BEAT_GUARD_S = 0.28
_PLAY_START_STATUS_DEFER_S = 1.10
_JOG_CUE_REFINE_S = 0.35

_EXPLICIT_NOT_PLAYING_STATES = {
    PlayStateRaw.NO_DISC,
    PlayStateRaw.LOADING,
    PlayStateRaw.PAUSED,
    PlayStateRaw.PAUSED_CUE,
    PlayStateRaw.STOPPED,
    PlayStateRaw.STOPPED_CUE,
    PlayStateRaw.END_OF_TRACK,
}

# States that positively confirm forward/active playback regardless of flags byte.
_ACTIVE_PLAYING_STATES = {
    PlayStateRaw.PLAYING,
    PlayStateRaw.LOOP,
    PlayStateRaw.CUE_PLAY,
    PlayStateRaw.REVERSE,
    PlayStateRaw.EMERGENCY_LOOP,
}


class _Anchor:
    """Equivalent to TimeFinder's TrackPositionUpdate stored in positions dict."""

    __slots__ = (
        "timestamp",       # float: time.monotonic() when this was recorded
        "milliseconds",    # float: ms into track at anchor time
        "beat_number",     # int: reported beat number at anchor
        "definitive",      # bool: came from beat packet (True) or status (False)
        "playing",         # bool
        "pitch",           # float: pitch multiplier (1.0 = normal)
        "reverse",         # bool
        "beat_grid",       # TrackBeatGrid | None
    )

    def __init__(
        self,
        timestamp: float,
        milliseconds: float,
        beat_number: int,
        definitive: bool,
        playing: bool,
        pitch: float,
        reverse: bool,
        beat_grid: TrackBeatGrid | None,
    ) -> None:
        self.timestamp = timestamp
        self.milliseconds = milliseconds
        self.beat_number = beat_number
        self.definitive = definitive
        self.playing = playing
        self.pitch = pitch
        self.reverse = reverse
        self.beat_grid = beat_grid

    def interpolate(self, now: float) -> float:
        """Equivalent to TimeFinder.interpolateTimeSinceUpdate()."""
        if not self.playing:
            return self.milliseconds
        elapsed_ms = (now - self.timestamp) * 1000.0
        moved = self.pitch * elapsed_ms
        if self.reverse:
            return self.milliseconds - moved
        return self.milliseconds + moved


def _pitch_from_state(state: PlayerState) -> float:
    """Return pitch as a multiplier (1.0 = normal speed), equivalent to pitchToMultiplier().

    Guard: never return 0.0 — a zero multiplier would permanently freeze
    interpolation.  Hardware sending bytes 0x00 0x00 0x00 at the pitch offset
    produces state.pitch=-1.0 which should never happen on a playing deck.
    """
    mult = 1.0 + float(state.pitch)
    # CDJ max pitch range is ±16 % wide mode → multiplier range ~0.84..1.16.
    # Below 0.1 almost certainly means a parsing error; clamp to a safe minimum.
    return max(0.1, mult)


class PlayheadTracker:
    """
    Robust playhead estimator — faithfully ported from beat-link TimeFinder.

    Responsibilities (matching TimeFinder):
    - status packets (ingest_state): sanity-check interpolation against reported
      beat-number; correct if more than 1 beat off.
    - beat packets (ingest_beat): definitive anchor; handle beat-before-status race.
    - tick (UI interpolation timer): apply interpolateTimeSinceUpdate from last anchor.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._anchor: _Anchor | None = None
        self._duration_ms: int = 0
        self._beat_grid: TrackBeatGrid | None = None
        self._last_beat_packet_t: float | None = None
        self._last_precise_packet_t: float | None = None
        self._last_play_start_t: float | None = None
        self._awaiting_play_start_beat: bool = False
        self._eff_bpm: float = 0.0
        self._cue_point_ms: float | None = None
        self._pending_jog_cue_refine_until_t: float | None = None
        self._last_play_state_raw: PlayStateRaw | None = None
        self._loop_end_ms: float = 0.0
        self._loop_start_ms: float = 0.0
        self._loop_active: bool = False

    # ── Public properties ───────────────────────────────────────────────────────

    @property
    def duration_ms(self) -> int:
        return self._duration_ms

    @property
    def effective_bpm(self) -> float:
        if self._anchor is None or self._beat_grid is None:
            return 0.0
        # effective_bpm = pitch_multiplier × (track_bpm derived from beat_grid interval)
        # We don't store track_bpm separately; derive it from anchor.pitch applied to
        # any stored bpm.  Since we set pitch = pitchToMultiplier(state.pitch) we can
        # recover effective bpm if we kept it.  Store it separately for convenience.
        return self._eff_bpm

    @property
    def current_beat_number(self) -> int:
        if self._anchor is None:
            return 0
        return int(max(0, self._anchor.beat_number))

    @property
    def cue_point_ms(self) -> float | None:
        """Return the stored cue-point position in ms, or None if not yet set."""
        return self._cue_point_ms

    # ── Public mutators ─────────────────────────────────────────────────────────

    def set_beat_grid(self, beat_grid: TrackBeatGrid | None) -> None:
        self._beat_grid = beat_grid
        # When a new beat grid arrives (= new track), the old anchor is stale.
        if self._anchor is not None and self._anchor.beat_grid is not beat_grid:
            self._anchor = None
            self._cue_point_ms = None

    def ingest_state(self, state: PlayerState, now: float | None = None) -> int:
        """
        Process a CDJ status-packet equivalent.  Returns best position estimate (ms).
        Mirrors TimeFinder updateListener (non-definitive update path).

        Key: we NEVER snap to a stored cue point here.  Pre-CDJ-3000 players always
        report position_ms=0, so any "snap to cue" logic would always snap to 0 and
        destroy forward progress.  Instead we freeze in place on any paused state and
        rely on the beat_number sanity check to correct after a hardware CUE snap.
        """
        now_t = now if now is not None else time.monotonic()

        if state.track_duration_ms > 0:
            self._duration_ms = int(state.track_duration_ms)

        pitch_mult = _pitch_from_state(state)
        reverse = (state.play_state_raw == PlayStateRaw.REVERSE)
        prev_play_state = self._last_play_state_raw
        self._last_play_state_raw = state.play_state_raw
        if state.play_state_raw not in {PlayStateRaw.PAUSED_CUE, PlayStateRaw.STOPPED_CUE}:
            self._pending_jog_cue_refine_until_t = None
        # Determine playing: combine the FLAG_PLAYING-derived bool with explicit
        # play_state_raw check so hardware that fails to set FLAG_PLAYING (some
        # XDJ models) is still treated as playing when the state byte confirms it.
        explicitly_paused = state.play_state_raw in _EXPLICIT_NOT_PLAYING_STATES
        explicitly_playing = state.play_state_raw in _ACTIVE_PLAYING_STATES
        playing = (bool(state.is_playing) or explicitly_playing) and not explicitly_paused
        is_paused_cue = state.play_state_raw in {PlayStateRaw.PAUSED_CUE, PlayStateRaw.STOPPED_CUE}
        is_jog_search = (state.play_state_raw == PlayStateRaw.JOG_SEARCH)
        beat_num = int(state.beat_number)
        beat_in_bar = int(state.beat_in_bar)
        beat_grid = self._beat_grid

        log.debug(
            "TRACKER slot=%d playing=%s is_playing=%s state=%s beat_num=%d "
            "bpm=%.2f pitch=%.3f grid=%s anchor_ms=%s anchor_playing=%s dur=%d",
            state.player_number, playing, state.is_playing,
            state.play_state_raw.name if hasattr(state.play_state_raw, 'name') else state.play_state_raw,
            beat_num, state.bpm, state.pitch,
            f"{beat_grid.beat_count}beats" if beat_grid else "None",
            f"{self._anchor.milliseconds:.0f}" if self._anchor else "None",
            self._anchor.playing if self._anchor else "N/A",
            self._duration_ms,
        )

        # Track effective BPM for external callers (waveform grid rendering).
        raw_bpm = float(state.bpm) if state.bpm > 0 else 0.0
        self._eff_bpm = raw_bpm * pitch_mult if raw_bpm > 0 else 0.0

        last = self._anchor

        # If cue-play starts from a paused-cue state and no cue latch exists yet,
        # seed it from the current anchor so cue release can reliably snap back.
        if (
            last is not None
            and self._cue_point_ms is None
            and prev_play_state in {PlayStateRaw.PAUSED_CUE, PlayStateRaw.STOPPED_CUE}
            and state.play_state_raw == PlayStateRaw.CUE_PLAY
        ):
            self._cue_point_ms = max(0.0, float(last.interpolate(now_t)))
            log.debug("TRACKER cue latch seeded at %.0fms on cue-play start", self._cue_point_ms)

        # ── PPOS hold ─────────────────────────────────────────────────────────
        # CDJ-3000 class: precise packets are definitive; don't let coarse status
        # packets override them for a brief window after each precise update.
        if (
            last is not None
            and last.beat_grid is beat_grid
            and self._last_precise_packet_t is not None
            and (now_t - self._last_precise_packet_t) <= _PRECISE_HOLD_S
        ):
            held_time = last.interpolate(now_t)
            self._anchor = _Anchor(
                timestamp=now_t,
                milliseconds=held_time,
                beat_number=max(last.beat_number, beat_num),
                definitive=True,
                playing=playing,
                pitch=pitch_mult,
                reverse=reverse,
                beat_grid=beat_grid,
            )
            return int(max(0, held_time))

        # ── CUE_PLAY start: reseed beat number from stored cue position ─────────
        # Each brief CUE press (PAUSED_CUE → CUE_PLAY → PAUSED_CUE) may fire a
        # beat packet, advancing anchor.beat_number by 1.  On the NEXT CUE_PLAY
        # the tracker therefore starts one beat ahead, and the *first* beat packet
        # during sustained play fires at (stale_beat + 1) instead of
        # (cue_beat + 1), causing an immediate visible jump of 1 beat.
        # Fix: re-derive beat_number from the actual stored cue-point position
        # every time we enter CUE_PLAY from a latched-cue state.
        if (
            state.play_state_raw == PlayStateRaw.CUE_PLAY
            and prev_play_state in {PlayStateRaw.PAUSED_CUE, PlayStateRaw.STOPPED_CUE}
            and self._cue_point_ms is not None
            and beat_grid is not None
            and last is not None
        ):
            cue_beat = max(1, int(beat_grid.find_beat_at_time(self._cue_point_ms)))
            self._anchor = _Anchor(
                timestamp=now_t,
                milliseconds=self._cue_point_ms,
                beat_number=cue_beat,
                definitive=True,
                playing=True,
                pitch=pitch_mult,
                reverse=False,
                beat_grid=beat_grid,
            )
            # Reset duplicate-beat guard so the first genuine beat packet from
            # this CUE_PLAY episode is not rejected as a burst duplicate.
            # NOTE: do NOT reset _last_beat_packet_t to None here.  The player
            # was in PAUSED_CUE (ingest_beat returns early → no beats fire), so
            # the natural time-since-last-beat already exceeds the duplicate
            # window.  Resetting to None lets any stale beat packet queued in
            # UDP fire immediately when PLAY is next pressed, causing a 1-beat
            # forward jump.
            log.debug(
                "TRACKER CUE_PLAY reseed cue_ms=%.0f cue_beat=%d (was %d)",
                self._cue_point_ms,
                cue_beat,
                last.beat_number,
            )
            return int(max(0, self._cue_point_ms))

        # ── No anchor yet or new track ─────────────────────────────────────────
        if last is None or last.beat_grid is not beat_grid:
            if beat_grid is not None and beat_num > 0:
                status_beat = self._status_anchor_beat_number(beat_num, beat_in_bar, playing, beat_grid)
                ms = float(self._time_of_beat(status_beat))
                anchor_beat = status_beat
            else:
                ms = 0.0
                anchor_beat = max(0, beat_num)
            self._anchor = _Anchor(
                timestamp=now_t,
                milliseconds=ms,
                beat_number=anchor_beat,
                definitive=False,
                playing=playing,
                pitch=pitch_mult,
                reverse=reverse,
                beat_grid=beat_grid,
            )
            return int(max(0, ms))

        # ── Paused: freeze in place ────────────────────────────────────────────
        # Do NOT snap to a stored cue-point — we don't know it reliably.
        # After a hardware CUE press the player will begin sending status packets
        # with the new beat_number, and the next playing update will correct via
        # the beat_number sanity check below.
        if not playing:
            status_beat = self._status_anchor_beat_number(beat_num, beat_in_bar, False, beat_grid)
            paused_seek_detected = (
                (not is_paused_cue)
                and beat_grid is not None
                and status_beat > 0
                and status_beat != last.beat_number
            )
            if is_paused_cue:
                # Pioneer behavior: pressing CUE while paused stores a new cue.
                # Status transition PAUSED -> PAUSED_CUE is our reliable signal.
                entering_paused_cue_from_paused = (prev_play_state == PlayStateRaw.PAUSED)
                entering_paused_cue_from_jog = (prev_play_state == PlayStateRaw.JOG_SEARCH)
                entering_paused_cue_from_transport = prev_play_state in {
                    PlayStateRaw.PLAYING,
                    PlayStateRaw.LOOP,
                    PlayStateRaw.CUE_PLAY,
                    PlayStateRaw.REVERSE,
                    PlayStateRaw.EMERGENCY_LOOP,
                }
                entering_paused_cue = entering_paused_cue_from_paused or entering_paused_cue_from_jog
                if entering_paused_cue:
                    # First PAUSED_CUE packet can be stale; allow a short
                    # follow-up window where PAUSED_CUE packets may refine cue.
                    self._pending_jog_cue_refine_until_t = now_t + _JOG_CUE_REFINE_S
                captured = max(0.0, float(last.interpolate(now_t)))
                captured_quantized = self._quantize_to_nearest_beat_ms(captured)

                # Transport-originated CUE return must snap to the stored cue
                # latch immediately. Status beat numbers on this edge can be
                # transiently shifted and must not choose a new cue/grid point.
                if entering_paused_cue_from_transport and self._cue_point_ms is not None:
                    cue_ms = max(0.0, float(self._cue_point_ms))
                    cue_beat = last.beat_number
                    if beat_grid is not None:
                        cue_beat = max(1, int(beat_grid.find_beat_at_time(cue_ms)))
                    self._anchor = _Anchor(
                        timestamp=now_t,
                        milliseconds=cue_ms,
                        beat_number=cue_beat,
                        definitive=False,
                        playing=False,
                        pitch=pitch_mult,
                        reverse=False,
                        beat_grid=beat_grid,
                    )
                    log.debug(
                        "TRACKER cue-return snap stored cue_ms=%.0f cue_beat=%d prev_state=%s",
                        cue_ms,
                        cue_beat,
                        prev_play_state.name if hasattr(prev_play_state, 'name') else prev_play_state,
                    )
                    return int(cue_ms)

                refining_jog_cue = (
                    prev_play_state == PlayStateRaw.PAUSED_CUE
                    and self._pending_jog_cue_refine_until_t is not None
                    and now_t <= self._pending_jog_cue_refine_until_t
                )

                status_cue_ms = None
                status_cue_beat = status_beat
                if (entering_paused_cue or refining_jog_cue) and beat_num > 0:
                    # While setting/refining cue, prefer the raw deck cue beat.
                    # Do not apply paused anchor remapping in this path.
                    status_cue_beat = beat_num
                if beat_grid is not None and status_cue_beat > 0:
                    status_cue_ms = float(self._time_of_beat(status_cue_beat))

                if status_cue_ms is not None:
                    chosen_cue_ms = status_cue_ms
                    # Do not re-latch cue from jittery paused-cue packets after
                    # active playback CUE returns; keep the stored cue stable.
                    at_track_start = (chosen_cue_ms <= 120.0)
                    # Allow re-latch when cue beat changes significantly from a
                    # steady PAUSED_CUE state (user moved jog and re-pressed CUE).
                    # Exclude transport->PAUSED_CUE transitions; those can report
                    # a transient one-beat-shifted status beat.
                    large_cue_jump = (
                        not entering_paused_cue_from_transport
                        and self._cue_point_ms is not None
                        and abs(chosen_cue_ms - self._cue_point_ms) >= 50.0
                        and prev_play_state == PlayStateRaw.PAUSED_CUE
                    )
                    should_update = (
                        self._cue_point_ms is None
                        or entering_paused_cue
                        or refining_jog_cue
                        or at_track_start
                        or large_cue_jump
                    )
                    if should_update:
                        self._cue_point_ms = chosen_cue_ms
                        if refining_jog_cue:
                            self._pending_jog_cue_refine_until_t = None
                        log.debug(
                            "TRACKER cue set at %.0fms (from status beat=%d mapped=%d start=%s refine=%s)",
                            self._cue_point_ms,
                            beat_num,
                            status_cue_beat,
                            "yes" if at_track_start else "no",
                            "yes" if refining_jog_cue else "no",
                        )
                elif entering_paused_cue_from_paused or entering_paused_cue_from_jog:
                    # Fallback when beat number is unavailable.
                    self._cue_point_ms = captured_quantized
                    log.debug(
                        "TRACKER cue set at %.0fms (captured=%.0fms quantized=%s)",
                        self._cue_point_ms,
                        captured,
                        "yes" if self._cue_point_ms != captured else "no",
                    )
                elif self._cue_point_ms is None:
                    # Last-resort fallback when no cue beat is available.
                    self._cue_point_ms = captured_quantized
                    log.debug("TRACKER cue fallback set at %.0fms", self._cue_point_ms)
                frozen = max(0.0, float(self._cue_point_ms if self._cue_point_ms is not None else captured_quantized))
            elif is_jog_search and beat_grid is not None and beat_num > 0:
                # Jog search should move the visual playhead to the searched beat.
                frozen = float(self._time_of_beat(status_beat))
            elif paused_seek_detected:
                # Some XDJ firmware reports PAUSED while the jog wheel is moving.
                # If beat number changes while paused, treat it as an intentional seek.
                frozen = float(self._time_of_beat(status_beat))
            else:
                frozen = last.interpolate(now_t)
            # Keep paused beat index stable by default. Status beat numbers can
            # jitter while paused, and feeding that jitter into the anchor causes
            # resumed playback to quantize from the wrong beat after repeated
            # play/pause cycles.
            beat = last.beat_number
            if is_jog_search or paused_seek_detected:
                if status_beat > 0:
                    beat = status_beat
                elif beat_num > 0:
                    beat = beat_num
            if (
                (not is_jog_search)
                and (not is_paused_cue)
                and (not paused_seek_detected)
                and
                beat_num > 0
                and last.beat_number > 0
                and beat_num + 1 < last.beat_number
            ):
                # Some players keep reporting beat_num=1 while paused/playing.
                # Do not let paused updates collapse the anchor beat index back
                # to bar 1, otherwise next play transition restarts near 00:00.
                log.debug(
                    "TRACKER pause ignore stale beat_num=%d keep=%d",
                    beat_num,
                    last.beat_number,
                )
                beat = last.beat_number

            # When returning CUE_PLAY → PAUSED_CUE the CDJ status beat_num may
            # lag or lead by 1 (hardware briefly crossed a beat boundary during
            # the brief play).  Derive beat_number from the known cue position
            # so the anchor stays consistent with _cue_point_ms.
            if is_paused_cue and self._cue_point_ms is not None and beat_grid is not None:
                beat = max(1, int(beat_grid.find_beat_at_time(self._cue_point_ms)))

            self._anchor = _Anchor(
                timestamp=now_t,
                milliseconds=frozen,
                beat_number=beat,
                definitive=False,
                playing=False,
                pitch=pitch_mult,
                reverse=False,
                beat_grid=beat_grid,
            )
            return int(max(0, frozen))

        # ── Playing: interpolate + sanity check ────────────────────────────────────
        # beat_num=0 means "player not reporting beat" — skip sanity check.
        if beat_num <= 0:
            # Hold interpolated position with existing beat_number.
            interp = last.interpolate(now_t)
            self._anchor = _Anchor(
                timestamp=now_t,
                milliseconds=interp,
                beat_number=last.beat_number,
                definitive=False,
                playing=True,
                pitch=pitch_mult,
                reverse=reverse,
                beat_grid=beat_grid,
            )
            return int(max(0, interp))

        # Interpolate forward, then sanity-check reported beat_number.
        elapsed_ms = (now_t - last.timestamp) * 1000.0
        if reverse:
            interp = max(0.0, last.milliseconds - last.pitch * elapsed_ms)
        else:
            interp = last.milliseconds + last.pitch * elapsed_ms

        # Loop wrap: when interpolation overshoots loop_end, immediately wrap it
        # to loop_start + overshoot so the playhead keeps moving at constant speed.
        # This handles short loops and loops starting at 00:00 where beat_num never
        # changes (so the beat-grid correction branch below never fires).
        if (
            state.loop_active
            and state.loop_end_ms > 0
            and state.loop_start_ms >= 0
            and not reverse
            and interp > state.loop_end_ms
        ):
            loop_len = float(state.loop_end_ms - state.loop_start_ms)
            if loop_len > 0:
                overshoot = interp - float(state.loop_end_ms)
                interp = float(state.loop_start_ms) + (overshoot % loop_len)
            else:
                interp = float(state.loop_start_ms)

        # Track loop state for tick() clamping.
        self._loop_active = bool(state.loop_active)
        self._loop_end_ms = float(state.loop_end_ms) if state.loop_end_ms > 0 else 0.0
        self._loop_start_ms = float(state.loop_start_ms) if state.loop_start_ms >= 0 else 0.0

        # When transitioning from paused → playing, CDJ status beat_num can
        # lead by 1 (it tracks the *next* upcoming beat, not the current one).
        # Re-derive from the grid so ingest_beat()'s "next = last+1" is correct.
        starting_play = (not last.playing) and playing
        start_play_grace = starting_play and (elapsed_ms <= (_BEAT_PLAYING_GRACE_S * 1000.0))
        if starting_play and beat_grid is not None and interp >= 0:
            self._last_play_start_t = now_t
            self._awaiting_play_start_beat = True
            anchor_beat_num = max(1, int(beat_grid.find_beat_at_time(interp)))
        else:
            anchor_beat_num = beat_num
        if beat_grid is not None:
            interp_beat = beat_grid.find_beat_at_time(interp)
            # Right after paused->playing transitions, coarse status beat_num can
            # be stale/noisy; defer large beat_num-driven corrections until the
            # grace window passes so repeated play/pause does not accumulate drift.
            if (not start_play_grace) and abs(interp_beat - beat_num) >= 2:
                defer_status_correction = (
                    self._awaiting_play_start_beat
                    and self._last_play_start_t is not None
                    and (now_t - self._last_play_start_t) <= _PLAY_START_STATUS_DEFER_S
                    and not state.loop_active
                )
                if defer_status_correction:
                    log.debug(
                        "TRACKER defer post-start status correction interp_beat=%d status_beat=%d age=%.0fms",
                        interp_beat,
                        beat_num,
                        (now_t - self._last_play_start_t) * 1000.0,
                    )
                else:
                    corrected = float(self._time_of_beat(beat_num))
                    # Forward-play safeguard: some players intermittently report stale
                    # beat numbers (often 0/1) while still playing. Do not allow such
                    # status packets to drag the anchor backwards toward track start.
                    # Exception: a genuine loop wrap causes beat_num to drop below
                    # last.beat_number; when loop_active is set, accept the correction.
                    is_stale_backward = (
                        (not reverse)
                        and not state.loop_active
                        and beat_num > 0
                        and beat_num < last.beat_number
                        and corrected < (last.milliseconds - 150.0)
                    )
                    if is_stale_backward:
                        log.debug(
                            "TRACKER ignore stale beat_num=%d last=%d corrected=%.0f last_ms=%.0f",
                            beat_num,
                            last.beat_number,
                            corrected,
                            last.milliseconds,
                        )
                        anchor_beat_num = last.beat_number
                    else:
                        if state.loop_active and beat_num < last.beat_number:
                            # Loop wrap: snap to loop_start_ms but preserve forward
                            # motion by carrying over however far past loop_end we
                            # would have travelled.  This keeps the playhead moving
                            # at a constant speed through the boundary instead of
                            # resetting to exactly beat 1 and lingering.
                            loop_end = float(state.loop_end_ms) if state.loop_end_ms > 0 else None
                            if loop_end is not None and last.pitch > 0:
                                overshoot = max(0.0, (last.milliseconds + last.pitch * elapsed_ms) - loop_end)
                                interp = corrected + overshoot
                            else:
                                interp = corrected
                            log.debug(
                                "TRACKER loop wrap beat_num=%d last=%d corrected=%.0f interp=%.0f",
                                beat_num, last.beat_number, corrected, interp,
                            )
                        else:
                            # Jumped or drifted more than 1 beat — correct to reported beat.
                            interp = corrected
                        anchor_beat_num = beat_num

        new_playing = True and (not reverse or interp > 0)
        self._anchor = _Anchor(
            timestamp=now_t,
            milliseconds=interp,
            beat_number=anchor_beat_num,
            definitive=False,
            playing=new_playing,
            pitch=pitch_mult,
            reverse=reverse,
            beat_grid=beat_grid,
        )
        # Preserve cue latch across playback so CUE during play returns to the
        # stored cue point. Latch is reset only on track/grid changes.
        return int(max(0, interp))

    def ingest_beat(
        self,
        pitch_mult: float,
        now: float | None = None,
        next_beat_ms: int | None = None,
        second_beat_ms: int | None = None,
        effective_bpm: float | None = None,
    ) -> None:
        """
        Process a beat packet — definitive anchor.
        Mirrors TimeFinder beatListener.

        The beat packet does NOT carry an absolute beat number; we derive the
        next beat from the last anchor's beat_number + 1.  The 1/5-beat
        heuristic decides whether this beat packet is genuine (player moved far
        enough past the last beat boundary) or a duplicate/early arrival.
        If it\'s too early we simply ignore the packet — we must NOT snap back.
        """
        now_t = now if now is not None else time.monotonic()
        beat_grid = self._beat_grid
        last = self._anchor
        if beat_grid is None or last is None or last.beat_grid is not beat_grid:
            return
        if not last.playing:
            # Ignore beat packets during paused/cue states and state races.
            return
        if effective_bpm is not None and effective_bpm > 0:
            self._eff_bpm = float(effective_bpm)

        # Use the interpolated position for backstep protection and smoothing.
        interpolated_ms = last.interpolate(now_t)

        # Compute beat interval from the grid and drop only true duplicate bursts.
        # CUE/PLAY transitions can make position-based "too early" checks reject
        # valid beats, which pins progression near bar 1.
        if beat_grid.beat_count >= 2:
            beat_interval = float(self._time_of_beat(2) - self._time_of_beat(1))
        else:
            beat_interval = 500.0  # fallback if grid has < 2 beats

        if self._last_beat_packet_t is not None:
            since_last_ms = (now_t - self._last_beat_packet_t) * 1000.0
            duplicate_gap_ms = max(60.0, beat_interval * 0.35)
            if since_last_ms < duplicate_gap_ms:
                log.debug("BEAT DROP duplicate: since_last=%.0fms gap=%.0fms", since_last_ms, duplicate_gap_ms)
                return

        # Phase-aware beat choice:
        # - Normal flow: beat packet advances to next beat.
        # - After rapid play/pause races, anchor beat can transiently be off by
        #   one; using absolute +1 can jump the playhead a full beat ahead.
        interp_beat = max(1, int(beat_grid.find_beat_at_time(interpolated_ms)))
        base_time = float(self._time_of_beat(interp_beat))
        phase_in_beat = max(0.0, interpolated_ms - base_time)
        startup_first_beat = (
            self._awaiting_play_start_beat
            and self._last_play_start_t is not None
            and (now_t - self._last_play_start_t) <= _PLAY_START_STATUS_DEFER_S
        )
        if startup_first_beat:
            # First accepted beat right after play-start is where one-beat
            # overshoots most often happen. Anchor to the currently containing
            # beat (not +1) to keep transport visually continuous.
            next_beat = interp_beat
        elif phase_in_beat < (beat_interval * 0.35):
            next_beat = interp_beat
        else:
            next_beat = min(max(1, interp_beat + 1), beat_grid.beat_count)
        log.debug(
            "BEAT ACCEPT last_beat=%d interp_beat=%d next_beat=%d interp_ms=%.0f startup_first=%s",
            last.beat_number,
            interp_beat,
            next_beat,
            interpolated_ms,
            startup_first_beat,
        )
        beat_time = float(self._time_of_beat(next_beat))

        # Beat packets provide ms-until-upcoming-beat timing. Use it to compensate
        # packet transport delay in fallback (non-precise) mode.
        phase_age_ms = 0.0
        inferred_age = 0.0
        if beat_grid.beat_count >= 2:
            grid_interval = float(self._time_of_beat(2) - self._time_of_beat(1))
        else:
            grid_interval = 500.0
        if next_beat_ms is not None and next_beat_ms > 0:
            interval_hint = grid_interval
            if (
                second_beat_ms is not None
                and second_beat_ms > next_beat_ms
            ):
                timing_interval = float(second_beat_ms - next_beat_ms)
                if 200.0 <= timing_interval <= 2000.0:
                    interval_hint = timing_interval

            # Only apply a conservative correction. Larger values are usually
            # transition jitter (CUE/PLAY races), not true transport latency.
            inferred_age = interval_hint - float(next_beat_ms)
            max_age = min(_MAX_BEAT_PHASE_CORRECTION_MS, interval_hint * 0.2)
            if 0.0 <= inferred_age <= max_age:
                phase_age_ms = inferred_age

        candidate_ms = beat_time + phase_age_ms * max(0.0, float(pitch_mult))

        # Forward-snap guard: reject beat packets whose grid position is more
        # than ~65 % of a beat interval ahead of the current interpolated
        # position.  During normal play the interpolated position tracks the
        # CDJ closely, so a genuine beat arriving ~one interval later is never
        # blocked.  A stale packet queued during PAUSED/CUE fires when the
        # interpolated position is still near the cue point — far behind the
        # snapped-to grid beat — and is correctly dropped here.
        forward_limit_ms = interpolated_ms + beat_interval * 0.65
        in_play_start_guard = (
            self._last_play_start_t is not None
            and (now_t - self._last_play_start_t) <= _PLAY_START_BEAT_GUARD_S
        )
        if in_play_start_guard:
            # Right after play is pressed, stale queued beat packets can arrive
            # and jump the transport noticeably ahead. Use a stricter forward
            # bound for this short window.
            forward_limit_ms = interpolated_ms + beat_interval * 0.35
        if candidate_ms > forward_limit_ms:
            log.debug(
                "BEAT DROP forward-snap: candidate=%.0fms interp=%.0fms limit=%.0fms start_guard=%s",
                candidate_ms, interpolated_ms, forward_limit_ms, in_play_start_guard,
            )
            return

        # Avoid visible backward snaps when a beat packet arrives slightly late.
        # Permit only a tiny correction backwards to keep motion visually stable.
        if not last.reverse:
            floor_ms = interpolated_ms - _MAX_BACKSTEP_MS
            if candidate_ms < floor_ms:
                candidate_ms = floor_ms

        beat_time = candidate_ms

        self._anchor = _Anchor(
            timestamp=now_t,
            milliseconds=beat_time,
            beat_number=next_beat,
            definitive=True,
            playing=True,     # Beat packets only arrive while playing forward.
            pitch=pitch_mult,
            reverse=False,
            beat_grid=beat_grid,
        )
        self._last_beat_packet_t = now_t
        self._awaiting_play_start_beat = False

    def ingest_precise_position(
        self,
        position_ms: int,
        track_duration_ms: int = 0,
        pitch_mult: float = 1.0,
        playing: bool | None = None,
        reverse: bool = False,
        now: float | None = None,
    ) -> int:
        """
        Process a high-rate precise-position update (packet 0x0B).
        This is a definitive absolute position anchor.
        """
        now_t = now if now is not None else time.monotonic()
        if track_duration_ms > 0:
            self._duration_ms = int(track_duration_ms)

        pos = max(0.0, float(position_ms))
        if self._duration_ms > 0:
            pos = min(pos, float(self._duration_ms))

        last = self._anchor
        if (
            last is not None
            and self._beat_grid is not None
            and last.beat_grid is self._beat_grid
            and bool(playing)
            and last.playing
            and not bool(reverse)
        ):
            predicted = max(0.0, float(last.interpolate(now_t)))
            backstep = predicted - pos
            # Ignore impossible backward jumps during steady forward playback.
            # This protects against occasional malformed/stale 0x0B packets
            # that would otherwise yank the playhead back to track start.
            if (
                backstep > 2000.0
                and predicted > 3000.0
                and pos < (predicted * 0.5)
            ):
                log.debug(
                    "PPOS IGNORE jump predicted=%.0fms packet=%.0fms backstep=%.0fms",
                    predicted,
                    pos,
                    backstep,
                )
                pos = predicted

        beat_num = 0
        if self._beat_grid is not None and self._beat_grid.beat_count > 0:
            beat_num = max(1, int(self._beat_grid.find_beat_at_time(pos)))

        if playing is None:
            playing = self._anchor.playing if self._anchor is not None else False

        self._anchor = _Anchor(
            timestamp=now_t,
            milliseconds=pos,
            beat_number=beat_num,
            definitive=True,
            playing=bool(playing),
            pitch=max(0.0, float(pitch_mult)),
            reverse=bool(reverse),
            beat_grid=self._beat_grid,
        )
        self._last_precise_packet_t = now_t
        return int(pos)

    def tick(self, now: float | None = None) -> int | None:
        """
        Interpolate position for the UI timer (equivalent to TimeFinder.getTimeFor).
        Returns None if no position is known.
        """
        if self._anchor is None:
            return None
        now_t = now if now is not None else time.monotonic()
        pos = self._anchor.interpolate(now_t)
        pos = max(0.0, pos)
        # Wrap within loop bounds so the display doesn't overshoot while waiting
        # for the next status packet.  Use modulo so short loops also work.
        if self._loop_active and self._loop_end_ms > self._loop_start_ms and pos > self._loop_end_ms:
            loop_len = self._loop_end_ms - self._loop_start_ms
            pos = self._loop_start_ms + ((pos - self._loop_end_ms) % loop_len)
        if self._duration_ms > 0:
            pos = min(pos, float(self._duration_ms))
        return int(pos)

    # ── Private helpers ─────────────────────────────────────────────────────────

    def _time_of_beat(self, beat_num: int) -> float:
        """Mirrors TimeFinder.timeOfBeat() — extrapolates past last beat if needed."""
        bg = self._beat_grid
        if bg is None:
            return 0.0
        if beat_num <= bg.beat_count:
            return float(bg.time_for_beat(beat_num))
        # Extrapolate using last interval.
        if bg.beat_count < 2:
            return float(bg.time_for_beat(1))
        last_time = float(bg.time_for_beat(bg.beat_count))
        prev_time = float(bg.time_for_beat(bg.beat_count - 1))
        interval = last_time - prev_time
        return last_time + interval * (beat_num - bg.beat_count)

    def _quantize_to_nearest_beat_ms(self, position_ms: float) -> float:
        """Quantize an arbitrary position to the nearest beat-grid point."""
        bg = self._beat_grid
        if bg is None or bg.beat_count <= 0:
            return max(0.0, float(position_ms))

        pos = max(0.0, float(position_ms))
        probe = int(max(1, min(bg.beat_count, bg.find_beat_at_time(pos))))
        candidates = {probe}
        if probe > 1:
            candidates.add(probe - 1)
        if probe < bg.beat_count:
            candidates.add(probe + 1)

        nearest = min(
            (float(bg.time_for_beat(n)) for n in candidates),
            key=lambda t: abs(t - pos),
        )
        return max(0.0, nearest)

    def _status_anchor_beat_number(
        self,
        beat_num: int,
        beat_in_bar: int,
        playing: bool,
        beat_grid: TrackBeatGrid | None,
    ) -> int:
        """
        Map status beat numbers to anchor beat indices.
        """
        if beat_num <= 0:
            return 0

        # XDJ quirk: while paused, some streams report beat_num one beat ahead
        # with beat_in_bar=0 (unknown). Map back by one beat only in this case
        # so cue/paused anchors don't drift ahead across play/pause cycles.
        if (not playing) and beat_in_bar == 0 and beat_num > 1:
            return beat_num - 1

        # Some XDJ paused packets appear to report the previous beat index while
        # keeping beat_in_bar aligned to the upcoming beat. Detect this using the
        # rekordbox beat phase markers and shift forward by one beat only in this
        # specific mismatch pattern.
        if (
            (not playing)
            and beat_grid is not None
            and beat_grid.beat_count > 1
            and beat_grid.beat_within_bar
            and beat_in_bar in (1, 2, 3, 4)
        ):
            cur_phase = self._grid_beat_within_bar(beat_grid, beat_num)
            next_phase = self._grid_beat_within_bar(beat_grid, beat_num + 1)
            if cur_phase != beat_in_bar and next_phase == beat_in_bar:
                return min(beat_num + 1, beat_grid.beat_count)

        return beat_num

    def _grid_beat_within_bar(self, beat_grid: TrackBeatGrid, beat_number: int) -> int:
        if beat_number <= 0 or beat_number > beat_grid.beat_count:
            return 0
        if beat_number > len(beat_grid.beat_within_bar):
            return 0
        val = int(beat_grid.beat_within_bar[beat_number - 1])
        return val if 1 <= val <= 4 else 0
