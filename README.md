# Narwal Robot Vacuum for Home Assistant

[![Latest release](https://img.shields.io/github/v/release/sjmotew/NarwalIntegration?label=release)](https://github.com/sjmotew/NarwalIntegration/releases/latest)
[![CI](https://github.com/sjmotew/NarwalIntegration/actions/workflows/validate.yml/badge.svg)](https://github.com/sjmotew/NarwalIntegration/actions/workflows/validate.yml)
[![HACS custom](https://img.shields.io/badge/HACS-custom%20repository-41BDF5)](#installation)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Local control of Narwal robot vacuums from [Home Assistant](https://www.home-assistant.io/).
Talks to the robot directly over WebSocket on port 9002: no cloud account, no internet, no credentials.

**Current release: v1.0.12.** What changed, and what might break an automation, is in the
[CHANGELOG](CHANGELOG.md). Long-form notes per release are under [`docs/`](docs/).

## Supported models

| Status | Models |
|---|---|
| **Working** | Flow, Flow 2, Freo Z10 Ultra, Freo Z10 Pro / Turbo, Freo X10 Pro, Freo 20, Freo 20 Edge, JX |
| **Control only** | Freo Z Ultra (CX7): commands work, no live position; needs its cloud Device ID ([setup](docs/SETUP.md#freo-z-ultra-cx7-device-id)) |
| **Investigating** | Freo Z10 (plain): port 9002 refuses connections ([#92](https://github.com/sjmotew/NarwalIntegration/issues/92)) |
| **Not compatible** | Freo X Ultra, Freo X Plus, J1 / T10, J4 (different protocol or cloud-only) |

Firmware, product keys and who confirmed each model: [docs/MODELS.md](docs/MODELS.md).
Another model? Run `nmap -p 9002 <vacuum-ip>`; if the port is open, [open an issue](https://github.com/sjmotew/NarwalIntegration/issues/new/choose).

## Features

- **Vacuum entity**: start, stop, pause, resume, return to dock, locate, fan speed; room cleaning through `vacuum.clean_area`
- **Clean settings**: work mode, suction, water level, mop strength, passes, route; per-room profiles, a `narwal.clean_rooms` action, and `narwal.set_pending_profiles` to set every next-clean profile at once
- **Live map camera** with room labels, furniture, dock and trail; position, dock and trail attributes for map cards
- **Sensors and alerts**: battery, cleaning time and area, current room, task progress, tanks, dust bag, consumables, errors
- **Dock controls**: empty dustbin, wash mop, dry mop, dry dust bin, dry dock bag; ambient light on docks that have one
- **Connectivity**: push updates, auto-reconnect, keepalive, mDNS and DHCP discovery, German, French and Korean translations

## Installation

Requires Home Assistant 2025.1 or newer and port 9002 reachable from Home Assistant to the robot.

**HACS:** HACS → ⋮ → **Custom repositories** → add `https://github.com/sjmotew/NarwalIntegration`
as *Integration* → download **Narwal Flow Robot Vacuum** → restart Home Assistant.

**Manual:** copy `custom_components/narwal/` into your `config/custom_components/` and restart.

> Only one Narwal integration can be installed at a time; another project uses the same
> `custom_components/narwal` folder. [Details](docs/SETUP.md#only-one-narwal-integration-at-a-time).

## Setup

The robot is discovered on the local network and appears under **Settings → Devices & Services**.
Click **Configure**, pick your model, done. To add one by hand: **Add Integration → Narwal**, enter the
IP and model. Give the robot a static DHCP lease. The Freo Z Ultra also asks for its cloud Device ID.

**Room cleaning** targets Home Assistant *areas*, so there is a one-time mapping step on the vacuum
entity (⚙ → *Map vacuum segments to areas*). [docs/ROOM-CLEANING.md](docs/ROOM-CLEANING.md) walks
through it, the per-room profiles and a scheduled multi-room automation.

## Good to know

- Cleaning can only start from the dock; the robot answers `NOT_READY` otherwise.
- The robot accepts one client at a time, so close the Narwal app while Home Assistant is connected.
- Only the rooms of the robot's active map are visible.
- Fan speed is set-only; the robot does not report its current level.

The full list of limitations, quick fixes for common setup problems and how to attach diagnostics
to a report: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Documentation

| Document | Contents |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | Every release, newest first, breaking changes flagged |
| [docs/SETUP.md](docs/SETUP.md) | Discovery, VLANs, the CX7 Device ID |
| [docs/ROOM-CLEANING.md](docs/ROOM-CLEANING.md) | Area mapping, per-room profiles, actions, automation example |
| [docs/MAP-CARD.md](docs/MAP-CARD.md) | Map camera attributes for dashboard cards |
| [docs/MODELS.md](docs/MODELS.md) | Compatibility evidence per model |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Quick fixes, limitations, diagnostics |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | The local WebSocket protocol, for contributors |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What is next and what is parked |

## Issues and contributing

Use the [issue templates](https://github.com/sjmotew/NarwalIntegration/issues/new/choose) and attach
the diagnostics download (device page → ⋮ → *Download diagnostics*; IP, device ID and account are
redacted). Pull requests and test reports from owners of other models are welcome;
[docs/PROTOCOL.md](docs/PROTOCOL.md#12-contributing-captures) explains how to take a useful capture.
Tests: `py -m pytest -q`. `narwal_client/` and `custom_components/narwal/narwal_client/` must stay identical.

Every release is CI-tested, deployed to a live Home Assistant instance and verified on hardware before it is tagged.

## Disclaimer and license

Unofficial, community-developed, not affiliated with or endorsed by Narwal. The protocol was
reverse-engineered from network traffic and the Narwal app; a firmware update may break it at any
time. No data leaves your network. Use at your own risk. [MIT](LICENSE).
