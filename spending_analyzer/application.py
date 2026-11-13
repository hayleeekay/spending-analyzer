"""End-to-end import, categorization, persistence, and reporting workflow."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

from .categorization import (
    apply_rules_and_decisions,
    ask_bill_responsibility,
    migrate_legacy_rules,
    prompt_for_bill_responsibilities,
    prompt_for_categories,
)
from .constants import CATEGORIES
from .providers import (
    account_id,
    clean_identifier_value,
    normalize_row,
    read_csv,
    transaction_base_key,
    unique_destination,
)
from .reports import generate_html, generate_xlsx, month_summary, write_month_csv
from .storage import (
    load_decision_shards,
    load_json,
    load_settings,
    load_year_shards,
    save_decision_shards,
    save_json,
    save_year_shards,
    sha256_file,
    stable_hash,
)
from .workspaces import LegacyWorkspaceError

def process(
    root: Path,
    non_interactive: bool,
    *,
    profile_name: str | None = None,
    external_import_source: Path | None = None,
) -> int:
    legacy_folders = [path for path in (root / "inbox", root / "processed") if path.exists()]
    if legacy_folders:
        raise LegacyWorkspaceError(
            f"An older workspace was found at {root}. "
            "Convert it explicitly with --migrate-profile NAME."
        )

    imports_dir = root / "imports"
    archive_dir = root / "archive"
    data_dir = root / "data"
    rules_dir = root / "rules"
    reports_dir = root / "reports"
    for directory in (imports_dir, archive_dir, data_dir, rules_dir, reports_dir):
        directory.mkdir(parents=True, exist_ok=True)

    if profile_name:
        print(f"Profile: {profile_name}")
    print(f"Profile folder: {root}")
    print(f"Import folder: {imports_dir}")

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
    known_files = {item["sha256"] for item in imports}
    imports_by_hash = {item["sha256"]: item for item in imports}
    if external_import_source:
        pending_hashes = {
            sha256_file(path)
            for path in imports_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".csv"
        }
        copied = already_seen = 0
        for source in sorted(external_import_source.iterdir()):
            if not source.is_file() or source.suffix.lower() != ".csv":
                continue
            source_hash = sha256_file(source)
            if source_hash in known_files or source_hash in pending_hashes:
                already_seen += 1
                continue
            destination = unique_destination(imports_dir, source.name)
            with tempfile.NamedTemporaryFile(
                dir=imports_dir, prefix=".external-import-", suffix=".tmp", delete=False,
            ) as temporary_handle:
                temporary = Path(temporary_handle.name)
            try:
                shutil.copy2(source, temporary)
                if sha256_file(temporary) != source_hash:
                    raise OSError(f"External CSV changed while it was being copied: {source}")
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            pending_hashes.add(source_hash)
            copied += 1
        print(f"External import source: {external_import_source} (read-only)")
        print(f"External CSVs: {copied} copied, {already_seen} already known")
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
    rule_count = f"{len(rules)} {'rule' if len(rules) == 1 else 'rules'}"
    category_count = f"{len(categories)} {'category' if len(categories) == 1 else 'categories'}"
    people_count = f"{len(people)} {'person' if len(people) == 1 else 'people'}"
    print(f"Setup: {rule_count}, {category_count}, {people_count}")
    existing = {transaction["id"]: transaction for transaction in transactions}
    misplaced_csvs = sorted(path.name for path in root.iterdir() if path.is_file() and path.suffix.lower() == ".csv")
    if misplaced_csvs:
        print(
            f"WARNING: {len(misplaced_csvs)} CSV file(s) in the workspace root were not imported.\n"
            f"Move them to the Import folder: {imports_dir}",
            file=sys.stderr,
        )
    csv_paths = sorted(
        path for path in imports_dir.iterdir()
        if path.is_file() and path.suffix.lower() == ".csv"
    )
    if not csv_paths:
        print("No new CSV files found.")
        if not transactions:
            print(f"Next: add bank CSV exports to {imports_dir} and run this command again.")
            return 0
        print("Using the existing transaction history; skipped categories can still be completed.")

    imported_files = duplicate_files = recovered_files = failed_files = 0
    new_transactions = repeated_transactions = 0
    for path in csv_paths:
        file_hash = sha256_file(path)
        if file_hash in known_files:
            try:
                import_record = imports_by_hash[file_hash]
                recorded_path = Path(import_record["archived_path"])
                destination = (root / recorded_path).resolve()
                if archive_dir.resolve() not in destination.parents:
                    raise ValueError(f"Invalid archived path in import history: {recorded_path}")
                if destination.exists():
                    duplicate_destination = unique_destination(archive_dir / "duplicates", path.name)
                    shutil.move(str(path), duplicate_destination)
                    print(f"Duplicate file archived without reimporting: {path.name}")
                    duplicate_files += 1
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), destination)
                    print(f"Completed interrupted archive move: {path.name}")
                    recovered_files += 1
            except Exception as exc:
                failed_files += 1
                print(f"ERROR: Could not finish importing {path.name}: {exc}", file=sys.stderr)
            continue
        try:
            provider, rows = read_csv(path)
            account = account_id(provider, path.name)
            occurrence: Counter[str] = Counter()
            file_new = file_repeated = 0
            file_new_transactions: dict[str, dict[str, Any]] = {}
            repeated_transaction_ids: list[str] = []
            dates: list[str] = []
            for row in rows:
                transaction = normalize_row(provider, account, row, path.name, settings)
                base = transaction_base_key(transaction)
                occurrence[base] += 1
                transaction_id = stable_hash(base, occurrence[base])
                transaction["id"] = transaction_id
                dates.append(transaction["date"])
                if transaction_id in existing:
                    repeated_transaction_ids.append(transaction_id)
                    file_repeated += 1
                else:
                    file_new_transactions[transaction_id] = transaction
                    file_new += 1
            start = min(dates) if dates else "empty"
            end = max(dates) if dates else "empty"
            archive_name = f"{account}_{start}_to_{end}_{file_hash[:8]}.csv"
            destination = unique_destination(archive_dir / account, archive_name)
            import_record = {
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
            }
            for transaction_id in repeated_transaction_ids:
                sources = existing[transaction_id].setdefault("source_files", [])
                if path.name not in sources:
                    sources.append(path.name)
            existing.update(file_new_transactions)
            imports.append(import_record)
            transactions = list(existing.values())
            save_year_shards(transaction_dir, transactions)
            save_json(import_path, imports)
            known_files.add(file_hash)
            imports_by_hash[file_hash] = import_record
            shutil.move(str(path), destination)
            imported_files += 1
            new_transactions += file_new
            repeated_transactions += file_repeated
            print(f"Imported {path.name}: {file_new} new, {file_repeated} already known")
        except Exception as exc:
            failed_files += 1
            print(f"ERROR: Could not import {path.name}: {exc}", file=sys.stderr)

    transactions = list(existing.values())
    # Persist successful imports before interactive questions so an interruption
    # leaves their source files available for a safe retry.
    save_year_shards(transaction_dir, transactions)
    save_json(import_path, imports)
    if failed_files:
        print("\nProcessing stopped before categorization and report generation.")
        print(f"  Files needing attention: {failed_files}")
        print("  Existing reports were not regenerated and may not include CSVs still in the Import folder.")
        print("  Fix or remove the unresolved CSVs, then run this command again.")
        return 1
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
    print(f"  Interrupted imports completed: {recovered_files}")
    print(f"  Files with errors (left in Import folder): {failed_files}")
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
    return 0
