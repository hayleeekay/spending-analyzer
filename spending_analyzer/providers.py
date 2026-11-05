"""Detect and normalize supported bank CSV exports."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
import re
from typing import Any, Iterable

from .constants import DEFAULT_SETTINGS, PROVIDER_HEADERS, PROVIDER_LABELS
from .storage import money, parse_date, stable_hash

def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def clean_identifier_value(provider: str, value: str) -> str:
    value = clean_text(value).upper()
    if provider == "amex":
        value = clean_text(re.sub(r"\bAPLPAY\b", " ", value))
    if provider == "discover":
        value = re.sub(r"APPLE PAY ENDING IN \d+.*$", "", value).strip()
    if provider == "chase_checking":
        value = re.sub(r"\s+\d{2}/\d{2}$", "", value)
        value = re.sub(r"\b(?:TRACE|ID|REF)[: #]*\d+\b", "", value)
        value = clean_text(value)
    return value or "(BLANK IDENTIFIER)"


def clean_identifier(provider: str, row: dict[str, str]) -> str:
    if provider == "apple":
        value = row.get("Merchant") or row.get("Description", "")
    elif provider == "amex":
        value = row.get("Appears On Your Statement As") or row.get("Description", "")
    else:
        value = row.get("Description", "")
    return clean_identifier_value(provider, value)


def account_id(provider: str, filename: str) -> str:
    if provider.startswith("chase"):
        match = re.search(r"Chase(\d{4})", filename, re.IGNORECASE)
        if match:
            return f"{provider}-{match.group(1)}"
    return provider


def account_label(transaction: dict[str, Any]) -> str:
    account = transaction["account"]
    provider = transaction["provider"]
    match = re.search(r"-(\d{4})$", account)
    return f"{PROVIDER_LABELS[provider]} {match.group(1)}" if match else PROVIDER_LABELS[provider]


def detect_provider(headers: Iterable[str]) -> str:
    header_set = {header.strip() for header in headers if header is not None}
    matches = [name for name, required in PROVIDER_HEADERS.items() if required <= header_set]
    if not matches:
        tested_formats = "; ".join(
            f"{PROVIDER_LABELS[name]} requires {', '.join(sorted(required))}"
            for name, required in PROVIDER_HEADERS.items()
        )
        raise ValueError(
            f"Unrecognized CSV layout. Received headers: {', '.join(sorted(header_set))}. "
            f"Tested layouts: {tested_formats}."
        )
    if len(matches) > 1:
        raise ValueError(f"CSV headers match more than one layout: {', '.join(matches)}")
    return matches[0]


def classify_kind(
    provider: str, row: dict[str, str], raw_amount: Decimal,
    settings: dict[str, Any] | None = None,
) -> str:
    description = clean_text(row.get("Description", "")).upper()
    category = clean_text(row.get("Category", "")).upper()
    tx_type = clean_text(row.get("Type", "")).upper()
    details = clean_text(row.get("Details", "")).upper()

    if provider == "apple":
        return "expense" if tx_type == "PURCHASE" else "card_payment" if tx_type == "PAYMENT" else "unknown"
    if provider == "chase_credit":
        if tx_type == "SALE":
            return "expense"
        if tx_type in {"RETURN", "REFUND"}:
            return "refund"
        if tx_type == "PAYMENT":
            return "card_payment"
        return "unknown"
    if provider == "discover":
        if raw_amount > 0:
            return "expense"
        if "PAYMENT" in description or "PAYMENTS AND CREDITS" in category:
            return "card_payment"
        return "refund"
    if provider == "amex":
        if raw_amount > 0:
            return "expense"
        if "PAYMENT" in description or "THANK YOU" in description:
            return "card_payment"
        return "refund"
    if provider == "chase_checking":
        if raw_amount < 0:
            if tx_type == "LOAN_PMT" or any(token in description for token in (
                "PAYMENT TO CHASE CARD", "APPLE CARD", "APPLECARD", "DISCOVER", "AMEX", "AMERICAN EXPRESS"
            )):
                return "card_payment"
            if tx_type in {"CHASE_TO_PARTNERFI"} or "TRANSFER TO" in description:
                return "transfer"
            return "expense"
        if raw_amount > 0:
            policy = DEFAULT_SETTINGS["incoming_checking_kinds"] | (
                (settings or {}).get("incoming_checking_kinds", {})
            )
            if tx_type in {"QUICKPAY_CREDIT", "PARTNERFI_TO_CHASE"} or "ZELLE" in description:
                return policy["p2p"]
            if tx_type in {"DEPOSIT", "MISC_CREDIT"} or details == "DSLIP" or "DEPOSIT" in description:
                return policy["deposit"]
            if tx_type == "ACH_CREDIT":
                return policy["ach_credit"]
            if tx_type in {"DEBIT_CARD", "ACH_DEBIT", "MISC_DEBIT"}:
                return "refund"
            return policy["other_credit"]
    return "unknown"


def normalize_row(
    provider: str, account: str, row: dict[str, str], source_name: str,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    amount_key = "Amount (USD)" if provider == "apple" else "Amount"
    date_key = {
        "apple": "Transaction Date",
        "chase_credit": "Transaction Date",
        "discover": "Trans. Date",
        "amex": "Date",
        "chase_checking": "Posting Date",
    }[provider]
    post_key = {
        "apple": "Clearing Date",
        "chase_credit": "Post Date",
        "discover": "Post Date",
        "amex": "Date",
        "chase_checking": "Posting Date",
    }[provider]
    raw_amount = money(row[amount_key])
    kind = classify_kind(provider, row, raw_amount, settings)
    identifier = clean_identifier(provider, row)
    reference = clean_text(row.get("Reference", "") or row.get("Check or Slip #", "") or row.get("Memo", ""))
    transaction = {
        "provider": provider,
        "account": account,
        "date": parse_date(row[date_key]),
        "post_date": parse_date(row[post_key]),
        "description": clean_text(row.get("Description", "") or identifier),
        "identifier": identifier,
        "raw_amount": f"{raw_amount:.2f}",
        "kind": kind,
        "reference": reference,
        "source_files": [source_name],
        "bank_category": clean_text(row.get("Category", "")),
        "bank_type": clean_text(row.get("Type", "")),
    }
    return transaction


def transaction_base_key(transaction: dict[str, Any]) -> str:
    return stable_hash(
        transaction["account"], transaction["date"], transaction["post_date"],
        transaction["description"].upper(), transaction["raw_amount"],
        transaction["kind"], transaction["reference"]
    )


def read_csv(path: Path) -> tuple[str, list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")
        provider = detect_provider(reader.fieldnames)
        rows = [{key: (value or "") for key, value in row.items() if key is not None} for row in reader]
    return provider, rows


def unique_destination(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / name
    counter = 2
    while candidate.exists():
        candidate = directory / f"{Path(name).stem}_{counter}{Path(name).suffix}"
        counter += 1
    return candidate
