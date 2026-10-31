# Troubleshooting and known limitations

## Quick fixes

| Problem | Solution |
|---|---|
| "Cannot connect" during setup | Verify the IP and that port 9002 is reachable. If it still fails, **open the Narwal app on your phone the moment you press Submit**; a sleeping robot may not answer within the setup timeout ([#40](https://github.com/sjmotew/NarwalIntegration/issues/40)). |
| Connection times out and the robot is on another VLAN | The robot refuses inbound connections from a foreign subnet. Source-NAT the traffic; see [SETUP.md](SETUP.md#home-assistant-and-the-robot-on-different-vlans). |
| Entities show "Unavailable" | The robot may be asleep. Open the Narwal app briefly to wake it. |
| Commands not responding | Close the Narwal app. The robot allows one WebSocket connection at a time. |
| Room clean returns `NOT_READY` | `clean/start_clean` only works from the dock. Send the robot home first, then start the room clean. |
| `vacuum.clean_area` fails with "Area mapping is not configured" | The one-time segment-to-area mapping is missing; the editor is on the **entity**, not the integration page. See [ROOM-CLEANING.md](ROOM-CLEANING.md). |
| Room cleaning runs the wrong rooms | Fixed in v1.0.2 ([#49](https://github.com/sjmotew/NarwalIntegration/pull/49)). If you are still on v1.0.1 or earlier this is expected; upgrade. |
| Map not showing | The map loads after the robot wakes. A new clean refreshes a stale map. |
| Z10 Ultra disconnects | Re-add the integration with the correct model selected. |
| Robot unavailable after a Home Assistant restart | Fixed in v1.0.10 ([#101](https://github.com/sjmotew/NarwalIntegration/issues/101)); discovery had swapped the stored address for an IPv6 one. |
| Robot reconnects every few minutes while docked | Fixed in v1.0.11 ([#113](https://github.com/sjmotew/NarwalIntegration/pull/113)); the keepalive now uses the app's heartbeat. |

## Known limitations

- **Wake from deep sleep is unreliable.** The robot may not respond after long idle periods. Opening the Narwal app briefly can help.
- **Single connection.** Close the Narwal app before using HA to avoid conflicts.
- **All cleaning requires the dock.** `clean/start_clean` returns `NOT_READY` if the robot is not docked when the command is sent. This applies to whole-house `vacuum.start` as well as room cleans.
- **Room cleaning needs a segment-to-area mapping.** `vacuum.clean_area` targets Home Assistant *areas*, not robot rooms. See [ROOM-CLEANING.md](ROOM-CLEANING.md).
- **Fan speed is set-only.** The robot does not broadcast its current level.
- **Only one floor at a time.** The integration uses the robot's *active* map and never enumerates the others. On a multi-floor home only the rooms of the current map are visible to Home Assistant, and HA floors are not related to robot maps ([#43](https://github.com/sjmotew/NarwalIntegration/issues/43)).
- **Map may be stale.** The robot can return an old map. A new clean cycle typically refreshes it.
- **Flow 2 clean-water and detergent tanks are not reported while docked.** On firmware v01.09.09 removing the clean-water tank or the detergent tank changes nothing in `base_status`, so the clean-water-tank sensor cannot fire on this model and the detergent sensor is disabled by default on the Flow / Flow 2 (field 41 reads 100 regardless). The sewage tank reports correctly: field 24 = 2 means full or removed ([#128](https://github.com/sjmotew/NarwalIntegration/issues/128), [#79](https://github.com/sjmotew/NarwalIntegration/issues/79)).

### CX7 (Freo Z Ultra)

- **No live stream.** The CX7 never broadcasts, so cleaning position and progress do not update live. Polled base status, battery, dock state, maps, consumables and commands remain available. `cleaning_time`, `cleaning_area` and `current_room` stay `unknown` throughout a clean because they are only carried in broadcasts. Requesting the broadcast-only topics directly does not substitute: `status/working_status`, `map/display_map`, `status/point_navi_plan_traj`, `info/get_clean_progress_info`, `robot/status/get`, `robot/task/status/get` and `info/battery_info` were all silent on a CX7, both idle and mid-clean.
- **State may not be a usable signal.** On the two CX7s first measured ([#5](https://github.com/sjmotew/NarwalIntegration/issues/5)), `base_status` field 3 read `{1: 19, 18: 1}` and did not change across 40 s while the robot physically drove back to its dock, so `is_docked` stayed `true` while it was away and the entity never showed CLEANING or RETURNING. A third unit on firmware `v01.13.11.02` ([#125](https://github.com/sjmotew/NarwalIntegration/issues/125)) reports normal codes instead: `{1: 1, 3: 6}` docked, `{1: 4, 7: n}` cleaning, `{1: 14, 12: 4}` washing mops after a clean, and its entity tracked `cleaning` and `docked` correctly. What separates the two behaviours is not yet known; code 19 has since been seen on an idle Flow 2 as well ([#128](https://github.com/sjmotew/NarwalIntegration/issues/128)), so it is a real idle state rather than a CX7 quirk. Either way, capability gating is skipped for non-broadcasting models and the robot arbitrates instead, returning `NOT_APPLICABLE` / `CONFLICT` / `NOT_READY` when it declines. `is_returning_to_dock` stays `false` on this model during returns; the codes that would drive it are tracked in [#142](https://github.com/sjmotew/NarwalIntegration/issues/142).

## Attach diagnostics

**Settings → Devices & Services → Narwal → ⋮ next to your device → Download diagnostics.**

Attaching that file to an issue answers most of what would otherwise be asked one question at
a time. It contains:

| Section | What it settles |
|---|---|
| `model_resolution` | Your robot's product key, and **whether this build recognises it at all** |
| `device` | Firmware version, and the six-character device suffix that matches your logs |
| `connection` | Whether the robot is connected, awake, and broadcasting |
| `feature_list` | What the robot says it supports, or why it refused to answer |
| `raw_base_status` | The undecoded protobuf fields new model support is built from |

**Redacted automatically:** your IP address, full device ID, and Narwal account UUID. The
**product key is deliberately kept**: it identifies a model, not a person, and it is usually the
answer.

If your robot reports a product key this integration doesn't know, say so in the issue. That
alone is often a one-line fix.
