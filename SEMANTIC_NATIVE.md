# Semantic native operations

The controller uses `_flmcp_native_20261121_clip.pyd` from its `OnIdle`
callback, falling back to the older versioned modules and `_flmcp_native.pyd`
when it is absent. It does not
modify FL's executable or DLL files. The compiled profile accepts only the
verified Windows x64 FL Studio 26.1.7.5654 engine SHA-256. The engine must be
stopped for writes. Reload the FLStudioMCP controller and reconnect the MCP
server after installing an updated controller and extension; copying files does
not update an already running session.

Use `fl_list_capabilities` to check the running controller and its operations.
The existing generic `native.describe` and public MIDI functions are separate.

## Sampler source (partial)

`fl_add_sampler(path, dry_run)` appends a Sampler and asks FL to load an
absolute, existing WAV. It does not place an Audio Clip. A committed request
returns `state: partial`, because this build cannot independently read the
Sampler's source path yet; `channel_verified` only confirms that a Sampler was
observed at the new location. Do not retry a submitted or unconfirmed request
without inspecting the channel first. The tested FL build records two Undo
steps: one for sample loading and one for channel creation. The controller and
native extension must be installed and reloaded together before use.

## Generator source (verified)

`fl_add_generator(plugin_name, preset_path, dry_run)` appends a Generator — a
plugin-hosting Channel Rack channel — and loads an FST preset into it.
`plugin_name` is resolved against FL's Plugin Database (`Generators` or
`Installed/Generators`, factory and user roots); pass `preset_path` to select an
explicit absolute `.fst` instead. The tool validates the FST's FLhd/FLdt header
before any write. FL must be stopped.

The native op mirrors `channel.add_sampler` through the same verified insert
and vtable `+0x150` load path; the only difference is the load `mode` (0 loads a
plugin/preset, 1 loads a WAV). Unlike a Sampler, a Generator's plugin display
name is observable: it is read from the channel object's plugin-holder field at
`+0x58` (the same holder band the Mixer effect name uses; the channel type field
at `+0x160` matches the reference layout for this exact build). A committed
request therefore returns `success: true`, `state: verified` and
`source_path_verified: true` only when all of the following agree: the native
index, channel count +1, a non-Sampler channel type (nonzero), and the read-back
plugin name matching the requested name case-insensitively. An unreadable name
is reported empty and fails verification rather than being inferred. The tool
re-reads the channel type and count after the response; a name mismatch or a
changed channel returns `state: unconfirmed` with the observed values, and a
submitted-but-unconfirmed request is never retried automatically.

Verified on an isolated FL 26.1.7.5654 with the `3x Osc` factory preset:
dry run without state change, empty-name rejection, commit with independent
readback (channel count +1, public channel type 2, channel name and plugin name
both `3x Osc`), unchanged adjacent channels, and a single `undoUp` restoring
the channel. Unlike Samplers (two undo steps for load + creation), loading the
FST preset here folds into FL's channel-insertion undo point. The native op's
own `+0x160` read is reported only as `observed_kind_native` in a failure
result: FL fills that field a later cycle, so it still reads 0 inside the idle
callback while the public readback after the RPC round trip already shows the
real type. Source-path readback remains unavailable for Samplers (see above),
which is why only Generators reach `verified` here.

## Sampler source readback (via save-copy)

`fl_read_sampler_sources()` closes the path-readback gap left by
`fl_add_sampler`: the loaded WAV path cannot be read from the live channel
object, but FL serialises it into every `.flp` as a UTF-16LE
`ChannelID.SamplePath` event (TEXT + 4 = 196). The tool writes a disposable
project copy through the verified `fl_save_project_copy` op, scans the event
stream with a tolerant reader, and returns one `{channel_index, channel_iid,
sample_path}` entry per Sampler. The temporary copy is deleted; the live
project is never saved over. FL must be stopped.

The scanner walks FLP events by their public encoded sizes (BYTE 1, WORD 2,
DWORD 4, TEXT/DATA varint length + data), starting a new channel at each
`ChannelID.New` (WORD = 64) and decoding only `SamplePath` (196). It does not
use pyflp's object parser: on FL 26.1.7.5654 a Sampler's binary parameter event
contains an illegal UTF-16 surrogate that aborts pyflp's whole parse, while the
tolerant scanner skips unknown events by size and never decodes them. Channel
order and the embedded channel iid map directly to the global Channel Rack
index the controller reports.

