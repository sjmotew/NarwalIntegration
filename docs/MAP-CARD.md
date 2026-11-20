# Map card attributes

The map camera describes each rendered frame in its attributes, so a dashboard card can draw
its own map or place markers on the camera image. All positions are map grid coordinates
(x to the right, y up), the same units the robot reports.

| Attribute | Shape | Meaning |
|---|---|---|
| `vacuum_position` | `{x, y, a}` | Robot position; `a` is the heading in degrees (0 = +x, 90 = +y) |
| `charger` | `{x, y}` | Dock position |
| `path` | `[[[x, y], …], …]` | Trail of the current clean as segments, decimated to at most 400 points with the newest kept dense |
| `calibration_points` | `[{vacuum: {x, y}, map: {x, y}}, …]` | Three grid points and their pixels in the camera image, after rotation and zoom |

`calibration_points` follows the format map cards such as
[lovelace-xiaomi-vacuum-map-card](https://github.com/PiotrMachowski/lovelace-xiaomi-vacuum-map-card)
read from a camera. These attributes change with every frame and are excluded from the recorder.

## Full trail over the websocket API

`path` is a decimated copy because attributes go to every connected frontend on every frame. A
card that draws the route itself can ask for the full trail with the websocket command
`narwal/trajectory`. It returns the trail as grid points converted the way the camera converts
them, in the same coordinates as the attributes above. The camera caps a frame at 50,000 points,
breaks included; this command converts every retained point.

```json
{"type": "narwal/trajectory", "entity_id": "camera.<robot>_map"}
{"type": "narwal/trajectory", "entity_id": "camera.<robot>_map", "signature": [...]}
```

`entity_id` is any entity of the robot, usually the map camera the card already shows; the
config entry id is accepted as `entry_id` instead. Sending both is rejected with
`invalid_format`. Without either, the command answers for the one Narwal entry that is configured
(and enabled). With several it returns `target_required`, also while a second robot is still
retrying its setup; with none it returns `not_found`. An `entity_id` or `entry_id` that is not a
loaded Narwal entry, or a robot that is configured but not loaded yet, returns `not_found` too.

The trail shows where the robot went, so a user who is not an administrator needs read
permission on the robot's vacuum entity; without it the answer is `unauthorized`. As in the
services, permission is checked first: such a user also needs read permission on the
`entity_id` sent, and gets `unauthorized` rather than `not_found` for an `entry_id` that is not
loaded or for no target without a loaded robot, so the answer does not tell which ids belong to a
robot.

The result:

| Key | Shape | Meaning |
|---|---|---|
| `version` | `1` | Shape of this answer; raised on any incompatible change |
| `entry_id` | string | Config entry that answered |
| `map` | `{id, created_at, width, height, resolution_mm}` or `null` | The static map the points belong to; `null` before the first map arrives. On a map the camera draws no frame for (a width or height of `0`, which means the robot's reply decoded to no map, or no map image), the trail is empty |
| `rotation`, `zoom` | `0`/`90`/`180`/`270`, `1.0`-`2.0` | The camera's view options, to place the points on its image |
| `signature` | list of integers or `null` | Identifies these points, trail and map together; send it back unchanged, `null` included. `null` when there is no trail or the map is not drawable; a trail that lies entirely off the map has a signature and `count` `0` |
| `unchanged` | boolean | `true` when the request's `signature` is the current one; `count`, `x` and `y` are then left out |
| `count` | integer | Number of points, breaks included. Every retained point is converted, none is decimated, so `count` can exceed the trail's 50,000-point cap: each break and each place where the route leaves the map adds a `NaN` pair (moves under a quarter grid cell are dropped). Size buffers on `count` |
| `x`, `y` | base64 | `count` little-endian float32 values each; a `NaN` pair marks a break in the route |

A card that polls sends the `signature` of the points it holds. While it is current, the answer
carries `unchanged: true` with `map`, `rotation` and `zoom`, and no points; nothing is decoded and
the points are not sent again. The signature covers the map fields the points depend on (id,
creation time, origin and size), so after a map change, including a map that grew and kept its
id, the answer carries the points again.

The command only reads what the integration already holds and sends nothing to the robot.

## Active map over the websocket API

The integration follows a floor switched in the Narwal app on its own, but apart from its own
start and a clean started from Home Assistant only when its poll asks the robot: every five
minutes, while the robot is on no task, and not at all on a model that does not broadcast (see
[More than one floor](ROOM-CLEANING.md#more-than-one-floor),
[#43](https://github.com/sjmotew/NarwalIntegration/issues/43)). A card that shows more than one
floor, or is about to act on one, can ask the robot which map is active now with the websocket
command `narwal/active_map`:

```json
{"type": "narwal/active_map", "entity_id": "vacuum.<robot>"}
```

`entity_id` is any entity of the robot; the config entry id is accepted as `entry_id` instead.
Sending both is rejected with `invalid_format`. Without either, the command answers for the one
Narwal entry that is configured (and enabled). With several it returns `target_required`, also
while a second robot is still retrying its setup; with none it returns `not_found`. An `entity_id`
or `entry_id` that is not a loaded Narwal entry, or a robot that is configured but not loaded yet,
returns `not_found` too.

Asking can wake the robot and replaces the map the entities show, so a user who is not an
administrator needs control permission on the robot's vacuum entity, the same check the
services make; without it the answer is `unauthorized`. As in the services, permission is
checked first: such a user also needs read permission on the `entity_id` sent, and gets
`unauthorized` rather than `not_found` for an `entry_id` that is not loaded or for no target
without a loaded robot.

The result:

| Key | Shape | Meaning |
|---|---|---|
| `version` | `1` | Shape of this answer; raised on any incompatible change |
| `entry_id` | string | Config entry that answered |
| `held` | boolean | `false`: the robot was asked. `true`: a task is running, and this is the map the integration holds |
| `map` | `{id, created_at, width, height}` or `null` | The active map; `null` only when `held` and no map is held yet |
| `rooms` | `[{id, name}, ...]` | Its rooms (id > 0), with the names the integration shows |

While a task runs (cleaning, paused, returning, remapping, or a finished task while the robot is
not back on the dock), the robot is not asked and no lock is taken: the map is fixed for the
duration of a task, and the answer is the held map with `held: true`. The same answer is given
when a clean begins in the app while the robot is being woken or asked, whether or not the
robot still answers. A model that does not broadcast, such as the Freo Z Ultra, does not report
reliably that a clean is running. The integration cannot tell there, so the command asks such a
robot whenever its status shows no task, which can be during a clean.

While the robot is docked or idle, the command takes the same lock the dock actions and a clean
start take and sends the same `get_map` a clean start sends. A robot off the dock that has gone
quiet is woken first; a docked robot is not, since it answers without a wake burst and stops
broadcasting again within a minute of each one. The lock is held while the robot is asked, for at
most 15 s on the dock and 25 s off it when it has to be woken. That limit includes the wait for
a command that is already on its way to the robot, so a request can run into it before its own
`get_map` went out. A
request that arrives during a clean start therefore waits for it; if that start began a task,
the answer is the held map with `held: true` and the robot is not asked. Concurrent requests
share one call, with each other and with the poll, and a fetched map is answered again for 30 s,
whoever asked for it, unless the integration fetched the map some other way in between. If the
robot or its connection fails (waking, `get_map`, a timeout), the
error is `map_failed`, and for the next 30 s further requests get the same error without asking
the robot again, so a card polling a robot that does not answer cannot keep the lock busy; a
robot that failed asleep and has woken since is asked again at once. A failed check of the
poll counts the same, with one difference: the poll never wakes the robot, so when it failed at
a quiet robot off the dock, a request still gets its own attempt, with the wake. A request that
shares such a check waits for it first, so the robot can be asked for 40 s in all before the
request is answered, and any wait for the lock comes on top. The newest
outcome decides: a failure is reported although an earlier success is younger than 30 s. A
request that is waiting when the entry is unloaded, or arrives while it is, gets `map_failed`
too. Any other error is a bug: it is
logged with its traceback and answered as `unknown_error`, and it starts the same 30 s pause,
during which requests get `map_failed`.

Every fetch is a `get_map` to the robot, so call the command when a card opens, when it shows
another floor, or before acting on a floor, rather than on a short timer.

The fetched map replaces the one the integration holds, and the entities are updated at once,
so the camera and the room list `narwal.clean_rooms` resolves against follow the active floor,
also on a robot that sends the same creation time for every floor. An entity that fails while
being told is logged and does not fail the request. The map and the trail are switched together
or not at all, so a request that fails leaves the floor the integration held.
A switch to another floor does what it does when the poll finds it: the previous floor's trail
and the last robot position leave the camera, and the switch neither writes nor reads the
stored trail: a save that was already queued for the last clean still goes out, and the stored
trail comes back at a Home Assistant start on its floor, as before, not when the robot returns
to that floor. The *segments changed* repair is raised if your rooms are mapped to areas on
one floor. Do not confirm it for a floor you only looked at, or the area mapping of your main
floor is replaced.
