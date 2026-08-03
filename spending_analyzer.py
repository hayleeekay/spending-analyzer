#!/usr/bin/env python3
"""Local, deterministic spending import and reporting tool."""

from __future__ import annotations

import argparse
from copy import deepcopy
import csv
import hashlib
import html
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable


__version__ = "1.0.0"

CATEGORIES = [
    "Groceries",
    "Dining",
    "Gas",
    "Transportation",
    "Shopping",
    "Gifts",
    "Activities",
    "Bills",
    "Cash",
    "Other",
    "Income",
    "Uncategorized",
]

DEFAULT_SETTINGS = {
    "default_person": "",
    "always_ask_person_categories": ["Bills"],
    "personless_categories": ["Income"],
    "other_people_categories": [],
    "incoming_checking_kinds": {
        "p2p": "reimbursement",
        "deposit": "reimbursement",
        "ach_credit": "income",
        "other_credit": "reimbursement",
    },
}

PROVIDER_HEADERS = {
    "apple": {"Transaction Date", "Clearing Date", "Merchant", "Type", "Amount (USD)"},
    "chase_credit": {"Transaction Date", "Post Date", "Description", "Type", "Amount", "Memo"},
    "discover": {"Trans. Date", "Post Date", "Description", "Amount", "Category"},
    "amex": {"Date", "Description", "Amount", "Extended Details", "Appears On Your Statement As", "Reference"},
    "chase_checking": {"Details", "Posting Date", "Description", "Amount", "Type", "Balance", "Check or Slip #"},
}

PROVIDER_LABELS = {
    "apple": "Apple Card",
    "chase_credit": "Chase credit",
    "discover": "Discover",
    "amex": "Amex",
    "chase_checking": "Chase checking",
}

SUPPORTED_PROVIDERS = tuple(PROVIDER_HEADERS)


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


def apply_rules_and_decisions(transactions: list[dict[str, Any]], rules: list[dict[str, str]], decisions: dict[str, Any]) -> None:
    for transaction in transactions:
        if "original_kind" not in transaction:
            transaction["original_kind"] = transaction["kind"]
        transaction["kind"] = transaction["original_kind"]
        decision = decisions.get(transaction["id"], {})
        matching_rule = best_matching_rule(transaction, rules)
        kind_override = decision.get("kind_override") or (
            matching_rule.get("kind_override", "") if matching_rule else ""
        )
        if kind_override:
            transaction["kind"] = kind_override
        if decision.get("category"):
            transaction["category"] = decision["category"]
        elif transaction["kind"] in {"expense", "refund"} and matching_rule:
            transaction["category"] = matching_rule["category"]
        else:
            transaction["category"] = "Uncategorized"
        transaction["reviewed"] = bool(decision.get("reviewed") or (
            matching_rule.get("reviewed") if matching_rule else False
        ))
        transaction["splits"] = decision.get("splits", []) if transaction["category"] == "Split" else []
        if transaction["kind"] in {"expense", "refund"} and transaction["category"] != "Uncategorized":
            transaction["responsibility"] = decision.get("responsibility") or (
                matching_rule.get("responsibility", "") if matching_rule else ""
            )
        else:
            transaction["responsibility"] = ""


def rule_pattern(rule: dict[str, str]) -> str:
    return (rule.get("pattern") or rule.get("identifier") or "").upper()


def rule_matches(transaction: dict[str, Any], rule: dict[str, str]) -> bool:
    if rule.get("account") != transaction["account"] or rule.get("provider") != transaction["provider"]:
        return False
    candidate = transaction["identifier"].upper()
    pattern = rule_pattern(rule)
    match_type = rule.get("match_type", "exact")
    if not pattern:
        return False
    if match_type == "exact":
        return candidate == pattern
    if match_type == "starts_with":
        return candidate.startswith(pattern)
    if match_type == "contains":
        return pattern in candidate
    if match_type == "contains_words":
        candidate_words = set(re.findall(r"[A-Z0-9]+", candidate))
        pattern_words = set(re.findall(r"[A-Z0-9]+", pattern))
        return bool(pattern_words) and pattern_words <= candidate_words
    return False


def best_matching_rule(transaction: dict[str, Any], rules: list[dict[str, str]]) -> dict[str, str] | None:
    priority = {"exact": 4, "starts_with": 3, "contains": 2, "contains_words": 1}
    matches = [rule for rule in rules if rule_matches(transaction, rule)]
    if not matches:
        return None
    return max(matches, key=lambda rule: (priority.get(rule.get("match_type", "exact"), 0), len(rule_pattern(rule))))


def migrate_legacy_rules(rules: list[dict[str, str]], transactions: list[dict[str, Any]]) -> int:
    accounts_by_provider: dict[str, set[str]] = defaultdict(set)
    for transaction in transactions:
        accounts_by_provider[transaction["provider"]].add(transaction["account"])
    migrated = 0
    for rule in rules:
        if rule.get("account"):
            continue
        accounts = accounts_by_provider.get(rule.get("provider", ""), set())
        if len(accounts) == 1:
            rule["account"] = next(iter(accounts))
            migrated += 1
    return migrated


def add_custom_category(
    categories: list[str], save_callback: Callable[[], None] | None = None,
    personless_categories: set[str] | None = None,
) -> str | None:
    personless_categories = personless_categories if personless_categories is not None else set()
    name = clean_text(input("New category name (Enter cancels): "))
    if not name:
        return None
    existing = next((category for category in categories if category.casefold() == name.casefold()), None)
    if existing:
        print(f"Category already exists: {existing}")
        return existing
    if name.casefold() == "split":
        print("“Split” is reserved; choose another category name.")
        return None
    insert_at = categories.index("Uncategorized") if "Uncategorized" in categories else len(categories)
    categories.insert(insert_at, name)
    personless = input(f"Should {name} skip person assignment? [y/N]: ").strip().lower()
    if personless in {"y", "yes"}:
        personless_categories.add(name)
    if save_callback:
        save_callback()
    print(f"Added category: {name}")
    return name


def ask_category(
    allow_split: bool, allow_back: bool = False, categories: list[str] | None = None,
    save_callback: Callable[[], None] | None = None,
    personless_categories: set[str] | None = None,
) -> tuple[str, bool]:
    categories = categories if categories is not None else CATEGORIES
    personless_categories = personless_categories if personless_categories is not None else set()
    while True:
        for number, category in enumerate(categories, 1):
            print(f"  {number}. {category}")
        split_number = len(categories) + 1
        if allow_split:
            print(f"  {split_number}. Split purchase")
        print("  Add P after a number to choose who it was for (example: 5p)")
        print("  A. Add a category")
        if allow_back:
            print("  B. Undo previous answer and go back")
        answer = input("Category number (Enter leaves uncategorized): ").strip().lower()
        if answer == "a":
            added = add_custom_category(categories, save_callback, personless_categories)
            if added:
                return added, False
            continue
        if answer == "b" and allow_back:
            return "__back__", False
        if not answer:
            return "__skip__", False
        choose_person = answer.endswith("p")
        number_text = answer[:-1] if choose_person else answer
        if number_text.isdigit() and 1 <= int(number_text) <= len(categories):
            return categories[int(number_text) - 1], choose_person
        if allow_split and number_text == str(split_number):
            return "Split", choose_person
        print("Please enter one of the listed numbers.")


