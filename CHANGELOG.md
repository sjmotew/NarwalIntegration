# Changelog

All notable changes to this integration, newest first. Each version links to the
[GitHub release](https://github.com/sjmotew/NarwalIntegration/releases) (what HACS installs)
and to the long-form notes under `docs/`, which carry the full story, the evidence and the
credits. Versions are git tags on `master`; every tag was deployed to a live Home Assistant
instance and verified on hardware before it was pushed.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). **Breaking** marks a
change that can alter the behaviour of an existing automation or dashboard.

## [v1.0.12] — 2026-11-13

*Room order sticks, the dock tells the truth, and the map is yours to draw.* **One breaking change.**
[Notes](docs/RELEASE-NOTES-v1.0.12.md).

- **Breaking:** the duplicate `Super` and `Ultra` suction select options are removed; use `Super Powerful` and `Ultra Powerful`. `vacuum.set_fan_speed` keeps its aliases. The live suction list offers only supported transitions and reads `unknown` rather than the next-clean default when the active level is unknown ([#122](https://github.com/sjmotew/NarwalIntegration/pull/122), closes [#139](https://github.com/sjmotew/NarwalIntegration/issues/139), @Sean-StarLabs).
- **Added:** room cleaning order is saved per map and survives restarts; `rooms: all` cleans in ascending id when nothing is saved ([#120](https://github.com/sjmotew/NarwalIntegration/pull/120), @Sean-StarLabs).
- **Added:** `narwal.set_pending_profiles` sets the whole-floor profile, room profiles and room selection in one validated call without sending anything to the robot ([#140](https://github.com/sjmotew/NarwalIntegration/pull/140), closes [#146](https://github.com/sjmotew/NarwalIntegration/issues/146), @Sean-StarLabs).
- **Added:** map camera attributes `vacuum_position`, `charger`, `path` and `calibration_points` for dashboard map cards ([#141](https://github.com/sjmotew/NarwalIntegration/pull/141), @slackstarr; see [docs/MAP-CARD.md](docs/MAP-CARD.md)).
- **Added:** Korean and German translations; the last hardcoded entity names now use translation keys, with no change for English users ([#134](https://github.com/sjmotew/NarwalIntegration/pull/134), @hyeok-yoo; [#145](https://github.com/sjmotew/NarwalIntegration/pull/145), [#147](https://github.com/sjmotew/NarwalIntegration/pull/147), @slackstarr).
- **Fixed:** on the Flow, telemetry no longer drops out for 30 s of every minute during a clean; the broadcast subscription is renewed every 20 s while cleaning ([#149](https://github.com/sjmotew/NarwalIntegration/pull/149), closes [#148](https://github.com/sjmotew/NarwalIntegration/issues/148)).
- **Fixed:** dock task switches no longer stay on after an accepted stop ([#121](https://github.com/sjmotew/NarwalIntegration/pull/121), closes [#138](https://github.com/sjmotew/NarwalIntegration/issues/138), @Sean-StarLabs).
- **Fixed:** pending commands fail immediately when the connection is lost or replaced instead of waiting out their timeout ([#135](https://github.com/sjmotew/NarwalIntegration/pull/135), @mario-jerkovic).
- **Fixed:** next-clean settings can be changed while the dock is drying ([#136](https://github.com/sjmotew/NarwalIntegration/pull/136), @mario-jerkovic).
- **Fixed:** `vacuum.clean_area` on the Freo Z Ultra (CX7) reuses a known map instead of failing on a slow map refresh ([#137](https://github.com/sjmotew/NarwalIntegration/pull/137), @mario-jerkovic).
- **Housekeeping:** a "Loads in HA" CI job sets the integration up in genuine Home Assistant on every PR ([#133](https://github.com/sjmotew/NarwalIntegration/pull/133)); README slimmed to a front page with history here and detail under `docs/` ([#131](https://github.com/sjmotew/NarwalIntegration/pull/131), [#132](https://github.com/sjmotew/NarwalIntegration/pull/132)); CX7 state notes are firmware-specific ([#144](https://github.com/sjmotew/NarwalIntegration/pull/144), refs [#125](https://github.com/sjmotew/NarwalIntegration/issues/125)).

## [v1.0.11] — 2026-10-25

*A docked robot stays connected, and a cleaning robot says so.* No breaking changes.
[Notes](docs/RELEASE-NOTES-v1.0.11.md).

- **Fixed:** a docked robot no longer reconnects every ~350 s. The robot closes a socket 60 s after the last command and the 60 s poll raced it; the keepalive now sends the app's own heartbeat, which keeps the socket open without waking anything ([#113](https://github.com/sjmotew/NarwalIntegration/issues/113), closes [#90](https://github.com/sjmotew/NarwalIntegration/issues/90), @wawtor).
- **Fixed:** responses are matched to commands by send order; a late answer is discarded instead of misdelivered; a wrong-shaped `get_map` reply parses to an empty map; `clean_rooms` with known ids no longer fails on an unneeded map refresh ([#110](https://github.com/sjmotew/NarwalIntegration/issues/110) / [#111](https://github.com/sjmotew/NarwalIntegration/issues/111) / [#112](https://github.com/sjmotew/NarwalIntegration/issues/112), closes [#108](https://github.com/sjmotew/NarwalIntegration/issues/108), @wawtor).
- **Fixed:** a Flow 2 on firmware v01.09.10 is shown cleaning, not idle; Stop / Pause stay available mid-clean ([#124](https://github.com/sjmotew/NarwalIntegration/issues/124)).
- **Fixed:** Freo Z Ultra (CX7) accepts commands. Non-broadcasting models skip the local capability vetoes and let the robot arbitrate ([#105](https://github.com/sjmotew/NarwalIntegration/issues/105), closes [#102](https://github.com/sjmotew/NarwalIntegration/issues/102), @fishscrounger; docs in [#106](https://github.com/sjmotew/NarwalIntegration/issues/106)).
- **Fixed:** cleans started from the app reconcile correctly; no more `task_status: returning` after docking ([#95](https://github.com/sjmotew/NarwalIntegration/issues/95), closes [#98](https://github.com/sjmotew/NarwalIntegration/issues/98), @Sean-StarLabs).
- **Fixed:** no phantom `display_map` dropout at connect or during quiet spells; first recovery no longer held back on a freshly booted host ([#114](https://github.com/sjmotew/NarwalIntegration/issues/114) / [#117](https://github.com/sjmotew/NarwalIntegration/issues/117), @wawtor).
- **Changed:** detergent sensor disabled by default on the Flow / Flow 2, where field 41 is not the level ([#116](https://github.com/sjmotew/NarwalIntegration/issues/116), @turmacar; see [#79](https://github.com/sjmotew/NarwalIntegration/issues/79), [#128](https://github.com/sjmotew/NarwalIntegration/issues/128)).
- **Added:** Narwal Freo 20 Edge in the model selector ([#109](https://github.com/sjmotew/NarwalIntegration/issues/109), confirmed by @cwisor).
- **Housekeeping:** Pillow dropped from the manifest (Hassfest), `CONFIG_SCHEMA` declared; T10 documented as the cloud-only J1 ([#44](https://github.com/sjmotew/NarwalIntegration/issues/44)); Flow ack census and the 60 s idle timeout in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## [v1.0.10] — 2026-09-14

*Robots no longer vanish after a Home Assistant restart.* No breaking changes.
[Notes](docs/RELEASE-NOTES-v1.0.10.md).

- **Fixed:** discovery re-runs at every boot and could replace the stored IPv4 address with the robot's IPv6 one; the WebSocket URL then failed to parse and the entry was marked failed, which HA never retries. Discovery now prefers IPv4 and never downgrades a stored host to IPv6, IPv6 literals are bracketed, and any startup failure is retried ([#101](https://github.com/sjmotew/NarwalIntegration/issues/101), reported with logs by @thorsten-gehrig).

## [v1.0.9] — 2026-09-12

*Phantom consumable alerts stopped, room controls opt-in.* No breaking changes.
[Notes](docs/RELEASE-NOTES-v1.0.9.md).

- **Fixed:** the maintenance / replacement problem sensors ignore ids that are not consumables. Some firmware answers `get_consumable_info` with scalars, not id lists, and both sensors toggled hourly on a phantom part `1000`. Unknown ids move to an `unknown_ids` attribute ([#100](https://github.com/sjmotew/NarwalIntegration/issues/100), closes [#99](https://github.com/sjmotew/NarwalIntegration/issues/99), @TgMrP).
- **Changed:** per-room profile controls are opt-in. New installs get the room selects disabled and the room-selection switches hidden; existing registrations are untouched ([#96](https://github.com/sjmotew/NarwalIntegration/issues/96), closes [#94](https://github.com/sjmotew/NarwalIntegration/issues/94), @Sean-StarLabs).
- **Added:** Narwal Freo 20 in the model selector, key `fjhpiem4ba` ([#97](https://github.com/sjmotew/NarwalIntegration/issues/97), confirmed by @kvkessler).
- **Added:** `tools/gen_dashboard.py`, which generates a room-picker dashboard section, `input_select` and a one-room script from the entity registry.

## [v1.0.8] — 2026-09-05

*The vacuum entity tells you what it can actually do.* **Two breaking changes.**
[Notes](docs/RELEASE-NOTES-v1.0.8.md).

- **Breaking:** the `current_room` sensor is gone; current room, progress and task status are attributes on the vacuum entity ([#88](https://github.com/sjmotew/NarwalIntegration/issues/88), @Sean-StarLabs).
- **Breaking:** suction tiers renamed as the Narwal app names them, Super Powerful and Ultra Powerful; the old Super / Ultra names still work as aliases ([#91](https://github.com/sjmotew/NarwalIntegration/issues/91), @Sean-StarLabs).
- **Added:** per-room cleaning profiles, room-selection switches, a `narwal.clean_rooms` service, and `vacuum.start` that cleans the selected rooms with their own settings in one job ([#87](https://github.com/sjmotew/NarwalIntegration/issues/87), @Sean-StarLabs).
- **Added:** the vacuum entity advertises only the commands it can run right now ([#88](https://github.com/sjmotew/NarwalIntegration/issues/88)).
- **Added:** native map trails from Narwal's own trajectory windows, kept across restarts ([#89](https://github.com/sjmotew/NarwalIntegration/issues/89), closes [#75](https://github.com/sjmotew/NarwalIntegration/issues/75), @Sean-StarLabs).
- **Added:** dock task switches on a dock device of their own, with robot commands gated while the dock is busy ([#86](https://github.com/sjmotew/NarwalIntegration/issues/86), @Sean-StarLabs).
- **Added:** downloadable diagnostics from the device page; the bug template asks for it first.
- **Fixed:** every product key a model ships is recognised; a regional Flow 2 key was showing as Unknown and losing the dock light. The model selector now defaults to Other / Auto-detect ([#81](https://github.com/sjmotew/NarwalIntegration/issues/81)).
- **Fixed:** `current_room` no longer stays `unknown` forever after a bare `get_map` ack on first connection ([#93](https://github.com/sjmotew/NarwalIntegration/issues/93)).
- **Fixed:** models with a product-specific topic prefix keep it after auto-detection ([#85](https://github.com/sjmotew/NarwalIntegration/issues/85), @Sean-StarLabs).

## [v1.0.7] — 2026-09-01

*The model name now matches the robot.* No breaking changes.
[Notes](docs/RELEASE-NOTES-v1.0.7.md).

- **Fixed:** the model label follows the product key the robot reported; accepting the default "Narwal Flow" had stored the right key under the wrong name ([#81](https://github.com/sjmotew/NarwalIntegration/issues/81), @DeNo64).

## [v1.0.6] — 2026-08-31

*Your robot was being poked 1,900 times a day.* No breaking changes.
[Notes](docs/RELEASE-NOTES-v1.0.6.md).

- **Fixed:** a docked robot that is merely quiet is no longer woken. A 15 s staleness threshold read every broadcast gap as sleep ([#90](https://github.com/sjmotew/NarwalIntegration/issues/90); root cause later refined in v1.0.11).
- **Fixed:** auto-detected robots are named after their model instead of a raw product key; the Device ID step is no longer a dead end when auto-detection fails ([#81](https://github.com/sjmotew/NarwalIntegration/issues/81)).

## [v1.0.5] — 2026-08-30

*Your robot finds itself now.* No breaking changes.
[Notes](docs/RELEASE-NOTES-v1.0.5.md).

- **Added:** automatic discovery via mDNS (`_narwal_sweeper._tcp.local.`) and DHCP (`narwal_*`) ([#78](https://github.com/sjmotew/NarwalIntegration/issues/78), @StratoGh0st99).
- **Added:** Narwal JX confirmed working and in the model selector ([#42](https://github.com/sjmotew/NarwalIntegration/issues/42)).
- **Added:** capture tooling under `tools/`: app-traffic capture guide, session recorder, coverage probe ([#35](https://github.com/sjmotew/NarwalIntegration/issues/35), @StratoGh0st99).
- **Fixed:** log flood from two wake/sleep INFO lines that were 29 % of a live install's log ([#82](https://github.com/sjmotew/NarwalIntegration/issues/82), @hyeok-yoo).
- **Docs:** VLAN / SNAT cause pinned ([#81](https://github.com/sjmotew/NarwalIntegration/issues/81)), a complete scheduled multi-room automation ([#83](https://github.com/sjmotew/NarwalIntegration/issues/83)), the HACS domain collision ([#84](https://github.com/sjmotew/NarwalIntegration/issues/84)).

## [v1.0.4] — 2026-08-17

*Consumable alerts never worked. Now they do.* **Upgrade if you own any supported model.**
[Notes](docs/RELEASE-NOTES-v1.0.4.md).

- **Fixed:** consumable alerts actually report. The lists were packed varints and had always been discarded, so both alert sensors said "no problem" on every robot ([#80](https://github.com/sjmotew/NarwalIntegration/issues/80), see [#79](https://github.com/sjmotew/NarwalIntegration/issues/79)).
- **Added:** Freo Z Ultra (CX7) local control via a pasted Device ID; polling only, no live map position ([#76](https://github.com/sjmotew/NarwalIntegration/issues/76)).
- **Changed:** fan tiers renamed Quiet / Standard / Strong / Super / Ultra; Ultra withheld on the AX26, where it silently applied Strong ([#70](https://github.com/sjmotew/NarwalIntegration/issues/70)).
- **Docs:** `CleanParam` tag 8 identified as the coverage-precision toggle; consumables documented in `docs/PROTOCOL.md`.

## [v1.0.3] — 2026-08-08

*The vacuum entity stops freezing at `docked`.* If you are on v1.0.2, upgrade.
[Notes](docs/RELEASE-NOTES-v1.0.3.md).

- **Fixed:** the robot only broadcasts `working_status` and `display_map` while a subscription is live, that subscription expires after 600 s, and nothing renewed it. The entity froze at `docked` mid-clean with the live map stuck ([#73](https://github.com/sjmotew/NarwalIntegration/issues/73)). v1.0.2 claimed this fix and did not contain it.

## [v1.0.2] — 2026-08-08

*Room cleaning actually works.* **Three breaking changes.**
[Notes](docs/RELEASE-NOTES-v1.0.2.md).

- **Fixed:** room-specific cleaning had never worked. Clean commands went to `clean/plan/start`, which runs the plan stored in the app and discards the rooms sent, while returning success. `clean/start_clean` is the correct command ([#49](https://github.com/sjmotew/NarwalIntegration/issues/49), closes [#37](https://github.com/sjmotew/NarwalIntegration/issues/37); found independently by @jgus, @Sean-StarLabs and @sytchi; confirmed on hardware by @shin906710 on a Freo Z10 Pro and @Zebble on a Flow).
- **Breaking:** room names changed. The room-type table was wrong for every model ([#48](https://github.com/sjmotew/NarwalIntegration/issues/48), closes [#22](https://github.com/sjmotew/NarwalIntegration/issues/22)).
- **Breaking:** fan speed values and tiers changed. The suction scale was off by one tier; the old `quiet` / `normal` / `strong` / `max` names keep working as aliases but now map to the correct tier, so actual suction may differ ([#49](https://github.com/sjmotew/NarwalIntegration/issues/49)).
- **Breaking:** `vacuum.start` requires the dock. Whole-house start goes through `clean/start_clean`, which only works from the dock, so an off-dock start now returns `NOT_READY` instead of appearing to succeed ([#69](https://github.com/sjmotew/NarwalIntegration/issues/69)).
- **Added:** clean settings as entities: work mode, water level, mop strength, passes ([#50](https://github.com/sjmotew/NarwalIntegration/issues/50)).
- **Added:** last-clean-result sensor; consumable maintenance and replacement alerts ([#53](https://github.com/sjmotew/NarwalIntegration/issues/53) / [#54](https://github.com/sjmotew/NarwalIntegration/issues/54)); station and consumable diagnostics from a `base_status` field audit ([#52](https://github.com/sjmotew/NarwalIntegration/issues/52)).
- **Added:** map rendering switches for room labels, furniture and furniture labels ([#62](https://github.com/sjmotew/NarwalIntegration/issues/62)); carpet-map camera image ([#67](https://github.com/sjmotew/NarwalIntegration/issues/67)); dock ambient light ([#61](https://github.com/sjmotew/NarwalIntegration/issues/61)); current-room telemetry ([#24](https://github.com/sjmotew/NarwalIntegration/issues/24)).
- **Changed:** live `working_status` telemetry is interpreted instead of a stale `base_status` ([#63](https://github.com/sjmotew/NarwalIntegration/issues/63)); unknown status values warn once ([#72](https://github.com/sjmotew/NarwalIntegration/issues/72), closes [#46](https://github.com/sjmotew/NarwalIntegration/issues/46)).
- **Housekeeping:** asyncio deprecation fix for Python 3.12 ([#71](https://github.com/sjmotew/NarwalIntegration/issues/71)); config-flow translation sync ([#47](https://github.com/sjmotew/NarwalIntegration/issues/47)); AX26 in the model selector ([#40](https://github.com/sjmotew/NarwalIntegration/issues/40), [#70](https://github.com/sjmotew/NarwalIntegration/issues/70)); Narwal JX product key ([#42](https://github.com/sjmotew/NarwalIntegration/issues/42)); `docs/PROTOCOL.md` published.

## [v1.0.1] — 2026-07-30

- **Fixed:** the cleaning-area sensor reports the real covered area ([#51](https://github.com/sjmotew/NarwalIntegration/issues/51)).

## Earlier

`v0.5` and the unreleased development before it are described in the git history. Nothing
earlier than v1.0.1 was published to HACS.

[v1.0.12]: https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.12
[v1.0.11]: https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.11
[v1.0.10]: https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.10
[v1.0.9]: https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.9
[v1.0.8]: https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.8
[v1.0.7]: https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.7
[v1.0.6]: https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.6
[v1.0.5]: https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.5
[v1.0.4]: https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.4
[v1.0.3]: https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.3
[v1.0.2]: https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.2
[v1.0.1]: https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.1
