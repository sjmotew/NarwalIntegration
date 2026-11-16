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

Field 1 of the reply is the result code. `SUCCESS` (1), `APPLIED` (6) and a reply without field 1
count as accepted; any other code (`NOT_APPLICABLE`, `CONFLICT`, `NOT_READY`, ...) fails the action with its name, so an
automation stops there and the error shows in its trace.

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
  answers keeps its place for the timeout plus 10 s, and other commands, polls included, time
  out behind it. Topics known never to be answered are refused up front, but a mistyped topic
  the robot ignores still causes this. That is why the timeout is capped at 10 s.
- **Other actions wait.** A raw command holds the same lock as start, stop, pause, return to
  dock, locate, `narwal.clean_rooms` and the dock tasks, so those wait until it finishes: up to 10 s to wake the robot plus
  the timeout.
- **No undo.** Any user who can control the vacuum can send any topic, including
  `common/reboot` and `common/shutdown`. Read a setting before changing it so you can put it
  back.
