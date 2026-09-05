# Narwal Robot Vacuum — Home Assistant Integration

A fully **local, cloud-independent** [Home Assistant](https://www.home-assistant.io/) custom integration for Narwal robot vacuums. Communicates directly with your vacuum over your local network via WebSocket — no cloud account or internet connection required.

> **Latest release: [v1.0.8](https://github.com/sjmotew/NarwalIntegration/releases/tag/v1.0.8)** (HACS) — per-room cleaning profiles, a vacuum entity that only advertises the commands it can run right now, native map trails that survive restarts, dock task switches, and a downloadable diagnostics dump ([notes](docs/RELEASE-NOTES-v1.0.8.md)). **Two breaking changes: the `current_room` sensor moved onto the vacuum entity, and the suction tiers were renamed to match the Narwal app** — the old names still work. **Coming from v1.0.1 or earlier? [Read the three breaking changes](docs/RELEASE-NOTES-v1.0.2.md) first**, then the [v1.0.4 notes](docs/RELEASE-NOTES-v1.0.4.md) — your consumable alerts were wrong before that release.

> ### ✅ Room cleaning is fixed — shipped in v1.0.2, verified on hardware in v1.0.3
>
> Community reverse-engineering found that **room-specific cleaning had never worked**. The integration sent clean commands to `clean/plan/start`, which is `StartWithPlan{planId, mapId}` — it runs the plan stored in the Narwal app and **discards the rooms we send**, while still returning success. That is why every previous fix appeared to work and changed nothing. `clean/start_clean` is the correct command.
>
> Found independently by [@jgus](https://github.com/sjmotew/NarwalIntegration/pull/49), [@Sean-StarLabs](https://github.com/sjmotew/NarwalIntegration/pull/58) and [@sytchi](https://github.com/sjmotew/NarwalIntegration/issues/37). Merged as [#49](https://github.com/sjmotew/NarwalIntegration/pull/49); [#37](https://github.com/sjmotew/NarwalIntegration/issues/37) is closed.
>
> **Confirmed on hardware, on two independent firmware lines:**
>
> | Reporter | Model | Firmware | Result |
> |---|---|---|---|
> | [@shin906710](https://github.com/sjmotew/NarwalIntegration/issues/70) | Freo Z10 Pro (AX26) | v01.02.00.15 | Two single-room cleans, each cleaned only the selected room |
> | [@Zebble](https://github.com/sjmotew/NarwalIntegration/pull/49) | Flow (AX12) | v01.08.03.07 | Two rooms, correct rooms, correct order, first attempt, ~35 min run |
>
> ### ⚠️ Upgrading from v1.0.1 — three breaking changes
>
> Full notes: [`docs/RELEASE-NOTES-v1.0.2.md`](docs/RELEASE-NOTES-v1.0.2.md). Read this before upgrading:
>
> - **Room names changed** ([#48](https://github.com/sjmotew/NarwalIntegration/pull/48)). The room-type table was wrong for every model. If you built automations or scripts on the old (incorrect) names, expect to redo those mappings.
> - **Fan speed values and tiers changed** ([#49](https://github.com/sjmotew/NarwalIntegration/pull/49)). The suction scale was off by one tier for this project's entire history. The list is now the app's own tiers — Quiet, Standard, Strong, Super Powerful, Ultra Powerful. Your existing `quiet` / `normal` / `strong` / `max` automations keep working as aliases, but they now map to the correct tier, so **actual suction may differ from what you were getting**. Earlier releases used shorter or differently capitalized labels; those spellings are still accepted.
> - **`vacuum.start` now requires the dock** ([#69](https://github.com/sjmotew/NarwalIntegration/issues/69)). Whole-house start goes through `clean/start_clean` and cleans every room instead of re-running the robot's saved plan. That command only works from the dock, so starting off-dock now returns `NOT_READY` instead of appearing to succeed — a real failure surfacing, since the old path was not starting the clean either.
>
> You will also see **many more entities** — 28 on a Flow, up from 9 — as clean settings, consumable alerts, map options and the dock light become HA entities. Verified on hardware (AX12, v01.08.03.07).
>
> **Fixed in v1.0.3:** the vacuum entity used to freeze at `docked` mid-clean, with the live map stuck and `cleaning_area` / `cleaning_time` never populating ([#73](https://github.com/sjmotew/NarwalIntegration/issues/73)). The robot only broadcasts `working_status` and `display_map` while a subscription is live, that subscription expires after 600 s, and nothing renewed it. Reproduced and fixed on hardware during a real room clean. **v1.0.2 does not contain this fix.**

## Device Compatibility

This integration uses a **local WebSocket connection on port 9002**. Only models that expose this port are supported.

| Model | Status | Notes |
|-------|--------|-------|
| **Narwal Flow** (AX12) | **Working** | Primary development target. Room cleaning confirmed on firmware v01.08.03.07 with [#49](https://github.com/sjmotew/NarwalIntegration/pull/49). On v01.07.22+, `vacuum.start` needs a loaded map ([#36](https://github.com/sjmotew/NarwalIntegration/issues/36)). |
| **Narwal Flow 2** (QxMSPG6VSO, iSuVlI1If2, mkbqaprvrb) | **Working** | Room cleaning fixed by [#49](https://github.com/sjmotew/NarwalIntegration/pull/49); on v1.0.1 see the warning above before using `vacuum.clean_area` |
| **Freo Z10 Ultra** (CX4) | **Working** | Community confirmed |
| **Freo Z10 Pro / Turbo** (AX26) | **Working** | Same product key and firmware (v01.02.00.15) reported under both names ([#40](https://github.com/sjmotew/NarwalIntegration/issues/40), [#70](https://github.com/sjmotew/NarwalIntegration/issues/70)). Room cleaning confirmed working with [#49](https://github.com/sjmotew/NarwalIntegration/pull/49). |
| **Freo X10 Pro** (AX15) | **Working** | Community confirmed ([#12](https://github.com/sjmotew/NarwalIntegration/issues/12)) |
| **Narwal JX** | **Working** | Confirmed by [@Smiorld](https://github.com/sjmotew/NarwalIntegration/issues/42) — port 9002 open, connects, map loads. Selectable in the model list; commands beyond connect/map not yet exercised ([#42](https://github.com/sjmotew/NarwalIntegration/issues/42)) |
| **Freo Z Ultra** (hardware CX7, cloud identity J5) | **Working on tested variant** | Confirmed with product key `hEA7OEshlx` on firmware `v01.13.11.02`. Requires the cloud-assigned Device ID because this model does not broadcast. Base status, maps, consumables, and commands work locally; live cleaning position/progress is unavailable. See the variant note below. |
| **Freo Z10** (plain, non-Ultra / non-Pro) | **Under investigation** | Advertises `_narwal_sweeper._tcp` over mDNS and is picked up by discovery, but port 9002 returns `ECONNREFUSED` in every device state — the host is healthy and nothing is listening. Distinct from the Z10 Pro / Turbo and Z10 Ultra above, both of which work ([#92](https://github.com/sjmotew/NarwalIntegration/issues/92)) |
| **Freo X Ultra** (AX18/AX19) | **Not Compatible** | Uses ZeroMQ (port 6789) + Tuya cloud, not WebSocket ([#4](https://github.com/sjmotew/NarwalIntegration/issues/4)) |
| **Freo X Plus** | **Not Compatible** | Cloud-only — no local API |
| **Narwal J-series** (J1/J4) | **Not Compatible** | J1: HTTP-only (port 8080); J4: cloud-only (Tuya). J5 is the cloud identity of the supported global CX7 listed above. |

Models marked **Not Compatible** use a different protocol or are cloud-only. This is a hardware/firmware limitation.

**Other models?** Check with `nmap -p 9002 <your-vacuum-ip>`. If open, [open an issue](https://github.com/sjmotew/NarwalIntegration/issues/new/choose) with your model and results.

## Features

### Vacuum Control
- **Start / Stop / Pause / Resume** — validated on hardware (see the note above for `start` on newer Flow firmware)
- **Return to dock** / **Locate** (robot announces "Robot is here")
- **Fan speed** — Quiet, Standard, Strong, Super Powerful, Ultra Powerful (set-only; robot doesn't broadcast current level). The prior `Ultra` level-5 label remains selectable for automation compatibility. Ultra Powerful is not offered on the Freo Z10 Pro / Turbo (AX26), where the app's own top tier is Super Powerful and value 5 is unreachable ([#70](https://github.com/sjmotew/NarwalIntegration/issues/70)). On v1.0.1 these are `quiet` / `normal` / `strong` / `max` and are off by one tier — see the breaking-change note above
- **Room-specific cleaning** — exposed in the HA UI (requires HA 2026.3+ and a segment-to-area mapping, see Known Limitations). **Fixed in v1.0.2** ([#49](https://github.com/sjmotew/NarwalIntegration/pull/49)); broken in v1.0.1 and earlier

### Clean Settings
Shipped in v1.0.2 ([#50](https://github.com/sjmotew/NarwalIntegration/pull/50)) — applied to both room and whole-house cleans, which previously hardcoded max suction / wet mop / single pass:
- **Work mode** — vacuum, mop, vacuum then mop, vacuum and mop
- **Water level** — dry, normal, wet
- **Mop strength** — normal, high
- **Passes** — 1 to 3

### Sensors
- Battery level, cleaning time, firmware version
- Docked status (binary sensor), charging state (Charging / Fully Charged / Not Charging)
- Cleaning area — reports real covered area as of v1.0.1 ([#51](https://github.com/sjmotew/NarwalIntegration/pull/51))
- Current room and task progress on the vacuum entity during active cleans
- Last clean result — why the previous task ended ([#53](https://github.com/sjmotew/NarwalIntegration/pull/53), v1.0.2)
- Dust bag health and detergent remaining ([#52](https://github.com/sjmotew/NarwalIntegration/pull/52), v1.0.2)
- Station and consumable binary sensors — clean water tank, sewage tank, dust box, dust bag, station bag, error ([#52](https://github.com/sjmotew/NarwalIntegration/pull/52), v1.0.2)
- Maintenance and replacement alerts, with the affected parts listed as attributes ([#54](https://github.com/sjmotew/NarwalIntegration/pull/54), v1.0.2)

### Live Map
- Color-coded floor plan with room labels (all rooms — user-named and auto-generated)
- Furniture/obstacle overlay from the robot's stored map data
- Dock marker and Narwal-native live robot trail from `display_map` during cleaning
- Carpet-map debug image as a second camera ([#67](https://github.com/sjmotew/NarwalIntegration/pull/67), v1.0.2)
- Display toggles for room labels, furniture and furniture labels ([#62](https://github.com/sjmotew/NarwalIntegration/pull/62), v1.0.2)

### Dock
- **Ambient light** — off, fireplace, nightlight, purple, on models with a dock light ([#61](https://github.com/sjmotew/NarwalIntegration/pull/61), v1.0.2)

### Dashboard
v1.0.8's per-room profiles create six selects and a switch **per room** — 168 entities on a 24-room map — which is right for automations and wrong for a dashboard. Room profile controls are disabled by default; selection switches stay enabled but hidden so a saved selection can never become an unreachable state. Enable the room profile entities you actually use, then run [`tools/gen_dashboard.py`](tools/gen_dashboard.py) against your Home Assistant entity registry. It emits a **room-picker section** from those controls: one dropdown, and only the chosen room's controls on screen, plus a Whole-house panel for the defaults, Start / Clear-selection buttons, a `script` that cleans exactly one room with its own profile, and a Dock-tasks section. Paste the YAML into any sections-view dashboard. Needs the `state-switch` card from HACS.

### Connectivity
- Real-time WebSocket push updates on broadcasting models
- Auto-reconnect with exponential backoff
- Wake system for sleeping robots + keepalive heartbeat
- 60-second polling fallback

## Installation

### HACS (Recommended)

1. Open **HACS** > three-dot menu > **Custom repositories**
2. Add: `https://github.com/sjmotew/NarwalIntegration` (category: Integration)
3. Find **Narwal Flow Robot Vacuum** and click **Download**
4. **Restart Home Assistant**

### Manual

1. Copy `custom_components/narwal/` to your HA `config/custom_components/` directory
2. **Restart Home Assistant**

> **Only one Narwal integration at a time.** A separate project,
> [nadavbau/narwal-integration](https://github.com/nadavbau/narwal-integration), also
> installs to `custom_components/narwal`, so HACS cannot hold both — installing one
> replaces the other. Neither project can rename without orphaning its users' existing
> config entries and entity ids, so this is expected rather than a bug you can work
> around. Pick whichever suits your hardware. See
> [#84](https://github.com/sjmotew/NarwalIntegration/issues/84).

### Setup

Home Assistant discovers Narwal robots on the local network, so in most cases the
robot appears on its own under **Settings > Devices & Services** as a discovered
device — click **Configure**, pick your model, and you're done. The IP is filled in
for you.

To add one by hand, or if discovery doesn't find it:

1. **Settings > Devices & Services > Add Integration** > search "Narwal"
2. Enter your vacuum's IP address and select your model
3. Entities are created automatically

> **Tip:** Assign a static IP to your vacuum in your router. Discovery re-points an
> existing entry when the address changes, but a static lease avoids the round trip.

<details>
<summary>How discovery finds the robot</summary>

The robot advertises `_narwal_sweeper._tcp.local.` over mDNS, as an instance named
`_app_wss_server_<6hex>` with hostname `NARWAL_<6hex>.local.` on port 9002. Those six
hex characters are the tail of the robot's device ID, which is how a discovery is
matched to a robot you already added manually.

Some networks drop multicast between VLANs or under wireless client isolation, and
mDNS then never arrives. DHCP hostname matching covers that case — Home Assistant
lowercases hostnames before matching, so the declared pattern is `narwal_*`.

</details>

<details>
<summary>If Home Assistant and the robot are on different VLANs</summary>

Robots have been reported not to answer connections whose source address is outside
their own subnet, even when 9002/TCP is explicitly permitted through the firewall and
other devices on the same VLAN are reachable. Both ICMP and the WebSocket handshake
time out, so the symptom is a plain connection failure rather than an integration
error:

```text
Failed to connect to ws://<robot-ip>:9002: timed out during opening handshake
```

**Workaround:** source-NAT the traffic from Home Assistant so it reaches the robot
with an address on the robot's own subnet. This is confirmed working by a user running
Home Assistant on a separate VLAN from an IoT network ([#81]).

**Why SNAT is the fix and not just a workaround.** Two causes could produce this: the
robot filtering by source subnet, or the robot ignoring its DHCP-supplied default
gateway and so having no route back. Packet captures in [#81] settle it as the first.
On the router's IoT interface — which every packet in this exchange must cross — the
inbound SYN **is** visible arriving at the robot, and **no SYN-ACK** comes back. The
robot also demonstrably has a working default route, since it reaches Narwal's cloud
servers and those SYN-ACKs are visible outbound. So it receives the connection and
declines to answer, rather than answering into a black hole. ICMP being dropped the
same way fits a rule about the source address rather than anything specific to 9002.

That rules out a static route as a cleaner alternative: **there is no reply to route.**
Rewriting the source address is the only approach that gets an answer.

**Discovery across VLANs.** mDNS is multicast and most networks do not forward it
between VLANs, so by default the robot will not be discovered from another VLAN and
you add it by IP. If your router reflects or proxies mDNS across VLANs, discovery
does work — reported end-to-end on v1.0.5 with a reflector plus the SNAT above ([#81]).

Note the asymmetry if you are in that position: the robot is happy to *announce*
itself over multicast, and still refuses inbound unicast from a foreign subnet. So
discovery finding the robot and the connection then failing without SNAT is a normal
combination, not a contradiction.

[#81]: https://github.com/sjmotew/NarwalIntegration/issues/81

</details>

The Freo Z Ultra (CX7) also requires its 32-character cloud-assigned Device ID. Selecting that
model opens a dedicated Device ID page; other models use automatic discovery. The integration
itself never contacts the cloud.

#### Finding the Device ID

The Narwal app does not currently display this value. It is the 32-character hexadecimal
identifier used as the second component of a Narwal MQTT topic:

```text
/<product_key>/<device_id>/status/working_status
```

You can obtain it from one of these sources:

- The `deviceId` field returned by Narwal's authenticated account endpoint
  `/user-device-platform-server/device-info/getDeviceInfoList`.
- A Narwal MQTT capture, where it appears in the topic position shown above.
- The stored device identifier or diagnostics from an existing Narwal cloud integration.

Account and MQTT tooling is deliberately kept separate from this integration so Home Assistant
never receives your Narwal credentials. Do not post the Device ID publicly; treat it as a device
identifier even though it is not an account password or access token.

#### CX7 variants

Local control is currently verified on one global Freo Z Ultra whose cloud identity is J5,
product key is `hEA7OEshlx`, and firmware is `v01.13.11.02`. Issue
[#5](https://github.com/sjmotew/NarwalIntegration/issues/5) also contains reports of firmware
`1.12.10.02` and a `BYWBPqSxeC` identity. That key remains in discovery coverage, but has not
been proven to accept addressed local commands. Reports from additional regions and firmware
versions are needed before support can be considered universal.

### Room cleaning setup (required before `vacuum.clean_area` works)

Home Assistant's room cleaning targets **HA areas**, not the robot's own rooms, so there is a one-time mapping step. Without it the service fails with *"Area mapping is not configured for vacuum.&lt;entity&gt;"*.

1. Create a Home Assistant **area** for each room you want to clean (Settings → Areas & Zones), if you don't already have one.
2. Open the **mapping editor** and match each robot segment to its area (see below).
3. `vacuum.clean_area` can then target those areas, and the robot cleans the matching rooms.

#### Where the mapping editor lives

It hangs off the **entity**, not the integration. There is **no such option on the integration page or the device page** — that is the most common place people look and it is not there.

Any of these three routes opens the same editor:

| Route | Where |
|---|---|
| **Entity settings** (most reliable) | Settings → Devices & Services → **Entities** tab → search your vacuum → open it → **cog icon** → *Map vacuum segments to areas* |
| **First-run prompt** | Open the vacuum → **Clean areas** → **Configure** (only shown while no mapping exists, and only to admins) |
| **Header action** | Open the vacuum → **Clean areas** → header action (use this one once a mapping already exists) |

Home Assistant shows the option when the entity's domain is `vacuum` and it advertises the `CLEAN_AREA` feature. This integration sets that flag, so if the row is missing:

- **Hard-refresh the browser** (<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>R</kbd>) — the frontend caches aggressively.
- **Check the entity is not `unavailable`.** The row is gated on a live state object, so it will not render while the robot is unreachable.
- Requires **Home Assistant 2026.3+**, when area mapping was introduced.

Room names come from the robot's map — rooms you named in the Narwal app keep those names, and the rest use the shared room-type table corrected in [#48](https://github.com/sjmotew/NarwalIntegration/pull/48). **Name your rooms in the Narwal app before mapping**: the mapping keys on segment *id*, so renaming later is safe, but naming first means your HA areas, the robot's map and the vacuum entity's current-room attribute all read the same words.

`cleaning_area_id` accepts an **ordered list**, so you can clean several rooms in one job and the robot follows the order you picked.

> **Remapping resets this.** A fresh full-house map in the Narwal app renumbers segments, which invalidates the mapping. The integration detects the change and raises a Home Assistant repair issue so you know to redo it.

#### Example: scheduled multi-room clean with settings

Clean settings are read **at the moment the job is dispatched**, so an automation
sets the entities first and calls `vacuum.clean_area` last. Requested on
[#83](https://github.com/sjmotew/NarwalIntegration/issues/83).

```yaml
alias: Narwal — weekday morning clean
description: Kitchen then hallway, vacuum + mop, two passes
triggers:
  - trigger: time
    at: "09:30:00"
conditions:
  - condition: time
    weekday: [mon, tue, wed, thu, fri]
  # Don't start if the robot is already busy or has a fault
  - condition: state
    entity_id: vacuum.narwal_flow_vacuum
    state: docked
actions:
  # 1. Settings first — these are read when the job is dispatched
  - action: select.select_option
    target:
      entity_id: select.narwal_flow_clean_mode
    data:
      option: Vacuum and mop

  - action: select.select_option
    target:
      entity_id: select.narwal_flow_mopping_humidity
    data:
      option: Slightly wet

  - action: select.select_option
    target:
      entity_id: select.narwal_flow_mop_strength
    data:
      option: High

  - action: number.set_value
    target:
      entity_id: number.narwal_flow_cleaning_passes
    data:
      value: 2

  - action: vacuum.set_fan_speed
    target:
      entity_id: vacuum.narwal_flow_vacuum
    data:
      fan_speed: Strong

  # 2. Then dispatch the rooms, in the order you want them cleaned
  - action: vacuum.clean_area
    target:
      entity_id: vacuum.narwal_flow_vacuum
    data:
      cleaning_area_id:
        - kitchen
        - hallway
mode: single
```

Notes:

- **Substitute your own entity ids.** They are derived from your device name, so
  yours will differ — check Settings → Devices & Services → Entities.
- `cleaning_area_id` takes **HA area ids**, not robot room names, and the
  segment-to-area mapping above must exist first.
- The clean-settings entities apply to the *next* job. Changing them mid-clean
  does not alter the job in progress.
- `select.select_option` takes the **displayed** option (`Vacuum and mop`), not
  the underlying key (`vacuum_and_mop`).
- Fan speed is a separate call because it lives on the vacuum entity rather than
  in the clean-settings block.
- Omit the settings steps you don't care about — whatever you leave alone keeps
  its current value.


## Requirements

- Narwal vacuum on the same local network as Home Assistant
- Port 9002 reachable (no firewall blocking)
- Home Assistant 2025.1.0+ / Python 3.12+

## Known Limitations

- **Wake from deep sleep is unreliable** — robot may not respond after long idle periods. Opening the Narwal app briefly can help.
- **Single connection** — close the Narwal app before using HA to avoid conflicts.
- **CX7 has no live stream** — it never broadcasts, so cleaning position and progress do not update live. Polled base status, battery, dock state, maps, consumables, and commands remain available. State follows the 60-second poll, so the vacuum entity reaches `cleaning` up to a minute after the robot starts (31 s in a recorded run), and `cleaning_time`, `cleaning_area` and `current_room` stay `unknown` throughout a clean because they are only carried in broadcasts.
- **Fan speed is set-only** — robot doesn't broadcast its current level.
- **All cleaning requires the dock** — `clean/start_clean` returns `NOT_READY` if the robot is not docked when the command is sent. This applies to whole-house `vacuum.start` as well as room cleans.
- **Room cleaning needs a segment-to-area mapping** — `vacuum.clean_area` targets Home Assistant *areas*, not robot rooms, and the mapping editor is on the **entity**, not the integration or device page. See [Room cleaning setup](#room-cleaning-setup-required-before-vacuumclean_area-works). Without it the service fails with "Area mapping is not configured".
- **Only one floor at a time** — the integration uses the robot's *active* map, and never enumerates the others. On a multi-floor home only the rooms of the current map are visible to Home Assistant, and HA floors are not related to robot maps ([#43](https://github.com/sjmotew/NarwalIntegration/issues/43)).
- **Map may be stale** — robot can return an old map. A new clean cycle typically refreshes it.

## Future Features (On Hold)

These features have been researched and probed but are **on hold** pending further reverse engineering:

| Feature | Status | Blocker |
|---------|--------|---------|
| **Camera snapshots** | Client method works (robot returns ~170KB) | Image data is **AES-encrypted** — APK reverse engineering needed for decryption key |
| **Camera LED control** | Partial response from robot | Correct payload format unconfirmed; needs idle-state testing |
| **Vision obstacle overlay** | Built, tested, and removed | Robot broadcasts raw AI candidates (3-6x more than app shows), not confirmed detections. Unusable for map overlay. |
| **Patrol / cruise mode** | Topics identified in APK | Not yet probed; depends on camera working first |

Camera snapshot and LED entities will be added once the AES decryption key is extracted from the Narwal APK.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Cannot connect" during setup | Verify IP and that port 9002 is reachable. If it still fails, **open the Narwal app on your phone the moment you press Submit** — a sleeping robot may not answer within the setup timeout ([#40](https://github.com/sjmotew/NarwalIntegration/issues/40)). |
| Room cleaning runs the wrong rooms | Fixed in v1.0.2 ([#49](https://github.com/sjmotew/NarwalIntegration/pull/49)). If you are still on v1.0.1 or earlier this is expected — upgrade. |
| Room clean returns `NOT_READY` | `clean/start_clean` only works from the dock. Send the robot home first, then start the room clean. |
| Entities show "Unavailable" | Robot may be asleep. Open Narwal app briefly to wake it. |
| Map not showing | Map loads after robot wakes. A new clean refreshes a stale map. |
| Commands not responding | Close the Narwal app — only one WebSocket connection at a time. |
| Z10 Ultra disconnects | Re-add the integration with the correct model selected. |

## Project Status

**Where things stand — updated 2026-09-05, at the v1.0.8 release.**

**v1.0.8 is released** — everything below is shipped to HACS. 854 tests passing, CI green, and the integration deployed to a live Home Assistant instance and verified against real hardware before tagging. No open pull requests.

| Merged in v1.0.8 | What it does |
|---|---|
| [#87](https://github.com/sjmotew/NarwalIntegration/pull/87) | **Per-room cleaning profiles** — mode, suction, water, scrub, route and passes per room, room-selection switches, a `narwal.clean_rooms` service, and `vacuum.start` that cleans the selected rooms with their own settings in one mixed job. From @Sean-StarLabs |
| [#88](https://github.com/sjmotew/NarwalIntegration/pull/88) | **The vacuum entity advertises only the commands it can run right now**, and carries current room, progress and task status as attributes. The `current_room` sensor is gone. From @Sean-StarLabs |
| [#89](https://github.com/sjmotew/NarwalIntegration/pull/89) | **Native map trails** — Narwal's own rolling trajectory windows joined into one route, kept across restarts, cleared when a new clean starts. Closes [#75](https://github.com/sjmotew/NarwalIntegration/issues/75). From @Sean-StarLabs |
| [#91](https://github.com/sjmotew/NarwalIntegration/pull/91) | **Suction tiers named as the Narwal app names them** — Super Powerful and Ultra Powerful; the old Super / Ultra names still work. From @Sean-StarLabs |
| [#86](https://github.com/sjmotew/NarwalIntegration/pull/86) | **Dock task switches** — empty dustbin, wash mop, dry mop, dry dust bin, dry dock bag — on a dock device of its own, with robot commands gated while the dock is busy. From @Sean-StarLabs |
| [#85](https://github.com/sjmotew/NarwalIntegration/pull/85) | Models with a product-specific topic prefix keep that prefix after auto-detection. From @Sean-StarLabs |
| — | **Downloadable diagnostics** from the device page, and the bug template asks for it first |
| [#81](https://github.com/sjmotew/NarwalIntegration/issues/81) | **Every product key a model ships is recognised**, not just one — a regional Flow 2 key (`mkbqaprvrb`) was showing as Unknown and silently costing its owner the dock light. The model selector now defaults to Other / Auto-detect |
| [#93](https://github.com/sjmotew/NarwalIntegration/issues/93) | **`current_room` no longer stays `unknown` forever** — a bare `get_map` ack on first connection was cached as an empty map and never retried |
| — | Docs: the plain Freo Z10 recorded as under investigation ([#92](https://github.com/sjmotew/NarwalIntegration/issues/92)) — its port 9002 refuses connections outright, a third distinct failure signature |

| Merged in v1.0.7 | What it does |
|---|---|
| [#81](https://github.com/sjmotew/NarwalIntegration/issues/81) | **The model label now follows the product key the robot reported.** The selector defaults to "Narwal Flow" and discovery cannot pre-select, so accepting that default stored the right key under the wrong model name |

| Merged in v1.0.6 | What it does |
|---|---|
| [#90](https://github.com/sjmotew/NarwalIntegration/issues/90) | **Stops waking a docked robot that is merely quiet.** A docked Narwal has a duty cycle — 30-45s of broadcasts, then 60-124s of silence — and a 15s staleness threshold read every gap as sleep, firing ~1,900 wake bursts a day |
| [#81](https://github.com/sjmotew/NarwalIntegration/issues/81) | **Auto-detected robots are named after their model** instead of a raw product key, and the Device ID step is no longer a dead end when auto-detection fails |
| — | Docs: VLAN cause settled as source filtering; mDNS does cross VLANs with a multicast reflector |

| Merged in v1.0.5 | What it does |
|---|---|
| [#78](https://github.com/sjmotew/NarwalIntegration/pull/78) | **Automatic discovery** via mDNS (`_narwal_sweeper._tcp.local.`) and DHCP (`narwal_*`) — no more hunting for the robot's IP. From @StratoGh0st99 |
| [#82](https://github.com/sjmotew/NarwalIntegration/pull/82) | **Log flood fixed** — two wake/sleep INFO lines were 29% of a live install's log. From @hyeok-yoo. The wake bursts underneath are [#90](https://github.com/sjmotew/NarwalIntegration/issues/90), still open |
| [#42](https://github.com/sjmotew/NarwalIntegration/issues/42) | **Narwal JX confirmed working** and added to the model selector, after the first successful report |
| [#35](https://github.com/sjmotew/NarwalIntegration/pull/35) | **Capture tooling published** under `tools/` — app-traffic capture guide, session recorder, coverage probe. From @StratoGh0st99 |
| — | Docs: VLAN/SNAT cause pinned ([#81](https://github.com/sjmotew/NarwalIntegration/issues/81)), a complete scheduled multi-room automation ([#83](https://github.com/sjmotew/NarwalIntegration/issues/83)), the HACS domain collision ([#84](https://github.com/sjmotew/NarwalIntegration/issues/84)) |

| Merged in v1.0.4 | What it does |
|---|---|
| [#80](https://github.com/sjmotew/NarwalIntegration/pull/80) | **Consumable alerts actually report** — the lists were packed varints and had always been discarded, so both alert sensors said "no problem" on every robot, always. See [#79](https://github.com/sjmotew/NarwalIntegration/issues/79) |
| [#76](https://github.com/sjmotew/NarwalIntegration/pull/76) | **Freo Z Ultra (CX7) local control**, via a pasted Device ID — still no cloud. Polling only; no live map position |
| — | Fan tiers renamed to Quiet/Standard/Strong/Super/Ultra; Ultra withheld on AX26, where it silently applied Strong ([#70](https://github.com/sjmotew/NarwalIntegration/issues/70)) |
| — | `CleanParam` tag 8 identified as the coverage-precision toggle; consumables documented in [`docs/PROTOCOL.md`](docs/PROTOCOL.md) |

| Merged since v1.0.1 | What it does |
|---|---|
| [#49](https://github.com/sjmotew/NarwalIntegration/pull/49) | **Room cleaning via `clean/start_clean`** — the headline fix. Closes [#37](https://github.com/sjmotew/NarwalIntegration/issues/37) |
| [#48](https://github.com/sjmotew/NarwalIntegration/pull/48) | Room-type names taken from the app's own strings. Closes [#22](https://github.com/sjmotew/NarwalIntegration/issues/22) |
| [#50](https://github.com/sjmotew/NarwalIntegration/pull/50) | Clean settings as HA entities — work mode, water, mop strength, passes |
| [#63](https://github.com/sjmotew/NarwalIntegration/pull/63) | Interprets live `working_status` telemetry rather than a stale `base_status` |
| [#73](https://github.com/sjmotew/NarwalIntegration/issues/73) | **v1.0.3** — renews the broadcast subscription before it lapses, so `working_status` and `display_map` keep arriving and the entity stops freezing at `docked` |
| [#62](https://github.com/sjmotew/NarwalIntegration/pull/62) | Map rendering options as switches — room labels, furniture, furniture labels |
| [#61](https://github.com/sjmotew/NarwalIntegration/pull/61) | Dock ambient light entity, on models that have one |
| [#24](https://github.com/sjmotew/NarwalIntegration/pull/24) | Current-room telemetry for the room being cleaned right now |
| [#53](https://github.com/sjmotew/NarwalIntegration/pull/53) / [#54](https://github.com/sjmotew/NarwalIntegration/pull/54) | Last-clean-result sensor; consumable maintenance and replacement alerts |
| [#52](https://github.com/sjmotew/NarwalIntegration/pull/52) | `base_status` field audit; station and consumable diagnostics |
| [#67](https://github.com/sjmotew/NarwalIntegration/pull/67) | Carpet-map camera image; `working_status 7` mapped to remapping |
| [#72](https://github.com/sjmotew/NarwalIntegration/pull/72) | Unknown status values warn once instead of flooding the log. Closes [#46](https://github.com/sjmotew/NarwalIntegration/issues/46) |
| [#71](https://github.com/sjmotew/NarwalIntegration/pull/71) | asyncio deprecation fix for Python 3.12 |
| [#47](https://github.com/sjmotew/NarwalIntegration/pull/47) | Config-flow translation sync |
| [#69](https://github.com/sjmotew/NarwalIntegration/issues/69) | `vacuum.start` routes through `clean/start_clean` instead of silently no-opping |
| — | AX26 in the model selector ([#40](https://github.com/sjmotew/NarwalIntegration/issues/40), [#70](https://github.com/sjmotew/NarwalIntegration/issues/70)); Narwal JX product key ([#42](https://github.com/sjmotew/NarwalIntegration/issues/42)); [`docs/PROTOCOL.md`](docs/PROTOCOL.md) published |

### Next steps

1. **Repair issues** — the integration never raises a Home Assistant repair. v1.0.8 renamed suction tiers and removed a sensor; both should surface as repairs rather than release-notes prose.
2. **Multi-floor map switching** ([#43](https://github.com/sjmotew/NarwalIntegration/issues/43)) — the only substantial unimplemented request, now tractable through the `map_id` plumbing that landed with the room profiles.
3. **Last-clean sensors** ([#32](https://github.com/sjmotew/NarwalIntegration/issues/32)).

### Open protocol questions — help wanted

- ~~**Is there a fifth suction tier?**~~ **Answered** ([#70](https://github.com/sjmotew/NarwalIntegration/issues/70)) — the AX26 app's top tier sends `4` (`DEEP`), while a Flow 2 accepted and actively reported a clean configured with `5` (`SUPER`). Ultra Powerful is withheld only on models known not to support it.
- **What is `CleanParam` tag 8?** The Narwal app sends `8 = 2`; we never send it and cleaning works without it. The best current candidate is the app's two-value coverage-precision toggle ([#25](https://github.com/sjmotew/NarwalIntegration/issues/25)).
- **The complete `WorkingStatus` enum.** Values have been discovered one user bug report at a time — `17` turned out to be the custom per-room clean and is decoded since v1.0.8. Anyone holding an APK `BuilderInfo` decode can end that ([#46](https://github.com/sjmotew/NarwalIntegration/issues/46)).
- **Does the Freo X Ultra speak the local protocol?** The compatibility table says no, per [#4](https://github.com/sjmotew/NarwalIntegration/issues/4). A product key and `nmap -p 9002` from an owner settles it either way.
- **Why does the plain Freo Z10 refuse port 9002?** It advertises over mDNS like every other model, then returns `ECONNREFUSED` ([#92](https://github.com/sjmotew/NarwalIntegration/issues/92)). A full port scan and an app-to-robot capture would tell us what it listens on instead.

## Reporting Issues

Use the [issue templates](https://github.com/sjmotew/NarwalIntegration/issues/new/choose) — they collect your HA version, model, and debug logs for faster diagnosis.

### Attach diagnostics

**Settings → Devices & Services → Narwal → ⋮ next to your device → Download diagnostics.**

Attaching that file answers most of what would otherwise be asked one question at a time. It contains:

| Section | What it settles |
|---------|-----------------|
| `model_resolution` | Your robot's product key, and **whether this build recognises it at all** |
| `device` | Firmware version, and the six-character device suffix that matches your logs |
| `connection` | Whether the robot is connected, awake, and broadcasting |
| `feature_list` | What the robot says it supports — or why it refused to answer |
| `raw_base_status` | The undecoded protobuf fields new model support is built from |

**Redacted automatically:** your IP address, full device ID, and Narwal account UUID. The **product key is deliberately kept** — it identifies a model, not a person, and it is usually the answer.

If your robot reports a product key this integration doesn't know, say so in the issue — that alone is often a one-line fix.

## Protocol Documentation

[**docs/PROTOCOL.md**](docs/PROTOCOL.md) documents the local WebSocket protocol — frame format, topic reference, message field maps, and the open questions. It also records the assumptions this project got wrong and how they were caught, which is the part most likely to save someone else time.

Corrections and captures are welcome; the doc explains how to take them.

## Disclaimer

This is an **unofficial**, community-developed integration — not affiliated with or endorsed by Narwal. The local protocol was reverse-engineered from network traffic and the Narwal mobile application.

- **Use at your own risk.** No warranty.
- **No cloud dependency.** No external data transmission.
- **Firmware updates** from Narwal may break this integration at any time.

## Contributing

Contributions and testing welcome! If you have a non-Flow Narwal model, testing reports are especially valuable.

## License

MIT
