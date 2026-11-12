import asyncio
import hashlib
import json
import logging
import math
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import BLOOD_GROUPS, PRODUCTS, SLA, URGENCIES, settings
from app.database import Database
from app.domain import BloodNetDomain
from app.integrations import LocalServiceUnavailable, LocalServices


logger = logging.getLogger("bloodnet")
db = Database(settings["DB_PATH"])
domain = BloodNetDomain(settings["KIT_PATH"], settings["ABUSE_RARE_DEMAND_TAIL"])
services = LocalServices(settings)
frontend_path = Path(__file__).resolve().parent.parent / "frontend"
static_path = frontend_path / "static"
latest_sim_time = 0.0
request_locks: dict[str, asyncio.Lock] = {}
travel_query_budget = 5
wave_gap_min = 45.0
max_waves = 4


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewRequest(StrictBody):
    hospital_id: str = Field(min_length=1, max_length=32)
    blood_group: str
    product: str
    units: int = Field(ge=1, le=20, strict=True)
    urgency: str
    note: str | None = Field(default=None, max_length=500)

    @field_validator("blood_group")
    @classmethod
    def validate_group(cls, value: str) -> str:
        if value not in BLOOD_GROUPS:
            raise ValueError("invalid blood_group")
        return value

    @field_validator("product")
    @classmethod
    def validate_product(cls, value: str) -> str:
        if value not in PRODUCTS:
            raise ValueError("invalid product")
        return value

    @field_validator("urgency")
    @classmethod
    def validate_urgency(cls, value: str) -> str:
        if value not in URGENCIES:
            raise ValueError("invalid urgency")
        return value


class AmendRequest(StrictBody):
    urgency: str

    @field_validator("urgency")
    @classmethod
    def validate_urgency(cls, value: str) -> str:
        if value not in URGENCIES:
            raise ValueError("invalid urgency")
        return value


class ApprovalPayload(StrictBody):
    approved: bool = True
    note: str | None = Field(default=None, max_length=500)


class DonorResponseCallback(StrictBody):
    event_id: str
    request_id: str
    donor_id: str
    notification_id: str | None = None
    response: Literal["accepted", "declined"]
    sim_time: float
    reason: str | None = None


class DonorProgressCallback(StrictBody):
    event_id: str
    request_id: str
    donor_id: str
    event: Literal["en_route", "arrived", "screen_pass", "screen_fail", "donated", "no_show"]
    sim_time: float
    reason: str | None = None


class GatewayStatusCallback(StrictBody):
    event_id: str
    notification_id: str
    request_id: str
    donor_id: str
    status: Literal["queued", "sent", "delivered", "failed", "expired"]
    sim_time: float


def _error(code: str, message: str, status: int = 400, headers: dict[str, str] | None = None):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}}, headers=headers)


def _parse(model: type[BaseModel], payload: dict[str, Any]):
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        return _error("validation_error", exc.errors(include_url=False).__str__(), 400)


def _auth(authorization: str | None, token: str, label: str):
    if authorization != f"Bearer {token}":
        return _error("unauthorized", f"{label} token required", 401)
    return None


async def _now(sim_header: str | None = None, fallback: float | None = None) -> float:
    global latest_sim_time
    if sim_header is not None:
        try:
            parsed = float(sim_header)
            if math.isfinite(parsed) and parsed >= 0:
                latest_sim_time = max(latest_sim_time, parsed)
                return parsed
        except ValueError:
            pass
    current = await services.clock()
    if current is not None and math.isfinite(current) and current >= 0:
        latest_sim_time = max(latest_sim_time, current)
        return current
    if fallback is not None and math.isfinite(fallback) and fallback >= 0:
        latest_sim_time = max(latest_sim_time, fallback)
        return fallback
    return latest_sim_time


def _request_lock(request_id: str) -> asyncio.Lock:
    lock = request_locks.get(request_id)
    if lock is None:
        lock = asyncio.Lock()
        request_locks[request_id] = lock
    return lock


def _request_remaining(
    request: dict[str, Any],
    summary: dict[str, Any],
    pending_donors: int = 0,
    open_invitations: int = 0,
) -> int:
    return max(
        0,
        int(request["units"])
        - int(summary["fulfilled"])
        - int(summary["stock_fulfilled"])
        - int(summary["stock_inflight"])
        - int(summary.get("donations_inflight", 0))
        - pending_donors
        - open_invitations,
    )


