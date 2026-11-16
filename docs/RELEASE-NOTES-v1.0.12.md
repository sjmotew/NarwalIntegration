# v1.0.12 — Room order sticks, the dock tells the truth, and the map is yours to draw

On top of [v1.0.11](RELEASE-NOTES-v1.0.11.md). **One breaking change** (the duplicate `Super` and `Ultra` suction options are gone; see below). Twelve pull requests from four contributors, plus one fix, one CI job and a documentation restructure of our own.

1,202 tests, up from 981, and a second suite that loads the integration in genuine Home Assistant on every pull request.

---

## Breaking: `Super` and `Ultra` are no longer suction options

[#122](https://github.com/sjmotew/NarwalIntegration/pull/122) from [@Sean-StarLabs](https://github.com/Sean-StarLabs), closing [#139](https://github.com/sjmotew/NarwalIntegration/issues/139).

The suction selects listed `Super` and `Ultra` next to `Super Powerful` and `Ultra Powerful`, two names for the same two levels. The duplicates are removed. **If an automation sets a suction select to `Super` or `Ultra`, change it to `Super Powerful` or `Ultra Powerful`.** `vacuum.set_fan_speed` keeps its aliases: `Super` maps to Super Powerful, and `Ultra` and `max` map to Ultra Powerful on five-tier models.

The same change makes the selects honest about what the robot can do right now. The live option list offers only supported transitions. AI and Ultra stay visible when the robot has confirmed them as the active level; while a clean runs, an Ultra request is sent as Super Powerful and the Ultra default is kept for the next clean. When the active suction is unknown, the select reads `unknown` instead of echoing the next-clean default.

## Room cleaning order is remembered

[#120](https://github.com/sjmotew/NarwalIntegration/pull/120) from [@Sean-StarLabs](https://github.com/Sean-StarLabs).

The order in which rooms are cleaned is now stored once per map and survives restarts. Rooms the map gains later are appended, and the saved order is completed from the full map before the selected rooms are filtered out of it. Where nothing is saved yet, `narwal.clean_rooms` keeps the order the caller gave and `rooms: all` cleans in ascending room id.

## Dock task switches follow the dock

[#121](https://github.com/sjmotew/NarwalIntegration/pull/121) from [@Sean-StarLabs](https://github.com/Sean-StarLabs), closing [#138](https://github.com/sjmotew/NarwalIntegration/issues/138).

After a dock task was stopped and the robot accepted the stop, the switch could stay on, showing activity that had ended. The result of the stop and any newer telemetry are now reconciled without extra refreshes. A verification that fails clears the accepted task; newer evidence that the dock is still working on it is kept.

## Half a clean was invisible on the Flow

[#149](https://github.com/sjmotew/NarwalIntegration/pull/149), closing [#148](https://github.com/sjmotew/NarwalIntegration/issues/148).

During a clean, a Flow on firmware v01.09.06.07 broadcasts for about 30 seconds after each subscription, whatever duration is asked for, and the integration re-subscribed only after 30 seconds of silence. Home Assistant therefore saw telemetry for 30 seconds and nothing for the next 30, for the whole clean: position, current room and progress all froze every other half minute. Measured over two cleans: windows of 29 to 31 seconds, and a subscription sent inside a live window extends it by another 30.

The coordinator now renews the subscription every 20 seconds while a clean is active. Verified on the same robot the same afternoon: 4,130 broadcasts over a 29-minute two-room clean, longest gap 3.3 seconds. A docked robot is untouched; nothing is sent to it that was not sent before.

## Draw your own map

[#141](https://github.com/sjmotew/NarwalIntegration/pull/141) from [@slackstarr](https://github.com/slackstarr).

The map camera already knew where the robot and the dock were and where the robot had been, but only painted them into the image. The same data is now exposed as camera attributes: `vacuum_position` with heading, `charger`, `path` as a trail of up to 400 points, and `calibration_points` in the format map cards such as the Xiaomi vacuum map card read. Dashboard cards can place their own markers on the camera image or draw the map themselves. [docs/MAP-CARD.md](MAP-CARD.md) has the shapes and units.

## Set every next-clean profile in one call

[#140](https://github.com/sjmotew/NarwalIntegration/pull/140) from [@Sean-StarLabs](https://github.com/Sean-StarLabs), closing [#146](https://github.com/sjmotew/NarwalIntegration/issues/146).

A scheduled reset of the next-clean settings used to be a series of select calls, which a clean starting at the same moment could interleave with, and which touched the live suction and water settings if the robot happened to be running. The new `narwal.set_pending_profiles` action updates the whole-floor profile, any number of room profiles and, optionally, the room selection in one validated step under the same lock a clean start takes. It sends nothing to the robot and does not wake it. Rooms are addressed by id or exact name; every value is checked before anything changes, and a changed map rejects the whole update. [docs/ROOM-CLEANING.md](ROOM-CLEANING.md#reset-next-clean-profiles) has the schema and an example.

## Korean and German

[#134](https://github.com/sjmotew/NarwalIntegration/pull/134) from [@hyeok-yoo](https://github.com/hyeok-yoo); [#145](https://github.com/sjmotew/NarwalIntegration/pull/145) and [#147](https://github.com/sjmotew/NarwalIntegration/pull/147) from [@slackstarr](https://github.com/slackstarr).

The last hardcoded English names, the two map cameras, the dock device, the legacy and per-room selects, the room switches and the `clean_rooms` action fields, now go through translation keys, so a translation can reach them. English users see the same names, entity ids and option values as before. On top of that, a complete Korean translation and a complete German translation, including the keys the first change added. Note for German instances: entities registered after this release get German entity ids, as Home Assistant does for its native languages; existing entities keep theirs.

---

## Smaller fixes

- [#135](https://github.com/sjmotew/NarwalIntegration/pull/135), [@mario-jerkovic](https://github.com/mario-jerkovic) — **a command no longer waits for a reply that cannot come.** When the WebSocket drops or is replaced, every pending command fails at once instead of running to its timeout, and an old listener can no longer wipe the replacement connection's state during cleanup.
- [#136](https://github.com/sjmotew/NarwalIntegration/pull/136), [@mario-jerkovic](https://github.com/mario-jerkovic) — **next-clean settings can be changed while the dock dries the mop.** The pending mode selector was unavailable whenever the dock reported drying without a fresh timer, so a switch from Vacuum and mop to Vacuum could silently keep the old mode.
- [#137](https://github.com/sjmotew/NarwalIntegration/pull/137), [@mario-jerkovic](https://github.com/mario-jerkovic) — **`vacuum.clean_area` on a Freo Z Ultra (CX7) reuses the map it already has.** The native room-clean path always refreshed the map first, and on a non-broadcasting model a slow map reply failed the clean with "Narwal map could not be refreshed". When the current map is known and holds every requested room, it is used directly; broadcasting models still refresh.
- [#144](https://github.com/sjmotew/NarwalIntegration/pull/144) — docs: the static CX7 state reported for [#5](https://github.com/sjmotew/NarwalIntegration/issues/5) is not universal. A unit on firmware v01.13.11.02 ([#125](https://github.com/sjmotew/NarwalIntegration/issues/125), [@simonhendricks](https://github.com/simonhendricks)) reports normal codes and tracks `cleaning` and `docked` correctly; the missing returning state is [#142](https://github.com/sjmotew/NarwalIntegration/issues/142).

## Housekeeping

- [#133](https://github.com/sjmotew/NarwalIntegration/pull/133) — **CI now loads the integration in real Home Assistant.** A second test suite under `tests_ha/` installs the genuine `homeassistant` package, sets a Flow config entry up end to end with only the robot I/O mocked, checks every platform registers entities and that the entry unloads cleanly, and fails on any deprecation warning attributed to the integration. It is a required check next to Tests, Hassfest and HACS Validation. The stub-based suite that catches the v1.0.2 class of import error could not, and this one does.
- [#131](https://github.com/sjmotew/NarwalIntegration/pull/131), [#132](https://github.com/sjmotew/NarwalIntegration/pull/132) — **the README is a front page again.** Version history moved to [CHANGELOG.md](../CHANGELOG.md), compatibility evidence to [docs/MODELS.md](MODELS.md), discovery and VLAN detail to [docs/SETUP.md](SETUP.md), room cleaning to [docs/ROOM-CLEANING.md](ROOM-CLEANING.md), limitations and quick fixes to [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md). This release trims it further to what someone installing from HACS needs.

## Also this cycle

- [#103](https://github.com/sjmotew/NarwalIntegration/pull/103) (native trail across room handoffs, [#123](https://github.com/sjmotew/NarwalIntegration/issues/123)) stays open. Two full cleans on the project's Flow with continuous telemetry show no `TASK_COMPLETED` between rooms and no trail reset; the fix waits for a capture from a robot where the reset happens.
- [#127](https://github.com/sjmotew/NarwalIntegration/pull/127) from [@jarostrug](https://github.com/jarostrug) ([#119](https://github.com/sjmotew/NarwalIntegration/issues/119)) now conflicts with master and was taken off this release; it returns when rebased.
- [#128](https://github.com/sjmotew/NarwalIntegration/issues/128) — the Flow 2 detergent alert is a separate record in field 48 that clears after a refill; the clean-water tank still has no local signal while docked.
- The two Home Assistant deprecation warnings (`via_device`, `async_get_device`) remain; both stop working in HA 2027.8, and the new CI job will flag the day they are fixed.

## Thanks

[@Sean-StarLabs](https://github.com/Sean-StarLabs), for four pull requests and the issues that scoped them; [@mario-jerkovic](https://github.com/mario-jerkovic), for three fixes ported from the Freo Z Ultra work; [@slackstarr](https://github.com/slackstarr), for the map-card attributes and all of the German; [@hyeok-yoo](https://github.com/hyeok-yoo), for Korean and the translation keys that made it possible; and [@simonhendricks](https://github.com/simonhendricks) for the CX7 firmware report that corrected the docs.

1,202 tests, CI green on the release commit, deployed to a live Home Assistant instance and verified on hardware before tagging.
