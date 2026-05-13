from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrackBeatGrid:
    player_num: int
    beat_times_ms: tuple[int, ...] = ()
    beat_within_bar: tuple[int, ...] = ()

    @property
    def beat_count(self) -> int:
        return len(self.beat_times_ms)

    def time_for_beat(self, beat_number: int) -> int:
        if beat_number <= 0 or not self.beat_times_ms:
            return 0
        index = min(max(beat_number - 1, 0), len(self.beat_times_ms) - 1)
        return int(self.beat_times_ms[index])

    def find_beat_at_time(self, position_ms: float) -> int:
        """Return beat number (1-based) that contains position_ms.
        Mirrors BeatGrid.findBeatAtTime() in beat-link."""
        if not self.beat_times_ms or position_ms < 0:
            return 0
        # Binary search for the rightmost beat that starts <= position_ms.
        lo, hi = 0, len(self.beat_times_ms) - 1
        if position_ms < self.beat_times_ms[0]:
            return 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.beat_times_ms[mid] <= position_ms:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1  # 1-based