async def _travel_for_donor(donor: dict[str, str], center_id: str | None, fallback: float) -> float:
    if center_id is None:
        return fallback
    center = domain.centers.get(center_id)
    if center is None:
        return fallback
    try:
        origin = (float(donor["lat"]), float(donor["lon"]))
        destination = (float(center["lat"]), float(center["lon"]))
    except (KeyError, ValueError):
        return fallback
    travel_minutes = await services.travel_minutes(origin, destination)
    return fallback if travel_minutes is None else travel_minutes


async def _resolve_donor_center(donor_id: str, fallback_center_id: str | None) -> str | None:
    donor = domain.donors.get(donor_id)
    if donor is None:
        return fallback_center_id
    try:
        origin = (float(donor["lat"]), float(donor["lon"]))
    except (KeyError, ValueError):
        return fallback_center_id
    candidates = domain.donor_center_candidates(donor)
    if not candidates:
        return fallback_center_id
    destinations = []
    for center_id in candidates:
        center = domain.centers[center_id]
        try:
            destination = (float(center["lat"]), float(center["lon"]))
        except (KeyError, ValueError):
            return fallback_center_id
        destinations.append(destination)
    route_times = await asyncio.gather(
        *(services.travel_minutes(origin, destination) for destination in destinations)
    )
    if any(minutes is None for minutes in route_times):
        logger.warning("Travel center resolution incomplete for donor %s; retaining area-matrix center", donor_id)
        return fallback_center_id
    return min(zip(route_times, candidates), key=lambda route: route[0])[1]


def _center_supply_travel(center_id: str, hospital_id: str) -> float:
    # The simulator uses this published matrix as the ground truth for supply ETAs.
    return domain.center_travel(center_id, hospital_id)


async def _send_wave(request: dict[str, Any], now: float) -> bool:
    if not await services.gateway_healthy():
        return False
    summary = db.fetch_request_summary(request["id"])
    if summary is None:
        return False
    pending = db.get_pending_accepted_invitations(request["id"])
    open_invitations = db.get_open_invitation_count(request["id"], now, wave_gap_min)
    remaining = _request_remaining(request, summary, len(pending), open_invitations)
    if remaining <= 0:
        return False

    excluded = db.get_invited_donors(request["id"])
    reserved_donors = db.get_reserved_donor_ids(now, wave_gap_min, 120.0)
    excluded.update(reserved_donors)
    recent_counts = db.get_recent_invitation_counts(now - 90 * 1440.0)
    donor_states = db.get_all_donor_states()
    ranked = [
        candidate
        for candidate in domain.rank_donors(request, now, recent_counts, donor_states, excluded)
        if candidate[2] is not None
    ]
    batch_size = min(remaining, 12)
    candidate_count = min(len(ranked), batch_size * 2, travel_query_budget)
    routed = []
    for score, donor, center_id, fallback_minutes in ranked[:candidate_count]:
        actual_minutes = await _travel_for_donor(donor, center_id, fallback_minutes)
        adjusted = score * math.exp(-max(0.0, actual_minutes - fallback_minutes) / 45.0)
        routed.append((adjusted, donor, center_id, actual_minutes))
    routed.sort(key=lambda item: (-item[0], item[3], item[1]["donor_id"]))
    selected = routed[:batch_size]
    if not selected:
        return False

    wave = db.claim_wave(request["id"], now, wave_gap_min, max_waves)
    if wave is None:
        return False
    sent_any = False
    for _, donor, center_id, travel_minutes in selected:
        notification_id = f"NT-{uuid.uuid4().hex[:12]}"
        invitation_id = f"INV-{uuid.uuid4().hex[:12]}"
        donor_id = donor["donor_id"]
        if not db.claim_donor_commitment(donor_id, request["id"], notification_id, now, now + 120.0):
            continue
        center_hospital_minutes = _center_supply_travel(center_id, request["hospital_id"])
        db.insert_invitation(
            invitation_id,
            request["id"],
            donor_id,
            notification_id,
            "request_wave",
            now,
            center_id=center_id,
            center_hospital_minutes=center_hospital_minutes,
            processing_minutes=float(domain.rules["proc_min"]),
        )
        payload = {
            "notification_id": notification_id,
            "donor_id": donor_id,
            "template": "request_wave",
            "request_id": request["id"],
            "callback_url": settings["SELF_URL"],
        }
        status, _, _ = await services.message(payload)
        if status in (200, 202, 409):
            db.update_invitation_gateway_status(notification_id, "sent")
            db.save_audit(request["id"], "donor_invited", {"donor_id": donor_id, "wave": wave, "travel_min": round(travel_minutes, 1)}, now)
            sent_any = True
        elif status in (0, 429, 503):
            attempts = db.schedule_invitation_retry(notification_id, now + 1.0)
            db.save_audit(
                request["id"],
                "gateway_notification_retry",
                {"notification_id": notification_id, "status": status, "attempt": attempts},
                now,
            )
        else:
            db.update_invitation_gateway_status(notification_id, "failed")
            db.release_donor_commitment(donor_id, request["id"], now)
            db.save_audit(
                request["id"],
                "gateway_notification_failed",
                {"notification_id": notification_id, "status": status},
                now,
            )
    return sent_any