def ask_split_category(
    label: str, excluded: str | None = None, categories: list[str] | None = None,
    save_callback: Callable[[], None] | None = None,
    personless_categories: set[str] | None = None,
) -> tuple[str, bool]:
    categories = categories if categories is not None else CATEGORIES
    personless_categories = personless_categories if personless_categories is not None else set()
    while True:
        choices = [category for category in categories if category != "Uncategorized" and category != excluded]
        print(label)
        for number, category in enumerate(choices, 1):
            print(f"  {number}. {category}")
        print("  A. Add a category")
        answer = input("Category number: ").strip()
        if answer.lower() == "a":
            added = add_custom_category(categories, save_callback, personless_categories)
            if added and added != excluded:
                return added, False
            continue
        choose_person = answer.lower().endswith("p")
        number_text = answer[:-1] if choose_person else answer
        if number_text.isdigit() and 1 <= int(number_text) <= len(choices):
            return choices[int(number_text) - 1], choose_person
        print("Please enter one of the listed numbers.")


def prompt_for_split(
    transaction: dict[str, Any], decisions: dict[str, Any], save_callback: Callable[[], None] | None = None,
    categories: list[str] | None = None, people: list[str] | None = None, default_person: str = "",
    category_person_defaults: dict[str, str] | None = None,
    personless_categories: set[str] | None = None,
) -> None:
    categories = categories if categories is not None else CATEGORIES
    people = people if people is not None else []
    category_person_defaults = category_person_defaults if category_person_defaults is not None else {}
    personless_categories = personless_categories if personless_categories is not None else set()
    total = expense_amount(transaction)
    while True:
        print(f"\nSplit {transaction['date']} | ${total:,.2f} | {transaction['description']}")
        (first, first_choose_person) = ask_split_category(
            "Choose the first category:", categories=categories, save_callback=save_callback,
            personless_categories=personless_categories,
        )
        (second, second_choose_person) = ask_split_category(
            "Choose the second category:", excluded=first, categories=categories, save_callback=save_callback,
            personless_categories=personless_categories,
        )
        while True:
            entered = input(f"Approximately how much of ${total:,.2f} belongs to {first}? $").strip()
            try:
                first_amount = money(entered)
            except ValueError:
                print("Please enter a dollar amount, such as 20 or 20.50.")
                continue
            if first_amount <= 0 or first_amount >= total:
                print(f"Enter an amount greater than $0.00 and less than ${total:,.2f}.")
                continue
            break
        second_amount = total - first_amount
        print(f"Split: ${first_amount:,.2f} {first}; ${second_amount:,.2f} {second}")
        break
    splits = [{"category": first, "amount": f"{first_amount:.2f}"}, {"category": second, "amount": f"{second_amount:.2f}"}]
    for split, choose_person in zip(splits, (first_choose_person, second_choose_person)):
        if split["category"] in personless_categories:
            split.pop("responsibility", None)
        elif choose_person or split["category"] == "Bills":
            split["responsibility"] = ask_bill_responsibility(
                f"Who is the ${money(split['amount']):,.2f} {split['category']} portion for?", people, save_callback
            )
        else:
            assigned_person = category_person_defaults.get(split["category"], default_person)
            if assigned_person:
                split["responsibility"] = assigned_person
            else:
                split.pop("responsibility", None)
    decisions[transaction["id"]] = {"category": "Split", "splits": splits}
    transaction["category"], transaction["splits"] = "Split", splits
    if save_callback:
        save_callback()
    print("Split saved.")


def prompt_to_save_rule(
    provider: str, account: str, identifier: str, category: str, rules: list[dict[str, str]],
    save_callback: Callable[[], None] | None = None, responsibility: str = "",
) -> None:
    entered = input(
        f"Rule for “{identifier}” — type contains text, Enter=exact, M=more options, N=no rule: "
    ).strip()
    if entered.lower() in {"n", "no"}:
        return
    if not entered:
        match_type, pattern = "exact", identifier
    elif entered.lower() not in {"m", "more"}:
        match_type, pattern = "contains_words", clean_text(entered).upper()
    else:
        print("How should future identifiers match?")
        print("  1. Starts with")
        print("  2. Contains exact text, including punctuation and spacing")
        while True:
            choice = input("Match type: ").strip()
            if choice in {"1", "2"}:
                break
            print("Please enter 1 or 2.")
        match_type = {"1": "starts_with", "2": "contains"}[choice]
        while True:
            pattern = clean_text(input("Text to match: ")).upper()
            if pattern:
                break
            print("Match text cannot be blank.")
    new_rule = {"provider": provider, "account": account, "match_type": match_type, "pattern": pattern, "category": category}
    if category == "Income":
        new_rule["kind_override"] = "income"
    if category == "Uncategorized":
        new_rule["reviewed"] = True
    if responsibility:
        new_rule["responsibility"] = responsibility
    rules[:] = [rule for rule in rules if not (
        rule.get("provider") == provider and rule.get("account") == account
        and rule.get("match_type", "exact") == match_type and rule_pattern(rule) == pattern
    )]
    rules.append(new_rule)
    if save_callback:
        save_callback()
    print(f"Saved {match_type.replace('_', ' ')} rule for “{pattern}”.")


