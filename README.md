# Narwal Robot Vacuum — Home Assistant Integration

[![Latest release](https://img.shields.io/github/v/release/sjmotew/NarwalIntegration?label=release)](https://github.com/sjmotew/NarwalIntegration/releases/latest)
[![CI](https://github.com/sjmotew/NarwalIntegration/actions/workflows/validate.yml/badge.svg)](https://github.com/sjmotew/NarwalIntegration/actions/workflows/validate.yml)
[![HACS custom](https://img.shields.io/badge/HACS-custom%20repository-41BDF5)](#installation)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#license)

A fully **local, cloud-independent** [Home Assistant](https://www.home-assistant.io/) custom
integration for Narwal robot vacuums. It talks to the robot directly over your network via
WebSocket on port 9002. No cloud account, no internet connection, no credentials.

> **Current release: v1.0.11.** No breaking changes since v1.0.8. Every version is listed in the
> [CHANGELOG](CHANGELOG.md) with its breaking changes flagged; long-form notes per release are
> under [`docs/`](docs/). **Upgrading from v1.0.1 or earlier?** Read the
> [v1.0.2 notes](docs/RELEASE-NOTES-v1.0.2.md) first: room names, suction tiers and
> `vacuum.start` all changed.

## Device compatibility

| Model | Status |
|---|---|
| **Narwal Flow** (AX12) | **Working**, primary development target |
| **Narwal Flow 2** | **Working** |
| **Freo Z10 Ultra** (CX4) | **Working** |
| **Freo Z10 Pro / Turbo** (AX26) | **Working** |
| **Freo X10 Pro** (AX15) | **Working** |
| **Narwal JX** | **Working** (connect and map confirmed; commands not yet exercised) |
| **Narwal Freo 20** | **Working** |
| **Narwal Freo 20 Edge** | **Working** |
| **Freo Z Ultra** (CX7) | **Working on the tested variant**; needs its cloud Device ID, no live position |
| **Freo Z10** (plain) | **Under investigation**: port 9002 refuses connections |
| **Freo X Ultra**, **Freo X Plus**, **J1 / T10**, **J4** | **Not compatible**: different protocol or cloud-only |

Firmware versions, product keys, who confirmed what and the evidence for each row are in
[**docs/MODELS.md**](docs/MODELS.md). **Other models?** Run `nmap -p 9002 <vacuum-ip>`; if the
port is open, [open an issue](https://github.com/sjmotew/NarwalIntegration/issues/new/choose).

## Features

| Area | What you get |
|---|---|
| **Vacuum control** | Start / Stop / Pause / Resume, Return to dock, Locate. Fan speed: Quiet, Standard, Strong, Super Powerful, Ultra Powerful (set-only). **Room-specific cleaning** via `vacuum.clean_area` (HA 2026.3+, one-time [area mapping](docs/ROOM-CLEANING.md)) |
| **Clean settings** | Work mode (vacuum, mop, vacuum then mop, vacuum and mop), water level, mop strength, passes 1 to 3. Applied to room and whole-house cleans |
| **Per-room profiles** | Mode, suction, water, scrub, route and passes per room; room-selection switches; a `narwal.clean_rooms` service. Disabled by default; [`tools/gen_dashboard.py`](tools/gen_dashboard.py) builds a room-picker dashboard from the ones you enable |
| **Sensors** | Battery, cleaning time and area, firmware, docked and charging state, last clean result, dust bag health, detergent; current room and task progress as vacuum attributes during a clean |
| **Alerts** | Clean-water tank, sewage tank, dust box, dust bag, station bag, error; maintenance and replacement alerts with the affected parts as attributes |
| **Live map** | Colour-coded floor plan with room labels, furniture overlay, dock marker and the robot's own live trail during cleaning; a carpet-map camera; toggles for labels and furniture |
| **Dock** | Ambient light (off, fireplace, nightlight, purple) on models that have one; dock task switches: empty dustbin, wash mop, dry mop, dry dust bin, dry dock bag |
| **Connectivity** | Push updates over WebSocket on broadcasting models, auto-reconnect with backoff, wake system plus keepalive heartbeat, 60-second polling fallback, mDNS / DHCP discovery |

## Installation

**Requirements:** Home Assistant 2025.1.0 or newer, and the robot on a network where port 9002
is reachable from Home Assistant.

### HACS (recommended)

1. Open **HACS** > three-dot menu > **Custom repositories**
2. Add `https://github.com/sjmotew/NarwalIntegration` (category: Integration)
3. Find **Narwal Flow Robot Vacuum** and click **Download**
4. **Restart Home Assistant**

### Manual

Copy `custom_components/narwal/` into your HA `config/custom_components/` directory and restart
Home Assistant.

> **Only one Narwal integration at a time.** Another project also installs to
> `custom_components/narwal`, so HACS cannot hold both. See
> [docs/SETUP.md](docs/SETUP.md#only-one-narwal-integration-at-a-time).

### Setup

Home Assistant discovers Narwal robots on the local network, so in most cases the robot appears
on its own under **Settings > Devices & Services**. Click **Configure**, pick your model, done.
To add one by hand: **Add Integration** > search "Narwal" > enter the IP and select the model.
A static DHCP lease for the robot avoids address changes.

The **Freo Z Ultra (CX7)** also needs its 32-character cloud Device ID, which the app does not
show. [docs/SETUP.md](docs/SETUP.md) explains how to find it, how discovery matches each model,
and the source-NAT fix when Home Assistant and the robot sit on different VLANs.

### Room cleaning

`vacuum.clean_area` targets **Home Assistant areas**, not robot rooms, so there is a one-time
mapping step. The editor hangs off the **entity** (cog icon → *Map vacuum segments to areas*),
not the integration or device page. [docs/ROOM-CLEANING.md](docs/ROOM-CLEANING.md) walks through
it and includes a complete scheduled multi-room automation.

## Known limitations

- **All cleaning requires the dock.** The robot returns `NOT_READY` otherwise, for `vacuum.start` as well as room cleans.
- **Single connection.** Close the Narwal app before using HA; the robot allows one WebSocket client.
- **Wake from deep sleep is unreliable** after long idle periods; opening the app briefly helps.
- **Fan speed is set-only.** The robot does not broadcast its current level.
- **One floor at a time.** Only the rooms of the robot's active map are visible ([#43](https://github.com/sjmotew/NarwalIntegration/issues/43)).
- **CX7 has no live telemetry** and its reported state lags; control works, position and progress do not.
- **Flow 2 clean-water and detergent tanks are not reported while docked** ([#128](https://github.com/sjmotew/NarwalIntegration/issues/128)).

The full list with the evidence, and a quick-fix table for the common setup problems, is in
[**docs/TROUBLESHOOTING.md**](docs/TROUBLESHOOTING.md).

## Reporting issues

Use the [issue templates](https://github.com/sjmotew/NarwalIntegration/issues/new/choose) and
attach the diagnostics download (**device page → ⋮ → Download diagnostics**). It carries the
product key, firmware, connection state and raw status fields, with your IP, device ID and
account UUID redacted. [What each section settles](docs/TROUBLESHOOTING.md#attach-diagnostics).

## Documentation

| Document | Contents |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | Every release, newest first, breaking changes flagged |
| [docs/MODELS.md](docs/MODELS.md) | Compatibility evidence per model: product keys, firmware, reporters |
| [docs/SETUP.md](docs/SETUP.md) | Discovery internals, VLAN / SNAT, CX7 Device ID |
| [docs/ROOM-CLEANING.md](docs/ROOM-CLEANING.md) | Area mapping, per-room profiles, dashboard generator, automation example |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Quick fixes, known limitations in detail, diagnostics |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | The local WebSocket protocol: frames, topics, field maps, corrections, open questions |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Next steps, parked features, captures that would settle open questions |
| [docs/RELEASE-NOTES-*.md](docs/) | Long-form notes per release |

## Project status

Actively maintained. Each release is CI-tested, deployed to a live Home Assistant instance and
verified against real hardware before it is tagged. Open pull requests and the work queued for
the next version are on the [pull requests](https://github.com/sjmotew/NarwalIntegration/pulls)
and [milestones](https://github.com/sjmotew/NarwalIntegration/milestones) pages.

## Contributing

Contributions and testing reports are welcome, especially from owners of models other than the
Flow. [docs/PROTOCOL.md](docs/PROTOCOL.md#12-contributing-captures) explains how to take a
capture that answers a protocol question. Run the test suite with `py -m pytest -q` (or
`python -m pytest -q`); `narwal_client/` and `custom_components/narwal/narwal_client/` must stay
identical. A second suite in `tests_ha/` loads the integration in the genuine Home Assistant
package (Linux only, pinned in `requirements-ha-test.txt`); CI runs it as the "Loads in HA" check.

## Disclaimer

This is an **unofficial**, community-developed integration, not affiliated with or endorsed by
Narwal. The local protocol was reverse-engineered from network traffic and the Narwal mobile
application. Use at your own risk; no warranty. No data leaves your network. Firmware updates
from Narwal may break this integration at any time.

## License

MIT

### Reset next-clean profiles

`narwal.set_pending_profiles` replaces one docked robot’s pending whole-floor profile, every current room profile and the final room selection as one in-memory update. Persistence is scheduled through the existing stores; it is not a crash-atomic disk transaction. It does not start, stop, wake or reconfigure a running clean. Requests are validated before any setting changes; an active task, unavailable map or unloaded profile store rejects the entire update.

This is useful for a scheduled reset: separate select calls can otherwise leave a partial profile or reach live suction/water controls if a clean starts between calls. Per-room profiles may differ from the whole-floor profile. Room order and profiles belonging to other maps are preserved. Enable the room-selection switches first: the service resolves and validates the supplied room entities against the current map.
