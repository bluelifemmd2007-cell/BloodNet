import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_test_database = tempfile.TemporaryDirectory()
_previous_db_path = os.environ.get("DB_PATH")
os.environ["DB_PATH"] = str(Path(_test_database.name) / "routing.db")
from app import api
if _previous_db_path is None:
    os.environ.pop("DB_PATH", None)
else:
    os.environ["DB_PATH"] = _previous_db_path


class DonorCenterRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_rounded_route_tie_using_area_matrix_order(self):
        donor = api.domain.donors["D09811"]
        candidates = api.domain.donor_center_candidates(donor)
        route_times = dict(zip(candidates, (48.5, 48.5, 108.2)))
        coordinates = {
            (float(center["lat"]), float(center["lon"])): center_id
            for center_id, center in api.domain.centers.items()
        }

        async def travel_minutes(_, destination):
            return route_times[coordinates[destination]]

        with patch.object(api.services, "travel_minutes", new=AsyncMock(side_effect=travel_minutes)) as travel:
            resolved = await api._resolve_donor_center("D09811", "MC1")

        self.assertEqual(resolved, "MC5")
        self.assertEqual(travel.await_count, 3)

    async def test_incomplete_travel_routes_keep_area_matrix_fallback(self):
        with patch.object(api.services, "travel_minutes", new=AsyncMock(return_value=None)) as travel:
            resolved = await api._resolve_donor_center("D00001", "MC5")

        self.assertEqual(resolved, "MC5")
        self.assertEqual(travel.await_count, 3)


if __name__ == "__main__":
    unittest.main()
