# Narwal Robot Vacuum — Home Assistant Integration

> **Status: Development Preview** — Not yet tested on a live Home Assistant instance. Core client library is proven, but the HA integration layer needs validation. Use at your own risk.

A fully **local, cloud-independent** [Home Assistant](https://www.home-assistant.io/) custom integration for Narwal robot vacuums. Communicates directly with your vacuum over your local network — no cloud account or internet connection required.

## Supported Devices

| Model | Internal Code | Status |
|-------|---------------|--------|
| Narwal Flow | AX12 | In Development |

Other Narwal models may work but have not been tested. If you have a different model, please open an issue.

## Planned Features

- **Vacuum control** — start, stop, pause, return to dock
- **Locate** — robot announces "Robot is here"
- **Fan speed** — quiet, normal, strong, max
- **Battery level** — real-time percentage
- **Cleaning stats** — area cleaned (m²), cleaning time
- **Firmware version** — diagnostic sensor
- **Docked status** — binary sensor
- **Real-time updates** — WebSocket push (no polling delay)
- **Map support** — planned for a future release

## Current Limitations

- Not yet tested on a live Home Assistant instance
- Python dependencies (`websockets`, `blackboxprotobuf`) need validation in HA environment
- Fan speed read-back not yet implemented (set works, display does not)
- Map/image entity is a stub (Phase 4)

## Requirements

- Narwal robot vacuum on the same local network as Home Assistant
- The vacuum must be reachable on port 9002 (no firewall blocking)
- Home Assistant 2025.1.0 or later

## How It Works

This integration communicates with your Narwal vacuum over a local WebSocket connection on port 9002. The vacuum broadcasts status updates in real time, and the integration sends commands using the same local protocol. No cloud services are involved.

## Contributing

This is an early-stage project. Contributions and testing are welcome! Please open an issue or pull request on [GitHub](https://github.com/sjmotew/NarwalIntegration).

## License

MIT