Verified on an isolated FL 26.1.7.5654, stopped transport: a Sampler created
with a distinctively named WAV was the only new path in the save-copy, matched
by file name to the new channel index; the two-step undo removed both the
channel and the path from a fresh copy. Evidence:
`evidence/stage5-sampler-path-readback-20261122.json`.

## Arrangement switching and cross-arrangement clips

`fl_set_current_arrangement(index)` switches the visible arrangement through the
engine's own switch routine and reads the current index back to confirm it; it is
a view change, so no undo entry is created. Insertion paths now target the
ACTIVE arrangement: `fl_insert_pattern_clip` and `fl_insert_automation_clip`
previously hardcoded arrangement 0, which made every insert fail as soon as a
second arrangement existed (found by the cross-arrangement check below).
Verified on isolated FL: two clips in arrangement A, a new arrangement B with one
clip, each arrangement reading back only its own clips across repeated switches,
and A's two clips intact after the round trip. Overlapping identical clips are
also verified: the same pattern placed three times at the same track and tick
keeps three distinct snapshot refs, and deleting one leaves the other two.
Evidence: `evidence/stage6-cross-arrangement-overlap-20261125.json`.

## Plugin adapter registry

`fl_describe_plugin_adapter(track, slot)` names the adapter that handles a Mixer
FX slot and returns its capability table. `classify_plugin_state(name, state)` is
the pure part: a VST wrapper's event-213 record embeds the plugin's file path, so
a `.vst3`/`.dll` marker (UTF-8 or UTF-16) identifies a hosted plugin; otherwise a
non-empty record is an FL-native effect. The registry distinguishes:

| adapter | state clone | preset load | wrapper state file (0x12) |
| --- | --- | --- | --- |
| `fl-native-effect` | factory FST skeleton + replaced 213 | empty direct; occupied remove+load | no |
| `vst3-wrapper` / `vst2-wrapper` | same skeleton route, verify digest | same, plus a real state file via 0x12 | yes |
| `unknown` | best effort | empty direct | no |

Verified on isolated FL: Fruity Delay 3 classifies as `fl-native-effect` with a
108-byte record and no format marker; a real 222-byte FabFilter Timeless 3 record
(mixer 3/7 of an earlier save-copy) classifies as `vst3-wrapper` with the wrapper
state-file capability. Evidence: `evidence/stage11-adapter-20261125.json`,
`evidence/stage11-adapter-vst3-offline-20261125.json`.

## Plugin preset listing and loading by name

FL stores each plugin's presets as `.fst` files under
`Data/Patches/Plugin presets/<Effects|Generators>/<Plugin>/` (factory) and
`Documents/Image-Line/FL Studio/Presets/Plugin presets/...` (user). That makes
"load a preset by name" a file lookup rather than an engine call:
`fl_list_plugin_presets(plugin_name, kind)` scans those folders and returns
names, relative paths and sizes (user entries take precedence over factory),
and `fl_load_plugin_preset(plugin_name, preset, track, slot)` resolves an
absolute path, a relative path or a bare stem and loads it. An empty slot is
loaded directly; an occupied slot must already host the same plugin, and the
occupant is removed and replaced (no restorable FL undo — reported as
`irreversible: true`). This replaces the `plugins.nextPreset/prevPreset` route,
which cannot report the preset name at all: the public `plugins` module exposes
`getPresetCount`, `nextPreset` and `prevPreset`, but no getter for the current
preset name, and `getName` returns the plugin name.

Verified on isolated FL: `Fruity Delay 3` listed 10 presets; `Bandpass` loaded
into an empty slot; `Chorus` then replaced it in the occupied slot and the
slot's event-213 digest changed. Evidence:
`evidence/stage5-sampler-preset-20261125.json`.

## Sampler insertion now verifies its source

