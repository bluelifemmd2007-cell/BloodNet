import json
import sqlite3
import uuid
from pathlib import Path

from app.config import settings


class Database:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings["DB_PATH"]
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _init_db(self):
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    hospital_id TEXT NOT NULL,
                    blood_group TEXT NOT NULL,
                    product TEXT NOT NULL,
                    units INTEGER NOT NULL,
                    urgency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT,
                    approved INTEGER NOT NULL DEFAULT 0,
                    created_sim REAL NOT NULL,
                    updated_sim REAL NOT NULL,
                    request_data TEXT
                );

                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    key_value TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    body_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS callback_events (
                    event_id TEXT PRIMARY KEY,
                    callback_type TEXT NOT NULL,
                    request_id TEXT,
                    donor_id TEXT,
                    payload TEXT NOT NULL,
                    processed_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS donor_invitations (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    donor_id TEXT NOT NULL,
                    notification_id TEXT NOT NULL,
                    template TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued',
                    created_sim REAL NOT NULL,
                    response TEXT,
                    UNIQUE(request_id, donor_id, notification_id)
                );

                CREATE TABLE IF NOT EXISTS dispatches (
                    dispatch_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    center_id TEXT NOT NULL,
                    blood_group TEXT NOT NULL,
                    product TEXT NOT NULL,
                    units INTEGER NOT NULL,
                    eta_sim_min REAL,
                    created_sim REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'created'
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT,
                    kind TEXT NOT NULL,
                    detail TEXT,
                    created_sim REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS donor_state (
                    donor_id TEXT PRIMARY KEY,
                    last_donation_sim REAL,
                    last_donation_type TEXT,
                    updated_sim REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS donor_commitments (
                    donor_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    notification_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_sim REAL NOT NULL,
                    updated_sim REAL NOT NULL
                );
                """
            )
            migrations = {
                "requests": {
                    "fulfilled_units": "INTEGER NOT NULL DEFAULT 0",
                    "stock_fulfilled": "INTEGER NOT NULL DEFAULT 0",
                    "stock_inflight": "INTEGER NOT NULL DEFAULT 0",
                    "wave_count": "INTEGER NOT NULL DEFAULT 0",
                    "last_wave_sim": "REAL NOT NULL DEFAULT -1",
                    "deadline_sim": "REAL",
                    "cancelled_sim": "REAL",
                    "expired_sim": "REAL",
                    "review_reason": "TEXT",
                },
                "donor_invitations": {
                    "gateway_status": "TEXT",
                    "progress_state": "TEXT NOT NULL DEFAULT 'invited'",
                    "accepted_sim": "REAL",
                    "last_progress_sim": "REAL",
                    "en_route_seen": "INTEGER NOT NULL DEFAULT 0",
                    "arrived_seen": "INTEGER NOT NULL DEFAULT 0",
                    "confirmation_notification_id": "TEXT",
                    "confirmed_sent": "INTEGER NOT NULL DEFAULT 0",
                    "retry_attempts": "INTEGER NOT NULL DEFAULT 0",
                    "retry_after_sim": "REAL",
                    "center_id": "TEXT",
                    "center_hospital_minutes": "REAL",
                    "processing_minutes": "REAL",
                    "donated_sim": "REAL",
                    "supply_eta_sim": "REAL",
                    "supply_delivered_sim": "REAL",
                },
                "dispatches": {"delivered_sim": "REAL"},
            }
            for table, columns in migrations.items():
                present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                legacy_donation_backfill = table == "donor_invitations" and "supply_delivered_sim" not in present
                for column, definition in columns.items():
                    if column not in present:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                if legacy_donation_backfill:
                    conn.execute(
                        "UPDATE donor_invitations SET donated_sim = COALESCE(last_progress_sim, created_sim), "
                        "supply_eta_sim = COALESCE(last_progress_sim, created_sim), "
                        "supply_delivered_sim = COALESCE(last_progress_sim, created_sim) "
                        "WHERE progress_state = 'donated'"
                    )
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_requests_hospital_created ON requests(hospital_id, created_sim DESC);
                CREATE INDEX IF NOT EXISTS idx_requests_active ON requests(status, deadline_sim);
                CREATE INDEX IF NOT EXISTS idx_invitations_request_state ON donor_invitations(request_id, progress_state);
                CREATE INDEX IF NOT EXISTS idx_invitations_supply_eta ON donor_invitations(supply_delivered_sim, supply_eta_sim);
                CREATE INDEX IF NOT EXISTS idx_dispatches_request_status ON dispatches(request_id, status);
                CREATE INDEX IF NOT EXISTS idx_callbacks_request_type ON callback_events(request_id, callback_type);
                CREATE INDEX IF NOT EXISTS idx_donor_commitments_expiry ON donor_commitments(status, expires_sim);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def save_audit(self, request_id: str | None, kind: str, detail: dict | str, now: float = 0.0):
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO audit_log(request_id, kind, detail, created_sim) VALUES (?, ?, ?, ?)",
                (request_id, kind, json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else str(detail), now),
            )
            conn.commit()
        finally:
            conn.close()

    def get_request(self, request_id: str):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
            if row is None:
                return None
            return dict(row)
        finally:
            conn.close()

    def list_requests_for_hospital(self, hospital_id: str):
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM requests WHERE hospital_id = ? ORDER BY created_sim DESC", (hospital_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def count_recent_hospital_requests(self, hospital_id: str, start_sim: float, end_sim: float) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM requests WHERE hospital_id = ? AND created_sim >= ? AND created_sim <= ?",
                (hospital_id, start_sim, end_sim),
            ).fetchone()
            return int(row["n"])
        finally:
            conn.close()

    def insert_request(self, req: dict):
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO requests(id, hospital_id, blood_group, product, units, urgency, status, note, approved, created_sim, updated_sim, request_data, deadline_sim, review_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    req["id"],
                    req["hospital_id"],
                    req["blood_group"],
                    req["product"],
                    req["units"],
                    req["urgency"],
                    req["status"],
                    req.get("note"),
                    1 if req.get("approved") else 0,
                    req["created_sim"],
                    req["created_sim"],
                    json.dumps(req.get("request_data", {}), ensure_ascii=False),
                    req.get("deadline_sim"),
                    req.get("review_reason"),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def create_idempotent_request(self, req: dict, key: str, body_hash: str):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT request_id, body_hash FROM idempotency_keys WHERE key_value = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return "existing", existing["request_id"], existing["body_hash"]

            conn.execute(
                "INSERT INTO requests(id, hospital_id, blood_group, product, units, urgency, status, note, approved, created_sim, updated_sim, request_data, deadline_sim, review_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    req["id"],
                    req["hospital_id"],
                    req["blood_group"],
                    req["product"],
                    req["units"],
                    req["urgency"],
                    req["status"],
                    req.get("note"),
                    1 if req.get("approved") else 0,
                    req["created_sim"],
                    req["created_sim"],
                    json.dumps(req.get("request_data", {}), ensure_ascii=False),
                    req.get("deadline_sim"),
                    req.get("review_reason"),
                ),
            )
            conn.execute(
                "INSERT INTO idempotency_keys(key_value, request_id, body_hash, created_at) VALUES (?, ?, ?, ?)",
                (key, req["id"], body_hash, req["created_sim"]),
            )
            conn.commit()
            return "created", req["id"], body_hash
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_request(self, request_id: str, updates: dict):
        if not updates:
            return
        allowed = {
            "urgency", "status", "approved", "updated_sim", "fulfilled_units",
            "stock_fulfilled", "stock_inflight", "wave_count", "last_wave_sim",
            "deadline_sim", "cancelled_sim", "expired_sim", "review_reason",
        }
        if not set(updates) <= allowed:
            raise ValueError("unsupported request update")
        fields = []
        values = []
        for key, value in updates.items():
            fields.append(f"{key} = ?")
            values.append(value)
        conn = self._connect()
        try:
            conn.execute(f"UPDATE requests SET {', '.join(fields)} WHERE id = ?", (*values, request_id))
            conn.commit()
        finally:
            conn.close()

    def transition_request(self, request_id: str, allowed_statuses: set[str], updates: dict) -> bool:
        if not updates:
            return False
        allowed = {
            "urgency", "status", "approved", "updated_sim", "fulfilled_units",
            "stock_fulfilled", "stock_inflight", "wave_count", "last_wave_sim",
            "deadline_sim", "cancelled_sim", "expired_sim", "review_reason",
        }
        if not set(updates) <= allowed:
            raise ValueError("unsupported request update")
        fields = ", ".join(f"{key} = ?" for key in updates)
        placeholders = ", ".join("?" for _ in allowed_statuses)
        conn = self._connect()
        try:
            cursor = conn.execute(
                f"UPDATE requests SET {fields} WHERE id = ? AND status IN ({placeholders})",
                (*updates.values(), request_id, *sorted(allowed_statuses)),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def get_or_create_idempotency(self, key: str, request_id: str, body_hash: str):
        conn = self._connect()
        try:
            row = conn.execute("SELECT request_id, body_hash FROM idempotency_keys WHERE key_value = ?", (key,)).fetchone()
            if row is not None:
                return row["request_id"], row["body_hash"]
            conn.execute(
                "INSERT INTO idempotency_keys(key_value, request_id, body_hash, created_at) VALUES (?, ?, ?, ?)",
                (key, request_id, body_hash, 0.0),
            )
            conn.commit()
            return request_id, body_hash
        finally:
            conn.close()

    def get_idempotency_for_key(self, key: str):
        conn = self._connect()
        try:
            row = conn.execute("SELECT request_id, body_hash FROM idempotency_keys WHERE key_value = ?", (key,)).fetchone()
            if row is None:
                return None
            return {"request_id": row["request_id"], "body_hash": row["body_hash"]}
        finally:
            conn.close()

    def insert_callback_event(self, event_id: str, callback_type: str, request_id: str | None, donor_id: str | None, payload: dict, now: float):
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO callback_events(event_id, callback_type, request_id, donor_id, payload, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, callback_type, request_id, donor_id, json.dumps(payload, ensure_ascii=False), now),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def _insert_callback(self, conn, event_id: str, callback_type: str, request_id: str | None, donor_id: str | None, payload: dict, now: float) -> bool:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO callback_events(event_id, callback_type, request_id, donor_id, payload, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, callback_type, request_id, donor_id, json.dumps(payload, ensure_ascii=False), now),
        )
        return cursor.rowcount == 1

    def apply_donor_response(self, payload: dict, now: float):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            inserted = self._insert_callback(conn, payload["event_id"], "donor-response", payload["request_id"], payload["donor_id"], payload, now)
            if payload.get("notification_id"):
                invitation = conn.execute(
                    "SELECT * FROM donor_invitations WHERE request_id = ? AND donor_id = ? AND notification_id = ?",
                    (payload["request_id"], payload["donor_id"], payload["notification_id"]),
                ).fetchone()
            else:
                invitation = conn.execute(
                    "SELECT * FROM donor_invitations WHERE request_id = ? AND donor_id = ? AND template = 'request_wave' ORDER BY created_sim DESC LIMIT 1",
                    (payload["request_id"], payload["donor_id"]),
                ).fetchone()
            if not inserted or invitation is None:
                conn.commit()
                return inserted, None if invitation is None else dict(invitation), False
            request = conn.execute("SELECT status FROM requests WHERE id = ?", (payload["request_id"],)).fetchone()
            if request is None or request["status"] in {"cancelled", "expired", "fulfilled"}:
                conn.commit()
                return True, dict(invitation), False
            accepted = payload["response"] == "accepted"
            response = invitation["response"]
            changed = False
            if response is None:
                conn.execute(
                    "UPDATE donor_invitations SET response = ?, state = ?, progress_state = ?, accepted_sim = ? WHERE id = ?",
                    (payload["response"], payload["response"], "accepted" if accepted else "declined", payload.get("sim_time") if accepted else None, invitation["id"]),
                )
                changed = accepted
                conn.execute(
                    "UPDATE donor_commitments SET status = ?, expires_sim = ?, updated_sim = ? WHERE donor_id = ? AND request_id = ?",
                    ("accepted" if accepted else "released", float(payload.get("sim_time") or now) + 120.0 if accepted else now, now, payload["donor_id"], payload["request_id"]),
                )
                if accepted:
                    self._rebuild_invitation_progress(conn, payload["request_id"], payload["donor_id"], invitation["id"], now)
            updated = conn.execute("SELECT * FROM donor_invitations WHERE id = ?", (invitation["id"],)).fetchone()
            conn.commit()
            return True, dict(updated), changed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def apply_gateway_status(self, payload: dict, now: float):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            inserted = self._insert_callback(conn, payload["event_id"], "gateway-status", payload["request_id"], payload["donor_id"], payload, now)
            if not inserted:
                conn.commit()
                return False
            invitation = conn.execute(
                "SELECT id, gateway_status FROM donor_invitations WHERE request_id = ? AND donor_id = ? AND notification_id = ?",
                (payload["request_id"], payload["donor_id"], payload["notification_id"]),
            ).fetchone()
            if invitation is not None:
                order = {"queued": 0, "sent": 1, "delivered": 2, "failed": 3, "expired": 3}
                old_rank = order.get(invitation["gateway_status"], -1)
                new_rank = order.get(payload["status"], -1)
                if new_rank >= old_rank:
                    conn.execute("UPDATE donor_invitations SET gateway_status = ? WHERE id = ?", (payload["status"], invitation["id"]))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _replay_progress(events: list[dict], accepted_sim: float | None):
        state = "accepted" if accepted_sim is not None else "invited"
        en_route_seen = False
        arrived_seen = False
        donated_seen = False
        event_order = {"en_route": 0, "arrived": 1, "screen_pass": 2, "screen_fail": 2, "donated": 3, "no_show": 4}
        events.sort(key=lambda item: (float(item.get("sim_time") or 0), event_order.get(item.get("event"), 9), item.get("event_id", "")))
        for event in events:
            event_name = event.get("event")
            event_time = float(event.get("sim_time") or 0)
            if accepted_sim is None or event_time < float(accepted_sim):
                continue
            if event_name == "en_route" and state == "accepted":
                state = "en_route"
                en_route_seen = True
            elif event_name == "arrived" and state == "en_route":
                state = "arrived"
                arrived_seen = True
            elif event_name == "screen_pass" and state == "arrived":
                state = "screen_pass"
            elif event_name == "screen_fail" and state == "arrived":
                state = "screen_fail"
            elif event_name == "donated" and state == "screen_pass":
                state = "donated"
                donated_seen = True
            elif event_name == "no_show" and state in {"accepted", "en_route", "arrived"}:
                state = "no_show"

        donated_sim = max(
            (float(item.get("sim_time") or 0) for item in events if item.get("event") == "donated"),
            default=None,
        ) if donated_seen else None
        latest_sim = max((float(item.get("sim_time") or 0) for item in events), default=None)
        return state, en_route_seen, arrived_seen, donated_seen, latest_sim, donated_sim

    def _rebuild_invitation_progress(self, conn: sqlite3.Connection, request_id: str, donor_id: str, invitation_id: str, now: float):
        invitation = conn.execute("SELECT * FROM donor_invitations WHERE id = ?", (invitation_id,)).fetchone()
        request = conn.execute("SELECT status, product, cancelled_sim, expired_sim FROM requests WHERE id = ?", (request_id,)).fetchone()
        rows = conn.execute(
            "SELECT payload FROM callback_events WHERE callback_type = 'donor-progress' AND request_id = ? AND donor_id = ?",
            (request_id, donor_id),
        ).fetchall()
        all_events = [json.loads(row["payload"]) for row in rows]
        all_events.sort(key=lambda item: (float(item.get("sim_time") or 0), item.get("event_id", "")))
        terminal_sim = None
        if request is not None and request["status"] == "cancelled":
            terminal_sim = request["cancelled_sim"]
        elif request is not None and request["status"] == "expired":
            terminal_sim = request["expired_sim"]
        events = all_events
        if terminal_sim is not None:
            events = [event for event in all_events if float(event.get("sim_time") or 0) <= float(terminal_sim)]
        state, en_route_seen, arrived_seen, donated_seen, latest_sim, donated_sim = self._replay_progress(events, invitation["accepted_sim"])
        _, _, _, actually_donated, _, actual_donated_sim = self._replay_progress(all_events, invitation["accepted_sim"])
        conn.execute(
            "UPDATE donor_invitations SET progress_state = ?, last_progress_sim = ?, en_route_seen = ?, arrived_seen = ?, "
            "donated_sim = ?, supply_eta_sim = ? WHERE id = ?",
            (
                state,
                latest_sim,
                int(en_route_seen),
                int(arrived_seen),
                donated_sim,
                (
                    donated_sim
                    + float(invitation["processing_minutes"] or 0)
                    + float(invitation["center_hospital_minutes"] or 0)
                    if donated_seen and donated_sim is not None
                    else None
                ),
                invitation_id,
            ),
        )
        if actually_donated and request is not None and actual_donated_sim is not None:
            conn.execute(
                "INSERT INTO donor_state(donor_id, last_donation_sim, last_donation_type, updated_sim) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(donor_id) DO UPDATE SET last_donation_type = CASE WHEN excluded.last_donation_sim >= COALESCE(last_donation_sim, -1) THEN excluded.last_donation_type ELSE last_donation_type END, "
                "last_donation_sim = MAX(COALESCE(last_donation_sim, -1), excluded.last_donation_sim), updated_sim = excluded.updated_sim",
                (donor_id, actual_donated_sim, request["product"], now),
            )
            conn.execute(
                "UPDATE donor_commitments SET status = 'released', expires_sim = ?, updated_sim = ? WHERE donor_id = ? AND request_id = ?",
                (now, now, donor_id, request_id),
            )
        elif state in {"no_show", "screen_fail"}:
            conn.execute(
                "UPDATE donor_commitments SET status = 'released', expires_sim = ?, updated_sim = ? WHERE donor_id = ? AND request_id = ?",
                (now, now, donor_id, request_id),
            )
        self._refresh_request(conn, request_id, now)
        return state, donated_seen

    def apply_donor_progress(self, payload: dict, now: float):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            inserted = self._insert_callback(conn, payload["event_id"], "donor-progress", payload["request_id"], payload["donor_id"], payload, now)
            if not inserted:
                conn.commit()
                return False, None, False
            invitation = conn.execute(
                "SELECT * FROM donor_invitations WHERE request_id = ? AND donor_id = ? ORDER BY created_sim LIMIT 1",
                (payload["request_id"], payload["donor_id"]),
            ).fetchone()
            if invitation is None:
                conn.commit()
                return True, None, False
            _, donated_seen = self._rebuild_invitation_progress(conn, payload["request_id"], payload["donor_id"], invitation["id"], now)
            conn.commit()
            return True, dict(invitation), donated_seen
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _refresh_request(self, conn: sqlite3.Connection, request_id: str, now: float):
        row = conn.execute("SELECT status, units FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            return
        donor_units = conn.execute(
            "SELECT COUNT(*) AS n FROM donor_invitations WHERE request_id = ? AND template = 'request_wave' AND supply_delivered_sim IS NOT NULL",
            (request_id,),
        ).fetchone()["n"]
        donor_donated = conn.execute(
            "SELECT COUNT(*) AS n FROM donor_invitations WHERE request_id = ? AND template = 'request_wave' AND progress_state = 'donated'",
            (request_id,),
        ).fetchone()["n"]
        stock_delivered = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS n FROM dispatches WHERE request_id = ? AND status = 'delivered'",
            (request_id,),
        ).fetchone()["n"]
        stock_inflight = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS n FROM dispatches WHERE request_id = ? AND status = 'in_transit'",
            (request_id,),
        ).fetchone()["n"]
        donor_inflight = max(0, donor_donated - donor_units)
        status = row["status"]
        if status not in {"cancelled", "expired"}:
            supplied = donor_units + stock_delivered
            if supplied >= row["units"]:
                status = "fulfilled"
            elif supplied > 0:
                status = "partially_fulfilled"
            else:
                accepted = conn.execute(
                    "SELECT 1 FROM donor_invitations WHERE request_id = ? AND response = 'accepted' LIMIT 1",
                    (request_id,),
                ).fetchone()
                sent = conn.execute(
                    "SELECT 1 FROM donor_invitations WHERE request_id = ? AND gateway_status IN ('queued', 'sent', 'delivered') LIMIT 1",
                    (request_id,),
                ).fetchone()
                status = "mobilizing" if accepted or sent else "open"
        conn.execute(
            "UPDATE requests SET fulfilled_units = ?, stock_fulfilled = ?, stock_inflight = ?, status = ?, updated_sim = ? WHERE id = ?",
            (donor_units, stock_delivered, stock_inflight, status, now, request_id),
        )

    def callback_seen(self, event_id: str):
        conn = self._connect()
        try:
            row = conn.execute("SELECT 1 FROM callback_events WHERE event_id = ?", (event_id,)).fetchone()
            return row is not None
        finally:
            conn.close()

    def insert_invitation(
        self,
        invitation_id: str,
        request_id: str,
        donor_id: str,
        notification_id: str,
        template: str,
        now: float,
        center_id: str | None = None,
        center_hospital_minutes: float | None = None,
        processing_minutes: float | None = None,
    ):
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO donor_invitations("
                "id, request_id, donor_id, notification_id, template, state, created_sim, response, center_id, "
                "center_hospital_minutes, processing_minutes) VALUES (?, ?, ?, ?, ?, 'queued', ?, NULL, ?, ?, ?)",
                (
                    invitation_id,
                    request_id,
                    donor_id,
                    notification_id,
                    template,
                    now,
                    center_id,
                    center_hospital_minutes,
                    processing_minutes,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def invitation_exists(self, request_id: str, donor_id: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute("SELECT 1 FROM donor_invitations WHERE request_id = ? AND donor_id = ? LIMIT 1", (request_id, donor_id)).fetchone()
            return row is not None
        finally:
            conn.close()

    def get_invitation(self, request_id: str, donor_id: str, notification_id: str | None = None):
        conn = self._connect()
        try:
            if notification_id:
                row = conn.execute(
                    "SELECT * FROM donor_invitations WHERE request_id = ? AND donor_id = ? AND notification_id = ?",
                    (request_id, donor_id, notification_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM donor_invitations WHERE request_id = ? AND donor_id = ? ORDER BY created_sim LIMIT 1",
                    (request_id, donor_id),
                ).fetchone()
            return None if row is None else dict(row)
        finally:
            conn.close()

    def update_invitation_route(
        self,
        request_id: str,
        donor_id: str,
        center_id: str,
        center_hospital_minutes: float,
    ):
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE donor_invitations SET center_id = ?, center_hospital_minutes = ? "
                "WHERE request_id = ? AND donor_id = ? AND template = 'request_wave'",
                (center_id, center_hospital_minutes, request_id, donor_id),
            )
            conn.commit()
        finally:
            conn.close()

    def set_invitation_confirmation(self, request_id: str, donor_id: str, notification_id: str, sent: bool):
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE donor_invitations SET confirmation_notification_id = COALESCE(confirmation_notification_id, ?), confirmed_sent = ? WHERE request_id = ? AND donor_id = ?",
                (notification_id, int(sent), request_id, donor_id),
            )
            conn.commit()
        finally:
            conn.close()

    def claim_confirmation(self, request_id: str, donor_id: str) -> str | None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT confirmation_notification_id, confirmed_sent FROM donor_invitations WHERE request_id = ? AND donor_id = ? AND response = 'accepted'",
                (request_id, donor_id),
            ).fetchone()
            if row is None or row["confirmed_sent"]:
                conn.rollback()
                return None
            notification_id = row["confirmation_notification_id"] or f"NT-{uuid.uuid4().hex[:12]}"
            conn.execute(
                "UPDATE donor_invitations SET confirmation_notification_id = ?, confirmed_sent = 1 WHERE request_id = ? AND donor_id = ?",
                (notification_id, request_id, donor_id),
            )
            conn.commit()
            return notification_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_invitation(self, request_id: str, donor_id: str, state: str, response: str | None = None):
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE donor_invitations SET state = ?, response = ? WHERE request_id = ? AND donor_id = ?",
                (state, response, request_id, donor_id),
            )
            conn.commit()
        finally:
            conn.close()

    def insert_dispatch(self, dispatch_id: str, request_id: str, center_id: str, blood_group: str, product: str, units: int, eta_sim_min: float | None, now: float):
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO dispatches(dispatch_id, request_id, center_id, blood_group, product, units, eta_sim_min, created_sim, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'created')",
                (dispatch_id, request_id, center_id, blood_group, product, units, eta_sim_min, now),
            )
            conn.commit()
        finally:
            conn.close()

    def update_dispatch(self, dispatch_id: str, status: str, eta_sim_min: float | None = None, now: float = 0.0):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT request_id FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
            conn.execute(
                "UPDATE dispatches SET status = ?, eta_sim_min = COALESCE(?, eta_sim_min) WHERE dispatch_id = ?",
                (status, eta_sim_min, dispatch_id),
            )
            if row is not None:
                self._refresh_request(conn, row["request_id"], now)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_dispatches(self, request_id: str | None = None):
        conn = self._connect()
        try:
            if request_id:
                rows = conn.execute("SELECT * FROM dispatches WHERE request_id = ? ORDER BY created_sim", (request_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM dispatches WHERE status IN ('created', 'in_transit') ORDER BY created_sim").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def list_dispatches(self, limit: int = 100):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT d.*, r.hospital_id FROM dispatches d JOIN requests r ON r.id = d.request_id "
                "ORDER BY d.created_sim DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def list_invitations(self, limit: int = 100):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT i.id, i.request_id, r.hospital_id, i.donor_id, i.template, i.state, i.response, "
                "i.progress_state, i.created_sim, i.accepted_sim, i.gateway_status, i.center_id, "
                "i.supply_eta_sim, i.supply_delivered_sim "
                "FROM donor_invitations i JOIN requests r ON r.id = i.request_id "
                "WHERE i.template = 'request_wave' ORDER BY i.created_sim DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def list_recent_audit(self, limit: int = 50):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, request_id, kind, detail, created_sim FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_invited_donors(self, request_id: str) -> set[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT donor_id FROM donor_invitations WHERE request_id = ? AND template = 'request_wave'",
                (request_id,),
            ).fetchall()
            return {row["donor_id"] for row in rows}
        finally:
            conn.close()

    def get_open_invitation_count(self, request_id: str, now: float, reservation_minutes: float) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(DISTINCT donor_id) AS n FROM donor_invitations "
                "WHERE request_id = ? AND template = 'request_wave' AND response IS NULL "
                "AND created_sim >= ? AND COALESCE(gateway_status, 'sent') NOT IN ('failed', 'expired')",
                (request_id, now - reservation_minutes),
            ).fetchone()
            return int(row["n"])
        finally:
            conn.close()

    def get_reserved_donor_ids(self, now: float, response_ttl: float, accepted_ttl: float) -> set[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT donor_id FROM donor_commitments WHERE status IN ('invited', 'accepted', 'en_route', 'arrived', 'screen_pass') AND expires_sim >= ?",
                (now,),
            ).fetchall()
            return {row["donor_id"] for row in rows}
        finally:
            conn.close()

    def claim_donor_commitment(self, donor_id: str, request_id: str, notification_id: str, now: float, expires_sim: float) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT request_id, status, expires_sim FROM donor_commitments WHERE donor_id = ?", (donor_id,)).fetchone()
            if row is not None and row["request_id"] != request_id and row["status"] != "released" and float(row["expires_sim"]) >= now:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO donor_commitments(donor_id, request_id, notification_id, status, expires_sim, updated_sim) VALUES (?, ?, ?, 'invited', ?, ?) "
                "ON CONFLICT(donor_id) DO UPDATE SET request_id = excluded.request_id, notification_id = excluded.notification_id, status = 'invited', expires_sim = excluded.expires_sim, updated_sim = excluded.updated_sim",
                (donor_id, request_id, notification_id, expires_sim, now),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release_donor_commitment(self, donor_id: str, request_id: str, now: float):
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE donor_commitments SET status = 'released', expires_sim = ?, updated_sim = ? WHERE donor_id = ? AND request_id = ?",
                (now, now, donor_id, request_id),
            )
            conn.commit()
        finally:
            conn.close()

    def close_invitation(self, request_id: str, donor_id: str, progress_state: str):
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE donor_invitations SET progress_state = ? WHERE request_id = ? AND donor_id = ? "
                "AND template = 'request_wave' AND (response IS NULL OR response = 'accepted')",
                (progress_state, request_id, donor_id),
            )
            conn.execute(
                "UPDATE donor_commitments SET status = 'released', expires_sim = updated_sim WHERE request_id = ? AND donor_id = ?",
                (request_id, donor_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_recent_invitation_counts(self, since_sim: float) -> dict[str, int]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT donor_id, COUNT(*) AS n FROM donor_invitations WHERE template = 'request_wave' AND created_sim >= ? GROUP BY donor_id",
                (since_sim,),
            ).fetchall()
            return {row["donor_id"]: int(row["n"]) for row in rows}
        finally:
            conn.close()

    def get_all_donor_states(self) -> dict[str, dict]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM donor_state").fetchall()
            return {row["donor_id"]: dict(row) for row in rows}
        finally:
            conn.close()

    def claim_wave(self, request_id: str, now: float, minimum_gap: float, maximum_waves: int) -> int | None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status, wave_count, last_wave_sim FROM requests WHERE id = ?", (request_id,)).fetchone()
            if row is None or row["status"] in {"pending_review", "cancelled", "fulfilled", "expired"}:
                conn.rollback()
                return None
            if int(row["wave_count"]) >= maximum_waves or (
                int(row["wave_count"]) > 0 and now - float(row["last_wave_sim"]) < minimum_gap
            ):
                conn.rollback()
                return None
            wave = int(row["wave_count"]) + 1
            conn.execute(
                "UPDATE requests SET wave_count = ?, last_wave_sim = ?, updated_sim = ? WHERE id = ?",
                (wave, now, now, request_id),
            )
            conn.commit()
            return wave
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_invitation_gateway_status(self, notification_id: str, status: str):
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE donor_invitations SET gateway_status = ?, retry_after_sim = NULL WHERE notification_id = ?",
                (status, notification_id),
            )
            conn.commit()
        finally:
            conn.close()

    def schedule_invitation_retry(self, notification_id: str, retry_after_sim: float) -> int:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE donor_invitations SET gateway_status = 'retry', retry_attempts = retry_attempts + 1, "
                "retry_after_sim = ? WHERE notification_id = ?",
                (retry_after_sim, notification_id),
            )
            row = conn.execute(
                "SELECT retry_attempts FROM donor_invitations WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            conn.commit()
            return 0 if row is None else int(row["retry_attempts"])
        finally:
            conn.close()

    def get_retryable_invitations(self, now: float, limit: int = 20):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT i.*, r.status AS request_status FROM donor_invitations i "
                "JOIN requests r ON r.id = i.request_id "
                "WHERE i.template = 'request_wave' AND i.gateway_status = 'retry' "
                "AND i.retry_after_sim <= ? ORDER BY i.retry_after_sim, i.created_sim LIMIT ?",
                (now, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_pending_accepted_invitations(self, request_id: str):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM donor_invitations WHERE request_id = ? AND response = 'accepted' AND progress_state IN ('accepted', 'en_route', 'arrived', 'screen_pass')",
                (request_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_outstanding_invitations(self, request_id: str):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM donor_invitations WHERE request_id = ? AND template = 'request_wave' "
                "AND ((response IS NULL AND COALESCE(gateway_status, 'queued') NOT IN ('failed', 'expired', 'cancelled')) "
                "OR (response = 'accepted' AND progress_state IN ('accepted', 'en_route', 'arrived', 'screen_pass'))) "
                "ORDER BY created_sim",
                (request_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def expire_due_requests(self, now: float):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id FROM requests WHERE status IN ('open', 'mobilizing', 'partially_fulfilled') AND deadline_sim IS NOT NULL AND deadline_sim <= ? AND fulfilled_units + stock_fulfilled < units",
                (now,),
            ).fetchall()
            request_ids = [row["id"] for row in rows]
            if request_ids:
                conn.executemany(
                    "UPDATE requests SET status = 'expired', expired_sim = ?, updated_sim = ? WHERE id = ?",
                    [(now, now, request_id) for request_id in request_ids],
                )
            conn.commit()
            return request_ids
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_dispatches_delivered(self, now: float):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            request_ids = [row["request_id"] for row in conn.execute(
                "SELECT DISTINCT request_id FROM dispatches WHERE status = 'in_transit' AND eta_sim_min IS NOT NULL AND eta_sim_min <= ?",
                (now,),
            ).fetchall()]
            conn.execute(
                "UPDATE dispatches SET status = 'delivered', delivered_sim = ? WHERE status = 'in_transit' AND eta_sim_min IS NOT NULL AND eta_sim_min <= ?",
                (now, now),
            )
            for request_id in request_ids:
                self._refresh_request(conn, request_id, now)
            conn.commit()
            return request_ids
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_donor_supplies_delivered(self, now: float):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            request_ids = [
                row["request_id"]
                for row in conn.execute(
                    "SELECT DISTINCT request_id FROM donor_invitations "
                    "WHERE progress_state = 'donated' AND supply_delivered_sim IS NULL "
                    "AND supply_eta_sim IS NOT NULL AND supply_eta_sim <= ?",
                    (now,),
                ).fetchall()
            ]
            conn.execute(
                "UPDATE donor_invitations SET supply_delivered_sim = supply_eta_sim "
                "WHERE progress_state = 'donated' AND supply_delivered_sim IS NULL "
                "AND supply_eta_sim IS NOT NULL AND supply_eta_sim <= ?",
                (now,),
            )
            for request_id in request_ids:
                self._refresh_request(conn, request_id, now)
            conn.commit()
            return request_ids
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_due_requests(self, now: float):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM requests WHERE status IN ('open', 'mobilizing', 'partially_fulfilled') AND deadline_sim IS NOT NULL AND deadline_sim <= ?",
                (now,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_active_requests(self):
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM requests WHERE status IN ('open', 'mobilizing', 'partially_fulfilled') ORDER BY created_sim").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_donor_state(self, donor_id: str):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM donor_state WHERE donor_id = ?", (donor_id,)).fetchone()
            return None if row is None else dict(row)
        finally:
            conn.close()

    def invitation_count_since(self, donor_id: str, since_sim: float) -> int:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM donor_invitations WHERE donor_id = ? AND created_sim >= ?",
                (donor_id, since_sim),
            ).fetchone()["n"]
        finally:
            conn.close()

    def fetch_request_summary(self, request_id: str):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, hospital_id, blood_group, product, units, urgency, status, note, approved, created_sim, updated_sim, deadline_sim, stock_fulfilled, stock_inflight FROM requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            confirmed = conn.execute(
                "SELECT COUNT(*) as c FROM donor_invitations WHERE request_id = ? AND response = 'accepted'",
                (request_id,),
            ).fetchone()["c"]
            en_route = conn.execute(
                "SELECT COUNT(*) as c FROM donor_invitations WHERE request_id = ? AND en_route_seen = 1",
                (request_id,),
            ).fetchone()["c"]
            arrived = conn.execute(
                "SELECT COUNT(*) as c FROM donor_invitations WHERE request_id = ? AND arrived_seen = 1",
                (request_id,),
            ).fetchone()["c"]
            donated = conn.execute(
                "SELECT COUNT(*) as c FROM donor_invitations WHERE request_id = ? AND template = 'request_wave' AND progress_state = 'donated'",
                (request_id,),
            ).fetchone()["c"]
            fulfilled = conn.execute(
                "SELECT COUNT(*) as c FROM donor_invitations WHERE request_id = ? AND template = 'request_wave' AND supply_delivered_sim IS NOT NULL",
                (request_id,),
            ).fetchone()["c"]
            return {
                "request_id": row["id"],
                "hospital_id": row["hospital_id"],
                "blood_group": row["blood_group"],
                "product": row["product"],
                "urgency": row["urgency"],
                "status": row["status"],
                "needed": row["units"],
                "confirmed": confirmed,
                "en_route": en_route,
                "arrived": arrived,
                "fulfilled": fulfilled,
                "donated": donated,
                "donations_inflight": max(0, donated - fulfilled),
                "stock_fulfilled": row["stock_fulfilled"],
                "stock_inflight": row["stock_inflight"],
                "created_sim_min": row["created_sim"],
                "deadline_sim_min": row["deadline_sim"],
            }
        finally:
            conn.close()
