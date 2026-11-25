# fl-native-bridge

A native C extension that runs inside FL Studio's own Python process and calls the
engine's internal routines — exposed to AI assistants as MCP tools.

Most FL Studio MCP servers wrap the public controller API. That API is real, but it
stops where FL's own UI stops: you can move a clip, you cannot replace a plugin's
state. This project loads a compiled extension into FL and works one layer lower,
which is what makes operations like full-state effect cloning possible.

## What this does that the public API cannot

| Operation | Public API | This bridge |
| --- | --- | --- |
| Read a plugin's complete state | parameter names/values only | raw state record (event 213), hashed — measured 8 B for a small FL effect, 108 B for Fruity Delay 3, 222 B for a VST3 |
| Clone an effect with its full state | not possible | rebuilds the plugin's factory FST skeleton with the captured record, loads it, compares SHA-256 |
| Swap two FX slots | not possible | both plugins move with their complete state; recovery path from the captured records |
| Load a preset by name | `nextPreset`/`prevPreset` only, and no getter for the current preset name | resolves the `.fst` in the Plugin Database, loads it |
| Switch the active arrangement | not exposed | `fl_set_current_arrangement`, confirmed by readback |
| Insert a clip into the *current* arrangement | n/a | active-arrangement insert (a hardcoded index-0 bug previously broke this with 2+ arrangements) |
| Non-linear automation | curve dword is stored but the derived cache stays zero on this build | dense linear point shaping (`ease_in`/`out`/`in_out`/`exponential`) |

Each claim above was measured in an isolated FL instance, not inferred. Details live
in `SEMANTIC_NATIVE.md` and `NATIVE_BRIDGE.md`.

## Requirements

| | |
| --- | --- |
| FL Studio | **26.1.7.5654**, Windows x64 — offsets are calibrated to this build |
| Python | the 3.12 runtime bundled with FL (`Shared/Python`) |
| MIDI ports | **not required** — the bridge talks over a file-queue RPC, so no loopMIDI or IAC Driver |
| Build tools | none, unless you want to recompile (then MSVC + CPython 3.12 headers) |

The extension refuses to load when the detected engine profile does not match, so a
wrong FL version fails closed instead of writing to the wrong addresses.

## Install

1. Copy the controller files into FL's Hardware directory:

```
%USERPROFILE%\Documents\Image-Line\FL Studio\Settings\Hardware\FLStudioMCP\
    device_FLStudioMCP.py
    semantic_native.py
    bridge_extension.py
    native_rpc.py
    _flmcp_native.pyd
```

2. Install the MCP server:

```bash
uv sync          # or: pip install -e .
```

3. Register the server with your MCP client (Claude Desktop / Claude Code):
   command `fl-native-bridge`, or `python -m fl_studio_mcp.server`.

4. In FL Studio: **Options → MIDI settings → Controller type → FLStudioMCP**, then
   enable the port. The controller publishes a heartbeat and the MCP server attaches
   to the running instance.

## Safety and limits

- **The bridge writes into FL's memory.** A wrong call can corrupt the project or
  crash FL. Save a copy before driving structural edits — `fl_save_project_copy`
  exists for exactly that, and destructive tools report what they could not verify.
- **Version locked.** FL updates move the offsets. Loading is gated on the engine
  profile string; unsupported builds are refused rather than guessed at.
- **Not everything is undoable.** Effect removal and preset replacement register no
  restorable undo on this build. Tools that cannot be undone say so in their result
  (`irreversible: true`) instead of pretending otherwise.
- **No FL binaries are distributed.** This repository contains only this project's C
  sources, Python code and offset constants. FL Studio, its DLLs and its factory
  content are not included.

## Architecture

```
MCP client (Claude, etc.)
        │  MCP tool calls
        ▼
src/fl_studio_mcp/                    Python MCP server, schemas, validation
        │  file-queue RPC (native_rpc/<pid>/)
        ▼
fl_controller/device_FLStudioMCP.py   runs inside FL's Python
        │  extension call
        ▼
_flmcp_native.pyd                     built from fl_controller/native/*.c
        │  direct calls / reads / writes
        ▼
FLEngine_x64.dll                      engine internals
```

The MCP server never touches FL directly. Every mutation crosses the file queue with a
request id and a deadline, runs on FL's idle thread, and is read back before the tool
reports success.

## Tool surface

Roughly 125 MCP tools across transport, mixer, channels, patterns, piano roll,
playlist, arrangements, plugins and project save-copy. The native layer exposes 35
operations:

| Area | Ops | Examples |
| --- | --- | --- |
| playlist | 21 | clip insert/delete/duplicate/mute, trim, source window |
| automation | 12 | point read/write/move, source and link creation, vtable dump |
| arrangement | 10 | read/add/clone/delete/rename/switch |
| timeline | 6 | markers |
| pattern | 5 | notes read/add/write |
| mixer | 4 | effect load/remove/replace, slot state |
| channel | 3 | sampler insert, sample replace |

## Testing

```bash
uv run pytest -q        # 100+ tests, no FL required (RPC layer is mocked)
```

The native layer is verified against a live FL instance by isolated end-to-end
scripts that launch FL with a disposable project, drive it, and read state back.

## Credits

Built on [karl-andres/fl-studio-mcp](https://github.com/karl-andres/fl-studio-mcp)
(MIT) — the controller framework, MCP server structure and tool conventions come from
that project, originally by calvinw and Karl Andres. The native extension, the
semantic layer and the engine-level operations documented here are this project's
work.

## License

MIT — see [LICENSE](LICENSE). The original copyright notices are preserved.