def _dispatch_key(request_id: str, center_id: str, blood_group: str, product: str, attempt: int) -> str:
    raw = f"{request_id}|{center_id}|{blood_group}|{product}|{attempt}".encode()
    return "DSP-" + hashlib.sha256(raw).hexdigest()[:20]


async def _send_dispatch(dispatch: dict[str, Any], request: dict[str, Any], now: float) -> bool:
    payload = {
        "dispatch_id": dispatch["dispatch_id"],
        "request_id": request["id"],
        "center_id": dispatch["center_id"],
        "blood_group": dispatch["blood_group"],
        "product": dispatch["product"],
        "units": int(dispatch["units"]),
        "hospital_id": request["hospital_id"],
        "recipient_blood_group": request["blood_group"],
    }
    status, body, _ = await services.dispatch(payload)
    if status in (200, 201):
        try:
            eta = float(body["eta_sim_min"])
            if not math.isfinite(eta) or eta < 0:
                raise ValueError("invalid ETA")
        except (KeyError, TypeError, ValueError):
            db.update_dispatch(dispatch["dispatch_id"], "created", now=now)
            db.save_audit(
                request["id"],
                "stock_dispatch_invalid_eta",
                {"dispatch_id": dispatch["dispatch_id"]},
                now,
            )
            logger.error("Inventory dispatch %s returned no valid ETA", dispatch["dispatch_id"])
            return False
        db.update_dispatch(dispatch["dispatch_id"], "in_transit", eta, now)
        db.save_audit(request["id"], "stock_dispatched", {"dispatch_id": dispatch["dispatch_id"], "units": dispatch["units"], "eta_sim_min": eta}, now)
        return True
    if status in (0, 429, 503):
        db.update_dispatch(dispatch["dispatch_id"], "created", now=now)
        db.save_audit(
            request["id"],
            "stock_dispatch_retry",
            {"dispatch_id": dispatch["dispatch_id"], "status": status},
            now,
        )
        return False
    db.update_dispatch(dispatch["dispatch_id"], "failed", now=now)
    db.save_audit(
        request["id"],
        "stock_dispatch_failed",
        {"dispatch_id": dispatch["dispatch_id"], "status": status},
        now,
    )
    return False


