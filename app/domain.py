import csv
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


SNAPSHOT = datetime(2027, 1, 15)
DONATION_TYPE = {"red_cells": "whole_blood", "platelets": "platelets", "plasma": "plasma"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


class BloodNetDomain:
    def __init__(self, kit_path: str | Path, rare_demand_tail: float = 0.02):
        self.kit_path = Path(kit_path)
        self.donor_rows = _read_csv(self.kit_path / "donors.csv")
        self.hospital_rows = _read_csv(self.kit_path / "hospitals.csv")
        self.center_rows = _read_csv(self.kit_path / "centers.csv")
        self.donors = {row["donor_id"]: row for row in self.donor_rows}
        self.hospitals = {row["hospital_id"]: row for row in self.hospital_rows}
        self.centers = {row["center_id"]: row for row in self.center_rows}
        self.rules = yaml.safe_load((self.kit_path / "rules.yaml").read_text(encoding="utf-8"))
        compatibility = yaml.safe_load((self.kit_path / "compat.yaml").read_text(encoding="utf-8"))
        self.compatibility = {
            product: {recipient: set(donors) for recipient, donors in recipient_map.items()}
            for product, recipient_map in compatibility.items()
        }
        self.deferrals = self._load_latest_deferrals()
        self.area_center = self._load_travel("travel_area_to_center.csv", "area_id")
        self.area_hospital = self._load_travel("travel_area_to_hospital.csv", "area_id")
        self.center_hospital = self._load_travel("travel_center_to_hospital.csv", "center_id")
        self.global_outcomes, self.donor_outcomes = self._load_history()
        self.demand_review_thresholds = self._load_demand_review_thresholds(rare_demand_tail)

    def _load_demand_review_thresholds(self, tail_fraction: float) -> dict[str, int]:
        if not 0 < tail_fraction < 1:
            raise ValueError("rare_demand_tail must be between 0 and 1")
        counts: dict[str, list[int]] = defaultdict(list)
        for row in _read_csv(self.kit_path / "history" / "requests.csv"):
            try:
                counts[row["product"]].append(int(row["units"]))
            except (KeyError, TypeError, ValueError):
                continue
        thresholds = {}
        for product, units in counts.items():
            total = len(units)
            threshold = max(units) + 1
            for value in sorted(set(units), reverse=True):
                if sum(unit >= value for unit in units) / total <= tail_fraction:
                    threshold = value
            thresholds[product] = threshold
        return thresholds

    def demand_review_reason(self, product: str, units: int) -> str | None:
        threshold = self.demand_review_thresholds.get(product)
        if threshold is not None and units >= threshold:
            return "rare_historical_demand"
        return None

    def _load_travel(self, filename: str, row_key: str) -> dict[tuple[str, str], float]:
        output = {}
        for row in _read_csv(self.kit_path / filename):
            origin = row[row_key]
            for destination, raw_minutes in row.items():
                if destination == row_key or not raw_minutes:
                    continue
                try:
                    output[(origin, destination)] = float(raw_minutes)
                except ValueError:
                    continue
        return output

    def _load_latest_deferrals(self) -> dict[str, datetime]:
        latest = {}
        for row in _read_csv(self.kit_path / "donor_events.csv"):
            donor_id = row["donor_id"]
            event_at = datetime.fromisoformat(row["event_at"])
            if donor_id not in latest or event_at >= latest[donor_id][0]:
                latest[donor_id] = (event_at, datetime.fromisoformat(row["defer_until"]))
        return {donor_id: dates[1] for donor_id, dates in latest.items()}

    def _load_history(self):
        request_by_id = {row["rid"]: row for row in _read_csv(self.kit_path / "history" / "requests.csv")}
        global_outcomes = defaultdict(lambda: [0, 0])
        donor_outcomes = defaultdict(lambda: [0, 0])
        for row in _read_csv(self.kit_path / "history" / "responses.csv"):
            request = request_by_id.get(row["request_id"])
            if request is None:
                continue
            key = (request["product"], request["urgency"])
            donated = int(row["donated"] or 0)
            global_outcomes[key][0] += 1
            global_outcomes[key][1] += donated
            donor_key = (row["donor_id"], *key)
            donor_outcomes[donor_key][0] += 1
            donor_outcomes[donor_key][1] += donated
        return dict(global_outcomes), dict(donor_outcomes)

    def compatible(self, donor_group: str, recipient_group: str, product: str) -> bool:
        return donor_group in self.compatibility.get(product, {}).get(recipient_group, set())

    def donor_travel(self, donor: dict[str, str], hospital_id: str) -> tuple[str | None, float]:
        area_id = donor.get("area_id", "")
        candidates = [
            (minutes, center_id)
            for (area, center_id), minutes in self.area_center.items()
            if area == area_id and center_id in self.centers
        ]
        if candidates:
            minutes, center_id = min(candidates)
            return center_id, minutes
        fallback = self.area_hospital.get((area_id, hospital_id), 9999.0)
        return None, fallback

    def donor_center_candidates(self, donor: dict[str, str], limit: int = 3) -> list[str]:
        area_id = donor.get("area_id", "")
        candidates = sorted(
            (minutes, center_id)
            for (area, center_id), minutes in self.area_center.items()
            if area == area_id and center_id in self.centers
        )
        return [center_id for _, center_id in candidates[:limit]]

    def center_travel(self, center_id: str, hospital_id: str) -> float:
        return self.center_hospital.get((center_id, hospital_id), 9999.0)

    @staticmethod
    def _sim_datetime(sim_time: float) -> datetime:
        return SNAPSHOT + timedelta(minutes=max(0.0, sim_time))

    def eligible(self, donor: dict[str, str], product: str, sim_time: float, donor_state: dict[str, Any] | None = None) -> bool:
        try:
            age = int(donor["age"])
            weight = float(donor["weight_kg"])
            if not (int(self.rules["age_min"]) <= age <= int(self.rules["age_max"])):
                return False
            if weight < float(self.rules["weight_min_kg"]) or donor.get("permanently_excluded") == "1":
                return False

            current = self._sim_datetime(sim_time)
            deferred_until = self.deferrals.get(donor["donor_id"])
            if deferred_until is not None and current < deferred_until:
                return False

            last_type = donor.get("last_donation_type", "")
            last_sim = None
            if donor_state and donor_state.get("last_donation_sim") is not None:
                last_sim = float(donor_state["last_donation_sim"])
                last_type = str(donor_state.get("last_donation_type") or last_type)
                if last_type in DONATION_TYPE:
                    last_type = DONATION_TYPE[last_type]
            if last_sim is not None:
                elapsed_days = (sim_time - last_sim) / 1440.0
            elif donor.get("last_donation_at"):
                last_donation = datetime.fromisoformat(donor["last_donation_at"])
                elapsed_days = (current - last_donation).total_seconds() / 86400.0
            else:
                elapsed_days = math.inf

            if last_type:
                target_type = DONATION_TYPE[product]
                if last_type == target_type:
                    if product == "red_cells":
                        required_days = int(self.rules["interval_wb_female" if donor.get("sex") == "F" else "interval_wb_male"])
                    else:
                        required_days = int(self.rules["interval_apheresis"])
                else:
                    required_days = int(self.rules["cross_block"])
                if elapsed_days < required_days:
                    return False

            return True
        except (KeyError, TypeError, ValueError, OverflowError):
            return False

    def quiet_now(self, donor: dict[str, str], sim_time: float) -> bool:
        try:
            start = int(donor.get("quiet_start_hour", -1))
            end = int(donor.get("quiet_end_hour", -1))
            if start < 0 or end < 0 or start == end:
                return False
            hour = int((sim_time % 1440.0) // 60)
            if start < end:
                return start <= hour < end
            return hour >= start or hour < end
        except (TypeError, ValueError):
            return False

    def success_probability(self, donor_id: str, product: str, urgency: str) -> float:
        key = (product, urgency)
        notifications, donations = self.global_outcomes.get(key, (0, 0))
        prior = (donations + 1.0) / (notifications + 20.0) if notifications else 0.05
        donor_notifications, donor_donations = self.donor_outcomes.get((donor_id, *key), (0, 0))
        return (donor_donations + 20.0 * prior) / (donor_notifications + 20.0)

    def rank_donors(
        self,
        request: dict[str, Any],
        sim_time: float,
        recently_notified: dict[str, int] | None = None,
        donor_states: dict[str, dict[str, Any]] | None = None,
        excluded: set[str] | None = None,
    ) -> list[tuple[float, dict[str, str], str | None, float]]:
        recently_notified = recently_notified or {}
        donor_states = donor_states or {}
        excluded = excluded or set()
        candidates = []
        for donor in self.donor_rows:
            donor_id = donor["donor_id"]
            if donor_id in excluded or self.quiet_now(donor, sim_time):
                continue
            if not self.compatible(donor["blood_group"], request["blood_group"], request["product"]):
                continue
            if not self.eligible(donor, request["product"], sim_time, donor_states.get(donor_id)):
                continue
            center_id, travel_minutes = self.donor_travel(donor, request["hospital_id"])
            if travel_minutes > 180:
                continue
            fatigue = int(donor.get("notified_90d", 0)) + int(recently_notified.get(donor_id, 0))
            probability = self.success_probability(donor_id, request["product"], request["urgency"])
            score = probability * math.exp(-travel_minutes / 45.0) / (1.0 + 0.12 * fatigue)
            candidates.append((score, donor, center_id, travel_minutes))
        candidates.sort(key=lambda item: (-item[0], item[3], item[1]["donor_id"]))
        return candidates