def prompt_for_categories(
    transactions: list[dict[str, Any]], rules: list[dict[str, str]], decisions: dict[str, Any],
    save_callback: Callable[[], None] | None = None, categories: list[str] | None = None,
    people: list[str] | None = None, default_person: str = "", always_ask_categories: set[str] | None = None,
    category_person_defaults: dict[str, str] | None = None,
    personless_categories: set[str] | None = None,
) -> int:
    categories = categories if categories is not None else CATEGORIES
    people = people if people is not None else []
    always_ask_categories = always_ask_categories if always_ask_categories is not None else {"Bills"}
    category_person_defaults = category_person_defaults if category_person_defaults is not None else {}
    personless_categories = personless_categories if personless_categories is not None else set()
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for transaction in transactions:
        if (
            transaction["kind"] in {"expense", "refund"}
            and transaction["category"] == "Uncategorized"
            and not transaction.get("reviewed")
        ):
            groups[(transaction["provider"], transaction["account"], transaction["identifier"])].append(transaction)
    changed = 0
    if not groups:
        return changed
    print(f"\n{sum(len(value) for value in groups.values())} expense/refund transaction(s) need categories.")
    group_list = sorted(groups.items(), key=lambda item: item[0])
    history: list[tuple[int, dict[str, Any], list[dict[str, str]], int]] = []
    index = 0
    while index < len(group_list):
        (provider, account, identifier), items = group_list[index]
        apply_rules_and_decisions(items, rules, decisions)
        if all(item["category"] != "Uncategorized" for item in items):
            index += 1
            continue
        expenses = sum((expense_amount(item) for item in items), Decimal("0"))
        refunds = sum((credit_amount(item) for item in items), Decimal("0"))
        print(f"\n{account_label(items[0])} | {identifier}")
        print(f"{len(items)} transaction(s): ${expenses:,.2f} purchases, ${refunds:,.2f} refunds/credits")
        for number, item in enumerate(sorted(items, key=lambda value: value["date"]), 1):
            amount = expense_amount(item) or credit_amount(item)
            print(f"  Transaction {number}: {item['date']} | {item['kind'].title()} | ${amount:,.2f}")
        category, choose_person = ask_category(
            allow_split=any(item["kind"] == "expense" for item in items), allow_back=bool(history),
            categories=categories, save_callback=save_callback, personless_categories=personless_categories,
        )
        if category == "__back__":
            previous_index, previous_decisions, previous_rules, previous_changed = history.pop()
            decisions.clear()
            decisions.update(previous_decisions)
            rules[:] = previous_rules
            changed = previous_changed
            apply_rules_and_decisions(transactions, rules, decisions)
            if save_callback:
                save_callback()
            index = previous_index
            continue
        if category == "__skip__":
            index += 1
            continue
        snapshot = (index, deepcopy(decisions), deepcopy(rules), changed)
        if category == "Split":
            if len(items) == 1 and items[0]["kind"] == "expense":
                prompt_for_split(
                    items[0], decisions, save_callback, categories, people, default_person, category_person_defaults
                    , personless_categories
                )
                changed += 1
                history.append(snapshot)
                index += 1
                continue
            print("Splits apply to one purchase, so these transactions will be reviewed individually.")
            for item in sorted(items, key=lambda value: value["date"]):
                amount = expense_amount(item) or credit_amount(item)
                print(f"\n{item['date']} | {item['kind'].title()} | ${amount:,.2f} | {item['description']}")
                individual_category, individual_choose_person = ask_category(
                    allow_split=item["kind"] == "expense", categories=categories, save_callback=save_callback
                    , personless_categories=personless_categories
                )
                if individual_category == "__skip__":
                    continue
                if individual_category == "Split":
                    prompt_for_split(
                        item, decisions, save_callback, categories, people, default_person, category_person_defaults
                        , personless_categories
                    )
                else:
                    decisions.setdefault(item["id"], {})["category"] = individual_category
                    decisions[item["id"]].pop("splits", None)
                    decisions[item["id"]].pop("kind_override", None)
                    decisions[item["id"]].pop("reviewed", None)
                    if individual_category == "Income":
                        decisions[item["id"]]["kind_override"] = "income"
                        item["kind"] = "income"
                    elif individual_category == "Uncategorized":
                        decisions[item["id"]]["reviewed"] = True
                    item["category"] = individual_category
                    item["splits"] = []
                    person = ""
                    if individual_category in personless_categories or individual_category in {"Income", "Uncategorized"}:
                        person = ""
                    elif individual_choose_person or individual_category in always_ask_categories:
                        person = ask_bill_responsibility("Who was this for?", people, save_callback)
                    else:
                        person = category_person_defaults.get(individual_category, default_person)
                    decisions[item["id"]]["responsibility"] = person
                    item["responsibility"] = person
                    if save_callback:
                        save_callback()
                changed += 1
            history.append(snapshot)
            index += 1
            continue
        for item in items:
            decisions.setdefault(item["id"], {})["category"] = category
            decisions[item["id"]].pop("splits", None)
            decisions[item["id"]].pop("kind_override", None)
            decisions[item["id"]].pop("reviewed", None)
            if category == "Income":
                decisions[item["id"]]["kind_override"] = "income"
                item["kind"] = "income"
            elif category == "Uncategorized":
                decisions[item["id"]]["reviewed"] = True
            item["category"] = category
            item["splits"] = []
            changed += 1
        person = ""
        if category in personless_categories or category in {"Income", "Uncategorized"}:
            person = ""
        elif choose_person or category in always_ask_categories:
            review_each_person = False
            if len(items) > 1:
                same_answer = input(f"Use the same person for all {len(items)} transactions? [Y/n]: ").strip().lower()
                review_each_person = same_answer in {"n", "no"}
            if review_each_person:
                for item in sorted(items, key=lambda value: value["date"]):
                    amount = expense_amount(item) or credit_amount(item)
                    item_person = ask_bill_responsibility(
                        f"{item['date']} | ${amount:,.2f} | {item['description']} — who was this for?",
                        people, save_callback,
                    )
                    decisions[item["id"]]["responsibility"] = item_person
                    item["responsibility"] = item_person
            else:
                person = ask_bill_responsibility("Who was this for?", people, save_callback)
        else:
            person = category_person_defaults.get(category, default_person)
        if person:
            for item in items:
                decisions[item["id"]]["responsibility"] = person
                item["responsibility"] = person
        if save_callback:
            save_callback()
        prompt_to_save_rule(
            provider, account, identifier, category, rules, save_callback, responsibility=person
        )
        history.append(snapshot)
        index += 1
    return changed


def ask_bill_responsibility(
    prompt: str, people: list[str] | None = None, save_callback: Callable[[], None] | None = None
) -> str:
    people = people if people is not None else []
    while True:
        print(prompt)
        if people:
            print("Saved people:")
            for number, person in enumerate(people, 1):
                print(f"  {number}. {person}")
        answer = clean_text(input("Enter a saved number or a new name: "))
        if answer.isdigit():
            if 1 <= int(answer) <= len(people):
                return people[int(answer) - 1]
            print("That saved-person number does not exist.")
            continue
        if not answer:
            print("Please enter a name or a saved number.")
            continue
        existing = next((person for person in people if person.casefold() == answer.casefold()), None)
        if existing:
            return existing
        people.append(answer)
        if save_callback:
            save_callback()
        print(f"Saved person: {answer}")
        return answer


def prompt_for_bill_responsibilities(
    transactions: list[dict[str, Any]], rules: list[dict[str, str]], decisions: dict[str, Any],
    save_callback: Callable[[], None] | None = None, offer_rule: bool = True,
    people: list[str] | None = None,
    personless_categories: set[str] | None = None,
) -> int:
    people = people if people is not None else []
    personless_categories = personless_categories if personless_categories is not None else set()
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for transaction in transactions:
        if (
            transaction["kind"] in {"expense", "refund"}
            and transaction.get("category") not in {None, "", "Uncategorized", "Split"}
            and transaction.get("category") not in personless_categories
            and not transaction.get("responsibility")
        ):
            groups[(transaction["provider"], transaction["account"], transaction["identifier"])].append(transaction)
    changed = 0
    for (provider, account, identifier), items in sorted(groups.items(), key=lambda item: item[0]):
        category = items[0]["category"]
        print(f"\nWho was this for? {account_label(items[0])} | {identifier} | {category}")
        review_each = False
        if len(items) > 1:
            answer = input(f"Use the same person for all {len(items)} bills? [Y/n]: ").strip().lower()
            review_each = answer in {"n", "no"}
        if review_each:
            for item in sorted(items, key=lambda value: value["date"]):
                amount = expense_amount(item) or credit_amount(item)
                responsibility = ask_bill_responsibility(
                    f"{item['date']} | ${amount:,.2f} | {item['description']} — who is this for?",
                    people, save_callback,
                )
                decisions.setdefault(item["id"], {})["responsibility"] = responsibility
                item["responsibility"] = responsibility
                changed += 1
                if save_callback:
                    save_callback()
            continue

        responsibility = ask_bill_responsibility("Who is this bill for?", people, save_callback)
        for item in items:
            decisions.setdefault(item["id"], {})["responsibility"] = responsibility
            item["responsibility"] = responsibility
            changed += 1
        if save_callback:
            save_callback()
        if offer_rule:
            prompt_to_save_rule(
                provider, account, identifier, category, rules, save_callback, responsibility=responsibility
            )
    return changed


def expense_amount(transaction: dict[str, Any]) -> Decimal:
    return abs(money(transaction["raw_amount"])) if transaction["kind"] == "expense" else Decimal("0")


def credit_amount(transaction: dict[str, Any]) -> Decimal:
    return abs(money(transaction["raw_amount"])) if transaction["kind"] == "refund" else Decimal("0")


def inflow_amount(transaction: dict[str, Any]) -> Decimal:
    return abs(money(transaction["raw_amount"])) if transaction["kind"] in {"income", "reimbursement"} else Decimal("0")