async def _dispatch_stock(request: dict[str, Any], now: float, items: list[dict[str, Any]] | None = None) -> bool:
    if request["status"] in {"pending_review", "cancelled", "fulfilled", "expired"}:
        return False
    existing = db.get_dispatches(request["id"])
    changed = False
    for dispatch in existing:
        if dispatch["status"] == "created":
            changed = await _send_dispatch(dispatch, request, now) or changed
            latest = db.get_dispatches(request["id"])
            if any(item["status"] == "created" for item in latest):
                return changed

    request = db.get_request(request["id"]) or request
    summary = db.fetch_request_summary(request["id"])
    pending = db.get_pending_accepted_invitations(request["id"])
    open_invitations = db.get_open_invitation_count(request["id"], now, wave_gap_min)
    remaining = _request_remaining(request, summary, len(pending), open_invitations)
    if remaining <= 0:
        return changed
    inventory = items if items is not None else await services.inventory()
    candidates = []
    for item in inventory:
        try:
            expiry = float(item["expires_sim_min"])
            units = int(item["units"])
            group = str(item["blood_group"])
            product = str(item["product"])
            center_id = str(item["center_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if expiry <= now or units <= 0 or product != request["product"]:
            continue
        if not domain.compatible(group, request["blood_group"], product):
            continue
        candidates.append((expiry, domain.center_travel(center_id, request["hospital_id"]), center_id, group, product, units))

    if not candidates:
        return changed

    best_centers = []
    for _, _, center_id, _, _, _ in sorted(candidates):
        if center_id not in best_centers:
            best_centers.append(center_id)
        if len(best_centers) >= 2:
            break
    hospital = domain.hospitals.get(request["hospital_id"])
    if hospital:
        destination = (float(hospital["lat"]), float(hospital["lon"]))
        for center_id in best_centers:
            center = domain.centers[center_id]
            route = await services.travel_minutes((float(center["lat"]), float(center["lon"])), destination)
            if route is not None:
                candidates = [
                    (expiry, route if existing_center == center_id else minutes, existing_center, group, product, units)
                    for expiry, minutes, existing_center, group, product, units in candidates
                ]

    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    picks: dict[tuple[str, str, str], dict[str, Any]] = {}
    for expiry, travel, center_id, group, product, available in candidates:
        if remaining <= 0:
            break
        key = (center_id, group, product)
        take = min(available, remaining)
        if take <= 0:
            continue
        if key not in picks:
            picks[key] = {"units": 0, "expiry": expiry, "travel": travel}
        picks[key]["units"] += take
        remaining -= take

    for (center_id, group, product), pick in picks.items():
        matching = [
            item for item in db.get_dispatches(request["id"])
            if item["center_id"] == center_id and item["blood_group"] == group and item["product"] == product
        ]
        attempt = len(matching)
        dispatch_id = _dispatch_key(request["id"], center_id, group, product, attempt)
        db.insert_dispatch(dispatch_id, request["id"], center_id, group, product, pick["units"], None, now)
        dispatch = next(item for item in db.get_dispatches(request["id"]) if item["dispatch_id"] == dispatch_id)
        changed = await _send_dispatch(dispatch, request, now) or changed
    return changed


async def _reconcile_request(request_id: str, now: float | None = None, items: list[dict[str, Any]] | None = None):
    async with _request_lock(request_id):
        request = db.get_request(request_id)
        if request is None or request["status"] in {"pending_review", "cancelled", "fulfilled", "expired"}:
            return
        current = await _now(fallback=now)
        await _dispatch_stock(request, current, items)
        request = db.get_request(request_id)
        if request is None or request["status"] in {"pending_review", "cancelled", "fulfilled", "expired"}:
            return
        summary = db.fetch_request_summary(request_id)
        if any(item["status"] == "created" for item in db.get_dispatches(request_id)):
            return
        pending = db.get_pending_accepted_invitations(request_id)
        open_invitations = db.get_open_invitation_count(request_id, current, wave_gap_min)
        if _request_remaining(request, summary, len(pending), open_invitations) <= 0:
            return
        await _send_wave(request, current)


async def _delayed_reconcile(request_id: str):
    await asyncio.sleep(0.1)
    try:
        await _reconcile_request(request_id)
    except Exception:
        logger.exception("request reconciliation failed for %s", request_id)


async def _send_stop_message(request: dict[str, Any], invitation: dict[str, Any], template: str, now: float):
    raw = f"{request['id']}|{invitation['donor_id']}|{template}".encode()
    notification_id = "NT-" + hashlib.sha256(raw).hexdigest()[:20]
    invitation_id = "INV-" + hashlib.sha256((notification_id + "|stop").encode()).hexdigest()[:20]
    db.insert_invitation(invitation_id, request["id"], invitation["donor_id"], notification_id, template, now)
    payload = {
        "notification_id": notification_id,
        "donor_id": invitation["donor_id"],
        "template": template,
        "request_id": request["id"],
        "callback_url": settings["SELF_URL"],
    }
    status, _, _ = await services.message(payload)
    if status in (200, 202, 409):
        db.update_invitation_gateway_status(notification_id, "sent")
        db.close_invitation(request["id"], invitation["donor_id"], template)


async def _stop_outreach(request_id: str, template: str):
    request = db.get_request(request_id)
    if request is None:
        return
    now = await _now(fallback=float(request["updated_sim"] or 0))
    for invitation in db.get_outstanding_invitations(request_id):
        await _send_stop_message(request, invitation, template, now)


async def _retry_gateway_notifications(now: float):
    for invitation in db.get_retryable_invitations(now):
        request = db.get_request(invitation["request_id"])
        if request is None or request["status"] in {"cancelled", "expired", "fulfilled", "pending_review"}:
            db.update_invitation_gateway_status(invitation["notification_id"], "cancelled")
            db.release_donor_commitment(invitation["donor_id"], invitation["request_id"], now)
            continue
        payload = {
            "notification_id": invitation["notification_id"],
            "donor_id": invitation["donor_id"],
            "template": invitation["template"],
            "request_id": invitation["request_id"],
            "callback_url": settings["SELF_URL"],
        }
        status, _, _ = await services.message(payload)
        if status in (200, 202, 409):
            db.update_invitation_gateway_status(invitation["notification_id"], "sent")
            db.save_audit(
                invitation["request_id"],
                "gateway_notification_recovered",
                {"notification_id": invitation["notification_id"]},
                now,
            )
        elif status in (0, 429, 503) and int(invitation["retry_attempts"]) < 3:
            attempts = db.schedule_invitation_retry(invitation["notification_id"], now + 1.0)
            db.save_audit(
                invitation["request_id"],
                "gateway_notification_retry",
                {"notification_id": invitation["notification_id"], "status": status, "attempt": attempts},
                now,
            )
        else:
            db.update_invitation_gateway_status(invitation["notification_id"], "failed")
            db.release_donor_commitment(invitation["donor_id"], invitation["request_id"], now)
            db.save_audit(
                invitation["request_id"],
                "gateway_notification_failed",
                {"notification_id": invitation["notification_id"], "status": status},
                now,
            )


async def _confirm_donor(request_id: str, donor_id: str):
    request = db.get_request(request_id)
    if request is None:
        return
    invitation = db.get_invitation(request_id, donor_id)
    if invitation is None:
        return
    now = await _now(fallback=float(request["updated_sim"] or 0))
    if request["status"] in {"cancelled", "expired", "fulfilled"}:
        template = "cancelled" if request["status"] == "cancelled" else "not_needed"
        await _send_stop_message(request, invitation, template, now)
        return
    if request["status"] not in {"open", "mobilizing", "partially_fulfilled"}:
        return
    summary = db.fetch_request_summary(request_id)
    other_pending = [
        item for item in db.get_pending_accepted_invitations(request_id)
        if item["donor_id"] != donor_id
    ]
    open_invitations = db.get_open_invitation_count(request_id, now, wave_gap_min)
    if _request_remaining(request, summary, len(other_pending), open_invitations) <= 0:
        await _send_stop_message(request, invitation, "not_needed", now)
        return
    notification_id = db.claim_confirmation(request_id, donor_id)
    if notification_id is None:
        return
    resolved_center = await _resolve_donor_center(donor_id, invitation.get("center_id"))
    if resolved_center is not None:
        db.update_invitation_route(
            request_id,
            donor_id,
            resolved_center,
            _center_supply_travel(resolved_center, request["hospital_id"]),
        )
    payload = {
        "notification_id": notification_id,
        "donor_id": donor_id,
        "template": "confirmed",
        "request_id": request_id,
        "callback_url": settings["SELF_URL"],
    }
    status, _, _ = await services.message(payload)
    if status not in (200, 202, 409):
        db.set_invitation_confirmation(request_id, donor_id, notification_id, False)
    else:
        db.save_audit(request_id, "donor_confirmed", {"donor_id": donor_id}, now)


async def _workflow_worker():
    while True:
        try:
            now = await _now()
            await _retry_gateway_notifications(now)
            delivered = set(db.mark_dispatches_delivered(now))
            delivered.update(db.mark_donor_supplies_delivered(now))
            for request_id in delivered:
                request = db.get_request(request_id)
                if request and request["status"] == "fulfilled":
                    await _stop_outreach(request_id, "not_needed")
            expired = db.expire_due_requests(now)
            for request_id in expired:
                await _stop_outreach(request_id, "not_needed")

            active = db.get_active_requests()
            due = [
                request for request in active
                if int(request["wave_count"]) == 0
                or now - float(request["last_wave_sim"]) >= wave_gap_min
            ]
            if due:
                items = await services.inventory()
                await asyncio.gather(*(_reconcile_request(request["id"], now, items) for request in due))
        except asyncio.CancelledError:
            raise
        except LocalServiceUnavailable as exc:
            logger.warning("workflow service unavailable; retrying: %s", exc)
            await asyncio.sleep(1.0)
            continue
        except Exception:
            logger.exception("workflow reconciliation failed")
        await asyncio.sleep(0.25)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global latest_sim_time
    db._init_db()
    await services.start()
    latest_sim_time = await services.clock() or latest_sim_time
    worker = asyncio.create_task(_workflow_worker(), name="bloodnet-workflow")
    try:
        yield
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await services.close()


app = FastAPI(title="BloodNet", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=static_path), name="static")


@app.get("/")
async def home():
    return FileResponse(frontend_path / "index.html")


@app.get("/health")
async def health():
    return {"ok": True, "sim_time": latest_sim_time}


@app.get("/operations/snapshot")
async def operations_snapshot(
    hospital_id: str | None = Query(default=None),
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    denied = _auth(authorization, settings["HOSPITAL_TOKEN"], "hospital")
    if denied:
        return denied
    if hospital_id is not None and hospital_id not in domain.hospitals:
        return _error("unknown_hospital", "unregistered hospital_id", 400)
    try:
        inventory, gateway_available = await asyncio.gather(
            services.inventory(),
            services.gateway_healthy(),
        )
    except LocalServiceUnavailable as exc:
        logger.warning("operations snapshot unavailable: %s", exc)
        return _error("service_unavailable", str(exc), 503, {"Retry-After": "2"})

    requests = (
        db.list_requests_for_hospital(hospital_id)
        if hospital_id is not None
        else [
            row
            for registered_hospital in domain.hospitals
            for row in db.list_requests_for_hospital(registered_hospital)
        ]
    )
    requests.sort(key=lambda row: float(row["created_sim"]), reverse=True)
    return {
        "sim_time": latest_sim_time,
        "services": {"api": True, "gateway": gateway_available, "inventory": True},
        "requests": [db.fetch_request_summary(row["id"]) for row in requests[:100]],
        "inventory": inventory,
        "dispatches": db.list_dispatches(),
        "invitations": db.list_invitations(),
        "events": db.list_recent_audit(),
    }


@app.get("/hospitals")
async def list_hospitals():
    return {"hospitals": [{"hospital_id": row["hospital_id"], "name": row["name"]} for row in domain.hospital_rows]}


@app.post("/requests")
async def create_request(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None, alias="Authorization"),
    idem: str | None = Header(default=None, alias="Idempotency-Key"),
    sim_header: str | None = Header(default=None, alias="X-Sim-Time"),
):
    denied = _auth(authorization, settings["HOSPITAL_TOKEN"], "hospital")
    if denied:
        return denied
    if not idem or not idem.strip() or len(idem) > 128:
        return _error("missing_idempotency_key", "Idempotency-Key must contain 1 to 128 characters", 400)
    parsed = _parse(NewRequest, payload)
    if isinstance(parsed, JSONResponse):
        return parsed
    if parsed.hospital_id not in domain.hospitals:
        return _error("unknown_hospital", "unregistered hospital_id", 400)

    now = await _now(sim_header)
    body_hash = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    request_id = f"R-{uuid.uuid4().hex[:12]}"
    risk_reasons = []
    demand_reason = domain.demand_review_reason(parsed.product, parsed.units)
    if demand_reason:
        risk_reasons.append(demand_reason)
    recent_requests = db.count_recent_hospital_requests(
        parsed.hospital_id,
        now - settings["ABUSE_BURST_WINDOW_MINUTES"],
        now,
    )
    if recent_requests >= settings["ABUSE_BURST_REQUESTS"]:
        risk_reasons.append("hospital_request_burst")
    suspicious = bool(risk_reasons)
    status = "pending_review" if suspicious else "open"
    request_row = {
        "id": request_id,
        "hospital_id": parsed.hospital_id,
        "blood_group": parsed.blood_group,
        "product": parsed.product,
        "units": parsed.units,
        "urgency": parsed.urgency,
        "status": status,
        "note": parsed.note,
        "approved": int(not suspicious),
        "created_sim": now,
        "deadline_sim": now + SLA[parsed.urgency],
        "review_reason": ",".join(risk_reasons) or None,
        "request_data": payload,
    }
    try:
        result, stored_request_id, stored_hash = db.create_idempotent_request(request_row, idem, body_hash)
    except Exception:
        logger.exception("request persistence failed")
        return _error("temporarily_unavailable", "request could not be persisted; retry with the same key", 503, {"Retry-After": "1"})
    if result == "existing":
        if stored_hash != body_hash:
            return _error("idempotency_key_reuse", "same key, different body", 422)
        existing = db.get_request(stored_request_id)
        if existing is None:
            return _error("temporarily_unavailable", "idempotent request record is unavailable", 503, {"Retry-After": "1"})
        return JSONResponse(status_code=200, content={"request_id": existing["id"], "status": existing["status"], "needed": existing["units"]})

    db.save_audit(request_id, "request_created", {"status": status, "review_reason": request_row["review_reason"]}, now)
    if status == "open":
        background_tasks.add_task(_delayed_reconcile, request_id)
    return JSONResponse(status_code=201, content={"request_id": request_id, "status": status, "needed": parsed.units})


@app.get("/requests")
async def list_requests(
    hospital_id: str | None = Query(default=None),
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    denied = _auth(authorization, settings["HOSPITAL_TOKEN"], "hospital")
    if denied:
        return denied
    if hospital_id is not None and hospital_id not in domain.hospitals:
        return _error("unknown_hospital", "unregistered hospital_id", 400)
    if hospital_id is not None:
        rows = db.list_requests_for_hospital(hospital_id)
    else:
        rows = []
        for hospital in domain.hospitals:
            rows.extend(db.list_requests_for_hospital(hospital))
        rows.sort(key=lambda row: float(row["created_sim"]), reverse=True)
    return {"requests": [db.fetch_request_summary(row["id"]) for row in rows if row]}


@app.get("/requests/{request_id}")
async def get_request(request_id: str, authorization: str | None = Header(default=None, alias="Authorization")):
    denied = _auth(authorization, settings["HOSPITAL_TOKEN"], "hospital")
    if denied:
        return denied
    summary = db.fetch_request_summary(request_id)
    if summary is None:
        return _error("not_found", "request not found", 404)
    return summary


@app.post("/requests/{request_id}/cancel")
async def cancel_request(
    request_id: str,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None, alias="Authorization"),
    sim_header: str | None = Header(default=None, alias="X-Sim-Time"),
):
    denied = _auth(authorization, settings["HOSPITAL_TOKEN"], "hospital")
    if denied:
        return denied
    request = db.get_request(request_id)
    if request is None:
        return _error("not_found", "request not found", 404)
    now = await _now(sim_header, float(request["updated_sim"] or 0))
    if request["status"] not in {"cancelled", "fulfilled", "expired"}:
        changed = db.transition_request(
            request_id,
            {"pending_review", "open", "mobilizing", "partially_fulfilled"},
            {"status": "cancelled", "cancelled_sim": now, "updated_sim": now},
        )
        if changed:
            db.save_audit(request_id, "request_cancelled", {}, now)
            background_tasks.add_task(_stop_outreach, request_id, "cancelled")
    current = db.get_request(request_id)
    return {"request_id": request_id, "status": current["status"] if current else "cancelled"}


@app.post("/requests/{request_id}/approve")
async def approve_request(
    request_id: str,
    background_tasks: BackgroundTasks,
    payload: dict[str, Any] | None = None,
    authorization: str | None = Header(default=None, alias="Authorization"),
    sim_header: str | None = Header(default=None, alias="X-Sim-Time"),
):
    denied = _auth(authorization, settings["COORDINATOR_TOKEN"], "coordinator")
    if denied:
        return denied
    request = db.get_request(request_id)
    if request is None:
        return _error("not_found", "request not found", 404)
    if request["status"] != "pending_review":
        return _error("not_pending_review", "request is not pending review", 409)
    parsed = _parse(ApprovalPayload, payload or {})
    if isinstance(parsed, JSONResponse):
        return parsed
    now = await _now(sim_header, float(request["created_sim"] or 0))
    next_status = "open" if parsed.approved else "cancelled"
    updates = {"status": next_status, "approved": int(parsed.approved), "updated_sim": now}
    if not parsed.approved:
        updates["cancelled_sim"] = now
    changed = db.transition_request(request_id, {"pending_review"}, updates)
    if not changed:
        current = db.get_request(request_id)
        return _error("not_pending_review", f"request is {current['status'] if current else 'unavailable'}", 409)
    db.save_audit(request_id, "request_reviewed", {"approved": parsed.approved, "note": parsed.note}, now)
    if parsed.approved:
        background_tasks.add_task(_delayed_reconcile, request_id)
    return {"request_id": request_id, "status": next_status}


@app.post("/requests/{request_id}/amend")
async def amend_request(
    request_id: str,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None, alias="Authorization"),
    sim_header: str | None = Header(default=None, alias="X-Sim-Time"),
):
    denied = _auth(authorization, settings["HOSPITAL_TOKEN"], "hospital")
    if denied:
        return denied
    request = db.get_request(request_id)
    if request is None:
        return _error("not_found", "request not found", 404)
    parsed = _parse(AmendRequest, payload)
    if isinstance(parsed, JSONResponse):
        return parsed
    if request["status"] not in {"open", "mobilizing", "partially_fulfilled"}:
        return _error("not_amendable", f"request is {request['status']}", 409)
    now = await _now(sim_header, float(request["updated_sim"] or 0))
    deadline = float(request["created_sim"]) + SLA[parsed.urgency]
    changed = db.transition_request(
        request_id,
        {"open", "mobilizing", "partially_fulfilled"},
        {"urgency": parsed.urgency, "deadline_sim": deadline, "updated_sim": now},
    )
    if not changed:
        current = db.get_request(request_id)
        return _error("not_amendable", f"request is {current['status'] if current else 'unavailable'}", 409)
    db.save_audit(request_id, "request_amended", {"urgency": parsed.urgency, "deadline_sim": deadline}, now)
    return {"request_id": request_id, "urgency": parsed.urgency}


