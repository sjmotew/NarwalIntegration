"""Tests for narwal_client.map_renderer — render_base_map and render_overlay.

Covers MAP-01 (map rendering pipeline) validation gaps:
  - render_base_map returns valid PIL Image with rooms and dock
  - render_base_map handles empty/missing grid data gracefully
  - render_overlay returns valid PNG bytes with trail and robot
"""

from __future__ import annotations

import io
import zlib

from narwal_client.map_renderer import (
    MAP_RENDER_SCALE,
    OBSTACLE_COLOR_DEFAULT,
    OBSTACLE_COLORS,
    _scaled_coord,
    _transform_point,
    calibration_points,
    render_base_map,
    render_map_png,
    render_overlay,
)
from narwal_client.models import ObstacleInfo


def _make_compressed_grid(width: int, height: int, fill_value: int = 0) -> bytes:
    """Create a compressed map grid with all pixels set to fill_value.

    Builds a protobuf-style packed varint field (field 1, wire type 2)
    containing width*height varint-encoded pixel values.
    """
    # Encode each pixel as a varint
    raw_varints = bytearray()
    for _ in range(width * height):
        val = fill_value
        while val > 0x7F:
            raw_varints.append((val & 0x7F) | 0x80)
            val >>= 7
        raw_varints.append(val & 0x7F)

    # Wrap in protobuf field 1 length-delimited header
    length = len(raw_varints)
    length_varint = bytearray()
    v = length
    while v > 0x7F:
        length_varint.append((v & 0x7F) | 0x80)
        v >>= 7
    length_varint.append(v & 0x7F)

    data = bytes([0x0A]) + bytes(length_varint) + bytes(raw_varints)
    return zlib.compress(data)


def _make_room_grid(width: int, height: int, room_id: int = 1) -> bytes:
    """Create a compressed grid where all pixels belong to a specific room.

    Pixel value encoding: room_id << 8 | pixel_type.
    pixel_type 0x00 = floor (no wall flag).
    """
    pixel_value = (room_id << 8) | 0x00
    return _make_compressed_grid(width, height, fill_value=pixel_value)


class TestRenderBaseMap:
    """Tests for render_base_map() — static floor plan rendering."""

    def test_returns_pil_image_with_rooms(self) -> None:
        """Given valid MapData with rooms and grid data, returns a PIL Image."""
        from PIL import Image

        width, height = 20, 20
        compressed = _make_room_grid(width, height, room_id=1)

        result = render_base_map(
            compressed, width, height,
            room_names={1: "Kitchen"},
        )

        assert result is not None
        assert isinstance(result, Image.Image)
        assert result.size == (width * MAP_RENDER_SCALE, height * MAP_RENDER_SCALE)
        assert result.mode == "RGBA"
        assert result.getpixel((0, 0))[3] == 255

    def test_scaled_coordinate_is_clamped_to_image_edge(self) -> None:
        """Fractional coordinates in the last grid cell remain visible."""
        assert _scaled_coord(29.9, MAP_RENDER_SCALE, 90) == 89

    def test_with_dock_position(self) -> None:
        """Given MapData with dock_x/dock_y, render_base_map includes dock."""
        from PIL import Image

        width, height = 30, 30
        compressed = _make_room_grid(width, height, room_id=2)

        result = render_base_map(
            compressed, width, height,
            dock_x=15.0, dock_y=15.0,
        )

        assert result is not None
        assert isinstance(result, Image.Image)
        # The dock is drawn as a white circle — check that the center pixel
        # at the dock position (Y-flipped) is white or near-white
        dock_px_y = height - 1 - 15  # Y-flip
        dock_px = int(round(15 * MAP_RENDER_SCALE + (MAP_RENDER_SCALE / 2)))
        dock_py = int(round(dock_px_y * MAP_RENDER_SCALE + (MAP_RENDER_SCALE / 2)))
        region = [
            result.getpixel((x, y))[:3]
            for x in range(dock_px - 5, dock_px + 6)
            for y in range(dock_py - 5, dock_py + 6)
        ]
        assert any(r > 200 and g > 200 and b > 200 for r, g, b in region)

    def test_empty_compressed_data(self) -> None:
        """Given empty compressed data, returns None gracefully."""
        result = render_base_map(b"", 100, 100)
        assert result is None

    def test_zero_dimensions(self) -> None:
        """Given zero width/height, returns None."""
        compressed = _make_room_grid(10, 10)
        assert render_base_map(compressed, 0, 100) is None
        assert render_base_map(compressed, 100, 0) is None

    def test_no_room_names(self) -> None:
        """render_base_map works without room_names (no labels drawn)."""
        from PIL import Image

        width, height = 15, 15
        compressed = _make_room_grid(width, height, room_id=3)

        result = render_base_map(compressed, width, height)
        assert result is not None
        assert isinstance(result, Image.Image)


