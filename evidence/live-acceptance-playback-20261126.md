# Live acceptance: automation target value during playback

Date: 2026-11-26. Explicit user authorisation to play audio was obtained before
the run. Transport was started and stopped through the RPC; the disposable
project had no notes so playback was effectively silent.

## Procedure

1. Installed the current controller and freshly compiled `_flmcp_native.pyd`
   (`b1a880e3...`) into the FL Hardware directory; original files backed up
   and restored after the run (hash-verified).
2. Started an isolated FL instance (PID 12892), FL Studio **26.1.7.5654**.
3. Loaded `Fruity Delay 3 / Default` into Mixer track 3, slot 5.
4. Created an Automation Clip (channel 6) targeting that slot's parameter 0
   ("输入湿音"): `target_event_id = 0x70B0A000 = 1891991552`.
5. Read links with `include_names`, `include_targets`, `include_values` while
   stopped and while playing.
6. Set the clip's two points to 0.2 and 0.9 via `fl_set_automation_point_value`,
   moved the second point from beat 4 to beat 1 so both points fall inside the
   Playlist clip, placed the clip on Playlist track 1 at tick 0, then repeated
   the playback reads.

## Result

| Step | `target_value` |
| --- | --- |
| Stopped (before edit) | 1.0 |
| Playing, 4 samples over ~2.4 s | 1.0, 1.0, 1.0, 1.0 |
| Stopped (after edit, points 0.2 / 0.9, clip placed) | 1.0 |
| Restarted transport, 12 samples over ~2.4 s | 1.0 throughout |
| Stopped again | 1.0 |

`fl_set_automation_point_value` itself verified correctly: request 0.2 / 0.9
read back as 0.20000000298023224 / 0.8999999761581421. `fl_list_automation_points`
confirmed the points moved to beats 0 and 1. `fl_insert_automation_clip`
confirmed a clip on Playlist track 1, source_index 6.

## Conclusion

`include_values` is functional: it returns the target's current value with no
error while the transport runs. However, on this build the underlying
`device.getLinkedValue(event_id)` did **not** reflect the Automation Clip's
interpolated playback value. The value stayed at the parameter's own value
(1.0) regardless of the clip's point values or Playlist placement.

Therefore the response field must be described as the **linked parameter's
current value**, not as an automation-playback value. Any consumer that needs
the clip's playback curve must read the clip's points and compute it.

## Additional finding

`fl_set_automation_point_value`'s `binding` field was `null` on real links.
Cause: `_binding_for_source` used `channel=0` and matched a per-link
`source_event`, but the controller reports `source_event` at the response
level. Fixed to read the clip's own channel and match by target kind.
