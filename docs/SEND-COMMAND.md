# Raw commands

Some robot settings have no entity, such as the Freo 20's "Hey Nawa" voice assistant or the
robot's timezone. Two actions reach them by sending one topic over the integration's own
connection:

| Action | Use it to | Reply |
|---|---|---|
| `vacuum.send_command` | Change a setting | Success, or an error if the robot refuses |
| `narwal.get_topic_response` | Read a setting | The robot's reply as hex, uninterpreted |

Both wake the robot first if it is asleep. They are low-level tools: you need the topic and,
for most writes, a protobuf request body. [PROTOCOL.md](PROTOCOL.md#9-topic-reference) lists
the known topics and field maps.

If a setting has an entity, change it through the entity. After a raw `config/set` of that
setting, the entity shows the old value until the next refresh.

## Refused topics

Topics the integration sends itself to start, stop or change a task are refused by both
actions. A raw copy would skip the integration's own bookkeeping, so entities would show stale
state. The error names what to use instead:

| Topic | Use instead |
|---|---|
| `clean/plan/start`, `clean/start_clean`, `clean/easy_clean/start` | `vacuum.start` or `narwal.clean_rooms` |
| `task/pause` | `vacuum.pause` |
| `task/resume` | `vacuum.start` |
| `task/force_end` | `vacuum.stop`, or turn off the dock task switch |
| `task/cancel` | `vacuum.stop` |
| `supply/recall` | `vacuum.return_to_base` |
| `supply/wash_mop`, `supply/wash_mop_by_robot_status` | Wash mop switch |
| `supply/dry_mop` | Dry mop switch |
| `supply/dry_dust_bag` | Dry and disinfect dust bin switch |
| `supply/dry_station_bag` | Dry and disinfect dock bag switch |
| `supply/dust_gathering` | Empty dustbin switch |
| `clean/set_fan_level` | `vacuum.set_fan_speed` |
| `clean/set_mop_humidity` | Mopping humidity select |
| `supply/ambient_light_ctrl` | Dock light |

Any other topic under `clean/` or `task/` is refused as well. The one exception is the read
`clean/current_clean_task/get`, which `narwal.get_topic_response` may send. Other topics,
such as `config/*`, `region/*`, `map/*` and `common/reboot`, are not restricted.

## Change a setting

```yaml
action: vacuum.send_command
target:
  entity_id: vacuum.narwal_freo_20_vacuum
data:
  command: config/set
  params:
    payload_hex: "a00200"   # SetConfig field 36 speechControlEnabled = false
```

| Field | Meaning |
|---|---|
| `command` | Short topic without product key and device ID: lowercase `[a-z0-9_]` segments separated by `/`, e.g. `config/set` |
| `params.payload_hex` | Request body as a quoted hex string. Empty by default |
| `params.timeout` | Seconds to wait for the reply, 1 to 10. Default 5 |

Field 1 of the reply is the result code. The action succeeds when the code is `SUCCESS` (1) or
`APPLIED` (6), or when the reply has no field 1 or is empty. Any other code (`NOT_APPLICABLE`,
`CONFLICT`, `NOT_READY`, ...) fails the action with its name, so an automation stops there and
the error shows in its trace. If field 1 is data instead of a number, the topic answered like a
read: the command was sent, but the action fails and points to `narwal.get_topic_response`,
which returns the reply.

## Read a setting

`narwal.get_topic_response` returns the reply to the caller. Store it with `response_variable`;
the result is keyed by entity id:

```yaml
- action: narwal.get_topic_response
  target:
    entity_id: vacuum.narwal_freo_20_vacuum
  data:
    topic: region/get
  response_variable: reply
- action: persistent_notification.create
  data:
    message: "{{ reply['vacuum.narwal_freo_20_vacuum'].response_hex }}"
```

| Field | Meaning |
|---|---|
| `topic` | Short topic, same format as `command` above |
| `payload_hex` | Request body as a quoted hex string. Most reads take none |
| `timeout` | Seconds to wait for the reply, 1 to 10. Default 5 |

The response holds `response_hex` (the whole reply) and `response_bytes` (its length). Field 1 of
a read reply can be data rather than a result code, so this action never judges the reply and
never fails because of it. A refusal comes back as bytes like any other reply: `0802` is field 1
= 2, `NOT_APPLICABLE`, which is what `map/get_map` returns from a sleeping robot. Decode the
reply before trusting it.

To try a read by hand, run the action from **Developer tools → Actions**; the response is shown
under the form. In an automation, the trace keeps it for each run.

The `response_variable` value is stored in every saved automation trace. When only the size
matters, use `response_bytes` rather than `response_hex`. Do not read maps (`map/get_map`)
from automations, because a whole map would be stored with each trace.

## Decoding a reply

Replies are protobuf without a schema. `protoc --decode_raw` shows the field numbers and values:

```console
$ echo 0802 | xxd -r -p | protoc --decode_raw
1: 2
```

Requests are encoded the same way. In `a00200`, `a0 02` is the tag for field 36 with a varint
value (36 × 8 + 0 = 288, varint-encoded) and `00` is the value, false.

## Tested settings

Freo 20, product key `fjhpiem4ba`, firmware v01.00.21.05:

| Setting | Topic | Notes |
|---|---|---|
| "Hey Nawa" wake word off | `config/set`, `a00200` | Stays off after a reboot |
| Timezone | `region/get`, `region/set` | `region/set` takes `{1: Region}`. With Europe/Rome and `UTC +01:00` the clock moved to Rome time and kept it across `common/reboot`. The robot rebuilds Region field 5 (the POSIX rule) from the offset string, so it most likely won't switch to summer time by itself; send `UTC +02:00` in summer |

## Limits

- **One command at a time.** Replies carry no topic and are matched to requests by send order
  ([#108](https://github.com/sjmotew/NarwalIntegration/issues/108)). A topic the robot never
  answers blocks other commands, polls included, until its timeout. Topics known never to be
  answered are refused up front, but a mistyped topic the robot ignores still causes this. That
  is why the timeout is capped at 10 s.
- **A late reply.** When a raw command times out, its place is released at once, so the next
  command's reply is not discarded. A reply that arrives after the timeout can instead be taken
  for the answer to the next command, if one is in flight.
- **Writes make other actions wait.** `vacuum.send_command` holds the same lock as start, stop,
  pause, return to dock, locate, `narwal.clean_rooms` and the dock tasks, so those wait until
  it finishes: up to 10 s to wake the robot plus the timeout. `narwal.get_topic_response` does
  not take or wait for that lock.
- **No undo.** Any user who can control the vacuum can send any topic that is not refused,
  including `common/reboot` and `common/shutdown`. Read a setting before changing it so you can put it
  back.
