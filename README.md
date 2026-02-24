# Narwal Robot Vacuum — Home Assistant Integration

A fully **local, cloud-independent** [Home Assistant](https://www.home-assistant.io/) custom integration for Narwal robot vacuums. Communicates directly with your vacuum over your local network — no cloud account or internet connection required.

## Supported Devices

| Model | Internal Code | Status |
|-------|---------------|--------|
| Narwal Flow | AX12 | Supported |

Other Narwal models may work but have not been tested. If you have a different model, please open an issue.

## Features

- **Vacuum control** — start, stop, pause, return to dock
- **Locate** — robot announces "Robot is here"
- **Fan speed** — quiet, normal, strong, max
- **Battery level** — real-time percentage
- **Cleaning stats** — area cleaned (m²), cleaning time
- **Firmware version** — diagnostic sensor
- **Docked status** — binary sensor
- **Real-time updates** — WebSocket push (no polling delay)

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu, select **Custom repositories**
3. Add `https://github.com/sjmotew/NarwalIntegration` as an **Integration**
4. Search for "Narwal Robot Vacuum" and install
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/narwal/` folder into your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings > Devices & Services > Add Integration**
2. Search for **Narwal Robot Vacuum**
3. Enter your vacuum's IP address (find it in your router's DHCP table or the Narwal app)
4. The integration will connect and create all entities automatically

## Entities Created

| Entity | Type | Description |
|--------|------|-------------|
| Vacuum | `vacuum` | Main vacuum control with state, battery, fan speed |
| Battery | `sensor` | Battery percentage |
| Cleaning area | `sensor` | Area cleaned in m² |
| Cleaning time | `sensor` | Duration of current/last clean |
| Firmware version | `sensor` | Current firmware (diagnostic) |
| Docked | `binary_sensor` | Whether the vacuum is on the dock |

## Requirements

- Narwal robot vacuum on the same local network as Home Assistant
- The vacuum must be reachable on port 9002 (no firewall blocking)
- Home Assistant 2025.1.0 or later

## How It Works

This integration communicates with your Narwal vacuum over a local WebSocket connection on port 9002. The vacuum broadcasts status updates in real time, and the integration sends commands using the same local protocol. No cloud services are involved.

## Troubleshooting

- **Cannot connect**: Ensure the vacuum is powered on and connected to your Wi-Fi network. Verify the IP address is correct.
- **Entities unavailable**: The vacuum may be in sleep mode. It will wake up when the integration sends a command or when you interact with it via the Narwal app.
- **IP changed**: If your vacuum's IP address changes (DHCP), update the integration configuration. Consider setting a static IP or DHCP reservation for your vacuum.

## Contributing

Contributions are welcome! Please open an issue or pull request on [GitHub](https://github.com/sjmotew/NarwalIntegration).

## License

MIT
