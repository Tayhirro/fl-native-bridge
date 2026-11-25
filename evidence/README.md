# Live verification evidence

Five isolated FL Studio 26.1.7.5654 (Windows x64) records are kept here. Each
was produced by launching FL with a disposable project, driving it through the
MCP tools, and reading state back in a separate callback. Paths, process IDs
and generated run names are replaced with placeholders; digests, parameter
values, counts and observed results are unchanged.

| Record | Operation | Expected | Readback | Undo |
| --- | --- | --- | --- | --- |
| [`stage4-clone-full-20261125.json`](stage4-clone-full-20261125.json) | `fl_clone_mixer_effect_full` | target event-213 digest equals source (Fruity Delay 3, 108 B) | `state_matched`, five named values | preset load; no restorable entry assumed |
| [`stage4-swap-effects-20261125.json`](stage4-swap-effects-20261125.json) | `fl_swap_mixer_effects` | two digests cross-match (Delay 3 108 B / Hyper Chorus 52 B) | both `a_after`/`b_after` digests and parameters | removal is irreversible; recovery from captured records |
| [`stage6-cross-arrangement-overlap-20261125.json`](stage6-cross-arrangement-overlap-20261125.json) | arrangement switch + clip insert | each arrangement reads only its own clips; three identical overlapping clips stay distinct | repeated switches, delete-one-leaves-two | view switch creates no undo entry; clip insert has one |
| [`stage5-sampler-path-readback-20261122.json`](stage5-sampler-path-readback-20261122.json) | `fl_read_sampler_sources` via save-copy | the new Sampler's `SamplePath` matches the requested WAV | tolerant event walker on a disposable `.flp` | two undo steps (sample load, channel create) |
| [`stage2-shape-tool-20261125.json`](stage2-shape-tool-20261125.json) | `fl_shape_automation_segment` | 16 dense linear points turn a 0.2→0.8 ramp into a visible ease-in | quarter-ramp 0.275 vs linear 0.347 | one undo step per inserted point |

The engine build, operation, expected result, observed readback and applicable
Undo behaviour are all recorded in each file's own fields. Records from an
earlier audit remain in the maintainer's local archive and are not required to
reproduce these five results.