`fl_add_sampler` closes its own loop: after the channel is created and its
identity verified, it writes a disposable save-copy, reads the new channel's
`SamplePath` and compares it with the requested file. A match reports
`state="verified"`, `source_path_verified=true` and the observed path; otherwise
the result stays `partial` with the observed path for inspection. The tolerant
FLP scanner now also understands the tagged `0xAC` event written by FL 26.1.3+,
so the readback survives current-version save-copies. Verified on isolated FL:
a 440 Hz WAV inserted, readback path matched exactly.

## Non-linear automation via dense linear points

On this build a Sampler-like automation point stores a curve-shape dword and a
tension float, but the engine renders from a 16-byte derived coefficient cache
at record `+0x10..+0x1F` that stays zero: writing curve/tension is readable and
undoable but does not change playback. A vtable dump op
(`automation.vtable_dump`, RVA of slot `+0x40` = `0x79FE40`) showed that address
lands in the middle of a larger function, so the existing `recompute(envelope)`
call cannot populate the cache. Rather than rely on that unresolved engine path,
`fl_shape_automation_segment(channel, start_index, shape, count, dry_run)`
creates the eased ramp from 8..62 interior **linear** points (tension=0, curve=0)
sampled from `ease_in`, `ease_out`, `ease_in_out` or `exponential`. Dry run plans
without writing; each inserted point is one undo step. Verified on isolated FL:
16 points turned a linear 0.2→0.8 ramp into a visible ease-in (quarter-ramp
value 0.275 vs linear 0.347). Evidence:
`evidence/stage2-shape-tool-20261125.json`; analysis `CURVE-CONCLUSION-20261125.md`.

## Full-state Mixer effect cloning and slot swap

`fl_clone_mixer_effect_full(source_track, source_slot, target_track, target_slot,
dry_run)` copies a slot's **complete** plugin state, not just named parameters.
It reads the source's event-213 record from a project save-copy, rebuilds the
plugin's own factory FST skeleton with that record substituted for the
skeleton's default state (regenerating the event length prefix), loads the
synthetic preset into an empty target slot, then compares the target's event-213
SHA-256 with the source. A matching digest means the whole saved state — opaque
plugin chunks included — was carried over. The synthetic FST is written into the
writable user `Presets/Plugin database/Effects` folder (the loader only accepts
presets from a Plugin Database) and deleted afterwards. Verified on isolated FL
26.1.7.5654 with Fruity Delay 3 (26 parameters, 108-byte record): source and
target digests were identical and five distinct parameter values matched exactly.
The loader accepts this specific construction — FL's own factory skeleton with
only its event-213 record replaced. It does **not** accept an FST assembled from
scratch; the verified claim is limited to skeleton replacement, not to arbitrary
synthetic presets.

`fl_swap_mixer_effects(track, slot_a, slot_b, dry_run)` swaps two FX slots with
their complete state: capture both records, remove each plugin, reload it into
the other slot from a synthetic preset, then verify the digests cross-match. A
failed step triggers restoration from the captured records. Removal has no
restorable FL undo on this build, so the result reports `irreversible: true`.
Verified with Fruity Delay 3 and Hyper Chorus: the 108-byte and 52-byte records
crossed exactly and parameters followed their plugins. Evidence:
`evidence/stage4-clone-full-20261125.json`, `evidence/stage4-swap-effects-20261125.json`.

## Mixer effect state readback (read side of cloning)

`fl_read_mixer_effect_state(track, slot)` reads a Mixer FX slot's complete
plugin state from a disposable project save-copy. FL stores each occupied FX
slot's full plugin data — including opaque chunks that are not exposed as named
parameters — as event 213 in the `.flp`. The tool parses those records with a
tolerant event walker (pyflp cannot read this build's stream: event 0xAC is
tagged and desynchronises naive walkers) and returns a digest, byte length and
base64 of the record; omitting track/slot lists every slot's digest. The live
project is never saved over and the temporary copy is deleted. FL must be
stopped. This is the read half of effect-slot cloning: it can prove two slots
hold identical state, but does not itself generate a loadable FST.

