import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from scripts import convert_map
from scripts.png_utils import decode_gray_png, encode_png


class MapToolsTests(unittest.TestCase):
    def test_origin_translation_uses_existing_map_rotation(self):
        self.assertEqual(convert_map.shifted_origin([10, 20, 0], 2, 1), [12, 21, 0])
        result = convert_map.shifted_origin([10, 20, math.pi / 2], 2, 1)
        self.assertEqual(result[:2], [9, 22])
        self.assertEqual(result[2], math.pi / 2)

    def test_png_input_conversion_preserves_pixels_and_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            destination = source / "output"
            (source / "input.png").write_bytes(encode_png(2, 2, bytes([0, 254, 205, 254]), 1))
            metadata = {"image": "input.png", "resolution": 0.05, "origin": [1, 2, 0.3]}
            (source / "input.yaml").write_text(yaml.safe_dump(metadata))
            with patch.object(convert_map, "MAP_DIR", destination), patch.object(
                sys, "argv", ["convert_map", str(source / "input.yaml")]
            ):
                convert_map.main()
            self.assertEqual(decode_gray_png((destination / "map.png").read_bytes()),
                             (2, 2, bytes([0, 254, 205, 254])))
            expected = {**metadata, "image": "map.png"}
            self.assertEqual(yaml.safe_load((destination / "map.yaml").read_text()), expected)

    def test_bundled_patrol_points_are_inside_free_pixels_with_rotated_origin(self):
        directory = Path(__file__).resolve().parents[1] / "maps"
        metadata = yaml.safe_load((directory / "map.yaml").read_text())
        patrol = json.loads((directory / "patrol.json").read_text())
        width, height, pixels = decode_gray_png((directory / "map.png").read_bytes())
        ox, oy, yaw = metadata["origin"]
        cos, sin = math.cos(yaw), math.sin(yaw)
        for x, y in patrol["waypoints"]:
            dx, dy = x - ox, y - oy
            col = math.floor((dx * cos + dy * sin) / metadata["resolution"])
            row = height - 1 - math.floor((dy * cos - dx * sin) / metadata["resolution"])
            self.assertTrue(0 <= col < width and 0 <= row < height)
            self.assertGreater(pixels[row * width + col], 250)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is required for --pretty")
    def test_pretty_map_returns_image_and_finite_coordinate_transform(self):
        from scripts.map_vectorizer import prettify_map
        pixels = bytes(254 if 4 <= x < 16 and 4 <= y < 16 else 205
                       for y in range(20) for x in range(20))
        width, height, output, corner, yaw = prettify_map(20, 20, pixels)
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        self.assertEqual(len(output), width * height)
        self.assertTrue(all(math.isfinite(value) for value in (*corner, yaw)))
        self.assertTrue({0, 205, 254}.issuperset(set(output)))


if __name__ == "__main__":
    unittest.main()
