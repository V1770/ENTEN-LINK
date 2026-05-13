# AGENTS.md — orientation for AI coding assistants

> If you are a chat-bot opening this repo for the first time, **read this
> file end-to-end before editing anything**. It encodes hard-won lessons
> from the Pioneer DJ Link reverse-engineering effort. Most of these bugs
> took hours to find and one line to fix; please don't re-introduce them.

## What the project is

A Python 3.13 + PyQt6 desktop app that joins a Pioneer **DJ Link** network
(CDJ-3000s, XDJ players, DJM mixers) and shows live decks, waveforms,
beat grids, metadata and cue points. asyncio runs inside a single QThread
(`core/network/network_worker.py`); everything talks through `core/event_bus.py`
PyQt signals.

Authoritative external references vendored in-tree:
- `beat-link-main/` — Deep Symmetry Java implementation of DJ Link.
- `beat-link-trigger-main/` — UI built on top of beat-link.
- The crate-digger `rekordbox_pdb.ksy` Kaitai spec is the source of truth
  for the rekordbox `export.pdb` format. **If your hand-coded offsets
  disagree with the .ksy, the .ksy is right.**

## Module map (the parts that bite)

| Path | Purpose | Watch out for |
|------|---------|---------------|
| `app.py` | Qt + worker bootstrap, CLI, `restart_network()` | Virtual CDJ is **on by default**; do not gate it behind `-t` again. |
| `config.py` | Dataclass settings persisted to JSON | `network.virtual_cdj_player` is the *live* value; `default_virtual_cdj_player` is the saved default. Settings dialog writes both. |
| `core/network/discovery.py` | UDP 50000 announce listener | — |
| `core/network/beat_receiver.py` | UDP 50001 — beats **and** PRECISE_POSITION (CDJ-3000+) | CDJ-3000s never send 50002 status, only PRECISE_POSITION on 50001. |
| `core/network/status_receiver.py` | UDP 50002 CDJ_STATUS | Older players only. |
| `core/network/virtual_cdj.py` | Our keep-alive broadcast | Without it dbserver/NFS silently refuse handshakes. |
| `core/network/metadata_client.py` | dbserver TCP queries | Requester slot must be 1-16; prefer ≥5 to avoid CDJ-3000 rejections. |
| `core/network/nfs_client.py` | UDP NFSv2 fetcher for ANLZ/PDB | UDP not TCP. UTF-16LE paths. Mount path `/C/`. |
| `core/network/pdb_parser.py` | rekordbox export.pdb | See "PDB cheat-sheet" below. |
| `core/devices/device_manager.py` | Device watchdog, slot bookkeeping | Resets timeout on PRECISE_POSITION too — don't remove. |
| `ui/main_window.py` | Receives `restart_network` callback | Calls it when settings change VP. |
| `ui/settings_dialog.py` | Network tab edits live + saved VP together | — |
| `packaging/windows/` | PyInstaller spec + Inno Setup + GH Actions workflow | See `packaging/windows/README.md`. |

## Things that have already been debugged — do not redo

1. **Virtual CDJ is mandatory.** Pioneer dbserver and NFS silently refuse
   handshakes if you are not broadcasting as a peer player. Symptom: every
   network bit looks fine, no metadata ever arrives. Default-on lives in
   `app.py`; `--no-vcdj` opts out, `--vp N` overrides for a session.
2. **CDJ-3000 watchdog.** They never send CDJ_STATUS (UDP 50002). They only
   announce on 50000 and emit PRECISE_POSITION on 50001. `device_manager.py`
   listens to `precise_position_received` and bumps `_last_seen` — without
   that handler decks vanish after 5 s.
3. **NFS = UDP, not TCP.** Pioneer's NFSv2 implementation is UDP-only.
4. **NFS file paths are UTF-16LE.** UTF-8 → ENOENT for every track.
5. **PDB header `table_off = 28`**, NOT 32 (common misread of crate-digger).
6. **PDB track_row offsets** must match the Kaitai .ksy exactly — see below.
7. **Default VP slot ≥ 5.** Slots 1-4 collide with real decks and CDJ-3000s
   sometimes refuse them outright.
8. **Worker restart pattern** = `worker.stop(); worker.wait(3000)` then build
   a NEW `MetadataClient` + `NetworkWorker`. Reusing instances leaks signal
   connections.
9. **Hidden-slot trap.** `ui.hidden_slots` in saved settings can hide players
   that *are* online. Check it before chasing a "missing device" bug.
10. **Duplicate Python method names** silently override; once made the
    waveform ring-buffer never advance. Grep for `def name(` before assuming
    a method is the one being called.
11. **PyQt `clicked(bool)`** overrides lambda default args — capture loop
    vars with `_checked=False` as the first param.

## PDB cheat-sheet (rekordbox `export.pdb`)

Header: `_, len_page, num_tables, _next, _, _seq = struct.unpack_from("<6I", data, 0)`
then a 4-byte gap → `table_off = 28`.

Track row field offsets (must match crate-digger ksy exactly):

```
sample_rate u4 @8    file_size  u4 @16   artwork_id u4 @28
key_id      u4 @32   bitrate    u4 @48   track_no   u4 @52
tempo       u4 @56   genre_id   u4 @60   album_id   u4 @64
artist_id   u4 @68   track_id   u4 @72        ← was wrongly @76
disc_no     u2 @76   play_count u2 @78   year       u2 @80
sample_dep  u2 @82   duration   u2 @84        ← was wrongly @88
color_id    u1 @88   rating     u1 @89
ofs         21×u2 @94
```

Smoke-test on a real USB: 1430 tracks / 810 artists / 746 albums.
Symptom of any wrong offset: thousands of rows collapse to a handful of
duplicate fake track ids.

## Threading rules

- All network I/O lives in the asyncio loop owned by `NetworkWorker`.
- Never call asyncio code from the Qt thread; never call Qt widget code
  from the asyncio thread. Use `event_bus` signals (Qt cross-thread queues
  them automatically).
- Stop the worker via `loop.call_soon_threadsafe(stop_event.set)` —
  already wrapped in `NetworkWorker.stop()`.

## Windows packaging

See `packaging/windows/README.md`. Two paths:
- **Cloud (recommended on macOS):** push to GitHub → Actions tab →
  *Build Windows installer* → Run workflow → download
  `PioneerDJLink-Setup.exe` artifact.
- **Local on Windows:** install Python 3.12 + Inno Setup 6, then
  `powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1`.

If you ever start importing `aubio`, `sounddevice`, or `python-rtmidi`,
add them to `packaging/windows/requirements-windows.txt` AND remove them
from the `excludes` list in `packaging/windows/app.spec`. They are
intentionally excluded today because they aren't actually imported and
their Windows wheels are flaky.

## When you finish a debugging session

Add a one-line entry to the "Things that have already been debugged" list
above with the file:line that fixed it. That list is the only record of
why some code looks weird.