The parser was verified against real FL 26.1.7.5654 project files: a save with
Fruity Balance at track 3 slot 7 yielded a 222-byte state record at exactly
`(3, 7)`, while a baseline save with no effects yielded none. FL 26 has no
public state-export/getChunk API, so the read side relies on a save-copy plus
event-213 scan. On the write side, only the factory-skeleton replacement
described above is known to load; an FST assembled from scratch is rejected.
Parameter-level cloning (`fl_capture_plugin_parameter_state` /
`fl_restore_plugin_parameter_state`) stays available but covers named public
parameters only; VSTs with unnamed slots (e.g. Addictive Drums 2) lose state.

## Sampler sample replacement

`fl_replace_sampler_sample(channel, path, dry_run)` loads a new WAV into an
existing Sampler channel. It reuses the verified load path from
`fl_add_sampler`: locate the channel object through the channel list, then call
the vtable `+0x150` loader with mode 1 (load sample). No channel is inserted
and the count is unchanged. The tool reads the channel's source path before and
after through `fl_read_sampler_sources` and confirms that the target path
changed to the requested WAV while every other Sampler's path stayed the same.
A missing target path, an absolute-path/format failure, an existing same-path
load and a non-Sampler channel are all refused before any write. `dry_run`
validates without writing. FL must be stopped.

FL renames a Sampler channel to the loaded file name, so the post-load channel
name is expected to differ from the pre-load name; channel identity is held by
the index, the Sampler type (0) and the channel count, and both names are
reported for audit. The sample load records one FL undo entry: a single
`undoUp` restores the previous source path (verified below), so unlike effect
slot removal this operation is undoable.

Verified on an isolated FL 26.1.7.5654, stopped transport: a Sampler loaded
with `...AAA.wav` was replaced with `...BBB.wav`, the save-copy readback showed
the new path and no other changes, the channel stayed a Sampler at the same
index, the non-Sampler target was rejected, dry run was inert, and one undo
restored the original path. Evidence:
`evidence/stage5-sample-replace-20261122.json`.

## Audio clips

`fl_insert_audio_clip(channel, track, start_tick, length_tick, source_start, source_end, dry_run)` places an existing Sampler as a Playlist Audio Clip. Tracks are one-based and positions are PPQ ticks. Without a source window, FL derives the window from the Sampler. With one, both bounds are ticks and `length_tick` must equal `source_end - source_start`: the engine sizes the clip to the window it receives, so a different length is rejected instead of silently overwritten.

Readback reports an audio clip's window as `source_start_ms` and `source_end_ms`. FL stores the two fields at `+0x18/+0x1c` as float milliseconds of song time, not seconds and not ticks. Measured in the probe project, one tick is 4.80769 ms; explicit windows of 0-384, 0-192 and 96-384 ticks read back as 0/1846.1538, 0/923.0769 and 461.5385/1846.1538 ms. Pattern clips keep integer tick markers in the same bytes.

`plugins.setParamValue` needs six positional arguments for a Mixer effect: value, parameter, track, slot, 0, use-global. The five-argument form is accepted without error but does not change the value; the controller now uses the six-argument form and returns `verified`.

## Playlist and automation

`fl_list_playlist_clips` returns one-based Playlist track, PPQ tick position,
length and a session-scoped `snapshot_ref`. Pass the snapshot to
`fl_move_playlist_clip(snapshot_ref, start_tick, track, dry_run)`. A preview
does not change the project or create an undo entry. The move rejects stale or
identical observed records, independently reads back the full 36-byte observed
record, and creates an FL Playlist undo entry. Identity checking is bounded to
8,192 clips; larger arrangements are rejected for moving, but can be read.

`fl_delete_playlist_clip(snapshot_ref, dry_run)`, `fl_duplicate_playlist_clip(snapshot_ref, start_tick, track, dry_run)` and `fl_set_playlist_clip_muted(snapshot_ref, muted, dry_run)` share the same snapshot discipline. Deletion mirrors the engine's own cleanup: the record source field is invalidated and FL's compaction helper removes it and shrinks the count; the commit refuses when any other record is already invalidated. Duplication byte-copies the full 88-byte record (source, source window, appearance and mute state), writes the requested start/track, clears the selection bit, and inserts through the engine's clip-list vtable so FL assigns a new clip identifier. The mute flag is bit 0x20 of record byte 0x13, confirmed three ways: engine getter/setter pairs dedicated to that bit, fixture clips muted in the UI carrying exactly that bit, and an isolated FLP save diffing by exactly one clip byte 0x00 to 0x20. Delete, duplicate and mute each create one FL Playlist undo entry; undo restores the deleted clip with its mute state. FLP saves differ in a few header bytes between any two saves; only clip-data bytes are compared.