@app.post("/callbacks/donor-response")
async def donor_response(payload: dict[str, Any], background_tasks: BackgroundTasks):
    parsed = _parse(DonorResponseCallback, payload)
    if isinstance(parsed, JSONResponse):
        return parsed
    callback = parsed.model_dump()
    inserted, invitation, _ = db.apply_donor_response(callback, callback["sim_time"])
    if inserted and invitation and callback["response"] == "accepted":
        background_tasks.add_task(_confirm_donor, callback["request_id"], callback["donor_id"])
    elif inserted and callback["response"] == "declined":
        request = db.get_request(callback["request_id"])
        if request and request["status"] not in {"pending_review", "cancelled", "fulfilled", "expired"}:
            background_tasks.add_task(_delayed_reconcile, callback["request_id"])
    return {"ok": True}


@app.post("/callbacks/donor-progress")
async def donor_progress(payload: dict[str, Any], background_tasks: BackgroundTasks):
    parsed = _parse(DonorProgressCallback, payload)
    if isinstance(parsed, JSONResponse):
        return parsed
    callback = parsed.model_dump()
    inserted, invitation, donated = db.apply_donor_progress(callback, callback["sim_time"])
    if inserted and invitation:
        request = db.get_request(callback["request_id"])
        if callback["event"] in {"no_show", "screen_fail"} and request and request["status"] not in {"cancelled", "fulfilled", "expired", "pending_review"}:
            background_tasks.add_task(_delayed_reconcile, callback["request_id"])
        if donated and request and request["status"] == "fulfilled":
            background_tasks.add_task(_stop_outreach, callback["request_id"], "not_needed")
    return {"ok": True}


@app.post("/callbacks/gateway-status")
async def gateway_status(payload: dict[str, Any]):
    parsed = _parse(GatewayStatusCallback, payload)
    if isinstance(parsed, JSONResponse):
        return parsed
    db.apply_gateway_status(parsed.model_dump(), parsed.sim_time)
    return {"ok": True}