# Supported models

This integration uses a **local WebSocket connection on port 9002**. Only models that expose
that port are supported. The short table in the [README](../README.md#device-compatibility)
is the summary; this page carries the evidence behind each row.

**Other models?** Check with `nmap -p 9002 <your-vacuum-ip>`. If the port is open,
[open an issue](https://github.com/sjmotew/NarwalIntegration/issues/new/choose) with your model,
firmware and the diagnostics download (see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#attach-diagnostics)).

## Working

| Model | Product keys | Firmware seen | Evidence |
|---|---|---|---|
| **Narwal Flow** (AX12) | — | v01.07.22, v01.08.03.07 | Primary development target. Room cleaning confirmed on v01.08.03.07 with [#49](https://github.com/sjmotew/NarwalIntegration/pull/49). On v01.07.22+, `vacuum.start` needs a loaded map ([#36](https://github.com/sjmotew/NarwalIntegration/issues/36)). |
| **Narwal Flow 2** | `QxMSPG6VSO`, `iSuVlI1If2`, `mkbqaprvrb` | v01.09.09, v01.09.10 | Room cleaning fixed by [#49](https://github.com/sjmotew/NarwalIntegration/issues/49). Firmware v01.09.10 reports `working_status 2` while cleaning off-dock; decoded since v1.0.11 ([#124](https://github.com/sjmotew/NarwalIntegration/issues/124)). Clean-water and detergent tanks are not reported while docked ([#128](https://github.com/sjmotew/NarwalIntegration/issues/128)). |
| **Freo Z10 Ultra** (CX4) | — | — | Community confirmed. |
| **Freo Z10 Pro / Turbo** (AX26) | — | v01.02.00.15 | Same product key and firmware reported under both names ([#40](https://github.com/sjmotew/NarwalIntegration/issues/40), [#70](https://github.com/sjmotew/NarwalIntegration/issues/70)). Room cleaning confirmed with [#49](https://github.com/sjmotew/NarwalIntegration/issues/49). The app's top suction tier here is Super Powerful; Ultra Powerful (value 5) is unreachable and not offered. |
| **Freo X10 Pro** (AX15) | — | — | Community confirmed ([#12](https://github.com/sjmotew/NarwalIntegration/issues/12)). |
| **Narwal JX** | — | — | Confirmed by @Smiorld: port 9002 open, connects, map loads. Commands beyond connect / map not yet exercised ([#42](https://github.com/sjmotew/NarwalIntegration/issues/42)). Not the same platform as the Freo 20; the two report different product keys. |
| **Narwal Freo 20** | `fjhpiem4ba` | v01.00.35.03, v01.00.36.11 | Confirmed by @kvkessler: map streaming, current room, cleaning area and dock sensors all live. Selectable by name since v1.0.9 ([#97](https://github.com/sjmotew/NarwalIntegration/issues/97)). |
| **Narwal Freo 20 Edge** (SKU YJCC041) | `ulonq49mm1` | v01.06.10.48 | Confirmed by @cwisor via Other / Auto-detect: broadcasts, map streaming, dock sensors, consumables and a room clean all working. Distinct key from the plain Freo 20; selectable by name since v1.0.11 ([#109](https://github.com/sjmotew/NarwalIntegration/issues/109)). |

## Working on the tested variant

### Freo Z Ultra (hardware CX7, cloud identity J5)

Confirmed with product key `hEA7OEshlx` on firmware `v01.13.11.02`, on two independent robots
(one EU). This model **does not broadcast**, so it needs the cloud-assigned Device ID at setup
(see [SETUP.md](SETUP.md#freo-z-ultra-cx7-device-id)). Base status, maps, consumables,
room-selective cleaning and work-mode selection all work locally. Live cleaning position and
progress are unavailable (the robot sends them to Narwal's cloud only), and the reported state
tracks cleaning on some units but sat at one static code on the first two measured; see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#cx7-freo-z-ultra) for what that means in practice.

Issue [#5](https://github.com/sjmotew/NarwalIntegration/issues/5) also contains reports of
firmware `1.12.10.02` and a `BYWBPqSxeC` identity. That key remains in discovery coverage but
has not been proven to accept addressed local commands. Reports from more regions and firmware
versions are needed before support can be called universal.

## Under investigation

### Freo Z10 (plain, non-Ultra / non-Pro)

Advertises `_narwal_sweeper._tcp` over mDNS and is picked up by discovery, but port 9002
returns `ECONNREFUSED` in every device state. The host is healthy and nothing is listening.
Two units so far (firmware v01.04.50.01 on the second): every closed port answers RST and 22
high ports between 33629 and 57609 are open, so the robot simply does not run the local server
on 9002. Distinct from the Z10 Pro / Turbo and Z10 Ultra, both of which work
([#92](https://github.com/sjmotew/NarwalIntegration/issues/92)). An `nmap -sV` fingerprint of
the open ports and an app-to-robot capture would tell us what it listens on instead.

## Not compatible

These use a different protocol or are cloud-only. This is a hardware / firmware limitation.

| Model | Why |
|---|---|
| **Freo X Ultra** (AX18 / AX19) | ZeroMQ on port 6789 plus Tuya cloud, not WebSocket ([#4](https://github.com/sjmotew/NarwalIntegration/issues/4)). A product key and `nmap -p 9002` from an owner would settle it either way. |
| **Freo X Plus** | Cloud-only; no local API. |
| **Narwal J1** and its international twin the **T10** | No port 9002; port 8080 answers `501 Not Implemented`; the official app controls the robot only through Narwal's cloud (`usaclient` / `usawebsocket.narwaltech.com`). A full 65,535-port scan and an app capture are in [#44](https://github.com/sjmotew/NarwalIntegration/issues/44). |
| **Narwal J4** | Cloud-only (Tuya). |

J5 is the cloud identity of the supported global CX7 above, not a separate model.
