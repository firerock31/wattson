import io
import json
import unittest
from unittest import mock

from PIL import Image

import report


class TripMapBoundsTests(unittest.TestCase):
    def setUp(self):
        tile = io.BytesIO()
        Image.new("RGB", (256, 256), (11, 13, 16)).save(tile, "PNG")
        self.tile = tile.getvalue()

    def render(self, points):
        requests = []

        def cached_tile(z, x, y):
            requests.append((z, x, y))
            if len(requests) > 32:
                raise RuntimeError("Map exceeded the bounded tile budget")
            return self.tile

        trip = {"path": json.dumps(points), "miles": 100.0,
                "kwh_used": 30.0, "start_label": "Start",
                "end_label": "End"}
        with mock.patch("report._tile_cache_get", side_effect=cached_tile), \
                mock.patch("urllib.request.urlopen",
                           side_effect=AssertionError("Unexpected network request")):
            parts = report._trip_map_parts([trip])
        self.assertIsNotNone(parts)
        jpeg, svg, width, height = parts
        self.assertLessEqual(width, report._MAP_W + 2)
        self.assertLessEqual(height, report._MAP_H + 2)
        self.assertIsNotNone(jpeg)
        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.size, (width, height))
        self.assertIn("<polyline", svg)
        self.assertLessEqual(len(requests), 32)
        return requests

    def test_long_east_west_route_zooms_out(self):
        requests = self.render([[40.0, -120.0], [40.0, -75.0]])
        self.assertLess(requests[0][0], 10)

    def test_long_north_south_route_zooms_out(self):
        requests = self.render([[-50.0, 0.0], [70.0, 0.0]])
        self.assertLess(requests[0][0], 10)

    def test_distant_gps_outlier_keeps_map_bounded(self):
        self.render([[40.0, -75.0], [0.0, 0.0], [40.01, -74.99]])

    def test_antimeridian_span_keeps_map_bounded(self):
        self.render([[10.0, 179.0], [10.0, -179.0]])

    def test_short_route_keeps_street_level_zoom(self):
        requests = self.render([[40.0, -75.0], [40.001, -74.999]])
        self.assertGreaterEqual(requests[0][0], 10)

    def test_missing_paths_still_have_no_map(self):
        with mock.patch("report._tile_cache_get") as cache:
            self.assertIsNone(report._trip_map_parts([{"path": "[]"}]))
        cache.assert_not_called()


if __name__ == "__main__":
    unittest.main()
