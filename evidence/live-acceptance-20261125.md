# Live acceptance — 2026-11-25

Engine: FL Studio **26.1.7.5654** (Windows x64), `FLEngine_x64.dll` SHA-256
`6d9c5ab0...8e02`, matching the compiled profile exactly.

Extension under test: isolated MinGW build with `dependencies.c`
(SHA-256 `fd2f17c7...011a`), installed into the live controller for this run.
The original extension (`c8fb5abd...7575`) was restored afterwards.

Transport was stopped (`isPlaying=0`, `isRecording=0`). The disposable project
had 5 channels and one occupied Mixer FX slot (Master, slot 9, "Emphasizer",
event-213 record 29 bytes, digest `2c344637...4d3`).

| Check | Result |
| --- | --- |
| Controller activation | `fl_bridge_status`: `active=true`, build `2026-11-16.1`, `needs_reload=false`, endpoint bound to PID 49016 |
| Dependency map via `fl_list_capabilities()` | Present; `mixer.insert_empty_slot`/`mixer.remove_effect` = build-gated entry + layout, verified calling convention; `project.save_copy` = verified entry, build-gated globals/layout, verified calling convention |
| `fl_clone_mixer_effect_full` dry-run | success, `submitted=false`, source digest `2c344637...4d3`, 3 steps |
| `fl_clone_mixer_effect_full` commit | success, `state_matched=true`, target digest equals source, 4 status IDs, no retained artifacts |
| `fl_swap_mixer_effects` dry-run | success, `submitted=false`, `irreversible=true`, both digests reported |
| `fl_swap_mixer_effects` commit | success, `state_matched=true`, both post digests `2c344637...4d3`, 8 steps, 6 status IDs, `irreversible=true` |
| `fl_get_request_status(queue_request_id)` | `state=completed`, action visible (`native.saveProjectCopy`, `native.call`) |
| Thread / heartbeat | RPC runs in FL PID 49016; `TFruityLoopsMainForm` thread id matches PID; heartbeat revision advanced to 17 during tests |

**Defect found and fixed during acceptance**: the success path of
`load_effect_at_slot` did not forward `effect_request_id`/`queue_request_id`,
so `load_state` steps reported no queue ID. Commit `2b6fbef` preserves both;
a repeat clone after the fix reported a queue ID on `load_state` and 5 status IDs.

Offline checks after the fix: 116 tests passed, Ruff clean, `git diff --check` clean.
