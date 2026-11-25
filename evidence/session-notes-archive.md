# Historical session notes (archive)

These notes are retained for traceability only. They describe observations from
specific FL processes and disposable projects, with process IDs, absolute paths
and one-off acceptance values. They are **not** guarantees about later sessions
and are deliberately kept out of `NATIVE_BRIDGE.md`.

## Activation session

Reloading the controller does not require saving the project. In the original
session the user prohibited project save/Save As and additional backups; the
controller was reloaded through FL's Script output window instead of restarting
FL or replacing the project. This was a constraint of that session, not an
installation requirement.

## Runtime observations

- Controller build `2026-11-16.1` was active with `needs_reload=false`; source
  and installed copies of the controller files matched.
- Catalog discovery found 439 public callables across 13 available modules;
  the catalog includes the deliberately blocked `general.processRECEvent`.
  Discovery does not mean every callable was exercised.
- A stdio MCP client discovered 114 tools in one run and 120 in a later run;
  the current registered count is 173 and should be read from the server, not
  from these historical numbers.
- Effect loading was accepted once at mixer track 26, slot 9 (~1.9 s). Earlier
  attempts reached unintended tracks and were recorded as failed acceptance.
- A pattern clone raised the count from 6 to 7; one native Undo restored 6.
- The old REC adapter crashed FL: plugin location ID `0x06890000` was used as a
  complete REC event ID without the plugin event namespace, and the engine read
  address `0x160`. Both `plugins.setParamRec` and generic
  `general.processRECEvent` are now blocked. The replacement path uses
  `plugins.setParamValue` with a subsequent-callback readback; the optional
  `allow_mixer_fallback=True` path requires a complete event namespace and
  parameter name and is disabled by default.
- Audio import and routing were verified in a fresh stdio service: channel
  count 38→39, new channel named `MCP_INTERFACE_TEST`, mixer route read back as
  26, and a visible clip on Playlist Track 18.
- Clip-time editing changed start 71636→71637 and length 192→193 at one-tick
  resolution, then restored. A posted `WM_CLOSE` overtook pending wheel input
  and changed the Playlist view; that failed trial is retained as a warning.
- Plugin parameter capture exposed 4240 entries for Addictive Drums 2, 452
  named. Restore skips unnamed slots and reports the count. A real FST
  save/load matched all named values; one unnamed slot changed.
- A one-second render was measured at 44100 stereo PCM frames with no
  nonfinite or full-scale samples.
- OLE drag/drop: two MCP drops timed out before the helper logged a phase;
  readback confirmed unchanged channel count and changed flag before retest.
  `stdin=DEVNULL` fixed Windows venv child startup (~0.05 s). Foreground
  attachment was refused by Windows and was not worked around by changing
  security settings; after the user focused FL, an audio drop was accepted in
  about 1.61 s.
- Screenshot capture used the visible HWND's Window DC at physical pixels
  (2560×1368) without foreground activation. An already-running MCP server can
  retain an older screenshot function until reconnected.

Historical FLP evidence, acceptance records and controller backups remain in
the maintainer's local archive; the five records under `evidence/` are the
public, scrubbed subset.