`fl_trim_playlist_clip(snapshot_ref, window_start_tick, window_end_tick, dry_run)` trims audio clips by writing the source window in ticks through the engine's own source-range setter (which converts to song milliseconds) and pins the clip length to the window width; pattern and automation clips have no source window, so the trim changes only the clip length. `fl_split_playlist_clip(snapshot_ref, split_tick, dry_run)` duplicates the record to the split tick and trims the halves, with one FL undo entry covering the whole split and partial failures listing completed steps. The song tempo used to interpret audio windows in milliseconds is read from FL's own state: `device.getLinkedValue(0x40000005) * 512 + 10` BPM (event name "Tempo") and `general.getRecPPQ()`; both were cross-checked against a known 130 BPM project and the window readbacks. Undo removes one split per step; FL groups a pattern-clip trim and a following split of the same clip into one undo step, so restoring that step returns the pattern clip to its untrimmed length.

`fl_insert_pattern_clip(pattern, track, start_tick, length_tick, dry_run)` places a Pattern as a Pattern Clip. The source field is `0x50005000 | (pattern << 16)`; without an explicit length FL's own derived pattern length (patterns-module steps times PPQ/4) is used. Pattern clip window markers are integer -1; the record initializer zeroes the whole record before writing defaults, so the native insert rewrites the caller fields and the pattern window markers after initializing. The clip is read back and compared against a same-pattern clip already in the project, and one FL undo entry covers the placement.

`fl_manage_pattern(mode, index, name, color, dry_run)` covers Pattern creation, cloning and metadata through FL's public patterns module, which is one-based. Creation jumps to `patternCount + 1`, which makes FL create and select a default pattern; cloning inserts the copy directly after the source (new index = index + 1), selects it, and auto-renames it, so confirmation uses the selected position and matching length rather than the name. `fl_set_pattern_length(index, steps, dry_run)` changes a pattern's length in steps. Pattern colors read back as ARGB with the alpha byte set; only the low 24 bits are compared. Out-of-range pattern getters return template values instead of failing, so index validation combines the real maximum (`patternCount` and `patternNumber`) with the length getter. `findFirstNextEmptyPat` with flags 0 or 1 hangs FL's idle thread on this build and is not used. Pattern deletion has no public interface and is not offered.

`fl_read_pattern_notes(pattern, offset, limit)` reads a pattern's piano-roll notes (1-based pattern number) from the engine's per-pattern note stores: a static record array whose +0x28 slot holds a 24-byte-record collection (position ticks at +0x00, `(channel << 16) | 0x4000` at +0x04, length ticks at +0x08, key at +0x0C, fine pitch at +0x10, release at +0x12, mute flag 0x20 at +0x13, pan at +0x14, velocity at +0x15, cut-self at +0x16). The decoded records were cross-checked against the fixture FLP's own note data for a whole pattern.

`fl_add_pattern_notes(pattern, notes, dry_run)` appends complete notes through the engine's record-note alias (the collection's commit slot and the pattern rebuild follow), creates the note store on demand, and covers the batch with one Piano-Roll undo entry. `fl_edit_pattern_notes(pattern, edits, deletes, allow_multiple, dry_run)` matches targets against the live snapshot by (channel, key, position[, length]), refuses stacked duplicates unless allow_multiple is set, rewrites the array in place (sorted by position, never growing), and covers everything with one undo entry. Piano-Roll undo points exist only while the edited pattern is shown in the Piano Roll window, so both operations select the pattern and open the window first (a visible UI change). Note edits do not resize placed pattern clips here; the pattern rebuild re-derives them the same way FL's own note editing does.

