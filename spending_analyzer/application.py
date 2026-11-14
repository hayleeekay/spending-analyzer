"""End-to-end import, categorization, persistence, and reporting workflow."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl

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


def _acquire_file_lock(descriptor: int) -> None:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_file_lock(descriptor: int) -> None:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def _profile_lock(root: Path):
    # Keep the marker file. The OS lock, not the filename, owns the active run.
    lock_path = root / ".spending-analyzer.lock"
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b" ")
        try:
            _acquire_file_lock(descriptor)
        except OSError as exc:
            raise RuntimeError(
                "Another Spending Analyzer run is using this profile."
            ) from exc
        acquired = True
        os.lseek(descriptor, 0, os.SEEK_SET)
        owner = str(os.getpid()).encode("ascii")
        os.write(descriptor, owner)
        os.ftruncate(descriptor, len(owner))
        os.fsync(descriptor)
        yield
    finally:
        try:
            if acquired:
                _release_file_lock(descriptor)
        finally:
            os.close(descriptor)


def _require_profile_path(root: Path, path: Path) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
        raise ValueError(f"Managed file path leaves the profile folder: {path}")
    return path


def _prepare_managed_directories(root: Path, directories: list[Path]) -> None:
    for directory in directories:
        if directory.is_symlink():
            raise ValueError(f"Managed profile folders cannot be symlinks: {directory}")
        _require_profile_path(root, directory / ".profile-path-check")
        directory.mkdir(parents=True, exist_ok=True)
    resolved = [directory.resolve() for directory in directories]
    for index, first in enumerate(resolved):
        for second in resolved[index + 1:]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError("Managed profile folders must not overlap")


def _unique_profile_destination(root: Path, directory: Path, name: str) -> Path:
    _require_profile_path(root, directory / name)
    destination = unique_destination(directory, name)
    return _require_profile_path(root, destination)


def _require_archive_path(root: Path, path: Path) -> Path:
    _require_profile_path(root, path)
    archive_root = (root / "archive").resolve()
    resolved = path.resolve()
    if path.is_symlink() or resolved == archive_root or archive_root not in resolved.parents:
        raise ValueError(f"Managed archive path leaves the archive folder: {path}")
    return path


def _unique_archive_destination(root: Path, directory: Path, name: str) -> Path:
    _require_archive_path(root, directory / name)
    destination = unique_destination(directory, name)
    return _require_archive_path(root, destination)


def _recorded_archive_path(root: Path, import_record: dict[str, Any]) -> Path:
    relative = Path(str(import_record.get("archived_path", "")))
    if (
        relative.is_absolute() or not relative.parts
        or relative.parts[0] != "archive" or ".." in relative.parts
    ):
        raise ValueError(f"Invalid archived path in import history: {relative}")
    return _require_archive_path(root, root / relative)


def _file_matches(path: Path, expected_hash: str) -> bool:
    return path.is_file() and sha256_file(path) == expected_hash


def _require_claim_path(root: Path, path: Path) -> Path:
    claims_root = (root / "data/pending-imports/claims").resolve()
    resolved = path.resolve()
    if path.is_symlink() or resolved == claims_root or claims_root not in resolved.parents:
        raise ValueError(f"Invalid claimed import path: {path}")
    return path


def _claim_paths(claims_dir: Path, source_name: str) -> tuple[Path, Path]:
    claim_id = stable_hash(source_name)[:20]
    return claims_dir / f"{claim_id}.csv", claims_dir / f"{claim_id}.json"


def _claim_public_source(
    root: Path, imports_dir: Path, claims_dir: Path, source: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    if source.is_symlink() or source.parent.resolve() != imports_dir.resolve():
        raise ValueError(f"Import sources must be regular files in the Import folder: {source}")
    claim_path, metadata_path = _claim_paths(claims_dir, source.name)
    _require_claim_path(root, claim_path)
    if claim_path.exists() or metadata_path.exists():
        raise OSError(f"An earlier import claim still needs recovery: {source.name}")
    metadata = {
        "source_name": source.name,
        "claimed_name": claim_path.name,
        "archive_path": "",
    }
    save_json(metadata_path, metadata)
    try:
        source.replace(claim_path)
    except BaseException:
        if not claim_path.exists():
            metadata_path.unlink(missing_ok=True)
        raise
    return claim_path, metadata_path, metadata


def _restore_uncommitted_claim(
    root: Path, imports_dir: Path, claim_path: Path, metadata_path: Path,
    source_name: str,
) -> None:
    if claim_path.exists():
        while True:
            destination = _unique_profile_destination(root, imports_dir, source_name)
            try:
                # A hard link claims the destination without overwriting a newer file.
                os.link(claim_path, destination, follow_symlinks=False)
            except FileExistsError:
                continue
            claim_path.unlink()
            break
    metadata_path.unlink(missing_ok=True)


def _load_claim(
    root: Path, claims_dir: Path, metadata_path: Path,
) -> tuple[Path, dict[str, Any]]:
    metadata = load_json(metadata_path, {})
    claimed_name = Path(str(metadata.get("claimed_name", "")))
    if claimed_name.name != str(claimed_name) or not metadata.get("source_name"):
        raise ValueError(f"Invalid import claim metadata: {metadata_path.name}")
    claim_path = _require_claim_path(root, claims_dir / claimed_name)
    return claim_path, metadata


def _archive_source(root: Path, source: Path, destination: Path, expected_hash: str) -> None:
    _require_claim_path(root, source)
    _require_archive_path(root, destination)
    if source.resolve() == destination.resolve():
        raise ValueError("Claimed import and archive destination must be different files")
    if not _file_matches(source, expected_hash):
        raise OSError(f"Import source is missing or changed: {source.name}")
    if _file_matches(destination, expected_hash):
        source.unlink()
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    _require_archive_path(root, destination)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=".spending-archive-", suffix=".tmp", delete=False,
    ) as temporary_handle:
        temporary = Path(temporary_handle.name)
    try:
        shutil.copy2(source, temporary)
        if not _file_matches(temporary, expected_hash):
            raise OSError(f"Archived copy did not match its source: {source.name}")
        temporary.replace(destination)
        if not _file_matches(destination, expected_hash):
            raise OSError(f"Archived copy could not be verified: {source.name}")
        source.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def _find_import_source(imports_dir: Path, source_name: str, expected_hash: str) -> Path | None:
    preferred = imports_dir / source_name
    candidates = [preferred] + [
        path for path in sorted(imports_dir.iterdir())
        if path.is_file() and path.suffix.lower() == ".csv" and path != preferred
    ]
    return next((path for path in candidates if _file_matches(path, expected_hash)), None)


def _duplicate_archive_destination(
    root: Path, directory: Path, name: str, expected_hash: str,
) -> Path:
    candidate = directory / name
    if _file_matches(candidate, expected_hash):
        return _require_archive_path(root, candidate)
    return _unique_archive_destination(root, directory, name)


def _merge_staged_import(
    pending: dict[str, Any], existing: dict[str, dict[str, Any]],
    imports: list[dict[str, Any]], imports_by_hash: dict[str, dict[str, Any]],
    transaction_dir: Path, import_path: Path,
) -> None:
    for staged in pending["transactions"]:
        current = existing.get(staged["id"])
        if current is None:
            existing[staged["id"]] = staged
            continue
        sources = current.setdefault("source_files", [])
        for source_name in staged.get("source_files", []):
            if source_name not in sources:
                sources.append(source_name)

    import_record = pending["import_record"]
    file_hash = pending["sha256"]
    if file_hash not in imports_by_hash:
        imports.append(import_record)
        imports_by_hash[file_hash] = import_record
    save_year_shards(transaction_dir, list(existing.values()))
    save_json(import_path, imports)


def _finish_pending_import(
    root: Path, imports_dir: Path, claims_dir: Path, pending_path: Path,
    existing: dict[str, dict[str, Any]], imports: list[dict[str, Any]],
    imports_by_hash: dict[str, dict[str, Any]], transaction_dir: Path, import_path: Path,
) -> dict[str, Any]:
    pending = load_json(pending_path, {})
    file_hash = pending.get("sha256", "")
    if not file_hash or not isinstance(pending.get("transactions"), list):
        raise ValueError(f"Invalid pending import record: {pending_path.name}")
    _merge_staged_import(
        pending, existing, imports, imports_by_hash, transaction_dir, import_path,
    )
    import_record = pending["import_record"]
    destination = _recorded_archive_path(root, import_record)
    metadata_path: Path | None = None
    source: Path | None = None
    if pending.get("claim_metadata"):
        metadata_path = claims_dir / Path(pending["claim_metadata"]).name
        source, _ = _load_claim(root, claims_dir, metadata_path)
    if source is None or not source.exists():
        public_source = _find_import_source(
            imports_dir, pending["source_name"], file_hash,
        )
        if public_source is not None:
            if metadata_path is None:
                source, metadata_path, _ = _claim_public_source(
                    root, imports_dir, claims_dir, public_source,
                )
                pending["claim_metadata"] = metadata_path.name
                pending["claimed_name"] = source.name
                save_json(pending_path, pending)
            else:
                public_source.replace(source)

    if source is not None and source.exists():
        _archive_source(root, source, destination, file_hash)
    elif not _file_matches(destination, file_hash):
        raise OSError(
            f"The original for pending import {pending['source_name']} is missing. "
            "Put that CSV back in the Import folder or provide it with --import-from."
        )
    pending_path.unlink()
    if metadata_path is not None:
        metadata_path.unlink(missing_ok=True)
    return import_record


def _archive_is_complete(root: Path, import_record: dict[str, Any]) -> bool:
    try:
        return _file_matches(
            _recorded_archive_path(root, import_record), import_record["sha256"],
        )
    except (KeyError, OSError, ValueError):
        return False


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
    with _profile_lock(root):
        return _process_locked(
            root, non_interactive, profile_name=profile_name,
            external_import_source=external_import_source,
        )


def _process_locked(
    root: Path,
    non_interactive: bool,
    *,
    profile_name: str | None = None,
    external_import_source: Path | None = None,
) -> int:

    imports_dir = root / "imports"
    archive_dir = root / "archive"
    data_dir = root / "data"
    rules_dir = root / "rules"
    reports_dir = root / "reports"
    _prepare_managed_directories(
        root, [imports_dir, archive_dir, data_dir, rules_dir, reports_dir],
    )

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
    pending_dir = data_dir / "pending-imports"
    claims_dir = pending_dir / "claims"
    _require_profile_path(root, pending_dir / ".profile-path-check")
    pending_dir.mkdir(parents=True, exist_ok=True)
    _require_profile_path(root, claims_dir / ".profile-path-check")
    if claims_dir.is_symlink():
        raise ValueError(f"Managed claim folder cannot be a symlink: {claims_dir}")
    claims_dir.mkdir(parents=True, exist_ok=True)
    orphaned_claims = sorted(
        claim for claim in claims_dir.glob("*.csv")
        if not claim.with_suffix(".json").is_file()
    )
    if orphaned_claims:
        names = ", ".join(claim.name for claim in orphaned_claims)
        print(
            f"ERROR: Claimed import metadata is missing for: {names}. "
            "The claimed CSV was kept unchanged for manual recovery.",
            file=sys.stderr,
        )
        return 1
    for stale_external_copy in imports_dir.glob(".external-import-*.tmp"):
        stale_external_copy.unlink()
    for stale_snapshot in pending_dir.glob(".source-*.tmp"):
        stale_snapshot.unlink()
    for stale_archive_copy in archive_dir.rglob(".spending-archive-*.tmp"):
        stale_archive_copy.unlink()
    transactions = load_year_shards(transaction_dir)
    for transaction in transactions:
        transaction["identifier"] = clean_identifier_value(transaction["provider"], transaction["identifier"])
    imports = load_json(import_path, [])
    known_files = {item["sha256"] for item in imports}
    imports_by_hash = {item["sha256"]: item for item in imports}
    pending_paths = sorted(pending_dir.glob("*.json"))
    pending_by_hash: dict[str, dict[str, Any]] = {}
    for pending_path in pending_paths:
        pending = load_json(pending_path, {})
        if pending.get("sha256"):
            pending_by_hash[pending["sha256"]] = pending
    if external_import_source:
        queued_hashes = {
            sha256_file(path)
            for path in imports_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".csv"
        }
        copied = already_seen = 0
        for source in sorted(external_import_source.iterdir()):
            if not source.is_file() or source.suffix.lower() != ".csv":
                continue
            source_hash = sha256_file(source)
            recovery_record = imports_by_hash.get(source_hash)
            if recovery_record is None and source_hash in pending_by_hash:
                recovery_record = pending_by_hash[source_hash].get("import_record")
            archive_complete = bool(
                recovery_record and _archive_is_complete(root, recovery_record)
            )
            if source_hash in queued_hashes or archive_complete:
                already_seen += 1
                continue
            destination = _unique_profile_destination(root, imports_dir, source.name)
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
            queued_hashes.add(source_hash)
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
    imported_files = duplicate_files = recovered_files = failed_files = 0
    new_transactions = repeated_transactions = 0
    for pending_path in pending_paths:
        try:
            import_record = _finish_pending_import(
                root, imports_dir, claims_dir, pending_path,
                existing, imports, imports_by_hash,
                transaction_dir, import_path,
            )
            file_hash = import_record["sha256"]
            known_files.add(file_hash)
            recovered_files += 1
            print(f"Completed interrupted import: {import_record['original_filename']}")
        except Exception as exc:
            failed_files += 1
            print(f"ERROR: Could not recover {pending_path.name}: {exc}", file=sys.stderr)
            break

    claimed_items: list[tuple[Path, Path, dict[str, Any]]] = []
    if not failed_files:
        for metadata_path in sorted(claims_dir.glob("*.json")):
            try:
                claim_path, metadata = _load_claim(root, claims_dir, metadata_path)
                if not claim_path.exists():
                    expected_hash = metadata.get("sha256", "")
                    archived_path = metadata.get("archive_path", "")
                    if expected_hash and archived_path and _file_matches(
                        _recorded_archive_path(root, {"archived_path": archived_path}),
                        expected_hash,
                    ):
                        metadata_path.unlink()
                        continue
                    public_source = (
                        _find_import_source(
                            imports_dir, metadata["source_name"], expected_hash,
                        )
                        if expected_hash else imports_dir / metadata["source_name"]
                    )
                    if not public_source.is_file() or public_source.is_symlink():
                        raise OSError(
                            f"Claimed import is missing: {metadata['source_name']}"
                        )
                    public_source.replace(claim_path)
                claimed_items.append((claim_path, metadata_path, metadata))
            except Exception as exc:
                failed_files += 1
                print(f"ERROR: Could not recover {metadata_path.name}: {exc}", file=sys.stderr)
                break

    csv_paths: list[Path] = []
    if not failed_files:
        csv_paths = sorted(
            path for path in imports_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".csv"
        )
        if not csv_paths and not claimed_items:
            print("No new CSV files found.")
            if existing:
                print("Using the existing transaction history; skipped categories can still be completed.")

    work_items: list[tuple[Path, Path | None, dict[str, Any] | None]] = [
        (claim_path, metadata_path, metadata)
        for claim_path, metadata_path, metadata in claimed_items
    ] + [(path, None, None) for path in csv_paths]
    for path, metadata_path, metadata in work_items:
        pending_created = False
        resume_needed = False
        try:
            if metadata is None or metadata_path is None:
                path, metadata_path, metadata = _claim_public_source(
                    root, imports_dir, claims_dir, path,
                )
            source_name = metadata["source_name"]
            file_hash = sha256_file(path)
            saved_hash = metadata.get("sha256", "")
            if saved_hash and saved_hash != file_hash:
                raise OSError(f"Claimed import changed unexpectedly: {source_name}")
            metadata["sha256"] = file_hash
            save_json(metadata_path, metadata)
            if file_hash in known_files:
                import_record = imports_by_hash[file_hash]
                if metadata.get("archive_path"):
                    destination = _recorded_archive_path(
                        root, {"archived_path": metadata["archive_path"]},
                    )
                elif _archive_is_complete(root, import_record):
                    duplicate_directory = archive_dir / "duplicates"
                    destination = _duplicate_archive_destination(
                        root, duplicate_directory, source_name, file_hash,
                    )
                else:
                    destination = _recorded_archive_path(root, import_record)
                metadata["archive_path"] = str(destination.relative_to(root))
                save_json(metadata_path, metadata)
                resume_needed = True
                primary_destination = _recorded_archive_path(root, import_record)
                _archive_source(root, path, destination, file_hash)
                metadata_path.unlink()
                if destination == primary_destination:
                    print(f"Restored missing or incomplete archive: {source_name}")
                    recovered_files += 1
                else:
                    print(f"Duplicate file archived without reimporting: {source_name}")
                    duplicate_files += 1
                continue

            provider, rows = read_csv(path)
            account = account_id(provider, source_name)
            occurrence: Counter[str] = Counter()
            file_new = file_repeated = 0
            staged_transactions: list[dict[str, Any]] = []
            dates: list[str] = []
            for row in rows:
                transaction = normalize_row(provider, account, row, source_name, settings)
                base = transaction_base_key(transaction)
                occurrence[base] += 1
                transaction_id = stable_hash(base, occurrence[base])
                transaction["id"] = transaction_id
                staged_transactions.append(transaction)
                dates.append(transaction["date"])
                if transaction_id in existing:
                    file_repeated += 1
                else:
                    file_new += 1
            start = min(dates) if dates else "empty"
            end = max(dates) if dates else "empty"
            archive_name = f"{account}_{start}_to_{end}_{file_hash[:8]}.csv"
            destination = _unique_archive_destination(
                root, archive_dir / account, archive_name,
            )
            import_record = {
                "sha256": file_hash,
                "original_filename": source_name,
                "archived_path": str(destination.relative_to(root)),
                "provider": provider,
                "account": account,
                "date_range": [start, end],
                "rows": len(rows),
                "new_transactions": file_new,
                "repeated_transactions": file_repeated,
                "processed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            pending_path = pending_dir / f"{file_hash}.json"
            save_json(pending_path, {
                "sha256": file_hash,
                "source_name": source_name,
                "claim_metadata": metadata_path.name,
                "claimed_name": path.name,
                "transactions": staged_transactions,
                "import_record": import_record,
            })
            pending_created = True
            _finish_pending_import(
                root, imports_dir, claims_dir, pending_path,
                existing, imports, imports_by_hash,
                transaction_dir, import_path,
            )
            known_files.add(file_hash)
            imported_files += 1
            new_transactions += file_new
            repeated_transactions += file_repeated
            print(f"Imported {source_name}: {file_new} new, {file_repeated} already known")
        except Exception as exc:
            source_name = metadata.get("source_name", path.name) if metadata else path.name
            if metadata is not None and metadata_path is not None and not pending_created and not resume_needed:
                _restore_uncommitted_claim(
                    root, imports_dir, path, metadata_path, source_name,
                )
            failed_files += 1
            print(f"ERROR: Could not import {source_name}: {exc}", file=sys.stderr)
            break

    if not failed_files:
        for import_record in imports:
            if _archive_is_complete(root, import_record):
                continue
            failed_files += 1
            print(
                f"ERROR: Archived original is missing or incomplete for "
                f"{import_record.get('original_filename', 'an imported CSV')}. "
                "Put the original CSV in the Import folder or provide it with --import-from.",
                file=sys.stderr,
            )
            break

    transactions = list(existing.values())
    if failed_files:
        print("\nProcessing stopped before categorization and report generation.")
        print(f"  Files needing attention: {failed_files}")
        print("  Existing reports were not regenerated and may not include unresolved imports.")
        print("  Original files were kept for retry. Fix or remove the unresolved CSVs, then run again.")
        return 1
    if not transactions:
        print(f"Next: add bank CSV exports to {imports_dir} and run this command again.")
        return 0
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
            profile_name=profile_name or "",
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
