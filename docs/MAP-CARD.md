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
`narwal/trajectory`. It returns the same grid points the camera draws, in the same coordinates as
the attributes above.

```json
{"type": "narwal/trajectory", "entity_id": "camera.<robot>_map"}
{"type": "narwal/trajectory", "entity_id": "camera.<robot>_map", "signature": [...]}
```

`entity_id` is any entity of the robot, usually the map camera the card already shows; the
config entry id is accepted as `entry_id` instead. Sending both is rejected with
`invalid_format`. Without either, the command answers only when exactly one Narwal entry is
configured (and enabled) and returns `target_required` otherwise, also while a second robot is
still retrying its setup. An `entity_id` or `entry_id` that is not a loaded Narwal entry, or a
robot that is configured but not loaded yet, returns `not_found`.

The trail shows where the robot went, so a user who is not an administrator needs read
permission on the robot's vacuum entity; without it the answer is `unauthorized`.

The result:

| Key | Shape | Meaning |
|---|---|---|
| `version` | `1` | Shape of this answer; raised on any incompatible change |
| `entry_id` | string | Config entry that answered |
| `map` | `{id, created_at, width, height, resolution_mm}` or `null` | The static map the points belong to; `null` before the first map arrives. On a map the camera draws no frame for (a width or height of `0`, which means the robot's reply decoded to no map, or no map image), the trail is empty |
| `rotation`, `zoom` | `0`/`90`/`180`/`270`, `1.0`-`2.0` | The camera's view options, to place the points on its image |
| `signature` | list of integers or `null` | Identifies these points, trail and map together; send it back unchanged. `null` when the answer carries no points |
| `unchanged` | boolean | `true` when the request's `signature` is the current one; `count`, `x` and `y` are then left out |
| `count` | integer | Number of points, breaks included. Not bounded by the trail's 50,000-point cap: the cap keeps every break, and a `NaN` pair is added where the route leaves the map (moves under a quarter grid cell are dropped). Size buffers on `count` |
| `x`, `y` | base64 | `count` little-endian float32 values each; a `NaN` pair marks a break in the route |

A card that polls sends the `signature` of the points it holds. While it is current, the answer
carries `unchanged: true` with `map`, `rotation` and `zoom`, and no points; nothing is decoded and
the points are not sent again. The signature covers the map the points are placed on, so after
a map change the answer carries the points again.

The command only reads what the integration already holds and sends nothing to the robot.
