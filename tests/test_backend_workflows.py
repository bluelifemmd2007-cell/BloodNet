import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.database import Database
from app.domain import BloodNetDomain


class DatabaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "workflow.db"
        self.database = Database(str(self.database_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_request(self, request_id="R-test", units=1, status="open"):
        return {
            "id": request_id,
            "hospital_id": "H01",
            "blood_group": "O-",
            "product": "red_cells",
            "units": units,
            "urgency": "critical",
            "status": status,
            "created_sim": 0.0,
            "deadline_sim": 90.0,
            "request_data": {},
        }

    def test_same_idempotency_key_creates_one_request_concurrently(self):
        def create(index):
            request = self.make_request(f"R-{index}")
            return self.database.create_idempotent_request(request, "shared-key", "same-body")

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(create, range(12)))

        created = [result for result in results if result[0] == "created"]
        self.assertEqual(len(created), 1)
        self.assertEqual({result[1] for result in results}, {created[0][1]})

    def test_existing_donations_are_backfilled_as_delivered(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE requests (
                    id TEXT PRIMARY KEY, hospital_id TEXT NOT NULL, blood_group TEXT NOT NULL,
                    product TEXT NOT NULL, units INTEGER NOT NULL, urgency TEXT NOT NULL,
                    status TEXT NOT NULL, note TEXT, approved INTEGER NOT NULL DEFAULT 0,
                    created_sim REAL NOT NULL, updated_sim REAL NOT NULL, request_data TEXT
                );
                INSERT INTO requests VALUES (
                    'R-legacy', 'H01', 'O-', 'red_cells', 1, 'critical', 'fulfilled',
                    NULL, 0, 0, 5, '{}'
                );
                CREATE TABLE donor_invitations (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL, donor_id TEXT NOT NULL,
                    notification_id TEXT NOT NULL, template TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued', created_sim REAL NOT NULL,
                    response TEXT, progress_state TEXT NOT NULL, last_progress_sim REAL,
                    UNIQUE(request_id, donor_id, notification_id)
                );
                INSERT INTO donor_invitations VALUES (
                    'I-legacy', 'R-legacy', 'D00001', 'N-legacy', 'request_wave',
                    'accepted', 1, 'accepted', 'donated', 5
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

        migrated = Database(str(legacy_path))
        summary = migrated.fetch_request_summary("R-legacy")
        self.assertEqual(summary["donated"], 1)
        self.assertEqual(summary["fulfilled"], 1)

    def test_duplicate_and_reordered_donor_callbacks_converge(self):
        self.database.create_idempotent_request(self.make_request(), "key", "body")
        self.database.insert_invitation(
            "I-test",
            "R-test",
            "D00001",
            "N-test",
            "request_wave",
            0.0,
            center_id="MC1",
            center_hospital_minutes=30.0,
            processing_minutes=20.0,
        )
        events = [
            ("E-route", "en_route", 2.0),
            ("E-arrive", "arrived", 3.0),
            ("E-screen", "screen_pass", 4.0),
            ("E-donate", "donated", 5.0),
        ]
        for event_id, event_name, sim_time in events:
            inserted, _, _ = self.database.apply_donor_progress(
                {
                    "event_id": event_id,
                    "request_id": "R-test",
                    "donor_id": "D00001",
                    "event": event_name,
                    "sim_time": sim_time,
                },
                sim_time,
            )
            self.assertTrue(inserted)

        response = {
            "event_id": "E-accept",
            "request_id": "R-test",
            "donor_id": "D00001",
            "notification_id": "N-test",
            "response": "accepted",
            "sim_time": 1.0,
        }
        self.database.apply_donor_response(response, 1.0)
        self.database.apply_donor_response(response, 1.0)

        summary = self.database.fetch_request_summary("R-test")
        self.assertEqual(summary["confirmed"], 1)
        self.assertEqual(summary["en_route"], 1)
        self.assertEqual(summary["arrived"], 1)
        self.assertEqual(summary["donated"], 1)
        self.assertEqual(summary["donations_inflight"], 1)
        self.assertEqual(summary["fulfilled"], 0)
        self.assertEqual(summary["status"], "mobilizing")
        self.assertEqual(self.database.mark_donor_supplies_delivered(54.0), [])
        self.assertEqual(self.database.mark_donor_supplies_delivered(55.0), ["R-test"])
        self.assertEqual(self.database.fetch_request_summary("R-test")["fulfilled"], 1)
        self.assertEqual(self.database.fetch_request_summary("R-test")["status"], "fulfilled")

    def test_simulator_response_without_notification_id_matches_invitation(self):
        self.database.create_idempotent_request(self.make_request(), "key", "body")
        self.database.insert_invitation("I-test", "R-test", "D00001", "N-test", "request_wave", 0.0)
        result = self.database.apply_donor_response(
            {
                "event_id": "E-accept",
                "request_id": "R-test",
                "donor_id": "D00001",
                "notification_id": None,
                "response": "accepted",
                "sim_time": 1.0,
            },
            1.0,
        )
        self.assertTrue(result[0])
        self.assertEqual(result[1]["notification_id"], "N-test")

    def test_confirmation_claim_is_stable_and_only_sent_once(self):
        self.database.create_idempotent_request(self.make_request(), "key", "body")
        self.database.insert_invitation("I-test", "R-test", "D00001", "N-test", "request_wave", 0.0)
        self.database.apply_donor_response(
            {
                "event_id": "E-accept",
                "request_id": "R-test",
                "donor_id": "D00001",
                "notification_id": "N-test",
                "response": "accepted",
                "sim_time": 1.0,
            },
            1.0,
        )
        notification_id = self.database.claim_confirmation("R-test", "D00001")
        self.assertTrue(notification_id.startswith("NT-"))
        self.assertIsNone(self.database.claim_confirmation("R-test", "D00001"))

    def test_deadline_expires_only_due_live_request(self):
        self.database.create_idempotent_request(self.make_request(), "key", "body")
        self.database.create_idempotent_request(self.make_request("R-later"), "key-2", "body-2")
        self.assertEqual(self.database.get_request("R-test")["deadline_sim"], 90.0)
        self.database.update_request("R-test", {"deadline_sim": 20.0})
        self.database.update_request("R-later", {"deadline_sim": 40.0})

        self.assertEqual(self.database.expire_due_requests(25.0), ["R-test"])
        self.assertEqual(self.database.get_request("R-test")["status"], "expired")
        self.assertEqual(self.database.get_request("R-test")["expired_sim"], 25.0)
        self.assertEqual(self.database.get_request("R-later")["status"], "open")

    def test_stock_dispatch_moves_from_transit_to_fulfilled_at_eta(self):
        request = self.make_request(units=2)
        self.database.create_idempotent_request(request, "key", "body")
        self.database.insert_dispatch("D-test", "R-test", "MC1", "O-", "red_cells", 2, 20.0, 1.0)
        self.database.update_dispatch("D-test", "in_transit", 20.0, 1.0)
        self.assertEqual(self.database.fetch_request_summary("R-test")["stock_inflight"], 2)

        self.assertEqual(self.database.mark_dispatches_delivered(20.0), ["R-test"])
        summary = self.database.fetch_request_summary("R-test")
        self.assertEqual(summary["stock_fulfilled"], 2)
        self.assertEqual(summary["stock_inflight"], 0)
        self.assertEqual(summary["status"], "fulfilled")

    def test_unanswered_invitation_reserves_capacity_until_timeout(self):
        request = self.make_request(units=2)
        self.database.create_idempotent_request(request, "key", "body")
        self.database.insert_invitation("I-one", "R-test", "D00001", "N-one", "request_wave", 10.0)
        self.database.insert_invitation("I-two", "R-test", "D00002", "N-two", "request_wave", 10.0)
        self.database.update_invitation("R-test", "D00002", "declined", "declined")

        self.assertEqual(self.database.get_open_invitation_count("R-test", 20.0, 45.0), 1)
        self.assertEqual(self.database.get_open_invitation_count("R-test", 60.0, 45.0), 0)

    def test_accepted_donor_is_reserved_across_requests_and_released_on_stop(self):
        self.database.create_idempotent_request(self.make_request("R-one"), "key-1", "body-1")
        self.database.create_idempotent_request(self.make_request("R-two"), "key-2", "body-2")
        self.assertTrue(self.database.claim_donor_commitment("D00001", "R-one", "N-one", 10.0, 130.0))
        self.database.insert_invitation("I-one", "R-one", "D00001", "N-one", "request_wave", 10.0)
        self.database.apply_donor_response(
            {
                "event_id": "E-accept",
                "request_id": "R-one",
                "donor_id": "D00001",
                "notification_id": "N-one",
                "response": "accepted",
                "sim_time": 11.0,
            },
            11.0,
        )

        self.assertIn("D00001", self.database.get_reserved_donor_ids(20.0, 45.0, 120.0))
        self.database.close_invitation("R-one", "D00001", "not_needed")
        self.assertNotIn("D00001", self.database.get_reserved_donor_ids(20.0, 45.0, 120.0))

    def test_concurrent_requests_cannot_claim_same_donor(self):
        def claim(request_id):
            return self.database.claim_donor_commitment(
                "D00001", request_id, f"N-{request_id}", 10.0, 130.0
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ("R-one", "R-two")))

        self.assertEqual(sum(results), 1)

    def test_terminal_sim_time_filters_late_donations_but_keeps_earlier_supply(self):
        for request_id in ("R-late", "R-early"):
            self.database.create_idempotent_request(self.make_request(request_id), f"key-{request_id}", f"body-{request_id}")
            self.database.insert_invitation(
                f"I-{request_id}",
                request_id,
                "D00001",
                f"N-{request_id}",
                "request_wave",
                0.0,
                center_id="MC1",
                center_hospital_minutes=20.0,
                processing_minutes=10.0,
            )
            self.database.apply_donor_response(
                {
                    "event_id": f"A-{request_id}",
                    "request_id": request_id,
                    "donor_id": "D00001",
                    "notification_id": f"N-{request_id}",
                    "response": "accepted",
                    "sim_time": 1.0,
                },
                1.0,
            )
            for event_name, sim_time in (("en_route", 2.0), ("arrived", 3.0), ("screen_pass", 4.0)):
                self.database.apply_donor_progress(
                    {
                        "event_id": f"P-{request_id}-{event_name}",
                        "request_id": request_id,
                        "donor_id": "D00001",
                        "event": event_name,
                        "sim_time": sim_time,
                    },
                    sim_time,
                )
            self.assertTrue(
                self.database.transition_request(
                    request_id,
                    {"open", "mobilizing", "partially_fulfilled"},
                    {"status": "cancelled", "cancelled_sim": 5.0, "updated_sim": 5.0},
                )
            )

        for request_id, sim_time in (("R-late", 6.0), ("R-early", 4.5)):
            self.database.apply_donor_progress(
                {
                    "event_id": f"D-{request_id}",
                    "request_id": request_id,
                    "donor_id": "D00001",
                    "event": "donated",
                    "sim_time": sim_time,
                },
                6.0,
            )

        self.assertEqual(self.database.fetch_request_summary("R-late")["fulfilled"], 0)
        self.assertEqual(self.database.fetch_request_summary("R-early")["fulfilled"], 0)
        self.assertEqual(self.database.fetch_request_summary("R-early")["donations_inflight"], 1)
        self.assertEqual(self.database.mark_donor_supplies_delivered(40.0), ["R-early"])
        self.assertEqual(self.database.fetch_request_summary("R-early")["fulfilled"], 1)
        late_donor_state = self.database.get_donor_state("D00001")
        self.assertEqual(late_donor_state["last_donation_sim"], 6.0)

    def test_dynamic_red_cell_history_uses_published_same_type_interval(self):
        domain = BloodNetDomain("participant_kit")
        donor = domain.donors["D00001"]
        donor_state = {"last_donation_sim": 0.0, "last_donation_type": "red_cells"}

        self.assertFalse(domain.eligible(donor, "red_cells", 60 * 1440.0, donor_state))
        self.assertTrue(domain.eligible(donor, "red_cells", 90 * 1440.0, donor_state))

    def test_rare_demand_review_uses_historical_product_distribution(self):
        domain = BloodNetDomain("participant_kit", rare_demand_tail=0.02)

        self.assertEqual(domain.demand_review_reason("red_cells", 8), "rare_historical_demand")
        self.assertIsNone(domain.demand_review_reason("red_cells", 6))
        self.assertIsNone(domain.demand_review_reason("platelets", 3))

    def test_recent_hospital_request_count_is_scoped_to_simulation_window(self):
        first = self.make_request("R-first")
        first["created_sim"] = 100.0
        self.database.create_idempotent_request(first, "key-first", "body-first")
        second = self.make_request("R-second")
        second["created_sim"] = 105.0
        self.database.create_idempotent_request(second, "key-second", "body-second")

        self.assertEqual(self.database.count_recent_hospital_requests("H01", 101.0, 105.0), 1)
        self.assertEqual(self.database.count_recent_hospital_requests("H02", 0.0, 200.0), 0)

    def test_gateway_retry_reuses_persisted_notification_id(self):
        self.database.create_idempotent_request(self.make_request(), "key", "body")
        self.database.insert_invitation("I-test", "R-test", "D00001", "N-stable", "request_wave", 0.0)

        self.assertEqual(self.database.schedule_invitation_retry("N-stable", 5.0), 1)
        self.assertEqual(self.database.get_retryable_invitations(4.0), [])
        due = self.database.get_retryable_invitations(5.0)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["notification_id"], "N-stable")
        self.assertEqual(due[0]["retry_attempts"], 1)

        self.database.update_invitation_gateway_status("N-stable", "sent")
        self.assertEqual(self.database.get_retryable_invitations(10.0), [])

    def test_terminal_outreach_includes_unanswered_sent_invitations(self):
        self.database.create_idempotent_request(self.make_request(), "key", "body")
        self.database.insert_invitation("I-pending", "R-test", "D00001", "N-pending", "request_wave", 1.0)
        self.database.update_invitation_gateway_status("N-pending", "sent")
        self.database.insert_invitation("I-declined", "R-test", "D00002", "N-declined", "request_wave", 1.0)
        self.database.update_invitation("R-test", "D00002", "declined", "declined")

        outstanding = self.database.get_outstanding_invitations("R-test")
        self.assertEqual([item["donor_id"] for item in outstanding], ["D00001"])
        self.database.close_invitation("R-test", "D00001", "cancelled")
        self.assertEqual(self.database.get_invitation("R-test", "D00001")["progress_state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
