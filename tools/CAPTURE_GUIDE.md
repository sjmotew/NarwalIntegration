# Capturing app → robot traffic for protocol RE

The Narwal local protocol on TCP/9002 is plaintext WebSocket (`ws://`,
not `wss://`). That means we don't need TLS interception — passive
sniffing is enough to read every byte the official Narwal mobile app
sends to the robot. This guide describes the simplest setup that
works on a Mac.

## Why we need this

`narwal_capture.py` records *broadcasts* (robot → world). Most Flow 2
protocol gaps left after the v1.0.0 reverse-engineering pass — single
room cleaning (#25), cleaning cycles (x1/x2/x3), a few clean-task
sub-fields — sit in the *opposite* direction (app → robot) and never
appear in broadcasts. The only way to learn that wire format is to
watch the app talk.

## The setup: Mac as Wi-Fi hotspot

Phone connects to a hotspot the Mac broadcasts; Mac is uplinked to the
real home network over Ethernet. All phone traffic passes through the
Mac, so Wireshark on the Mac sees the plaintext WebSocket frames the
app sends to the robot at `192.168.178.x:9002`.

```
   ┌─────────┐  ws://   ┌────────────┐  ws://   ┌────────┐
   │  Phone  │─────────▶│    Mac     │─────────▶│ Robot  │
   │ (Narwal │          │ (Wireshark │          │  9002  │
   │   app)  │          │   here)    │          └────────┘
   └─────────┘          └─────┬──────┘
                              │ ethernet
                              ▼
                       Fritzbox / home router
```

### One-time setup

1. **Install Wireshark** on the Mac:
   ```sh
   brew install --cask wireshark
   ```
   On first launch it asks to install the privileged helper for live
   capture — say yes.

2. **Enable Internet Sharing** on the Mac:
   System Settings → General → Sharing → Internet Sharing.
   Share your connection from **Ethernet** to **Wi-Fi**.
   Click Wi-Fi Options, set a network name + password.

3. **Connect the phone** to the Mac's Wi-Fi.
   Robot stays on the home Wi-Fi.

### Per session

1. Open Wireshark, start a capture on the **`bridge100`** interface
   (this is the hotspot bridge — won't exist until Internet Sharing
   has been on at least once).

2. Apply this display filter:
   ```
   tcp.port == 9002 && ip.addr == 192.168.178.138
   ```
   Replace `192.168.178.138` with the robot's IP.

3. **Important**: close Home Assistant's Narwal connection before
   starting the test (the robot only allows one WebSocket connection
   at a time). Easiest: temporarily disable the integration in
   Settings → Devices & Services, or stop the HA core.

4. Open the Narwal app on the phone, perform the action you want to
   capture (e.g. start a single-room clean on Toilet). The app's
   first frames after connecting will be the WebSocket handshake;
   then `clean/...` topic frames.

5. In Wireshark: right-click the first WebSocket packet of the
   session → Follow → TCP Stream. Save the stream as raw bytes.

## Decoding the captured frames

WebSocket framing is documented (RFC 6455). For our purposes each
text/binary frame holds a Narwal protocol message:

```
+------+---------------+------+---------------+-----------+---------+
| 0x01 | len(topic)+2  | 0x22 | len(topic)    | topic     | proto   |
| (1B) | (1B)          | (1B) | (1B, uint8)   | (UTF-8,   | bytes   |
|      | header byte   | tag  |               | no NUL)   |         |
+------+---------------+------+---------------+-----------+---------+
```

The topic is **not** NUL-terminated and the length is **not** a uint32 — an earlier
revision of this guide said both, and decoding a capture by hand from that produces
nonsense. Byte 2 is the protobuf tag: `0x22` (field 4) on requests and broadcasts,
`0x2a` (field 5) on command responses. Response frames carry an *empty* topic, so byte 1
is `2` and there is nothing to parse between the header and the payload — which also
means responses are not self-identifying and commands must be serialised.

Byte 1 must equal `len(topic) + 2` or the robot silently drops the connection.

(see `narwal_client/protocol.py` for the exact frame parser).

To decode the protobuf bytes, use the integration's own decoder:

```python
import blackboxprotobuf
from narwal_client.protocol import parse_frame

# `frame` = raw bytes copy-pasted out of Wireshark
msg = parse_frame(frame)
print("topic:", msg.full_topic)
decoded, typedef = blackboxprotobuf.decode_message(msg.payload)
print("payload:", decoded)
print("typedef:", typedef)
```

A short helper script lives at `tools/decode_frame.py` (TODO once we
need it).

## What to capture for each open question

| Question | Action to perform in the app | What to look for |
|---|---|---|
| #25 single-room | Tap one room on the map → Customization → Start | Topic for the start command, payload structure with the room ID |
| Cycle x1/x2/x3 | Customization → toggle cycle, Start | Differences between three otherwise-identical starts |
| Coverage Precision | Customization → toggle Standard/Meticulous, Start | Differences between two otherwise-identical starts |
| Tank states | Remove a water tank or empty cleaning solution while connected | Which broadcast field jumps from 1 to something else |

Save each capture (Wireshark → File → Save As, `.pcapng`) so we have
a record beyond the live session.

## Fallbacks if Mac hotspot isn't available

- **OpenWrt / managed switch with port mirroring**: tcpdump on the
  router (`tcpdump -i br-lan -w capture.pcap port 9002`) — no client
  reconfiguration. Cleanest if you have it.
- **`arpspoof` or `bettercap`**: trick the phone into routing through
  the Mac on the same Wi-Fi. Sketchier and easy to mess up; only do
  this if you understand the implications for the rest of your
  network.
- **Rooted phone with `tcpdump`**: capture directly on the device.
  Fine if the phone's already rooted, otherwise too much friction.