def category_entries(transaction: dict[str, Any]) -> list[tuple[str, Decimal, str]]:
    if transaction["kind"] == "expense" and transaction.get("splits"):
        return [
            (split["category"], money(split["amount"]), split.get("responsibility", ""))
            for split in transaction["splits"]
        ]
    if transaction["kind"] == "expense":
        return [(transaction.get("category", "Uncategorized"), expense_amount(transaction), transaction.get("responsibility", ""))]
    if transaction["kind"] == "refund":
        return [(transaction.get("category", "Uncategorized"), -credit_amount(transaction), transaction.get("responsibility", ""))]
    return []


def category_display(transaction: dict[str, Any]) -> str:
    if not transaction.get("splits"):
        return transaction.get("category", "")
    return "; ".join(
        f"{split['category']} ${money(split['amount']):,.2f}"
        + (f" ({split['responsibility']})" if split.get("responsibility") else "")
        for split in transaction["splits"]
    )


def person_display(transaction: dict[str, Any]) -> str:
    if not transaction.get("splits"):
        return transaction.get("responsibility", "")
    return "; ".join(
        f"{split['category']}: {split.get('responsibility') or 'Unassigned'}"
        for split in transaction["splits"]
    )


def month_summary(
    transactions: list[dict[str, Any]], personless_categories: set[str] | None = None,
    default_person: str = "", other_people_categories: set[str] | None = None,
) -> dict[str, Any]:
    personless_categories = personless_categories if personless_categories is not None else set()
    other_people_categories = other_people_categories if other_people_categories is not None else set()
    spending = sum((expense_amount(t) for t in transactions), Decimal("0"))
    refunds = sum((credit_amount(t) for t in transactions), Decimal("0"))
    income = sum((inflow_amount(t) for t in transactions if t["kind"] == "income"), Decimal("0"))
    reimbursements = sum((inflow_amount(t) for t in transactions if t["kind"] == "reimbursement"), Decimal("0"))
    categories: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    bill_responsibilities: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    people_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    other_people_spending = Decimal("0")
    for transaction in transactions:
        for category, amount, responsibility in category_entries(transaction):
            categories[category] += amount
            if category not in personless_categories:
                people_totals[responsibility or "Unassigned"] += amount
            if category == "Bills":
                bill_responsibilities[responsibility or "Unassigned"] += amount
            if category in other_people_categories or (
                responsibility and default_person and responsibility.casefold() != default_person.casefold()
            ):
                other_people_spending += amount
    return {
        "spending": spending,
        "refunds": refunds,
        "net_spending": spending - refunds,
        "income": income,
        "reimbursements": reimbursements,
        "other_people_spending": other_people_spending,
        "other_people_after_reimbursements": other_people_spending - reimbursements,
        "net_after_spending": income + reimbursements - (spending - refunds),
        "categories": dict(categories),
        "bill_responsibilities": dict(bill_responsibilities),
        "people_totals": dict(people_totals),
        "uncategorized_count": sum(t["kind"] in {"expense", "refund"} and t.get("category") == "Uncategorized" for t in transactions),
        "card_payment_count": sum(t["kind"] == "card_payment" for t in transactions),
        "transfer_count": sum(t["kind"] == "transfer" for t in transactions),
    }


