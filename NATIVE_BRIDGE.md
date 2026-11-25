# FL native bridge

The typed in-process extension and its Playlist/Automation contracts are
documented separately in [SEMANTIC_NATIVE.md](SEMANTIC_NATIVE.md).

This bridge adds public native API calls and Windows desktop operations without changing FL executable or DLL bytes.

## Activation

The installer has copied the controller and helpers to the existing FL user-data Hardware/FLStudioMCP directory. Installing files does not reload a running FL controller.

1. Open FL's **Script output** window, select **FLStudioMCP / loopMIDI**, and use **Reload**.
2. Restart/reconnect the MCP server to refresh its tool catalog.
3. Call `fl_bridge_status`. Continue when `active` is true, `needs_reload` is false, and the intended FL process is listed.

Reloading the controller does not require saving the project. For this session, the user explicitly prohibited project save/Save As and additional backups. Do not replace the current project or restart FL for activation; use the observed Reload control.

When multiple controller endpoints are active, call `fl_list_instances` and `fl_select_instance(pid)` first. Each process has a separate command directory.

## Operations

| Need | Tools |
|---|---|
| Discover native APIs | `fl_native_capabilities`, `fl_native_constants` |
| Call an enabled advertised public function | `fl_call_native(module, method, args, kwargs)`; blocked entries are explicit in capabilities |
| Ordered compound edit | `fl_native_batch`; not a transaction, partial results are explicit |
| Project, transport and track state | `fl_get_full_state(start, limit)` |
| Form visibility/focus | `fl_native_ui_snapshot`, `fl_native_window` |
| OS window hierarchy and geometry | `fl_list_windows`, `fl_move_window`, `fl_focus_window` |
| General native UI interaction | `fl_desktop_action` with click/drag/scroll/keys/text |
| Visual state | `fl_screenshot`; visible HWND Window DC, physical pixels, no foreground activation; background pixels may be cached |
| Audio/MIDI/preset drop | `fl_drop_asset` using the Windows Shell OLE data object |
| New audio channel + optional mixer route | `fl_import_audio`; explicit readback and clip-position coverage |
| Installed plugin presets | `fl_plugin_catalog` |
| Effect loading | `fl_load_effect_preset`; observed drop location + slot identity readback |
| Mixer sends/wet mix/public plugin parameters | `fl_native_set_send_level`, `fl_native_set_effect_mix`, `fl_set_effect_parameter` |
| Pattern edits | `fl_native_clone_pattern`, `fl_native_rename_pattern`, or general native calls |
| Selected clip time | `fl_get_selected_clip_bounds`, `fl_adjust_clip_time`; open and observe the intended clip's property editor first |
| Public plugin parameter state | `fl_get_plugin_parameter_state`, `fl_capture_plugin_parameter_state`, `fl_restore_plugin_parameter_state` |
| Edit loaded plugins | `fl_list_plugin_instances`, `fl_describe_plugin`, `fl_edit_plugin` |
| Operate a visible plugin editor | `fl_list_windows`, `fl_screenshot`, `fl_plugin_editor_ui` |
| Local render validation | `fl_verify_audio_output`; structure, duration, peak/RMS and invalid/full-scale samples |
| Change notifications | `fl_get_change_events(since)`; revision, generation and history-gap metadata |
| Uncertain timeout | `fl_get_request_status`; never automatically replay a timed-out mutation |

`args` are positional JSON arguments. For non-JSON native values, use `{"$bytes":"BASE64"}`, `{"$tuple":[...]}`, or `{"$constant":"midi.NAME"}`. Private attributes and modules outside the advertised list are rejected.

## Editing loaded plugins

1. Use `fl_list_plugin_instances(query)` to find the loaded plugin. `channel:13` is global Channel Rack index 13. `mixer:23:0` is mixer insert 23, first effect slot.
2. Use `fl_describe_plugin(target, query, include_text=true)` to find parameter names, indices, normalized values and display strings when supplied by the plugin. Follow `page.next_start` for remaining matches. A `null` cursor ends pagination.
3. Pass the observed fingerprint to `fl_edit_plugin`. Each edit needs `parameter` (a unique exact name or index) and `value` (0–1). Add `expected_value` and, for index-based edits, `expected_name` to check the observed state before writing. `dry_run=true` previews the edits.

A successful response contains subsequent host readback. Multi-parameter edits validate the entire request before writing, and read the resulting parameter values together again at the end. Writes already applied can remain after a partial failure. A fingerprint checks the host generation and plugin metadata at the specified location. Replacing a plugin with an indistinguishable instance at the same location can preserve that fingerprint.

