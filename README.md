# Narwal Flow Robot Vacuum — Home Assistant Integration

A fully **local, cloud-independent** [Home Assistant](https://www.home-assistant.io/) custom integration for Narwal robot vacuums. Communicates directly with your vacuum over your local network via WebSocket — no cloud account or internet connection required.

> **Status: v0.3.0 — Early Release** — Core vacuum control, sensors, and map display are working. Some features are still being refined. Use at your own risk.

## Supported Devices

| Model | Internal Code | Status | Notes |
|-------|---------------|--------|-------|
| Narwal Flow | AX12 | Working | Local WebSocket on port 9002 |
| Narwal FREO Z10 | — | Not Compatible | No local WebSocket; uses cloud/P2P protocol |

Other Narwal models *may* work if they expose a local WebSocket on port 9002 (same protocol as the Flow/AX12). If you have a different model, please open an issue with your model name, an nmap scan of the device, and any logs.

## Features

### Vacuum Control
- **Start / Stop / Pause / Resume** cleaning
- **Return to dock**
- **Locate** — robot announces "Robot is here"
- **Fan speed control** — Quiet, Normal, Strong, Max
- **Mop humidity control** — Dry, Normal, Wet (select entity)

### Sensors
- **Battery level** — real-time percentage from robot broadcasts
- **Cleaning area** — square meters cleaned in current session
- **Cleaning time** — current session duration in seconds
- **Firmware version** — diagnostic sensor
- **Docked status** — binary sensor (on dock / off dock), including charge-complete detection

### Map
- **Floor plan image** — rendered as a color-coded room map
- Supports 22+ rooms with distinct colors and wall borders
- Dock position indicator
- Room names from robot's stored map data

### Connectivity
- **Real-time updates** — WebSocket push (~1.5s when robot is awake)
- **Auto-reconnect** with exponential backoff
- **Wake system** — automatic wake commands to rouse a sleeping robot
- **Keepalive heartbeat** — prevents robot from going back to sleep during a session
- **Polling fallback** — 60-second poll if push updates stop

## Installation

### HACS (Recommended)

1. Open Home Assistant and go to **HACS** in the sidebar.
2. Click the **three-dot menu** (top right) and select **Custom repositories**.
3. Add the repository URL:
   ```
   https://github.com/sjmotew/NarwalIntegration
   ```
4. Set the category to **Integration** and click **Add**.
5. Find **Narwal Flow Robot Vacuum** in the HACS store and click **Download**.
6. **Restart Home Assistant**.

### Manual Installation

1. Download or clone this repository.
2. Copy the `custom_components/narwal/` folder into your Home Assistant `config/custom_components/` directory.
3. **Restart Home Assistant**.

### Setup

1. Go to **Settings > Devices & Services > Add Integration**.
2. Search for **Narwal Flow Robot Vacuum**.
3. Enter your vacuum's **IP address** (find it in your router's DHCP table or the Narwal app).
4. The integration will connect, discover the device, and create all entities automatically.

> **Tip:** Assign a static IP to your vacuum in your router settings so the address doesn't change.

## Requirements

- Narwal robot vacuum on the same local network as Home Assistant
- The vacuum must be reachable on **port 9002** (no firewall blocking)
- Home Assistant **2025.1.0** or later
- Python **3.12** or later

## How It Works

This integration communicates with your Narwal vacuum over a local WebSocket connection on port 9002. The vacuum uses a binary protobuf-like protocol — the integration reverse-engineered this protocol to provide full local control without any cloud dependency.

When the robot is awake, it broadcasts status updates every ~1.5 seconds. The integration listens to these broadcasts and keeps HA entities up to date in real time. Commands (start, stop, pause, etc.) are sent over the same WebSocket connection with sub-second response times.

### Robot Sleep Behavior

The Narwal vacuum enters a low-power sleep mode when idle. During sleep, the WebSocket port stays open but the robot does not respond to commands or send broadcasts. The integration includes an automatic wake system that sends a sequence of wake commands derived from the official app's protocol. A keepalive heartbeat runs every 15 seconds to prevent the robot from going back to sleep.

If the robot is in deep sleep (e.g., after being idle for a long time), it may take up to 30 seconds to wake — or it may require opening the Narwal app once to wake it initially. Once awake, the keepalive system maintains responsiveness.

## Known Limitations

### Confirmed Working
- All vacuum controls (start, stop, pause, resume, return to dock, locate)
- Battery level, cleaning area/time, firmware version sensors
- Docked status detection (including fully-charged state)
- Map image rendering with room colors and dock position

### Partial / In Progress
- **Fan speed read-back** — You can set fan speed, and the integration tracks what you set. However, if you change fan speed via the Narwal app, the integration won't know. The robot protocol does not broadcast the current fan speed setting.
- **Map updates during cleaning** — The robot sends `display_map` broadcasts during cleaning; this is handled but needs more real-world testing.

### Not Yet Implemented
- **Camera / video streaming** — The robot's camera can be triggered locally (`developer/take_picture`), but images are AES-encrypted and the decryption key is stored on the phone app. Video streaming uses Agora (cloud-only) and cannot work locally.
- **Room-specific cleaning** — The protocol supports it, but the room selection payload format needs further decoding.
- **Cleaning history / statistics** — Not implemented.

### Known Issues
- **Wake from deep sleep** — The integration will retry automatically, but the first interaction after a long idle period may be delayed. Opening the Narwal app once can help "prime" the robot.
- **Single connection** — The Narwal vacuum only handles one WebSocket connection reliably. Close the Narwal app before using the HA integration to avoid interference.
- **Local network only** — This integration does not use cloud services. Your HA instance must be on the same network as the vacuum.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Cannot connect" during setup | Verify the vacuum's IP address and that port 9002 is not blocked. The robot must be powered on. |
| Entities show "Unavailable" | The robot may be asleep. Open the Narwal app briefly to wake it, then the integration will take over. |
| Map not showing | The map requires a successful `get_map` response. If the robot was asleep at startup, the map appears after it wakes. |
| Commands not responding | Ensure the Narwal app is closed — two simultaneous WebSocket connections cause issues. |
| Fan speed shows unknown | Set fan speed once from HA; it will track from that point. The robot doesn't broadcast this value. |
| Docked status wrong | The integration uses multiple signals to detect dock status. If you see issues, please report with debug logs. |

## Disclaimer

This is an **unofficial**, community-developed integration. It is not affiliated with, endorsed by, or supported by Narwal in any way. The local WebSocket protocol was reverse-engineered from publicly observable network traffic and the Narwal mobile application.

- **Use at your own risk.** This integration sends commands to your vacuum over the local network. While every effort has been made to ensure commands are safe and correct, there is no warranty.
- **No cloud dependency.** This integration does not connect to Narwal's cloud servers, does not transmit any data externally, and does not require an internet connection.
- **Firmware updates** from Narwal may change the local protocol at any time, potentially breaking this integration.

## Contributing

Contributions and testing are welcome! Please open an issue or pull request on [GitHub](https://github.com/sjmotew/NarwalIntegration).

If you have a Narwal model other than the Flow (AX12), testing reports are especially valuable.

## License

MIT
