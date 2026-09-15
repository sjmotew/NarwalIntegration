# Narwal Local WebSocket Protocol

Reverse-engineered notes on the local control protocol used by Narwal robot vacuums, as
implemented by this integration. Published so that other projects don't have to rediscover
it, and so that contributors have somewhere to send corrections.

**Status: partial and empirical.** Everything here was derived from live traffic against
real hardware and from decompiling the official Android app. Field names taken from the
app's compiled `BuilderInfo` are marked as such; everything else is inference from observed
bytes and should be treated as a working hypothesis. Several claims in this document's
history have been *reversed* after contradicting evidence — see
[Corrections](#corrections-things-this-project-got-wrong) at the end, which exists to make
that visible rather than quietly rewriting history.

Corrections and new captures are welcome. See [Contributing captures](#contributing-captures).

---

## 1. Transport

| Property | Value |
|---|---|
| Endpoint | `ws://<robot_ip>:9002` |
| Server | WebSocket++ 0.8.2 |
| Framing | Binary WebSocket frames |
| Authentication | **None** |
| Encryption | None — payloads are cleartext protobuf |
| Concurrency | **One connection per source IP** |

Two consequences worth stating plainly:

**There is no authentication.** Anything on the same network segment can command the robot —
start a clean, return it to the dock, reboot it, or read the floor plan of the house. That is
the robot's design, not this integration's. Treat port 9002 the way you'd treat any other
unauthenticated LAN device and segment your network accordingly.

**The robot accepts one connection per IP.** A second connection from the same address is
refused with `connection with same ip, close old one`. In practice this means you must disable
the Home Assistant config entry before running a diagnostic script from the same host.

The robot also disconnects on a malformed frame — specifically on a mismatched header byte
(see below) — and refuses reconnection for roughly 10 seconds afterwards.

### Not all models speak this protocol

Three transports exist across the product line, and which one a robot uses is a property of
the hardware, not a setting:

| Transport | Port | Models observed | Local control? |
|---|---|---|---|
| WebSocket | 9002 | Flow (AX12), Flow 2, Freo Z10 Pro/Turbo (AX26), Freo X10 Pro (AX15), Freo Z10 Ultra (CX4), Freo Z Ultra (hardware CX7, cloud identity J5) | Yes — this document |
| ZeroMQ (ZMTP 2.0) | 6789 | Freo X Ultra (AX18/AX19) | No — cloud-mediated |

`nmap -p 9002 <robot_ip>` is the fastest triage. Most supported models broadcast when awake,
but the CX7 is an exception: it only responds to topics containing product key `hEA7OEshlx`
and its real cloud-assigned device ID. See the README's compatibility table for the current
per-model state.

This identity is verified for one global CX7/J5 on firmware `v01.13.11.02`. Earlier reports in
issue #5 associated CX7 with `BYWBPqSxeC` and firmware `1.12.10.02`; that key did not answer
addressed probes on the verified robot, and remains an unresolved regional or platform variant.

---

## 2. Frame format

Every frame — in both directions — is a small binary header followed by a protobuf payload.

```
Byte 0     0x01                  Frame type constant
Byte 1     len(topic) + 2        Header length byte — MUST match or the robot disconnects
Byte 2     0x22 or 0x2a          0x22 = field 4 (request / broadcast)
                                 0x2a = field 5 (command response)
Byte 3     len(topic)            Topic length, uint8
Bytes 4..  topic                 UTF-8, no trailing NUL
Remainder  payload               Protobuf, may be empty
```

### The header byte

```python
header_byte = len(topic.encode("utf-8")) + 2
```

This is the total length of the protobuf field-4 TLV that wraps the topic: one tag byte, one
length byte, and the topic itself. Getting it wrong is the single most common way to have the
robot hang up on you, and it fails *silently* — the connection simply drops.

```python
def build_frame(topic: str, payload: bytes = b"") -> bytes:
    topic_bytes = topic.encode("utf-8")
    return bytes([0x01, len(topic_bytes) + 2, 0x22, len(topic_bytes)]) + topic_bytes + payload
```

### Response frames

Command responses come back as field 5 (`0x2a`) with an **empty topic**, so the header byte
is `2` and there is no topic to parse. The payload carries the result directly:

```
01 02 2a 00 08 01
│  │  │  │  └──┴── protobuf: field 1 = 1  (SUCCESS)
│  │  │  └──────── topic length = 0
│  │  └─────────── field 5 → this is a response
│  └────────────── header byte = 0 + 2
└───────────────── frame type
```

Because the topic is empty, responses are **not self-identifying**. You cannot tell which
command a response belongs to from the frame itself. Serialize your commands, or you will
mis-attribute results — this integration holds a lock across send-and-await for that reason.

---

## 3. Topics

Topics are MQTT-style paths, and the same names are used on the cloud MQTT transport and the
local WebSocket transport. That equivalence is one of the more useful facts in this document:
anything the app does over the cloud is, in principle, expressible locally on a WebSocket
model. It was established by [@northwestsupra](https://github.com/northwestsupra)'s combined
capture (§9).

Full form:

```
/<product_key>/<device_id>/<category>/<message_type>
```

```
/QoEsI5qYXO/71c53f01c14f49088338863e147bb53c/clean/start_clean
 └─ product key                              └─ short topic
    (model)   └─ device id (32 hex chars)
```

The product key identifies the model (`QoEsI5qYXO` = AX12 Narwal Flow); the device ID is
per-robot. Both are returned by `common/get_device_info`, but there's a bootstrap problem —
you need the device ID to build a topic, and you need a topic to ask for the device ID. This
integration resolves it by cycling known product-key prefixes during the wake burst and
reading the device ID out of the first broadcast topic the robot emits. The CX7 never
broadcasts and ignores incorrectly addressed requests, so its cloud-assigned device ID must
be supplied before connecting.

### Result codes

Command responses carry a result code in field 1:

| Code | Name | Meaning |
|---|---|---|
| 1 | `SUCCESS` | Accepted |
| 2 | `NOT_APPLICABLE` | Cannot do this now — wrong state, or unsupported on this firmware |
| 3 | `CONFLICT` | Already doing it (e.g. recall while recalling) |
| 4 | *(unconfirmed)* | Reported as "not ready" on some newer firmware; not yet reproduced |

**`SUCCESS` means "frame accepted", not "did what you meant".** This distinction cost this
project roughly two months (§10). Query commands return data in field 1 rather than a code,
so treat a non-integer field 1 as a successful data response.

---

## 4. Sleep, wake, and keepalive

Most robots sleep aggressively. While awake they broadcast status every ~1.5 s; while asleep
they send nothing and ignore most commands — including map requests, which is the usual reason
rooms fail to appear in a fresh install. The CX7 never broadcasts and instead relies on
addressed polling, including `status/get_device_base_status`. Clients must treat successful
field5 responses as reachability for such models rather than waiting for a broadcast, renewing
broadcast subscriptions, or reconnecting when broadcasts do not arrive.

Working model:

- **Awake** — broadcasts every ~1.5 s. No broadcast for 15 s means it has gone back to sleep.
- **Wake burst** — `common/notify_app_event` ("app opened"), then
  `common/active_robot_publish` with a topic-subscription payload. Sent as a burst because
  neither is reliable alone.
- **Keepalive** — repeat every ~15 s to hold it awake; `status/app_status_heartbeat` and
  `developer/ping` also exist for this.
- **Subscription** — `common/active_robot_publish` carries a duration (this integration uses
  600 s) after which broadcasts stop even if the socket is still open.

A live socket is therefore *not* evidence of a live data stream. Track broadcast age
separately from connection state.

---

## 5. Commands

Confirmed working on AX12 unless noted. Most take an **empty payload** — the topic is the
command.

### Common

| Topic | Effect |
|---|---|
| `common/yell` | Robot announces itself ("Robot is here") — used for `locate` |
| `common/get_device_info` | `{1: product_key, 2: device_id, 3: firmware}` |
| `common/get_feature_list` | 84 feature flags |
| `common/reboot` | Reboot |
| `common/shutdown` | Power off |

### Task control

| Topic | Effect |
|---|---|
| `task/pause` | Pause the current job |
| `task/resume` | Resume |
| `task/force_end` | Force stop |
| `task/cancel` | Cancel |

### Dock / supply

| Topic | Effect |
|---|---|
| `supply/recall` | Return to dock |
| `supply/wash_mop` | Wash mop pads |
| `supply/dry_mop` | Dry mop pads |
| `supply/dust_gathering` | Empty dust bin into the station |

### Cleaning

| Topic | Effect |
|---|---|
| `clean/start_clean` | **The real clean command.** Takes a `CleanTask` payload (§6) |
| `clean/plan/start` | Runs the plan **stored on the robot by the app**. Ignores your payload (§10) |
| `clean/easy_clean/start` | Quick clean |
| `clean/set_fan_level` | Suction — only applies while cleaning |
| `clean/set_mop_humidity` | Water level |
| `clean/current_clean_task/get` | Read back the task currently loaded — the key diagnostic tool (§11) |

### Map

| Topic | Response |
|---|---|
| `map/get_map` | ~80 KB, full map (§8) |
| `map/get_all_reduced_maps` | ~169 KB, every saved map |
| `map/get_editable_map` | Editor representation |

### Developer

| Topic | Effect |
|---|---|
| `developer/take_picture` | Single camera frame |
| `developer/get_robot_debug_image` | Cleartext carpet/planning debug PNGs |
| `developer/led_control` | Toggle the robot's LED |
| `developer/ping` | Ping/pong |

---

## 6. The clean command

`clean/start_clean` takes a `CleanTask`:

```
CleanTask {
  1: map_id                     // from get_map field 2.1 — NOT always 1
  2: [ CleanItem {              // repeated, one per zone
       1: ZoneOption { 1: type, 2: zone_id }   // type 1 = room, 2 = free rectangle
       2: CleanParam { ... }                   // mode, fan, water, mop strength, passes
       3: order                                // execution order, 1-based
     } ]
  3: TaskOption {}              // empty in every capture, app included
  5: task_type
}
```

Notes that cost real debugging time:

- **`map_id` must be the robot's actual map id**, read from `get_map`. Hardcoding `1` happens
  to work on single-map households and is destructive elsewhere — a mismatched id has been
  observed to abort the job after ~1 s and clear the map selection in the Narwal app.
- **`clean/start_clean` works from the dock.** Sending it from an undocked STANDBY state has
  produced refusals on some firmware.
- **`ZoneOption` field 4 is not required.** It was reported as required on firmware
  v01.08.03.07; a capture of the app's own task on v01.09.05.01 shows the app omitting it.
  See [Corrections](#corrections-things-this-project-got-wrong).
- **`TaskOption` is empty** in every capture examined, including the app's.

### Verified against the app's own bytes

The strongest evidence available for this schema is a diff of our builder against a task
captured from the official app with matching settings, on the same firmware:

```
ours : 0a1c080112140a0408011003120a0804100418012003380318011a002804
app  : 0a1e080112160a0408011003120c08041004180120033803400218011a002804
                                                    ^^^^  CleanParam tag 8 = 2
```

Structurally identical. One field differs: `CleanParam` tag 8 = 2, which the app sends and we
never do. **Tag 8 is the coverage-precision setting** — 1 = *Standard*, 2 = *Meticulous* —
settled by a controlled capture pair on AX26 in which only that toggle moved and only the
byte `40 01` → `40 02` changed ([#70](https://github.com/sjmotew/NarwalIntegration/issues/70),
[#25](https://github.com/sjmotew/NarwalIntegration/issues/25)). The captured app task was
simply set to *Meticulous*.

---

## 7. Status broadcasts

### `status/robot_base_status` — primary state

Field names from the decompiled `BuilderInfo`; values live-validated where noted.

| Field | Meaning |
|---|---|
| 1 | `errorCode` (repeated; empty when healthy) |
| 2 | Battery %, **float32** |
| 3 | Nested task status — see below |
| 13 | Bound account UUID (string) |
| 20 | Dust box state (enum) |
| 21 | Dust bag state (enum) — *absent on AX12 v01.08.03.07* |
| 23 | Clean-water tank state (enum) |
| 24 | Sewage tank state (enum) |
| 25 | Device status code list |
| 26 | Active fan level |
| 29 | Active mop humidity |
| 35 | Station bag health %, float32 — *absent on AX12 v01.08.03.07* |
| 36 | Station bag health reset time (Unix seconds) — **unverified** |
| 38 | **Disputed** — `100` on every observation. Read as battery *design capacity* in one place and as curing-agent consumption % in another; see §11 |
| 39 | Station bag state (enum) |
| 41 | Detergent remaining % (`heavyDetergentRemainPercent`) — **unverified**, `100` on every observation; see §11 |
| 47 | Charging status (enum) |

Fields 20/21/35/36/38/39/41 are the consumables group — see [Consumables and station wear](#consumables-and-station-wear).

Field 3 sub-fields:

| Sub-field | Meaning |
|---|---|
| 3.1 | Working status enum (below) |
| 3.2 | `1` = paused (overlays the cleaning state) |
| 3.7 | `1` = returning to dock |
| 3.10 | Dock sub-state (1 = docked, 2 = docking) |
| 3.12 | Dock activity (2, 6 observed) |

**`WorkingStatus` values are empirical and deliberately do not match the app's compiled
`TaskType` enum**, whose numbering the field nominally uses. Trust live observation here:

| Value | Meaning |
|---|---|
| 1 | `STANDBY` — idle; also a transition state |
| 2 | `DOCKED_V2` — on dock (v01.07.23.00+) |
| 4 | `CLEANING` |
| 5 | `CLEANING_ALT` — observed while the robot was physically stuck |
| 7 | `REMAPPING` — exploring/rebuilding; camera active |
| 10 | `DOCKED` |
| 14 | `CHARGED` — reported before 100%; use battery level for charge state |
| 19 | `TASK_COMPLETED` — transitional, returning to base |

Values 2, 17 and 19 were each discovered when someone's log flooded with an unmapped value.
**No error value has ever been observed** — a robot fault cannot currently be represented.
If you can capture one (pick the robot up mid-clean), that's a genuinely useful contribution.

### `status/working_status` — metrics, not state

Names from the decompiled proto. This message reports *progress*, and reading state from it
is a mistake this project has made in both directions:

| Field | Meaning |
|---|---|
| 1 | `workingProgress`, float32 (0..1) |
| 2 | `coveredArea`, float32 — m² cleaned this session |
| 3 | `timeConsuming` — session elapsed seconds |
| 4 | `remainedTime` — seconds |
| 6 | `cleaningZoneId` — the room currently being cleaned |
| 8..17 | Station drying/sterilization/dust-bag timers. The cumulative counters (9/11/13/15/17) stay constant while idle — field 13 is `totalDryStationBagTime` (18000 = 5 h), once misread as cleaning area because 18000/10000 resembled a plausible 1.8 m² |

### `map/display_map` — live position

Sent every ~1.5 s while cleaning. Field 2 is a moving window rather than the
complete route: a live Freo X Ultra capture repeatedly carried 30 points, with
the next observed window sharing four points with the previous one. Consumers
must join exact overlapping coordinates if they want to retain the whole route.

| Field | Meaning |
|---|---|
| 1.1.1 / 1.1.2 | Robot X / Y — float32, **decimetres** |
| 1.2 | Heading — float32 radians, [-π, π] |
| 2 | Rolling trajectory window `{1: x_bytes, 2: y_bytes}` |
| 5 | Dock reference position (constant) |
| 7 | Cleaned-area overlay: `{1: width, 2: height, 3: zlib grid}` — *not* the house map |
| 10 | Timestamp, **milliseconds** since epoch |
| 12 | Active room list |

A `display_map` with position `(0.00, 0.00)` and `ts=0` means the robot has lost map context —
it is the signature of a job about to abort, not a robot at the origin.

### Other broadcasts

| Topic | Content |
|---|---|
| `upgrade/upgrade_status` | 2 status, 3 progress, 4 stage, 7 current firmware, 8 target firmware |
| `status/download_status` | OTA download state |
| `status/time_line_status` | Session timeline |
| `report/clean_report` | End-of-session summary: 3 elapsed, 6 start, 7 end, 12 detail, 33 end-reason string (localized) |
| `developer/planning_debug_info` | Bumper/cliff/mop-collision sensor debug |

A robot mid-OTA refuses commands it would normally accept. If `upgrade_status` and
`download_status` are chattering, `NOT_APPLICABLE` may mean "busy updating" rather than
anything about your payload.

### Consumables and station wear

Two sources, and they answer different questions.

**`consumable/get_consumable_info`** (queried, never broadcast) returns which parts want
attention — not how worn they are:

```
{1: ConsumableInfoPayload{
     1: maintainItems[],   // clean / check these
     2: replaceItems[]     // replace these
}}
```

Both lists are **packed repeated varints**. That matters more than it sounds: a decoder that
treats a repeated field as "an int, or a list of ints" gets neither — blackboxprotobuf hands
back a `str` whose code points are the values. A live capture from an AX12:

```
{'1': {'1': '\x04\x06\x08\n', '2': '\x03\x14'}}
   maintainItems = [4, 6, 8, 10]   wash ribs, universal wheel, side distance sensor, anti-winding brush
   replaceItems  = [3, 20]         side brush, station bag
```

An empty payload means nothing needs attention. **Do not confuse "parsed to nothing" with
"nothing to report"** — this project shipped exactly that bug, silently reporting healthy
consumables on a robot asking for six parts (§10).

`ConsumableMaintainItem`: 1 dust box, 2 dust filter, 4 wash ribs, 6 universal wheel,
7 cliff sensor, 8 side distance sensor, 9 water-tank sponge, 10 anti-winding brush,
11 smart-module sponge, 20 dust container.

`ConsumableReplaceItem`: 1 dust filter, 2 mop, 3 side brush, 4 clear-water filter,
5 roller brush, 6 detergent, 7 smart-module filter, 8 dust bag, 20 station bag,
21 silver ions, 22 curing agent, 23 heavy detergent, 24 inner dust box.

**Per-consumable remaining life % is believed cloud-only** — no local topic has produced it.
That claim is inherited rather than tested, and is listed in §11.

**`status/robot_base_status`** carries the numeric/station side: fields 20, 21, 23, 24, 35,
36, 38, 39, 41. Which of them a robot sends varies by model and firmware, and absence is
normal rather than an error — an AX12 on v01.08.03.07 omits 21 and 35 entirely, so a client
that assumes a dust-bag health score exists will render a permanently empty gauge. Report
absent fields as unknown; do not substitute zero.

---

## 8. Map data

`map/get_map` returns the full map under field 2.

| Field | Meaning |
|---|---|
| 2.1 | **Map id** — the value `CleanTask.map_id` needs |
| 2.3 | Resolution |
| 2.4 / 2.5 | Grid width / height |
| 2.6 | Coordinate transform: `6.1` = origin_y, `6.3` = origin_x |
| 2.8 | **Dock position** `{1: {1: x, 2: y}, 2: heading}` — decimetres |
| 2.11 | Door segments — room pair + two endpoints, 23 observed |
| 2.12 | **Rooms** — see below |
| 2.13 | Historical trajectory `{1: x, 2: y}` |
| 2.17 | Entire map, gzip-compressed (~20 KB) |
| 2.26 | Room boundary polygons, one per room |
| 2.32 | Furniture/annotations — type 14 doors, 2 furniture, 28 obstacles. Map-editor placed, **not** live detection |
| 2.33 | Map area |
| 2.34 | Creation timestamp |

Use field 2.8 for the dock, not field 48 — field 48 is position history and is unreliable
for this.

### Rooms (field 2.12)

| Sub-field | Meaning |
|---|---|
| 1 | `room_id` — the value `ZoneOption.zone_id` takes |
| 2 | `RoomType` enum (0–15) |
| 3 | User-assigned name (UTF-8, empty if never named) |
| 4 | Category — 1 = room, 2 = utility/small space |
| 8 | Instance index, 1-based ("Bathroom 2") |

When field 3 is empty, the app derives the name from the `RoomType` enum. That mapping is
**model-independent** — the app's `MapEnginei18nConfiger.roomTypei18nKey(int)` takes only the
enum, no product key — and resolves through one shared `en-US.json`:

| 0 Room | 1 Master bedroom | 2 Secondary bedroom | 3 Living room |
|---|---|---|---|
| **4** Kitchen | **5** Bathroom | **6** Toilet | **7** Balcony |
| **8** Dining room | **9** Closet | **10** Corridor | **11** Study |
| **12** Kids' room | **13** Entertainment room | **14** Storage room | **15** Others |

### Coordinate transform

```
pixel = (value_dm * 10) / (resolution / 10) - origin
```

`display_map` reports decimetres; `get_map`'s dock field is decimetres; some other map fields
are centimetres. Mixing them silently puts the robot in the wrong room on a rendered map.

---

## 9. Topic reference

Combined MQTT + WebSocket topic list contributed by
[@northwestsupra](https://github.com/northwestsupra) (issues #4, #5), merged from two
independent capture efforts. Direction is C→R (client to robot) or R→C.

Not all of these have been exercised locally. Rows this integration uses are marked ✓; the
rest are known-to-exist and unexplored — good starting points for anyone probing.

| # | Path | Dir | Category | Used |
|---|---|---|---|---|
| 01 | `/common/yell` | C→R | Common | ✓ |
| 02 | `/common/yell/response` | R→C | Common | ✓ |
| 03 | `/common/reboot` | C→R | Common | ✓ |
| 04 | `/common/shutdown` | C→R | Common | ✓ |
| 05 | `/common/get_device_info` | C→R | Common | ✓ |
| 06 | `/common/get_device_info/response` | R→C | Common | ✓ |
| 07 | `/common/get_feature_list` | C→R | Common | ✓ |
| 08 | `/common/active_robot_publish` | C→R | Wake | ✓ |
| 09 | `/common/active_robot_publish/response` | R→C | Wake | ✓ |
| 10 | `/common/notify_app_event` | C→R | Wake | ✓ |
| 11 | `/task/pause` | C→R | Task | ✓ |
| 12 | `/task/pause/response` | R→C | Task | ✓ |
| 13 | `/task/resume` | C→R | Task | ✓ |
| 14 | `/task/resume/response` | R→C | Task | ✓ |
| 15 | `/task/force_end` | C→R | Task | ✓ |
| 16 | `/task/force_end/response` | R→C | Task | ✓ |
| 17 | `/task/cancel` | C→R | Task | ✓ |
| 18 | `/task/cancel/response` | R→C | Task | ✓ |
| 19 | `/supply/recall` | C→R | Dock | ✓ |
| 20 | `/supply/recall/response` | R→C | Dock | ✓ |
| 21 | `/supply/wash_mop` | C→R | Dock | ✓ |
| 22 | `/supply/wash_mop/response` | R→C | Dock | ✓ |
| 23 | `/supply/dry_mop` | C→R | Dock | ✓ |
| 24 | `/supply/dry_mop/response` | R→C | Dock | ✓ |
| 25 | `/supply/dust_gathering` | C→R | Dock | ✓ |
| 26 | `/supply/dust_gathering/response` | R→C | Dock | ✓ |
| 27 | `/clean/start_clean` | C→R | Cleaning | ✓ |
| 28 | `/clean/start_clean/response` | R→C | Cleaning | ✓ |
| 29 | `/clean/plan/start` | C→R | Cleaning | ✓ |
| 30 | `/clean/plan/start/response` | R→C | Cleaning | ✓ |
| 31 | `/clean/easy_clean/start` | C→R | Cleaning | ✓ |
| 32 | `/clean/easy_clean/start/response` | R→C | Cleaning | ✓ |
| 33 | `/clean/set_fan_level` | C→R | Cleaning | ✓ |
| 34 | `/clean/set_fan_level/response` | R→C | Cleaning | ✓ |
| 35 | `/clean/set_mop_humidity` | C→R | Cleaning | ✓ |
| 36 | `/clean/set_mop_humidity/response` | R→C | Cleaning | ✓ |
| 37 | `/clean/current_clean_task/get` | C→R | Cleaning | ✓ |
| 38 | `/clean/current_clean_task/get/response` | R→C | Cleaning | ✓ |
| 39 | `/config/get` | C→R | Config | |
| 40 | `/config/get/response` | R→C | Config | |
| 41 | `/config/set` | C→R | Config | |
| 42 | `/config/set/response` | R→C | Config | |
| 43 | `/config/volume/set` | C→R | Config | |
| 44 | `/config/volume/set/response` | R→C | Config | |
| 45 | `/consumable/get_consumable_info` | C→R | Consumable | |
| 46 | `/consumable/get_consumable_info/response` | R→C | Consumable | |
| 47 | `/consumable/reset_consumable_info` | C→R | Consumable | |
| 48 | `/consumable/reset_consumable_info/response` | R→C | Consumable | |
| 49 | `/schedule/clean_schedule/get` | C→R | Schedule | |
| 50 | `/schedule/clean_schedule/get/response` | R→C | Schedule | |
| 51 | `/map/get_map` | C→R | Map | ✓ |
| 52 | `/map/get_map/response` | R→C | Map | ✓ |
| 53 | `/map/get_all_reduced_maps` | C→R | Map | ✓ |
| 54 | `/map/get_all_reduced_maps/response` | R→C | Map | ✓ |
| 55 | `/map/get_editable_map` | C→R | Map | |
| 56 | `/map/get_editable_map/response` | R→C | Map | |
| 57 | `/map/display_map` | R→C | Map | ✓ |
| 58 | `/map/display_map/response` | R→C | Map | |
| 59 | `/status/working_status` | R→C | Status | ✓ |
| 60 | `/status/robot_base_status` | R→C | Status | ✓ |
| 61 | `/status/upgrade_status` | R→C | Status | |
| 62 | `/upgrade/upgrade_status` | R→C | Status | ✓ |
| 63 | `/status/download_status` | R→C | Status | ✓ |
| 64 | `/status/time_line_status` | R→C | Status | ✓ |
| 65 | `/status/app_status_heartbeat` | R→C | Status | ✓ |
| 67 | `/report/clean_report` | C→R | Report | |
| 68 | `/clean/report` | C→R | Report | |
| 69 | `/developer/ping` | C→R | Developer | ✓ |
| 70 | `/developer/take_picture` | C→R | Developer | ✓ |
| 71 | `/developer/take_picture/response` | R→C | Developer | ✓ |
| 72 | `/developer/led_control` | C→R | Developer | ✓ |
| 73 | `/developer/planning_debug_info` | R→C | Developer | |
| 74 | `/info/get_clean_time_line` | C→R | Status | |
| 75 | `/info/get_clean_time_line/response` | R→C | Status | |

Also observed but not in the table above: `/status/get_device_base_status` (C→R, full status
dump on demand) and `/developer/get_robot_debug_image` (C→R, cleartext carpet/planning PNGs).

### Answered locally, previously unexplored

Probed read-only against a CX7 on fw `v01.13.11.02`. All returned data. Sizes are the raw
response frame.

| Topic | Size | Content |
|---|---|---|
| `/developer/get_robot_info` | ~830 B | Labelled sections: device/product id, network (**including the Wi-Fi SSID and PSK in cleartext**, over the unauthenticated socket), per-component firmware, and a battery block: level, real level, health %, charge cycles, charge-time-remaining, current, voltage, temperature |
| `/common/upgrade/get_firmware_version` | ~350 B | Per-module firmware: robot mcu/ble/cpu, base-station mcu/ble/cpu, vision ai/cpu, plus sensor modules (`0220_SS_LD02`, `0220_SS_CH01`) |
| `/config/get` | ~175 B | Settings block. Field 2.1 = `70` (volume-shaped), 2.4.2/2.4.3 = `79200`/`28800` — a 22:00→08:00 do-not-disturb window in seconds — 2.3 = timezone, plus ~8 unmapped flags |
| `/region/get` | ~75 B | Timezone, UTC offset, country, city, and the robot's own wall clock |
| `/config/language/get_current_voice_info` | ~35 B | Language code, voice-pack version and pack id |
| `/info/get_clean_time_line` | ~330 B | Session timeline |
| `/consumable/get_consumable_info` | 6 B | Empty payload when nothing needs attention — the §10 trap, and a useful fixture for it |

### Silent on CX7

Requested and never answered, idle and mid-clean. Worth recording so nobody re-probes them
hoping to substitute for the missing broadcasts:

`status/working_status` · `map/display_map` · `status/point_navi_plan_traj` ·
`info/get_clean_progress_info` · `robot/status/get` · `robot/task/status/get` ·
`info/battery_info` · `schedule/clean_schedule/get` · `config/key_mapping/get` ·
`operate/conf/get` · `voice/get_voice_list` · all 27 `clean_system/*` topics

### Confirmed cloud-only

These exist as topics but do not serve data locally:

| Topic | Behaviour |
|---|---|
| `get_vision_image` | Returns `NOT_APPLICABLE` — object-detection images are cloud-processed |
| `get_dynamic_map` | Times out, no response |
| Shortcuts / scene presets | Cloud-managed via Alibaba Alink IoT REST, no local WS equivalent |

---

## 10. Corrections: things this project got wrong

Published deliberately. Every entry below was stated confidently, acted on, and later
contradicted by evidence — if you are building against this protocol, these are the traps.

### `clean/plan/start` is a plan-runner, not a clean command

From 2025 until July 2026 this project believed `clean/plan/start` was the room-clean command
and that `clean/start_clean` was wrong. It is the other way round. **`clean/plan/start`
discards your payload and runs the plan the Narwal app last saved on the robot** — and it
returns `SUCCESS` while doing so.

Four separate payload-schema "fixes" were shipped against that topic. Each one returned
`code=1` and changed nothing, because the bytes were never read. Users reported the wrong room
cleaning; we kept re-encoding the room list.

Three contributors independently reached the correct answer — [@jgus](https://github.com/jgus),
[@Sean-StarLabs](https://github.com/Sean-StarLabs) and [@sytchi](https://github.com/sytchi) —
on different hardware, before it was accepted.

**Lesson: when a command ACKs successfully but has no effect, verify the topic contract before
touching the payload.** A `SUCCESS` response is not evidence that the robot read your bytes.

### `ZoneOption` field 4 was never required

Reported as mandatory on firmware v01.08.03.07, and believed for three days, which delayed a
merge. A capture of the app's own clean task showed the app omitting field 4 entirely — and a
firmware that genuinely required it would break the official app. Treat "field X is required"
as a hypothesis until a capture of the app's traffic confirms it.

### The room-name table was wrong for everyone

A user reported that room labels differed on Flow 2. A per-model override map was added to fix
"the Flow 2 case". The real cause was that the shared `RoomType` table was misaligned from
index 5 for *every* model, and the override entrenched the error. Decompiling
`roomTypei18nKey(int)` showed it takes no model argument at all.

**Lesson: a device-specific symptom usually means the shared table is wrong for everyone.**

### Enum values discovered one bug report at a time

`WorkingStatus` values 2, 17 and 19 were each found when a user's log flooded with an unmapped
value — every discovery shipped after someone hit it. `ERROR` remains a placeholder. Ask
contributors holding an APK decode for the *complete* enum rather than mapping values
reactively.

### A parse failure that looked exactly like good news

`consumable/get_consumable_info` returns its `maintainItems` / `replaceItems` lists as packed
repeated varints, which blackboxprotobuf surfaces as a `str`. The parser accepted "an int, or a
list of ints", so `int('\x04\x06\x08\n')` raised, the exception was swallowed per-item, and both
lists came back empty. Empty means "nothing needs attention" — so a robot asking for six parts
was reported as perfectly healthy, for as long as the feature had existed.

Nothing looked broken from the outside, which is the point: the failure mode of a `try/except
continue` inside a decoder is a confident wrong answer. Two habits fall out of it —
**distinguish "parsed to nothing" from "reported nothing"**, and **check a decoder against a
device in a known-bad state**, since anything that returns empty looks correct on a healthy one.

---

## 11. Open questions

Concrete, and each one is answerable with a capture rather than an argument:

**How many suction levels exist — answered.** The app 2.7.92 proto carries five (`MUTE`,
`NORMAL`, `STRONG`, `DEEP`, `SUPER`, values 1–5). Its English resources label those values
Quiet, Standard, Strong, Super Powerful and Ultra Powerful. AX26 captures show its highest
available tier sending tag 2 = **4** (`DEEP`), with the tier below it sending 3 (`STRONG`), so
`SUPER` (5) is unreachable from that model's app UI
([#70](https://github.com/sjmotew/NarwalIntegration/issues/70)). A Flow 2 accepted a room-clean
payload containing tag 2 = **5** for every room and reported the same value from
`clean/current_clean_task` while cleaning, confirming that `SUPER` is a real device-supported
tier. The app's ordinary picker still presents values 1–4 and adds value 5 through a separate
optional `enableSuperLevel` path. The live `clean/set_fan_level` enum (`SweepFanLevel`) has no
`SUPER`; the app maps 5 down to `STRONG` there. The integration instead clamps a live level-5
request to `DEEP` (4), the highest tier that endpoint can represent, while retaining level 5
as the pending setting for the next task. Home Assistant select entities also advertise the
previous `Ultra` spelling beside `Ultra Powerful`: the select service validates options before
entity-level alias normalization, so keeping that option is required for existing automations.

**The error state.** No `WorkingStatus` value for a fault has ever been observed. Until one is,
a robot error cannot be represented at all.

**Whether pass count is one field or two.** The app splits it by mode (vacuum passes, mop
passes); we expose one control.

**Consumables — several, tracked together in
[#79](https://github.com/sjmotew/NarwalIntegration/issues/79).**

- *What is `base_status` field 38?* It reads `100` on every observation, and this project
  describes it as battery design capacity in one place and curing-agent consumption % in
  another. Both survive the data; one is wrong.
- *Is field 41 really detergent remaining?* The name `heavyDetergentRemainPercent` comes from
  the decompiled app. It has only ever been observed as `100`, on a robot whose battery also
  reads `100`. A capture taken side by side with a visibly low cartridge settles it.
- *Are per-consumable life percentages truly cloud-only?* The app shows them; no local topic
  has produced them. The claim is inherited, not tested.
- *Which models send fields 21 and 35?* Absent on AX12 v01.08.03.07. One data point is not a
  rule, and clients currently render a permanently empty dust-bag gauge because of it.
- *Are the tank/box/bag enum thresholds right?* We treat `1` as OK and `>= 2` as attention by
  inference from enum ordering. [#77](https://github.com/sjmotew/NarwalIntegration/issues/77)
  suggests at least one model disagrees.

**Unexplored topics.** `/schedule/clean_schedule/get`, `/config/get`, `/config/volume/set` and
`/info/get_clean_time_line` are all known to exist and have never been probed locally.
`/consumable/get_consumable_info` **has** now been probed — see §7.

---

## 12. Contributing captures

The highest-value contribution is a capture of what the **official app** sends, because it
settles arguments that reasoning cannot. The method needs no packet capture and was worked out
by [@ken99999](https://github.com/ken99999):

1. **Disable the Home Assistant config entry.** The robot allows one connection per IP and will
   close yours otherwise.
2. Set the option you're testing **in the Narwal app** and start a clean from the app.
3. Call `clean/current_clean_task/get` from a local script and record the raw response.
4. Change exactly one setting, repeat, and diff.

Post **raw hex**, not your decoding. Field names in this protocol have been misread more than
once, including in this document's history — the bytes survive a bad interpretation, a summary
doesn't.

Also valuable:

- **`nmap -p 9002 <robot_ip>`** on any model not in the README's compatibility table
- **A robot fault captured live** — pick the robot up mid-clean and record `robot_base_status`
- **A complete enum decode** from the APK, rather than individual values

Corrections to this document are welcome as issues or PRs. If something here contradicts what
your hardware does, your hardware is right.