`fl_read_timeline_markers(offset, limit)`, `fl_add_timeline_marker(tick, name, dry_run)` and `fl_delete_timeline_marker(index, expected_tick, dry_run)` manage the active arrangement's timeline markers. The marker manager hangs off the Playlist form (+0xD84) as a Delphi dynamic array of 52-byte records (tick at +0x00, Delphi UnicodeString pointer at +0x08, character length at pointer-4). Additions go through the engine's own add routine and are inserted sorted by tick; deletions shift the tail down inside FL's array and shrink its length. Additions are covered by one FL undo entry; FL's undo does not restore a direct-array deletion, so a deletion is reversed by re-adding the same tick and name. The project-change notification is required after marker edits; without it FL treats the undo point as empty.

`fl_read_arrangements()`, `fl_add_arrangement(name, switch_to, dry_run)`, `fl_clone_arrangement(source, name, switch_to, dry_run)`, `fl_delete_arrangement(index, dry_run)` and `fl_set_arrangement_name(index, name, dry_run)` manage arrangements through the engine's arrangement routines (located via a uniform shift from the reference build and each verified by disassembly). The arrangements global is a Delphi dynamic array of arrangement pointers with the name string at +0x08. Cloning deep-copies clips and markers via the engine's copy-into routine. Deletion refuses to remove the last remaining arrangement and suppresses FL's autosave hook with the load-in-progress flag (restored afterwards), matching the reference implementation's verified behavior. Additions/clones/deletes/renames each carry one FL undo entry.

`fl_read_arrangements()` — see below — and mixer sends: `set_mixer_send` / `read_mixer_sends` / `delete_mixer_send` manage mixer routing through FL's public routing API. The routed indicator is `mixer.getRouteSendActive(source, destination)` (0 = no send, 1 = send exists); `getRouteToLevel` raises "Index out of range" when no send exists, so readback treats the active flag as authoritative and the level as available only for existing sends. `setRouteSendActive` does not exist on this build; a send is deactivated by re-routing with `setRouteTo(src, dst, 0)` and re-created with 1. Send operations use FL's own undo history (undoUp restores the pre-send routing). Sidechain routing has no public FL routine and is not exposed.

`remove_mixer_effect(track, slot)` removes a mixer effect by loading FL's own "(delete) effect" FST (from the engine's Data/System directory) into the occupied slot through the same verified preset loader used for inserts, recording a per-slot undo point first. Removal is verified by plugins.isValid flipping to false. **Limitation: the removal is NOT undoable** — general.saveUndo + undoUp, afterRoutingChanged + undoUp, and repeated undoUp all fail to restore the removed effect (the FST load path frees the plugin without registering restorable undo state; FL's own UI delete uses a different state-capture path). Treat removal as destructive in-session and rely on fl_save_project_copy for safety.

`fl_replace_mixer_effect(name, track, slot, dry_run, preset_path)` replaces the
occupant of an occupied slot: it refuses same-name replacements and empty
slots, removes through the delete-effect path above, then loads the
replacement through the verified insert loader and reads the slot back
independently (occupied + exact plugin name). **The whole replacement is
destructive**: the removal step cannot be undone, so undoing an executed
replacement undoes the load step and leaves the slot empty, and the original
occupant is not restorable; the result reports `undoable: false` with that
warning. A failure after removal returns `state: "partial"` with the slot left
empty and the completed step listed, so the caller can retry the load alone.
Verified on an isolated FL: Fruity Balance replaced by Fruity Reeverb 2,
independent readback (occupied, exact name, 15 parameters), undo probe
matching the documented limit.

`fl_save_project_copy(output_path)` writes the live project to an absolute .flp path through the engine's FLP writer worker, bypassing the Save command wrapper whose error path pops modal dialogs (the same wrapper that hangs FL's idle thread on errors). The copy serializes live state including unsaved changes — verified by adding a marker, saving a copy, and finding the marker's name serialized in the copy. The project itself is not modified; two consecutive saves differ only in a few timestamp fields. Headless WAV render is not implemented: FL's public Python API has no render routine, and the engine's export path requires a captured export-config structure (the same conclusion the reference implementation reached); the export dialog route remains available as a user-driven fallback.