def generate_html(
    month: str, transactions: list[dict[str, Any]], destination: Path,
    personless_categories: set[str] | None = None,
    default_person: str = "", other_people_categories: set[str] | None = None,
) -> None:
    summary = month_summary(
        transactions, personless_categories, default_person, other_people_categories,
    )
    personless_categories = personless_categories if personless_categories is not None else set()
    other_people_categories = other_people_categories if other_people_categories is not None else set()
    rows = []
    for transaction in sorted(transactions, key=lambda item: (item["date"], item["provider"], item["description"])):
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in (
            transaction["date"], PROVIDER_LABELS[transaction["provider"]], transaction["description"],
            transaction["kind"].replace("_", " ").title(), category_display(transaction), person_display(transaction),
            f"{money(transaction['raw_amount']):,.2f}"
        )) + "</tr>")
    category_rows = "".join(
        f"<tr><td>{html.escape(category)}</td><td>${amount:,.2f}</td></tr>"
        for category, amount in sorted(summary["categories"].items(), key=lambda item: (-item[1], item[0]))
    )
    bill_rows = "".join(
        f"<tr><td>{html.escape(responsibility)}</td><td>${amount:,.2f}</td></tr>"
        for responsibility, amount in sorted(summary["bill_responsibilities"].items())
    )
    people_rows = "".join(
        f"<tr><td>{html.escape(person)}</td><td>${amount:,.2f}</td></tr>"
        for person, amount in sorted(summary["people_totals"].items())
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Spending report {month}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:auto;padding:24px;color:#202124}}h1,h2{{margin-bottom:.4rem}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}.card{{background:#f4f6f8;border-radius:10px;padding:16px}}.value{{font-size:1.5rem;font-weight:700}}table{{border-collapse:collapse;width:100%;margin:12px 0 28px}}th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}th{{background:#f4f6f8;position:sticky;top:0}}.note{{color:#5f6368}}</style></head>
<body><h1>Spending report — {month}</h1><p class="note">Local report. Card payments and transfers are excluded from spending and income.</p>
<div class="cards">
<div class="card">Income<div class="value">${summary['income']:,.2f}</div></div>
<div class="card">Estimated reimbursements<div class="value">${summary['reimbursements']:,.2f}</div></div>
<div class="card">Spending assigned to others<div class="value">${summary['other_people_spending']:,.2f}</div></div>
<div class="card">Others spending minus reimbursements<div class="value">${summary['other_people_after_reimbursements']:,.2f}</div></div>
<div class="card">Gross spending<div class="value">${summary['spending']:,.2f}</div></div>
<div class="card">Refunds/credits<div class="value">${summary['refunds']:,.2f}</div></div>
<div class="card">Net spending<div class="value">${summary['net_spending']:,.2f}</div></div>
<div class="card">Income + reimbursements − net spending<div class="value">${summary['net_after_spending']:,.2f}</div></div>
</div>
<h2>Spending by category</h2><table><thead><tr><th>Category</th><th>Amount</th></tr></thead><tbody>{category_rows}</tbody></table>
<h2>Spending by person</h2><table><thead><tr><th>Person</th><th>Net amount</th></tr></thead><tbody>{people_rows}</tbody></table>
<h2>Bills by responsibility</h2><table><thead><tr><th>For</th><th>Net amount</th></tr></thead><tbody>{bill_rows}</tbody></table>
<h2>All transactions</h2><table><thead><tr><th>Date</th><th>Account</th><th>Description</th><th>Kind</th><th>Category</th><th>For</th><th>Original amount</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>"""
    destination.write_text(document, encoding="utf-8")


def generate_xlsx(
    month: str, transactions: list[dict[str, Any]], destination: Path,
    personless_categories: set[str] | None = None,
    default_person: str = "", other_people_categories: set[str] | None = None,
) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.chart import BarChart, DoughnutChart, LineChart, Reference
        from openpyxl.formatting.rule import CellIsRule
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.worksheet.table import Table, TableStyleInfo
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise RuntimeError("Excel support requires: python3 -m pip install -r requirements.txt") from exc
    summary = month_summary(
        transactions, personless_categories, default_person, other_people_categories,
    )
    personless_categories = personless_categories if personless_categories is not None else set()
    other_people_categories = other_people_categories if other_people_categories is not None else set()
    workbook = Workbook()
    dashboard = workbook.active
    dashboard.title = "Dashboard"
    navy, blue, pale_blue = "17365D", "4472C4", "D9EAF7"
    green, pale_green, red, pale_red = "548235", "E2F0D9", "C00000", "FCE4D6"
    currency_format = '$#,##0.00;[Red]-$#,##0.00'
    thin_gray = Side(style="thin", color="D9E1F2")

    def style_data_sheet(sheet, widths: list[int], table_name: str) -> None:
        sheet.freeze_panes = "A2"
        sheet.sheet_view.showGridLines = False
        sheet.auto_filter.ref = sheet.dimensions
        sheet.row_dimensions[1].height = 24
        for cell in sheet[1]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = PatternFill("solid", fgColor=navy)
            cell.alignment = Alignment(vertical="center")
        for index, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        if sheet.max_row >= 2:
            table = Table(displayName=table_name, ref=sheet.dimensions)
            table.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium2", showFirstColumn=False,
                showLastColumn=False, showRowStripes=True, showColumnStripes=False,
            )
            sheet.add_table(table)

    dashboard.sheet_view.showGridLines = False
    dashboard.sheet_properties.tabColor = blue
    dashboard.merge_cells("A1:H1")
    dashboard["A1"] = f"Spending Dashboard — {month}"
    dashboard["A1"].font = Font(size=22, bold=True, color="FFFFFF")
    dashboard["A1"].fill = PatternFill("solid", fgColor=navy)
    dashboard["A1"].alignment = Alignment(vertical="center")
    dashboard.row_dimensions[1].height = 38
    dashboard.merge_cells("A2:H2")
    dashboard["A2"] = "Money out is shown as negative; money in is positive. Card payments and transfers are excluded from spending totals."
    dashboard["A2"].font = Font(italic=True, color="595959")

    primary_label = default_person or "Default person"
    primary_by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    remaining_by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    dashboard_people: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for transaction in transactions:
        for category, amount, responsibility in category_entries(transaction):
            if default_person and responsibility.casefold() == default_person.casefold():
                primary_by_category[category] += amount
                dashboard_people[default_person] += amount
            else:
                remaining_by_category[category] += amount
                label = category if category in personless_categories else (responsibility or "Unassigned")
                dashboard_people[label] += amount
    primary_spending = sum(primary_by_category.values(), Decimal("0"))
    cards = [
        (f"{primary_label}'s spending", primary_spending, pale_blue),
        ("Spending assigned to others", summary["other_people_spending"], pale_blue),
        ("Estimated reimbursements", summary["reimbursements"], pale_green),
        ("Others spending − reimbursements", summary["other_people_after_reimbursements"], pale_red),
        ("Income", summary["income"], pale_green),
        ("Refunds and credits", summary["refunds"], pale_green),
    ]
    for index, (label, value, fill) in enumerate(cards):
        row = 4 + (index // 3) * 3
        col = 1 + (index % 3) * 3
        dashboard.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + 1)
        dashboard.merge_cells(start_row=row + 1, start_column=col, end_row=row + 1, end_column=col + 1)
        label_cell = dashboard.cell(row, col, label)
        value_cell = dashboard.cell(row + 1, col, float(value))
        for target in (label_cell, value_cell):
            target.fill = PatternFill("solid", fgColor=fill)
            target.border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)
        label_cell.font = Font(bold=True, color=navy)
        value_cell.font = Font(size=17, bold=True, color=green if value >= 0 else red)
        value_cell.number_format = currency_format

    category_items = [(name, amount) for name, amount in sorted(
        summary["categories"].items(), key=lambda item: (-item[1], item[0])
    ) if amount != 0]
    comparison_categories = sorted(
        set(primary_by_category) | set(remaining_by_category),
        key=lambda category: (-(primary_by_category[category] + remaining_by_category[category]), category),
    )
    dashboard.append([])
    dashboard["J1"], dashboard["K1"], dashboard["L1"] = "Category", primary_label, "Everyone else / groups"
    for row, category in enumerate(comparison_categories, 2):
        dashboard.cell(row, 10, category)
        dashboard.cell(row, 11, float(primary_by_category[category]))
        dashboard.cell(row, 12, float(remaining_by_category[category]))
    people_items = [(name, amount) for name, amount in sorted(
        dashboard_people.items(), key=lambda item: (-item[1], item[0])
    ) if amount != 0]
    dashboard["N1"], dashboard["O1"] = "Person or group", "Amount"
    for row, (name, amount) in enumerate(people_items, 2):
        dashboard.cell(row, 14, name)
        dashboard.cell(row, 15, float(amount))
    primary_items = [(name, amount) for name, amount in sorted(
        primary_by_category.items(), key=lambda item: (-item[1], item[0])
    ) if amount > 0]
    dashboard["Q1"], dashboard["R1"] = "Default person's category", "Amount"
    for row, (name, amount) in enumerate(primary_items, 2):
        dashboard.cell(row, 17, name)
        dashboard.cell(row, 18, float(amount))
    dashboard["T1"], dashboard["U1"] = "Reimbursement comparison", "Amount"
    dashboard["T2"], dashboard["U2"] = "Spending assigned to others", float(summary["other_people_spending"])
    dashboard["T3"], dashboard["U3"] = "Estimated reimbursements", float(summary["reimbursements"])
    dashboard["W1"], dashboard["X1"] = "All categories", "Amount"
    for row, (name, amount) in enumerate(category_items, 2):
        dashboard.cell(row, 23, name)
        dashboard.cell(row, 24, float(amount))

    if comparison_categories:
        chart = BarChart()
        chart.type = "bar"
        chart.grouping = "stacked"
        chart.overlap = 100
        chart.style = 10
        chart.title = f"Categories: {primary_label} vs everyone else"
        chart.x_axis.title = "Net spending"
        chart.add_data(Reference(dashboard, min_col=11, max_col=12, min_row=1, max_row=len(comparison_categories) + 1), titles_from_data=True)
        chart.set_categories(Reference(dashboard, min_col=10, min_row=2, max_row=len(comparison_categories) + 1))
        chart.height, chart.width = 10, 14
        dashboard.add_chart(chart, "A11")
    if people_items:
        chart = BarChart()
        chart.type = "bar"
        chart.style = 10
        chart.title = "Spending by person or group"
        chart.y_axis.title = "Person"
        chart.x_axis.title = "Amount"
        chart.add_data(Reference(dashboard, min_col=15, min_row=1, max_row=len(people_items) + 1), titles_from_data=True)
        chart.set_categories(Reference(dashboard, min_col=14, min_row=2, max_row=len(people_items) + 1))
        chart.height, chart.width = 10, 14
        dashboard.add_chart(chart, "G11")

    chart = BarChart()
    chart.type = "col"
    chart.style = 10
    chart.title = "Others' spending compared with reimbursements"
    chart.y_axis.title = "Amount"
    chart.add_data(Reference(dashboard, min_col=21, min_row=1, max_row=3), titles_from_data=True)
    chart.set_categories(Reference(dashboard, min_col=20, min_row=2, max_row=3))
    chart.height, chart.width = 8, 14
    dashboard.add_chart(chart, "A31")
    if primary_items:
        chart = DoughnutChart()
        chart.title = f"{primary_label}'s spending by category"
        chart.add_data(Reference(dashboard, min_col=18, min_row=1, max_row=len(primary_items) + 1), titles_from_data=True)
        chart.set_categories(Reference(dashboard, min_col=17, min_row=2, max_row=len(primary_items) + 1))
        chart.height, chart.width = 8, 14
        chart.holeSize = 55
        dashboard.add_chart(chart, "G31")

    daily: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for transaction in transactions:
        daily[transaction["date"]] += expense_amount(transaction) - credit_amount(transaction)
    dashboard["Z1"], dashboard["AA1"] = "Date", "Net spending"
    for row, (date, amount) in enumerate(sorted(daily.items()), 2):
        dashboard.cell(row, 26, date)
        dashboard.cell(row, 27, float(amount))
    if category_items:
        chart = DoughnutChart()
        chart.title = "All net spending by category (additional context)"
        chart.add_data(Reference(dashboard, min_col=24, min_row=1, max_row=len(category_items) + 1), titles_from_data=True)
        chart.set_categories(Reference(dashboard, min_col=23, min_row=2, max_row=len(category_items) + 1))
        chart.height, chart.width = 8, 14
        chart.holeSize = 55
        dashboard.add_chart(chart, "A48")
    if daily:
        chart = LineChart()
        chart.title = "Daily net spending (additional context)"
        chart.y_axis.title = "Amount"
        chart.add_data(Reference(dashboard, min_col=27, min_row=1, max_row=len(daily) + 1), titles_from_data=True)
        chart.set_categories(Reference(dashboard, min_col=26, min_row=2, max_row=len(daily) + 1))
        chart.height, chart.width = 8, 14
        dashboard.add_chart(chart, "G48")
    for column in range(10, 28):
        dashboard.column_dimensions[get_column_letter(column)].hidden = True
    for column in range(1, 13):
        dashboard.column_dimensions[get_column_letter(column)].width = 18

    category_person_totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal("0"))
    )
    category_detail_rows: dict[str, list[tuple[dict[str, Any], Decimal, str]]] = defaultdict(list)
    for transaction in transactions:
        for category, amount, responsibility in category_entries(transaction):
            if category in personless_categories:
                person = "Personless / group"
                detail_person = ""
            else:
                person = responsibility or "Unassigned"
                detail_person = person
            category_person_totals[category][person] += amount
            category_detail_rows[category].append((transaction, amount, detail_person))
    person_columns = sorted({
        person for totals in category_person_totals.values() for person in totals
    }, key=lambda person: (
        0 if default_person and person.casefold() == default_person.casefold() else
        2 if person == "Personless / group" else 1,
        person.casefold(),
    ))
    matrix_categories = sorted(
        category_person_totals,
        key=lambda category: (-sum(category_person_totals[category].values(), Decimal("0")), category),
    )

    def build_card_sheet(sheet, title: str, cards: list[tuple[str, list[tuple[str, Decimal]], Decimal]]) -> None:
        sheet.sheet_view.showGridLines = False
        sheet.merge_cells("A1:H1")
        sheet["A1"] = title
        sheet["A1"].font = Font(size=20, bold=True, color="FFFFFF")
        sheet["A1"].fill = PatternFill("solid", fgColor=navy)
        sheet["A1"].alignment = Alignment(vertical="center")
        sheet.row_dimensions[1].height = 34
        for column in ("A", "D", "G"):
            sheet.column_dimensions[column].width = 22
        for column in ("B", "E", "H"):
            sheet.column_dimensions[column].width = 15
        for column in ("C", "F"):
            sheet.column_dimensions[column].width = 3
        row_cursor = 3
        for start in range(0, len(cards), 3):
            card_row = cards[start:start + 3]
            height = max((len(lines) + 2 for _name, lines, _total in card_row), default=2)
            for index, (name, lines, total) in enumerate(card_row):
                column = 1 + index * 3
                sheet.merge_cells(
                    start_row=row_cursor, start_column=column,
                    end_row=row_cursor, end_column=column + 1,
                )
                heading = sheet.cell(row_cursor, column, name)
                heading.font = Font(bold=True, color="FFFFFF", size=12)
                heading.fill = PatternFill("solid", fgColor=blue)
                heading.alignment = Alignment(vertical="center")
                for offset, (label, amount) in enumerate(lines, 1):
                    label_cell = sheet.cell(row_cursor + offset, column, label)
                    amount_cell = sheet.cell(row_cursor + offset, column + 1, float(amount))
                    amount_cell.number_format = currency_format
                    label_cell.fill = amount_cell.fill = PatternFill("solid", fgColor="F7F9FC")
                total_row = row_cursor + len(lines) + 1
                total_label = sheet.cell(total_row, column, "Total")
                total_value = sheet.cell(total_row, column + 1, float(total))
                total_label.font = total_value.font = Font(bold=True, color=navy)
                total_label.fill = total_value.fill = PatternFill("solid", fgColor=pale_blue)
                total_value.number_format = currency_format
                for row in range(row_cursor, total_row + 1):
                    for col in range(column, column + 2):
                        sheet.cell(row, col).border = Border(
                            left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray
                        )
            row_cursor += height + 2

    categories = workbook.create_sheet("Categories")
    categories.sheet_properties.tabColor = "70AD47"
    categories.sheet_view.showGridLines = False
    categories.sheet_properties.outlinePr.summaryBelow = False
    categories.append([
        "Category / Person", "Date", "Description", "Account", "Type",
        "Net Spending Effect (+ purchase / − refund)",
    ])
    for cell in categories[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor=navy)
    for category in matrix_categories:
        entries = sorted(
            category_detail_rows[category],
            key=lambda item: (item[2], item[0]["date"], item[0]["provider"], item[0]["description"]),
        )
        category_row = categories.max_row + 1
        category_total = sum((amount for _transaction, amount, _person in entries), Decimal("0"))
        categories.append([category, None, f"{len(entries)} categorized item(s)", None, None, float(category_total)])
        for cell in categories[category_row]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=blue)
        categories.cell(category_row, 6).number_format = currency_format
        entries_by_person: dict[str, list[tuple[dict[str, Any], Decimal]]] = defaultdict(list)
        for transaction, amount, person in entries:
            entries_by_person[person].append((transaction, amount))
        for person, person_entries in entries_by_person.items():
            if person:
                person_row = categories.max_row + 1
                person_total = sum((amount for _transaction, amount in person_entries), Decimal("0"))
                categories.append([person, None, None, None, None, float(person_total)])
                for cell in categories[person_row]:
                    cell.font = Font(bold=True, color=navy)
                    cell.fill = PatternFill("solid", fgColor=pale_blue)
                categories.cell(person_row, 6).number_format = currency_format
                categories.row_dimensions[person_row].outlineLevel = 1
            for transaction, amount in person_entries:
                categories.append([
                    None, transaction["date"], transaction["description"], account_label(transaction),
                    transaction["kind"].replace("_", " ").title(), float(amount),
                ])
                categories.cell(categories.max_row, 6).number_format = currency_format
                categories.row_dimensions[categories.max_row].outlineLevel = 2 if person else 1
    category_grand_total_row = categories.max_row + 2
    categories.cell(category_grand_total_row, 1, "GRAND TOTAL")
    categories.cell(category_grand_total_row, 6, float(sum(summary["categories"].values(), Decimal("0"))))
    for cell in categories[category_grand_total_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=navy)
    categories.cell(category_grand_total_row, 6).number_format = currency_format
    categories.freeze_panes = "A2"
    for index, width in enumerate([26, 12, 48, 20, 16, 25], 1):
        categories.column_dimensions[get_column_letter(index)].width = width

    people_cards = []
    for person in person_columns:
        if person == "Personless / group":
            continue
        lines = [
            (category, category_person_totals[category][person])
            for category in matrix_categories if category_person_totals[category][person]
        ]
        lines.sort(key=lambda item: (-item[1], item[0]))
        people_cards.append((person, lines, sum((amount for _category, amount in lines), Decimal("0"))))
    for category in matrix_categories:
        amount = category_person_totals[category].get("Personless / group", Decimal("0"))
        if amount:
            people_cards.append((category, [], amount))
    people_sheet = workbook.create_sheet("People")
    people_sheet.sheet_properties.tabColor = "5B9BD5"
    build_card_sheet(people_sheet, "People and groups by category", people_cards)

    bills = workbook.create_sheet("Bills")
    bills.sheet_properties.tabColor = "ED7D31"
    bills.sheet_view.showGridLines = False
    bills.sheet_properties.outlinePr.summaryBelow = False
    bills.append(["For", "Date", "Bill", "Account", "Type", "Net Amount"])
    for cell in bills[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor=navy)
    bill_entries_by_person: dict[str, list[tuple[dict[str, Any], Decimal]]] = defaultdict(list)
    for transaction, amount, person in category_detail_rows.get("Bills", []):
        bill_entries_by_person[person or "Unassigned"].append((transaction, amount))
    for person, entries in sorted(bill_entries_by_person.items()):
        entries.sort(key=lambda item: (item[0]["date"], item[0]["description"]))
        person_row = bills.max_row + 1
        total = sum((amount for _transaction, amount in entries), Decimal("0"))
        bills.append([person, None, f"{len(entries)} bill(s)", None, None, float(total)])
        for cell in bills[person_row]:
            cell.font = Font(bold=True, color=navy)
            cell.fill = PatternFill("solid", fgColor=pale_blue)
        bills.cell(person_row, 6).number_format = currency_format
        for transaction, amount in entries:
            bills.append([
                None, transaction["date"], transaction["description"], account_label(transaction),
                transaction["kind"].replace("_", " ").title(), float(amount),
            ])
            bills.cell(bills.max_row, 6).number_format = currency_format
            bills.row_dimensions[bills.max_row].outlineLevel = 1
    bills_total_row = bills.max_row + 2
    bills.cell(bills_total_row, 1, "GRAND TOTAL")
    bills.cell(bills_total_row, 6, float(sum(summary["bill_responsibilities"].values(), Decimal("0"))))
    for cell in bills[bills_total_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=navy)
    bills.cell(bills_total_row, 6).number_format = currency_format
    bills.freeze_panes = "A2"
    for index, width in enumerate([24, 12, 48, 20, 16, 18], 1):
        bills.column_dimensions[get_column_letter(index)].width = width

    sheet = workbook.create_sheet("Transactions")
    sheet.sheet_properties.tabColor = "A5A5A5"
    headers = [
        "Date", "Account", "Description", "Type", "Category / Split", "For",
        "Money Out", "Money In", "Cash Flow", "Included in Spending?",
        "Provider Identifier", "Bank Category", "Bank Type", "Post Date",
    ]
    sheet.append(headers)
    for transaction in sorted(transactions, key=lambda item: (item["date"], item["provider"], item["description"])):
        kind = transaction["kind"]
        amount = abs(money(transaction["raw_amount"]))
        money_out = amount if kind in {"expense", "card_payment", "transfer"} else Decimal("0")
        money_in = amount if kind in {"refund", "income", "reimbursement"} else Decimal("0")
        cash_flow = money_in - money_out
        included = "Yes" if kind in {"expense", "refund"} else "No"
        sheet.append([
            transaction["date"], account_label(transaction), transaction["description"],
            kind.replace("_", " ").title(), category_display(transaction), person_display(transaction),
            float(money_out) if money_out else None, float(money_in) if money_in else None,
            float(cash_flow), included, transaction["identifier"],
            transaction["bank_category"], transaction["bank_type"], transaction["post_date"],
        ])
    style_data_sheet(sheet, [12, 20, 46, 19, 24, 20, 15, 15, 15, 21, 38, 25, 18, 12], "TransactionTable")
    for row in range(2, sheet.max_row + 1):
        for column in (7, 8, 9):
            sheet.cell(row, column).number_format = currency_format
    if sheet.max_row >= 2:
        sheet.conditional_formatting.add(
            f"I2:I{sheet.max_row}", CellIsRule(operator="lessThan", formula=["0"], font=Font(color=red))
        )
        sheet.conditional_formatting.add(
            f"I2:I{sheet.max_row}", CellIsRule(operator="greaterThan", formula=["0"], font=Font(color=green))
        )

    workbook.save(destination)


def write_month_csv(transactions: list[dict[str, Any]], destination: Path) -> None:
    headers = ["date", "post_date", "account", "description", "kind", "category", "split_details", "responsibility", "raw_amount", "identifier"]
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for transaction in sorted(transactions, key=lambda item: (item["date"], item["provider"], item["description"])):
            row = {key: transaction.get(key, "") for key in headers}
            row["split_details"] = category_display(transaction) if transaction.get("splits") else ""
            writer.writerow(row)


def process(root: Path, non_interactive: bool) -> int:
    inbox = root / "inbox"
    processed = root / "processed"
    data_dir = root / "data"
    rules_dir = root / "rules"
    reports_dir = root / "reports"
    for directory in (inbox, processed, data_dir, rules_dir, reports_dir):
        directory.mkdir(parents=True, exist_ok=True)

    import_path = data_dir / "imports.json"
    rule_path = rules_dir / "rules.json"
    category_path = rules_dir / "categories.json"
    people_path = rules_dir / "people.json"
    settings_path = rules_dir / "settings.json"
    transaction_dir = data_dir / "transactions"
    decision_dir = data_dir / "decisions"
    transactions = load_year_shards(transaction_dir)
    for transaction in transactions:
        transaction["identifier"] = clean_identifier_value(transaction["provider"], transaction["identifier"])
    imports = load_json(import_path, [])
    decisions = load_decision_shards(decision_dir)
    rules = load_json(rule_path, [])
    categories = load_json(category_path, list(CATEGORIES))
    people = load_json(people_path, [])
    settings = load_settings(settings_path)
    for default_category in CATEGORIES:
        if not any(category.casefold() == default_category.casefold() for category in categories):
            insert_at = categories.index("Uncategorized") if "Uncategorized" in categories else len(categories)
            categories.insert(insert_at, default_category)
    for rule in rules:
        if rule.get("provider") == "amex":
            if rule.get("pattern"):
                rule["pattern"] = clean_identifier_value("amex", rule["pattern"])
            elif rule.get("identifier"):
                rule["identifier"] = clean_identifier_value("amex", rule["identifier"])
    known_people = {person.casefold() for person in people}
    stored_responsibilities = [
        decision.get("responsibility", "") for decision in decisions.values()
    ] + [rule.get("responsibility", "") for rule in rules]
    for decision in decisions.values():
        stored_responsibilities.extend(
            split.get("responsibility", "") for split in decision.get("splits", [])
        )
    for transaction in transactions:
        stored_responsibilities.extend(
            split.get("responsibility", "") for split in transaction.get("splits", [])
        )
    for person in stored_responsibilities:
        if person and person.casefold() not in known_people:
            people.append(person)
            known_people.add(person.casefold())
    known_files = {item["sha256"] for item in imports}
    existing = {transaction["id"]: transaction for transaction in transactions}
    csv_paths = sorted(path for path in inbox.iterdir() if path.is_file() and path.suffix.lower() == ".csv")
    if not csv_paths:
        print(f"No CSV files found in {inbox}")
        if not transactions:
            print("Drop bank exports there and run this command again.")
            return 0
        print("Using the existing transaction history; skipped categories can still be completed.")

    imported_files = duplicate_files = failed_files = new_transactions = repeated_transactions = 0
    affected_months: set[str] = set()
    for path in csv_paths:
        file_hash = sha256_file(path)
        if file_hash in known_files:
            destination = unique_destination(processed / "duplicates", path.name)
            shutil.move(str(path), destination)
            print(f"Duplicate file archived without reimporting: {path.name}")
            duplicate_files += 1
            continue
        try:
            provider, rows = read_csv(path)
            account = account_id(provider, path.name)
            occurrence: Counter[str] = Counter()
            file_new = file_repeated = 0
            dates: list[str] = []
            for row in rows:
                transaction = normalize_row(provider, account, row, path.name, settings)
                base = transaction_base_key(transaction)
                occurrence[base] += 1
                transaction_id = stable_hash(base, occurrence[base])
                transaction["id"] = transaction_id
                dates.append(transaction["date"])
                affected_months.add(transaction["date"][:7])
                if transaction_id in existing:
                    sources = existing[transaction_id].setdefault("source_files", [])
                    if path.name not in sources:
                        sources.append(path.name)
                    file_repeated += 1
                else:
                    existing[transaction_id] = transaction
                    file_new += 1
            start = min(dates) if dates else "empty"
            end = max(dates) if dates else "empty"
            archive_name = f"{account}_{start}_to_{end}_{file_hash[:8]}.csv"
            destination = unique_destination(processed / account, archive_name)
            shutil.move(str(path), destination)
            imports.append({
                "sha256": file_hash,
                "original_filename": path.name,
                "archived_path": str(destination.relative_to(root)),
                "provider": provider,
                "account": account,
                "date_range": [start, end],
                "rows": len(rows),
                "new_transactions": file_new,
                "repeated_transactions": file_repeated,
                "processed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
            known_files.add(file_hash)
            imported_files += 1
            new_transactions += file_new
            repeated_transactions += file_repeated
            print(f"Imported {path.name}: {file_new} new, {file_repeated} already known")
        except Exception as exc:
            failed_files += 1
            print(f"ERROR: Could not import {path.name}: {exc}", file=sys.stderr)

    transactions = list(existing.values())
    # Persist successful imports before interactive questions so an interruption
    # never loses transactions whose source files have already been archived.
    save_year_shards(transaction_dir, transactions)
    save_json(import_path, imports)
    migrated_rules = migrate_legacy_rules(rules, transactions)
    always_ask_categories = set(settings.get("always_ask_person_categories", ["Bills"]))
    category_person_defaults = settings.get("category_person_defaults", {})
    personless_categories = set(settings.get("personless_categories", []))
    personless_categories.add("Income")
    other_people_categories = set(settings.get("other_people_categories", []))
    def persist_answers() -> None:
        save_decision_shards(decision_dir, decisions, transactions)
        save_json(rule_path, rules)
        save_json(category_path, categories)
        save_json(people_path, people)
        settings["personless_categories"] = sorted(personless_categories)
        save_json(settings_path, settings)

    default_person = settings.get("default_person", "")
    if not non_interactive and transactions and (
        not default_person or not any(person.casefold() == default_person.casefold() for person in people)
    ):
        default_person = ask_bill_responsibility(
            "Choose the default person for transactions:", people, persist_answers
        )
        settings["default_person"] = default_person
        persist_answers()
    if default_person:
        for decision in decisions.values():
            category = decision.get("category", "")
            if category in personless_categories:
                decision.pop("responsibility", None)
            if category and category not in {"Uncategorized", "Split"} and not decision.get("responsibility"):
                if category not in always_ask_categories and category not in personless_categories:
                    decision["responsibility"] = default_person
            for split in decision.get("splits", []):
                if split.get("category") in personless_categories:
                    split.pop("responsibility", None)
                elif not split.get("responsibility") and split.get("category") not in always_ask_categories:
                    split["responsibility"] = default_person
        for rule in rules:
            if rule.get("category") in personless_categories:
                rule.pop("responsibility", None)
            elif rule.get("category") not in always_ask_categories and not rule.get("responsibility"):
                rule["responsibility"] = default_person

    apply_rules_and_decisions(transactions, rules, decisions)
    categorized = 0 if non_interactive else prompt_for_categories(
        transactions, rules, decisions, persist_answers, categories, people,
        default_person, always_ask_categories, category_person_defaults,
        personless_categories,
    )
    apply_rules_and_decisions(transactions, rules, decisions)
    bills_assigned = 0 if non_interactive else prompt_for_bill_responsibilities(
        transactions, rules, decisions, persist_answers, people=people,
        personless_categories=personless_categories,
    )
    apply_rules_and_decisions(transactions, rules, decisions)
    save_year_shards(transaction_dir, transactions)
    save_json(import_path, imports)
    save_decision_shards(decision_dir, decisions, transactions)
    save_json(rule_path, rules)
    save_json(category_path, categories)
    save_json(people_path, people)
    save_json(settings_path, settings)

    months = sorted({transaction["date"][:7] for transaction in transactions})
    by_month_dir = data_dir / "by-month"
    by_month_dir.mkdir(parents=True, exist_ok=True)
    for month in months:
        month_transactions = [transaction for transaction in transactions if transaction["date"].startswith(month)]
        month_report_dir = reports_dir / month
        month_report_dir.mkdir(parents=True, exist_ok=True)
        write_month_csv(month_transactions, by_month_dir / f"{month}.csv")
        generate_html(
            month, month_transactions, month_report_dir / "spending-report.html",
            personless_categories, default_person, other_people_categories,
        )
        generate_xlsx(
            month, month_transactions, month_report_dir / "spending-report.xlsx",
            personless_categories, default_person, other_people_categories,
        )

    print("\nProcessing complete")
    print(f"  Files imported: {imported_files}")
    print(f"  Duplicate files skipped: {duplicate_files}")
    print(f"  Files with errors (left in inbox): {failed_files}")
    print(f"  New transactions: {new_transactions}")
    print(f"  Repeated transaction rows merged: {repeated_transactions}")
    print(f"  Transactions categorized this run: {categorized}")
    print(f"  People assigned this run: {bills_assigned}")
    if migrated_rules:
        print(f"  Older rules safely assigned to an account: {migrated_rules}")
    print(f"  Months currently reported: {', '.join(months) if months else 'none'}")
    if months:
        latest = months[-1]
        summary = month_summary(
            [transaction for transaction in transactions if transaction["date"].startswith(latest)],
            personless_categories, default_person, other_people_categories,
        )
        print(f"\nLatest month ({latest})")
        print(f"  Income: ${summary['income']:,.2f}")
        print(f"  Estimated reimbursements: ${summary['reimbursements']:,.2f}")
        print(f"  Spending assigned to others: ${summary['other_people_spending']:,.2f}")
        print(f"  Others spending minus reimbursements: ${summary['other_people_after_reimbursements']:,.2f}")
        print(f"  Net spending: ${summary['net_spending']:,.2f}")
        print(f"  Uncategorized expenses/refunds: {summary['uncategorized_count']}")
        print(f"  Reports: {reports_dir / latest}")
    return 1 if failed_files else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Import local bank CSVs and generate monthly spending reports.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--data-dir", type=Path, default=Path.cwd() / "spending-data",
        help="Data directory (default: spending-data in the current directory)",
    )
    parser.add_argument("--non-interactive", action="store_true", help="Do not ask for categories; leave new expenses uncategorized")
    args = parser.parse_args()
    try:
        return process(args.data_dir.expanduser().resolve(), args.non_interactive)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Files already archived were imported safely; run again to continue.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