The same API handles native plugins and third-party plugins with host-exposed parameters. Plugin display strings can include dB, Hz or enumerated labels. The setter takes normalized values. For private controls use the visible editor with screenshots and `fl_plugin_editor_ui`, or load a supported preset file. Preset coverage is defined by FL and the plugin. Custom UI changes require visual confirmation.

Live stdio verification discovered 35 plugin instances and exercised the same edit path on EQ 2, FLEX, Addictive Drums 2 and Timeless 3. All five changed values were read back and restored. Serum 2 and AmpliTube 5 parameter discovery also passed. The FLP's SHA-256, size and modification time matched before and after the test. These tools are loaded when a new MCP service starts.

## Transport

External clients atomically publish requests and `queue.json` under a cross-process index lock. FL's idle callback is the sole consumer. FL creates a persistent inflight receipt before dispatch and writes each unique response directly; clients retry incomplete JSON and retire completed pending/inflight files. This avoids `mkdir`, `replace`, `unlink`, `glob` and its unsupported audit hook inside FL's embedded host. Expired pending requests do not execute. Completed/inflight duplicate IDs do not repeat an edit. Query a timed-out request's ID before deciding whether to submit another request.

Controller reloads keep completed responses on disk. A request that was inflight during a crash or reload remains `outcome_unknown` rather than being replayed. Event generations change when the RPC module restarts; a history-gap flag indicates event-buffer overflow.

Existing MIDI tools retain their legacy transport until the first queue-v2 endpoint is loaded. Once a v2 endpoint is present, stale heartbeats produce an error rather than falling back to a different execution path. This does not serialize old, already-running MCP server processes: reconnect them to use the new code.

## Validation and present limits

Offline verification includes multi-thread/multi-process requests, correlation, duplicate suppression, expiration, inflight recovery, partial response reads, event overflow, edit guards, typed arguments, callback preservation, disabled-REC bypass protection, public parameter setter with subsequent-callback verification, OLE timeout ambiguity, and real Win32 geometry round trips on an owned test window. A real OLE cancellation test confirms that posting to the helper's own thread queue wakes the drag loop; no global mouse events or accepted drop are used in that test.

FL runtime verification passed queue activation, catalog discovery (439 public callables across 13 available modules), project/track/transport state, form movement/restoration, pattern rename/restoration, channel volume/restoration, send level/restoration, and change events. The catalog includes the blocked `general.processRECEvent`; it does not mean 439 functions have been exercised. A real stdio FastMCP Client discovered 114 tools, also present in this session's connected tool catalog. Effect loading passed at mixer track 26, slot 9 in about 1.9 seconds. Earlier attempts reached unintended tracks; they are recorded as failed acceptance. A changed plugin name does not prove complete preset contents. Pattern cloning increased the count from 6 to 7, then a single native Undo restored 6.

The REC test crashed FL. The old adapter used plugin location ID `0x06890000` as a full REC event ID, omitting the plugin event namespace. The engine read address `0x160`. The old `plugins.setParamRec` adapter and generic `general.processRECEvent` are now blocked. After the user-authorized recovery, `fl_set_effect_parameter` passed through `plugins.setParamValue`: original value `0.26305555555555554`, requested/read-back value `0.6`, then verified restoration. Wet mix also passed set/read-back/restoration in subsequent callbacks. The optional `allow_mixer_fallback=True` path checks the complete event namespace and parameter name before `mixer.automateEvent`; it is disabled by default and has only offline write-path tests. Read-only event-name resolution passed. Audio import and routing later passed in a fresh stdio MCP service: 38 to 39 global channels, new channel 38 named MCP_INTERFACE_TEST, mixer route read back as 26, and a visible clip on Playlist Track 18. Subsequent 2026-11-18 tests verified selected-clip start/length edits at one-tick resolution and a real one-second mix render; see TEST-20261118.md in the acceptance directory.

OLE runs in a separate helper with an 8-second limit. A timeout returns `OutcomeUnknown`, never an automatic retry. The helper sets process DPI awareness before OLE initialization. Its modal loop is woken with correctly positioned `PostThreadMessage` messages on its own thread. Effect loading uses `setActiveTrack`, `setTrackNumber(track, 1)`, a short UI-settle interval and a second selection check. The successful acceptance recorded identical physical cursor/message coordinates throughout the drag.

On 2026-11-17, PID 4760 reported controller build `2026-11-16.1`, `active=true` and `needs_reload=false`. Source and installed copies of all three controller files matched. Its project was `Projects/Backup/untitled.flp`, with an existing changed flag of 2, 38 global channels and 7 patterns. These are observations of that instance, not a guarantee that later instances have the same state. Disk installation, runtime activation, accepted drops and verified operations are reported separately. Do not retry the inflight REC request `95976b015d73400e9f19ca2d52d2272c`.

