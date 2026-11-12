import json
import unittest

import httpx

from app.integrations import LocalServiceUnavailable, LocalServices


class LocalServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = {
            "GATEWAY_URL": "http://gateway.test",
            "INVENTORY_URL": "http://inventory.test",
            "TRAVEL_URL": "http://travel.test",
        }

    async def test_gateway_transient_retry_reuses_same_payload(self):
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(503, headers={"Retry-After": "0.1"}, json={"error": "busy"})
            return httpx.Response(202, json={"accepted": True})

        services = LocalServices(self.settings)
        services.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            payload = {
                "notification_id": "N-stable",
                "donor_id": "D00001",
                "template": "request_wave",
                "request_id": "R-test",
                "callback_url": "http://team.test",
            }
            status, _, _ = await services.message(payload)
        finally:
            await services.close()

        self.assertEqual(status, 202)
        self.assertEqual(requests, [payload, payload])

    async def test_inventory_outage_is_not_reported_as_empty_stock(self):
        services = LocalServices(self.settings)
        services.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503, json={"error": "down"}))
        )
        try:
            with self.assertRaises(LocalServiceUnavailable):
                await services.inventory()
        finally:
            await services.close()


if __name__ == "__main__":
    unittest.main()