class TestRenderOverlay:
    """Tests for render_overlay() — robot + trail on cached base map."""

    def _make_base_image(self, width: int = 30, height: int = 30):
        """Create a simple base PIL Image for overlay tests."""
        from PIL import Image
        return Image.new("RGB", (width, height), (100, 100, 100))

    def test_returns_png_bytes(self) -> None:
        """render_overlay returns valid PNG bytes."""
        base = self._make_base_image()
        result = render_overlay(
            base, height=30,
            robot_x=15.0, robot_y=15.0,
            robot_heading=90.0,
        )

        assert isinstance(result, bytes)
        assert len(result) > 0
        # Verify it's a valid PNG (starts with PNG signature)
        assert result[:8] == b"\x89PNG\r\n\x1a\n"

    def test_with_trail(self) -> None:
        """render_overlay draws native trajectory positions as line segments."""
        base = self._make_base_image(width=50, height=50)
        trail = [(10.0, 10.0), (20.0, 20.0), (30.0, 30.0)]

        result = render_overlay(
            base, height=50,
            robot_x=30.0, robot_y=30.0,
            trail=trail,
        )

        assert isinstance(result, bytes)
        assert result[:8] == b"\x89PNG\r\n\x1a\n"

    def test_no_robot_position(self) -> None:
        """render_overlay works with no robot position (trail only or empty)."""
        base = self._make_base_image()
        result = render_overlay(base, height=30)

        assert isinstance(result, bytes)
        assert result[:8] == b"\x89PNG\r\n\x1a\n"

    def test_unsupported_rotation_uses_unrotated_transform(self) -> None:
        """Bitmap and overlay points share the same fallback rotation."""
        assert _transform_point(5, 10, 30, 20, 45, 1.0) == (5, 10)

    def test_does_not_modify_base(self) -> None:
        """render_overlay does not mutate the base image."""
        base = self._make_base_image()
        # Save original pixel for comparison
        original_pixel = base.getpixel((15, 15))

        render_overlay(
            base, height=30,
            robot_x=15.0, robot_y=15.0,
        )

        assert base.getpixel((15, 15)) == original_pixel

    def test_full_pipeline_base_then_overlay(self) -> None:
        """End-to-end: render_base_map then render_overlay produces valid PNG."""
        width, height = 40, 40
        compressed = _make_room_grid(width, height, room_id=1)

        base = render_base_map(
            compressed, width, height,
            dock_x=20.0, dock_y=20.0,
            room_names={1: "Living Room"},
        )
        assert base is not None

        trail = [(18.0, 18.0), (22.0, 22.0), (25.0, 20.0)]
        png = render_overlay(
            base, height=height,
            robot_x=25.0, robot_y=20.0,
            robot_heading=45.0,
            trail=trail,
        )

        assert isinstance(png, bytes)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        # Verify we can open the PNG
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        assert img.size == (width * MAP_RENDER_SCALE, height * MAP_RENDER_SCALE)


