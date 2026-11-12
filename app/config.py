import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _get_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is not None:
        return value
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip() and "=" in line and not line.strip().startswith("#"):
                key, val = line.split("=", 1)
                if key.strip() == name:
                    return val.strip()
    return default


settings = {
    "PORT": int(_get_env("PORT", "8000") or "8000"),
    "SELF_URL": (_get_env("SELF_URL") or "http://127.0.0.1:8000").rstrip("/"),
    "GATEWAY_URL": (_get_env("GATEWAY_URL") or "http://localhost:9100").rstrip("/"),
    "TRAVEL_URL": (_get_env("TRAVEL_URL") or "http://localhost:9200").rstrip("/"),
    "INVENTORY_URL": (_get_env("INVENTORY_URL") or "http://localhost:9300").rstrip("/"),
    "HOSPITAL_TOKEN": _get_env("HOSPITAL_TOKEN", "hospital-token") or "hospital-token",
    "COORDINATOR_TOKEN": _get_env("COORDINATOR_TOKEN", "coordinator-token") or "coordinator-token",
    "ABUSE_RARE_DEMAND_TAIL": float(_get_env("ABUSE_RARE_DEMAND_TAIL", "0.02") or "0.02"),
    "ABUSE_BURST_WINDOW_MINUTES": float(_get_env("ABUSE_BURST_WINDOW_MINUTES", "5") or "5"),
    "ABUSE_BURST_REQUESTS": int(_get_env("ABUSE_BURST_REQUESTS", "5") or "5"),
    "KIT_PATH": str((BASE_DIR / _get_env("KIT_PATH", "participant_kit")).resolve()),
    "DB_PATH": str((BASE_DIR / (_get_env("DB_PATH", "bloodnet.db") or "bloodnet.db")).resolve()),
}

BLOOD_GROUPS = ["O+", "A+", "B+", "AB+", "O-", "A-", "B-", "AB-"]
PRODUCTS = ["red_cells", "platelets", "plasma"]
URGENCIES = ["critical", "urgent", "planned"]
STATUSES = ["pending_review", "open", "mobilizing", "partially_fulfilled", "fulfilled", "cancelled", "expired"]
SLA = {"critical": 90.0, "urgent": 240.0, "planned": 1440.0}

VALID_HOSPITALS = set()
VALID_DONORS = {}
INVENTORY_ITEMS = []
COMPATIBILITY = {}
