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