The screenshot change passed through a fresh stdio MCP subprocess: 2560 by 1368 PNG, foreground HWND unchanged. An already-running MCP server can retain the previous screenshot function until reconnected; its older version still requires foreground focus. Windows refused foreground attachment in the current server. After the user selected FL in the foreground, successful audio-drop acceptance took about 1.61 seconds. Do not work around foreground refusal by changing Windows security settings.

Two preceding MCP drops timed out before any helper phase log. Readback confirmed unchanged channel count and changed flag before a controlled retest. A print-only child process reproduced the startup timeout inside MCP. Explicit stdin=DEVNULL fixed Windows venv child startup (about 0.05 seconds), and both MCP preflight and actual import then passed. The helper accepts argv only, never the MCP stdin pipe. Timeout responses now retain helper_trace and last_phase; a prepare_only internal diagnostic creates the Shell data object without foreground focus, pointer motion or DoDragDrop. The already-running MCP service still needs reconnection to load the parent-side stdin change. It was not killed or restarted automatically.

Latest local regression: `python -m pytest -q` reported 48 passed; Ruff F and git diff --check passed. A fresh stdio service discovered 120 tools. Current evidence and the A–E acceptance record are in `D:/hmdp/artifacts/fl-studio-interface-audit-20261115/IMPLEMENTATION.md` and `TEST-20261118.md`. PID 6864 opened Desktop/untitled.flp with 39 channels and 7 patterns. Controlled parameter/clip edits were restored, and one accidentally placed Pattern clip was removed after inspecting the Undo target. Undo history and the changed flag are not restored. No project save was performed; the FLP's observed size and last-write time remained unchanged.

Plugin parameter capture is paged and includes wrapper placeholders. Addictive Drums 2 exposed 4240 entries, 452 named. After a real FST save/load, all named values matched, while one unnamed slot changed. Restore skips unnamed slots and reports the skipped count. The FST round trip restores a host-supported preset blob; it does not independently expose every private runtime field. Plugin setter calls now pass pickupMode=0 and useGlobalIndex=True in their separate positions.

Clip-time editing uses Ctrl+wheel on observed start/duration fields, one event per tick. Modifiers are held long enough for processing, and property closure uses Escape after a host idle read. A posted WM_CLOSE was found to overtake pending wheel input and change the Playlist view; that failed trial is retained. The final MCP test changed start 71636 to 71637 and length 192 to 193, then restored 71636–71828. Bounds describe the selected range, not an inventory of all clip objects.

Literal text uses UTF-16 Unicode input packets rather than keyboard-layout-dependent typing, fixing filename corruption under a Chinese IME. The legacy export helper no longer sends Ctrl+S or deletes existing audio, and its child stdin is isolated. Its complete one-click UI workflow still has unverified fixed assumptions. The accepted render was performed with observed MCP UI steps and separately measured by fl_verify_audio_output: 44100 stereo PCM frames, exactly one second, no nonfinite/full-scale samples.

Public native APIs do not expose all FL internals, clip data, opaque plugin state or every custom editor operation. Generic desktop input allows further UI actions, but this is not a guarantee of complete semantic coverage or a claim that every FL capability has passed regression testing.

Generic dispatch accepts JSON values, bytes, tuples and advertised scalar constants. Native signatures are often unavailable to reflection, and arbitrary native Python objects do not have full RPC serialization. An enabled catalog entry only passes dispatch checks; each signature and result type still needs a suitable workflow and validation.

The adapter uses BitBlt from the selected HWND's Window DC. Desktop-DC capture was observed to disagree with the target window, so it is no longer used. It never invokes PrintWindow/WM_PRINT on FL or shows hidden FL windows directly with Win32. All HWNDs and input points are bound to the selected FL process; a moved cursor or changed target aborts before a button event. Geometry is in physical pixels and a move returns the observed result.

## Direct diagnostic CLI

```powershell
& .\.venv\Scripts\python.exe .\scripts\native_cli.py --status
& .\.venv\Scripts\python.exe .\scripts\native_cli.py native.uiSnapshot
& .\.venv\Scripts\python.exe .\scripts\native_cli.py native.call --params '{"module":"mixer","method":"trackCount","args":[]}'
```

Historical controller backups are under `backups/native-*`; no additional project backups are required. Pre-existing controller customizations are retained except that the crashing REC adapter is explicitly disabled. No FL kernel changes are part of this bridge.