class TestRenderMapPng:
    """Tests for the one-shot map rendering path."""

    def test_room_pixels_are_opaque_rgba(self) -> None:
        """Room colors match the RGBA image mode."""
        from PIL import Image

        width, height = 10, 10
        compressed = _make_room_grid(width, height, room_id=1)

        png = render_map_png(zlib.decompress(compressed), width, height)
        image = Image.open(io.BytesIO(png))

        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 255


class TestObstacleRendering:
    """Tests for obstacle rendering on base map."""

    def test_render_base_map_with_obstacles(self) -> None:
        """render_base_map with obstacles draws rectangles at correct grid positions."""
        from PIL import Image

        width, height = 50, 50
        compressed = _make_room_grid(width, height, room_id=1)
        obstacles = [
            ObstacleInfo(id=1, type_id=14, center_x=5.0, center_y=5.0, width=6.0, height=4.0),
        ]
        # origin (0,0) so grid coords = center coords
        result = render_base_map(
            compressed, width, height,
            obstacles=obstacles, origin_x=0, origin_y=0,
        )
        assert result is not None
        assert isinstance(result, Image.Image)
        assert result.size == (width * MAP_RENDER_SCALE, height * MAP_RENDER_SCALE)

    def test_obstacle_type_colors_exist(self) -> None:
        """OBSTACLE_COLORS dict has entries for all furniture enum types."""
        assert 2 in OBSTACLE_COLORS   # double bed
        assert 4 in OBSTACLE_COLORS   # dining table
        assert 14 in OBSTACLE_COLORS  # sofa
        assert 28 in OBSTACLE_COLORS  # toilet
        assert 33 in OBSTACLE_COLORS  # washbasin
        assert isinstance(OBSTACLE_COLOR_DEFAULT, tuple)
        assert len(OBSTACLE_COLOR_DEFAULT) == 3

    def test_obstacle_colors_are_distinct(self) -> None:
        """Different obstacle categories have distinct colors."""
        assert OBSTACLE_COLORS[2] != OBSTACLE_COLORS[14]   # bed != sofa
        assert OBSTACLE_COLORS[14] != OBSTACLE_COLORS[28]  # sofa != toilet
        assert OBSTACLE_COLORS[28] != OBSTACLE_COLORS[2]   # toilet != bed

    def test_empty_obstacles_same_as_no_obstacles(self) -> None:
        """render_base_map with empty obstacles list produces same output as without."""
        width, height = 20, 20
        compressed = _make_room_grid(width, height, room_id=1)

        result_none = render_base_map(compressed, width, height, obstacles=None)
        result_empty = render_base_map(compressed, width, height, obstacles=[])

        assert result_none is not None
        assert result_empty is not None
        # Both should produce identical images
        assert list(result_none.getdata()) == list(result_empty.getdata())

    def test_out_of_bounds_obstacles_skipped(self) -> None:
        """Obstacles with out-of-bounds coordinates are skipped (no crash)."""
        from PIL import Image

        width, height = 20, 20
        compressed = _make_room_grid(width, height, room_id=1)
        obstacles = [
            ObstacleInfo(id=1, type_id=14, center_x=500.0, center_y=500.0, width=6.0, height=4.0),
            ObstacleInfo(id=2, type_id=28, center_x=-100.0, center_y=-100.0, width=6.0, height=4.0),
        ]

        result = render_base_map(
            compressed, width, height,
            obstacles=obstacles, origin_x=0, origin_y=0,
        )
        assert result is not None
        assert isinstance(result, Image.Image)

    def test_obstacle_modifies_image(self) -> None:
        """An in-bounds obstacle should change some pixels compared to no-obstacle render."""
        width, height = 40, 40
        compressed = _make_room_grid(width, height, room_id=1)

        result_without = render_base_map(compressed, width, height)
        result_with = render_base_map(
            compressed, width, height,
            obstacles=[
                ObstacleInfo(
                    id=1,
                    type_id=2,
                    center_x=20.0,
                    center_y=20.0,
                    width=10.0,
                    height=10.0,
                )
            ],
            origin_x=0, origin_y=0,
        )

        assert result_without is not None
        assert result_with is not None
        # Images should differ (obstacle drawn on one but not other)
        assert list(result_without.getdata()) != list(result_with.getdata())