End-to-end chain verified on an isolated FL: WAV asset generation, Sampler creation, audio clip placement, mixer routing, Fruity Balance load with parameter write/read, pattern creation with batched note add and readback, pattern clip placement, timeline marker, mixer send, arrangement clone (deep copy including clips), and a project save-copy whose bytes contain the added marker name. Every step used independent readback.

Clip records are typed through the owning channel: sampler channels (type 0) back audio clips with float-millisecond source windows, automation channels (type 2) back automation clips, and pattern clips carry their own integer window markers. Only sampler-backed clips receive source-window writes.

`fl_insert_automation_clip(channel, track, start_tick, length_tick, dry_run)` places an automation channel's control as an Automation Clip. The channel must be an automation channel; the clip is read back and compared for position, track and source, but FL derives the clip extent from the automation envelope during refresh, so the requested length is advisory and the derived length is reported. Trimming an automation clip's length is rejected by confirmation because the refresh re-derives the extent; resizing automation clips therefore belongs to automation-envelope editing, not record writes.

`fl_list_automation_points(channel, offset, limit)` returns Automation Clip
points for the global Channel Rack index. Times are in beats; values are
normalized. Pass a point's `snapshot_ref` to
`fl_set_automation_point_value(snapshot_ref, value, dry_run)` or
`fl_set_automation_point_tension(snapshot_ref, tension, dry_run)`. Tension is
in -1..1. The preview
does not edit. The commit checks the channel, point bytes and preceding point
times, records the channel-specific FL undo state, applies the value, and
independently reads back the point's position and editable fields. The
recomputation can change cached coefficients beyond those fields. Identical point
records are rejected as ambiguous. `fl_move_automation_point` moves one point
within its neighbors while preserving the following point's absolute time; the
first point stays at beat zero. `fl_add_automation_point` inserts a linear point
strictly between existing points. `fl_delete_automation_point` deletes an
interior point and protects both endpoints. Insert/delete currently reject
curves containing nonzero curve modes. They compare the complete
curve snapshot in FL before writing and reject unordered or inexact times. They
use one FL undo entry. Link creation is available through
`fl_create_automation_source`; non-linear curve shapes are measured out and not
offered (see below).

`fl_set_automation_point_curve(snapshot_ref, curve, dry_run)` writes the
curve-shape dword at a point record's bytes +0x0C..+0x0F — the four bytes the
FLP format serialises as one dword right after the tension float (pyflp names
them "linked to tension"). The write is covered by the channel's undo snapshot
and read back independently. **Only the measured shapes are accepted**, and on
FL 26.1.7.5654 the measured set is `{0}` (linear): a 27-combination isolated
run wrote shape dwords 0..8 with tensions 0, +0.5 and -0.5 on a 4-beat slope
and sampled the driven parameter during playback — every combination rendered
identically linear. The record diff explains why: a tension write changes only
the tension float, and the derived cache at record +0x10..+0x1F stays zero for
routine-created envelopes through playback; FL renders from that cache, so
until the engine routine that fills it is located (remaining reverse
engineering; the reference implementation's docs also support linear type 0
only), non-linear shapes are stored correctly but do not change playback, and
they stay rejected rather than offered. Insert/delete likewise keep rejecting
any curve containing nonzero shape dwords.

`fl_list_automation_links(channel, offset, limit, include_names, include_values, include_targets)` enumerates
the FL link registry by automation-channel source event. It reports target
event IDs and optionally FL-provided display names; an unavailable registry is
an error, not an empty list. Names are not guaranteed unique instance IDs.
When requested, `target_value` is the current normalized host value or null if
the host cannot read it. Values are sampled, not a guarantee of future playback.
`include_targets=True` resolves supported Mixer effect parameter targets to the
current track, zero-based slot, parameter index and names. Limit the page to 16
links when requesting targets. Unsupported IDs or missing plugins return a null
target; locations are observations, not permanent plugin identity. Resolution
was checked with two same-name Fruity Balance instances on FL 26.1.7.5654.

