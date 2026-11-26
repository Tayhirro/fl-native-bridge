# Live acceptance — 2026-11-26

Engine: FL Studio **26.1.7.5654** x64. Isolated disposable project, transport
stopped. Controller files and a freshly compiled `_flmcp_native.pyd`
(`b1a880e3...`) were installed into the Hardware directory for the run and the
previous files were restored afterwards.

Scope: the two live items deferred by the 2026-11-26 modification plan —
automation-link resolution across an FX swap, and host-side slot arbitration.

## Result: PASS with defects found and fixed

| Check | Result |
| --- | --- |
| Fresh pyd loads, 35 ops / 35 deps | PASS |
| `native.scanMixerAutomationLinks` on real channels | PASS — found 1 affected link, `binding_confirmed: true` |
| FX swap between two different plugins | PASS — `state=swap_verified`, digests cross-matched, slot occupants actually exchanged (read directly via `mixer.readEffectSlot`) |
| Host-side lease arbitration | PASS in unit tests; live runs exposed two defects, both fixed (see below) |
| Automation link after swap | **Observed: link disappears** (empty link registry on the affected channel after a successful swap). The event ID stays location-encoded (`0x70B0A000` for track 3 / slot 5 / param 0) and, **before** the swap, FL resolves it to the current slot's plugin and parameter name (Fruity Delay 3 / 输入湿音). Post-swap resolution could not be read because the link registry was empty. This is a real finding, not a simulated result. |
| Playback-time target value | NOT TESTED — would require audio; plan requires separate authorisation. |

## Defects found live and fixed

1. `scan_mixer_automation_links` called `channels.channelCount()` without
   importing the FL host module inside the function. Live symptom: swap refused
   every structural write with a generic precheck error. Fixed by adding
   `import channels` and a regression test.
2. `remove_mixer_effect` released the slot lease on success, which broke the
   compound swap's later load steps ("Slot lease is missing or belongs to
   another write"). Fixed by leaving release to the compound caller's
   `finish()`; a lone caller's lease expires by TTL.

## Follow-up (not part of this run)

- Successful swaps were followed by an occupied lease for up to the 20 s TTL.
  `fl_swap_mixer_effects` now records `leases_released` per slot so a caller can
  see whether the release RPC succeeded. Whether the release RPC itself needs
  further work should be checked in a dedicated session.
- The disappearing automation link after swap needs a targeted investigation:
  determine whether FL removes the clip's link registry entry when its target
  plugin is replaced, or whether the tool's automation channel index shifts.
