"""JSON persistence, hashing, dates, and monetary values."""

from __future__ import annotations

from copy import deepcopy
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any

from .constants import DEFAULT_SETTINGS

def money(value: str | int | float | Decimal) -> Decimal:
    try:
        return Decimal(str(value).replace("$", "").replace(",", "").strip()).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid monetary amount: {value!r}") from exc


def parse_date(value: str) -> str:
    value = value.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"Unsupported date: {value!r}")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_settings(path: Path) -> dict[str, Any]:
    """Load user settings and fill in defaults added by newer versions."""
    saved = load_json(path, {})
    if not isinstance(saved, dict):
        raise ValueError(f"Settings must be a JSON object: {path}")

    settings = deepcopy(DEFAULT_SETTINGS)
    settings.update(saved)
    incoming = DEFAULT_SETTINGS["incoming_checking_kinds"] | saved.get("incoming_checking_kinds", {})
    allowed_incoming_kinds = {"income", "reimbursement"}
    invalid = {name: value for name, value in incoming.items() if value not in allowed_incoming_kinds}
    if invalid:
        raise ValueError(
            "incoming_checking_kinds values must be 'income' or 'reimbursement': "
            + ", ".join(f"{name}={value!r}" for name, value in invalid.items())
        )
    settings["incoming_checking_kinds"] = incoming
    for key in ("always_ask_person_categories", "personless_categories", "other_people_categories"):
        if not isinstance(settings.get(key), list) or not all(isinstance(item, str) for item in settings[key]):
            raise ValueError(f"{key} must be a JSON list of category names")
    if not isinstance(settings.get("default_person"), str):
        raise ValueError("default_person must be text")
    return settings


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def load_year_shards(directory: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if directory.exists():
        for path in sorted(directory.glob("[0-9][0-9][0-9][0-9].json")):
            records.extend(load_json(path, []))
    return records


def load_decision_shards(directory: Path) -> dict[str, Any]:
    decisions: dict[str, Any] = {}
    if directory.exists():
        for path in sorted(directory.glob("[0-9][0-9][0-9][0-9].json")):
            decisions.update(load_json(path, {}))
    return decisions


def save_year_shards(directory: Path, records: list[dict[str, Any]]) -> None:
    by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_year[record["date"][:4]].append(record)
    for year, year_records in by_year.items():
        save_json(directory / f"{year}.json", year_records)


def save_decision_shards(directory: Path, decisions: dict[str, Any], transactions: list[dict[str, Any]]) -> None:
    year_by_id = {transaction["id"]: transaction["date"][:4] for transaction in transactions}
    by_year: dict[str, dict[str, Any]] = defaultdict(dict)
    for transaction_id, decision in decisions.items():
        year = year_by_id.get(transaction_id)
        if year:
            by_year[year][transaction_id] = decision
    for year, year_decisions in by_year.items():
        save_json(directory / f"{year}.json", year_decisions)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_hash(*parts: Any) -> str:
    value = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