class TestCalibrationPoints:
    """calibration_points() must describe where render_overlay really draws."""

    @staticmethod
    def _affine(points, grid_x: float, grid_y: float) -> tuple[float, float]:
        """Map a grid point to image pixels the way a map card does."""
        origin, right, up = (point["map"] for point in points)
        width = points[1]["vacuum"]["x"]
        height = points[2]["vacuum"]["y"]
        return (
            origin["x"] + (right["x"] - origin["x"]) * grid_x / width
            + (up["x"] - origin["x"]) * grid_y / height,
            origin["y"] + (right["y"] - origin["y"]) * grid_x / width
            + (up["y"] - origin["y"]) * grid_y / height,
        )

    @staticmethod
    def _drawn_robot_centre(png: bytes, base) -> tuple[float, float]:
        """Return the centre of the pixels render_overlay changed."""
        from PIL import Image, ImageChops

        drawn = Image.open(io.BytesIO(png)).convert("RGB")
        if drawn.size != base.size:
            base = base.resize(drawn.size)
        box = ImageChops.difference(drawn, base.convert("RGB")).getbbox()
        assert box is not None, "robot was not drawn"
        return ((box[0] + box[2] - 1) / 2, (box[1] + box[3] - 1) / 2)

    def test_identity_view(self) -> None:
        """Unrotated, unzoomed: grid origin sits in the bottom-left cell centre."""
        points = calibration_points(40, 60 * MAP_RENDER_SCALE, 40 * MAP_RENDER_SCALE)

        assert [point["vacuum"] for point in points] == [
            {"x": 0.0, "y": 0.0},
            {"x": 60.0, "y": 0.0},
            {"x": 0.0, "y": 40.0},
        ]
        assert points[0]["map"] == {"x": 1.5, "y": 118.5}
        assert points[1]["map"] == {"x": 181.5, "y": 118.5}
        assert points[2]["map"] == {"x": 1.5, "y": -1.5}

    def test_matches_drawn_robot_for_every_view(self) -> None:
        """For each rotation and zoom the calibrated point is the drawn robot."""
        from PIL import Image

        width, height = 60, 40
        grid_x, grid_y = 22.0, 14.0
        for rotation in (0, 90, 180, 270):
            for zoom in (1.0, 1.5):
                base = Image.new(
                    "RGB",
                    (width * MAP_RENDER_SCALE, height * MAP_RENDER_SCALE),
                    (100, 100, 100),
                )
                png = render_overlay(
                    base, height=height, robot_x=grid_x, robot_y=grid_y,
                    robot_heading=None, rotation_degrees=rotation, zoom=zoom,
                )
                rotated = base.rotate(-rotation, expand=True) if rotation else base
                if zoom > 1.0:
                    crop_w = int(rotated.width / zoom)
                    crop_h = int(rotated.height / zoom)
                    left = (rotated.width - crop_w) // 2
                    top = (rotated.height - crop_h) // 2
                    rotated = rotated.crop((left, top, left + crop_w, top + crop_h))
                drawn = self._drawn_robot_centre(png, rotated)
                expected = self._affine(
                    calibration_points(height, base.width, base.height, rotation, zoom),
                    grid_x,
                    grid_y,
                )
                assert abs(drawn[0] - expected[0]) <= 1.5, (rotation, zoom, drawn, expected)
                assert abs(drawn[1] - expected[1]) <= 1.5, (rotation, zoom, drawn, expected)

    def test_empty_map_has_no_division_by_zero(self) -> None:
        """A zero-height map still returns finite points."""
        points = calibration_points(0, 0, 0)

        assert len(points) == 3
