# FL native bridge

The typed in-process extension and its Playlist/Automation contracts are
documented separately in [SEMANTIC_NATIVE.md](SEMANTIC_NATIVE.md).

This bridge adds public native API calls and Windows desktop operations without changing FL executable or DLL bytes.

## Activation

The installer has copied the controller and helpers to the existing FL user-data Hardware/FLStudioMCP directory. Installing files does not reload a running FL controller.

1. Open FL's **Script output** window, select **FLStudioMCP / loopMIDI**, and use **Reload**.
2. Restart/reconnect the MCP server to refresh its tool catalog.
3. Call `fl_bridge_status`. Continue when `active` is true, `needs_reload` is false, and the intended FL process is listed.

Reloading the controller does not require saving the project. Use FL's Reload
control rather than replacing the project.

When multiple controller endpoints are active, call `fl_list_instances` and
`fl_select_instance(pid)` first. Each process has a separate command directory.

Native writes are gated per operation. `fl_list_capabilities()` reports
`native.dependencies` with the function-entry, global-address, object-layout
and calling-convention state of `mixer.insert_empty_slot`,
`mixer.remove_effect` and `project.save_copy`. A dependency that is not at least
`build-gated`, or a calling convention that is not `verified`, refuses the call
before its handler runs.

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

## Failure and recovery semantics

- **Ambiguous writes are never replayed.** A timed-out mutation returns
  `error_type: OutcomeUnknown` with its queue envelope ID. The tool records both
  that ID and its own correlation ID in `steps[].queue_request_id` and
  `steps[].correlation_id`; `fl_get_request_status(queue_request_id)` resolves
  the real request. A compensating write is attempted only after the original
  request has left the queue and a readback confirms the pinned instance,
  project context and slot state. `fl_swap_mixer_effects` reports recovery only
  when **both** captured digests are independently matched again, and always
  reports `irreversible: true` because FL registers no restorable undo for the
  removal step.
- **Context is pinned per compound write.** The context captured before the
  first mutation is forwarded to the loader and every save-copy; a controller
  reload, project change or instance switch refuses later steps instead of
  silently acquiring a new context.
- **REC events are blocked.** `plugins.setParamRec` and generic
  `general.processRECEvent` are disabled after an adapter crash caused by an
  incomplete event namespace. `fl_set_effect_parameter` uses
  `plugins.setParamValue` with a subsequent-callback readback. The optional
  `allow_mixer_fallback=True` path checks the complete namespace and parameter
  name before `mixer.automateEvent`; it is off by default.
- **OLE drag/drop is bounded.** It runs in a helper with an 8-second limit; a
  timeout returns `OutcomeUnknown` with no retry. The helper sets DPI awareness
  before initialization and wakes its own modal loop with `PostThreadMessage`.
  It never sends global mouse events.
- **Screenshots use the visible HWND's Window DC** (physical pixels), never the
  desktop DC or `PrintWindow`. HWNDs and input points are bound to the selected
  FL process; a moved cursor or changed target aborts before any button event.

## Validation and present limits

Offline verification covers multi-thread/multi-process requests, correlation,
duplicate suppression, expiration, inflight recovery, partial response reads,
event overflow, edit guards, typed arguments, callback preservation,
disabled-REC bypass protection, public parameter setter readback, OLE timeout
ambiguity, idle-thread dispatch gating and Win32 geometry round trips. The C
gate is exercised by an isolated MinGW build that imports under CPython 3.12 and
refuses native execution when no matching engine is loaded.

Live results and their exact build, expected result, readback and Undo boundary
are in [evidence/README.md](evidence/README.md). Session-specific observations
are archived in [evidence/session-notes-archive.md](evidence/session-notes-archive.md).

Public native APIs do not expose all FL internals, clip data, opaque plugin
state or every custom editor operation. Generic desktop input allows further UI
actions, but is not a guarantee of complete semantic coverage. Generic dispatch
accepts JSON values, bytes, tuples and advertised scalar constants; native
signatures are often unavailable to reflection, so an enabled catalog entry
only passes dispatch checks and still needs a suitable workflow and validation.

## Direct diagnostic CLI

```powershell
& .\.venv\Scripts\python.exe .\scripts\native_cli.py --status
& .\.venv\Scripts\python.exe .\scripts\native_cli.py native.uiSnapshot
& .\.venv\Scripts\python.exe .\scripts\native_cli.py native.call --params '{"module":"mixer","method":"trackCount","args":[]}'
```

Historical controller backups are under `backups/native-*`. Pre-existing
controller customizations are retained except that the crashing REC adapter is
explicitly disabled. No FL binaries or kernel changes are part of this bridge.