`fl_create_automation_source(target_kind, track, slot, parameter,
target_event_id, dry_run)` appends an automation channel linked to a control
event through FL's own create-automation routine. The verified target form is
`target_kind="mixer_effect_parameter"` (track, zero-based slot, parameter on an
occupied slot; the event id is derived as `0x70008000 + ((track*64+slot)<<16) +
parameter`); `target_kind="event_id"` accepts a raw id only when FL can read
its name. The commit validates the channel count and independently reads back
the channel type and the link registry (the routine's own undo entry makes one
`undoUp` restore channel and link). FL populates the new channel's identity
fields on a later cycle, and the channel list itself can grow one cycle after
the call: the native operation reports whether the append is already visible,
the semantic layer re-reads, and the tool upgrades the result after a follow-up
read (`verified_on_followup`) — one extra RPC cycle, not an assumption. The
created envelope is seeded with the target's current value at two points, and
the channel reads public channel type 5 (CT_AutoClip): this build exposes two
automation kinds — legacy clips read 2, current automation clips read 5 — so
automation-clip placement, point editing and clip classification accept both.
The isolated acceptance covered: dry run without state change, empty-slot and
out-of-range parameter rejection, event-id naming check, commit with
independent readback, envelope read (two points at 0.0/4.0 beats seeded at the
target value), automation-clip placement on the created channel (FL derived
the extent), adjacent-object checks, and a two-step undo restoring the clip,
then the channel and link with the effect untouched. Playback response for a
routine-created source was covered by the earlier twin-fixture run, which
drove one of two same-name effects while the other stayed unchanged.

Snapshot references are tied to the controller session and project lifecycle.
Playlist snapshots also expire on every FL `OnRefresh` callback, including
unrelated refreshes. Read them again after a refresh before editing. This
conservative guard was verified across an Arrangement switch and switch back;
it depends on FL delivering its refresh notification. Time and bytes do not prove a
permanent host object ID: replacing an object with an indistinguishable one at
the same location may not be detectable. A submitted request without a
confirmed response must not be automatically retried.

## Validation boundary

In a separate FL 26.1.7.5654 process running a disposable Housework project,
the MCP path previewed a point at 0.85, wrote 0.65, read it back, rejected the
stale snapshot, preserved a second automation channel and restored the point
with one FL Undo. A second point at beat 8 was edited separately and restored
with Undo while the first point remained unchanged. The same isolated run
exercised a one-tick Audio Clip move, its Undo, and exact-slot Fruity Balance
insertion and Undo. These are isolated
tests; they do not establish all projects, plugin links, envelope playback,
audio output, redo, or future FL builds. The main user's project was not saved.

On 2026-11-21 an isolated process loaded the installed versioned extension
and exercised tension preview, a 0 to -0.6 change, readback, stale-snapshot
rejection and Undo. A second automation channel and the other point remained
unchanged; the value-edit path passed a write/Undo regression. This does not
yet establish the audible effect of tension or every curve mode.

A later isolated run loaded a generated WAV as a Sampler, placed a 768-tick
Audio Clip on Playlist track 5 at tick 3840, routed the channel to an empty
Mixer track, loaded two Fruity Balance instances, wrote 0.25 and 0.75 through
the six-argument public setter, read both back, then undid eight steps until
clips, channels and slots matched the starting state; the project file hash was
unchanged. A separate run wrote 0.3 and 0.7 through the controller action and
read back 0.3008 and 0.6992.

Evidence: `D:/hmdp/artifacts/fl-studio-interface-audit-20261115/evidence/automation-value-integration-20261120.json`.
Tension evidence: `D:/hmdp/artifacts/fl-studio-interface-audit-20261115/evidence/automation-tension-deployed-20261121.json`.
Point insert/move/delete evidence: `D:/hmdp/artifacts/fl-studio-interface-audit-20261115/evidence/automation-points-deployed-20261121.json`.
Link reading evidence: `D:/hmdp/artifacts/fl-studio-interface-audit-20261115/evidence/automation-links-deployed-20261121.json`.
Audio clip evidence: `D:/hmdp/artifacts/fl-studio-interface-audit-20261115/evidence/stage4-audio-clip-20261121.json`.
Source-window evidence: `D:/hmdp/artifacts/fl-studio-interface-audit-20261115/evidence/clip-window-20261121.json`.
Controller parameter evidence: `D:/hmdp/artifacts/fl-studio-interface-audit-20261115/evidence/param-controller-20261121.json`.
