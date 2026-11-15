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
card that draws the route itself can ask for the full trail once, when it needs it, with the
websocket command `narwal/trajectory`. It returns the same grid points the camera draws (up to
50,000), in the same coordinates as the attributes above.

```json
{"type": "narwal/trajectory"}
{"type": "narwal/trajectory", "entry_id": "<config entry id>"}
```

Without `entry_id` the first loaded Narwal entry answers. The result:

| Key | Shape | Meaning |
|---|---|---|
| `version` | `1` | Shape of this answer; raised on any incompatible change |
| `entry_id` | string | Config entry that answered |
| `map` | `{id, created_at, width, height, resolution_mm}` or `null` | The static map the points belong to; `null` before the first map arrives |
| `rotation`, `zoom` | `0`/`90`/`180`/`270`, `1.0`-`2.0` | The camera's view options, to place the points on its image |
| `signature` | `[a, b, c]` or `null` | Changes whenever the trail changes; `null` when there is none |
| `count` | integer | Number of points, breaks included |
| `x`, `y` | base64 | `count` little-endian float32 values each; a `NaN` pair marks a break in the route |

An unknown `entry_id`, or no loaded entry, returns the error `not_found`. The command only reads
what the integration already holds and sends nothing to the robot.
