import csv
from io import StringIO
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import spending_analyzer as analyzer
from spending_analyzer import application


def write_csv(path, headers, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def claimed_csvs(root):
    return list((root / "data/pending-imports/claims").glob("*.csv"))


class SpendingAnalyzerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "spending-data"
        self.imports = self.root / "imports"
        self.imports.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def test_settings_are_merged_and_validated(self):
        path = self.root / "rules/settings.json"
        analyzer.save_json(path, {"default_person": "Primary"})
        settings = analyzer.load_settings(path)
        self.assertEqual(settings["default_person"], "Primary")
        self.assertEqual(settings["incoming_checking_kinds"]["p2p"], "reimbursement")

        analyzer.save_json(path, {"incoming_checking_kinds": {"p2p": "maybe"}})
        with self.assertRaisesRegex(ValueError, "income.*reimbursement"):
            analyzer.load_settings(path)

    def test_unknown_csv_layout_explains_tested_formats(self):
        with self.assertRaisesRegex(ValueError, "Unrecognized CSV layout.*Chase credit requires"):
            analyzer.detect_provider(["Date", "Merchant Name", "Value"])

    def test_launcher_creates_and_selects_profiles(self):
        temporary_root = Path(self.temporary.name)
        project = temporary_root / "project"
        caller = temporary_root / "caller"
        source_root = Path(__file__).resolve().parents[1]
        shutil.copytree(source_root / "spending_analyzer", project / "spending_analyzer")
        shutil.copy2(source_root / "run.sh", project / "run.sh")
        python = project / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        python.chmod(0o755)
        caller.mkdir()

        def run_launcher(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [project / "run.sh", *arguments, "--non-interactive"],
                cwd=caller,
                check=check,
                capture_output=True,
                text=True,
            )

        profile_result = run_launcher("--profile", "Alex")
        alex_profile = project / "spending-data/profiles/alex"
        self.assertTrue((alex_profile / "imports").is_dir())
        self.assertTrue((alex_profile / "archive").is_dir())
        self.assertIn("Profile: alex", profile_result.stdout)
        self.assertIn(
            f"Import folder: {(alex_profile / 'imports').resolve()}",
            profile_result.stdout,
        )
        self.assertIn("Setup: 0 rules, 12 categories, 0 people", profile_result.stdout)

        analyzer.save_json(alex_profile / "rules/rules.json", [{
            "provider": "discover", "account": "discover", "pattern": "FICTIONAL",
            "match_type": "exact", "category": "Other", "reviewed": True,
        }])
        profile_result = run_launcher("--profile", "alex")
        self.assertIn("Setup: 1 rule, 12 categories, 0 people", profile_result.stdout)
        jordan_result = run_launcher("--profile", "jordan")
        self.assertIn("Setup: 0 rules, 12 categories, 0 people", jordan_result.stdout)
        self.assertFalse(
            (project / "spending-data/profiles/jordan/rules/rules.json").exists()
        )

        fictional_row = {
            "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
            "Description": "FICTIONAL", "Amount": "10.00", "Category": "Shopping",
        }
        for profile in ("alex", "jordan"):
            write_csv(
                project / f"spending-data/profiles/{profile}/imports/discover.csv",
                list(analyzer.PROVIDER_HEADERS["discover"]),
                [fictional_row],
            )
            run_launcher("--profile", profile)
        alex_transactions = json.loads(
            (project / "spending-data/profiles/alex/data/transactions/2026.json").read_text()
        )
        jordan_transactions = json.loads(
            (project / "spending-data/profiles/jordan/data/transactions/2026.json").read_text()
        )
        self.assertEqual(alex_transactions[0]["category"], "Other")
        self.assertEqual(jordan_transactions[0]["category"], "Uncategorized")

        alias_profile = project / "spending-data/profiles/alias"
        alias_profile.symlink_to(project / "spending-data/profiles/jordan", target_is_directory=True)
        jordan_before = (project / "spending-data/profiles/jordan/data/transactions/2026.json").read_text()
        alias_result = run_launcher("--profile", "alias", check=False)
        self.assertEqual(alias_result.returncode, 1)
        self.assertIn("cannot be a symbolic link", alias_result.stderr)
        self.assertEqual(
            (project / "spending-data/profiles/jordan/data/transactions/2026.json").read_text(),
            jordan_before,
        )

        absolute_storage = temporary_root / "absolute-storage"
        custom_profile = run_launcher(
            "--storage-dir", str(absolute_storage), "--profile", "alex"
        )
        self.assertTrue((absolute_storage / "profiles/alex/imports").is_dir())
        self.assertIn(
            f"Profile folder: {(absolute_storage / 'profiles/alex').resolve()}",
            custom_profile.stdout,
        )

        relative_profile = run_launcher(
            "--storage-dir", "./profile-base", "--profile", "alex"
        )
        self.assertTrue((caller / "profile-base/profiles/alex/imports").is_dir())
        self.assertFalse((project / "profile-base").exists())

        unsafe = run_launcher("--profile", "../outside", check=False)
        self.assertEqual(unsafe.returncode, 2)
        self.assertFalse((project / "outside").exists())
        reserved = run_launcher("--profile", "rules", check=False)
        self.assertEqual(reserved.returncode, 2)
        self.assertIn("reserved", reserved.stderr)

        multiple = run_launcher(check=False)
        self.assertEqual(multiple.returncode, 1)
        self.assertIn("More than one profile exists", multiple.stderr)
        self.assertIn("alex, jordan", multiple.stderr)

        single_storage = temporary_root / "single-storage"
        run_launcher("--storage-dir", str(single_storage), "--profile", "only")
        single = run_launcher("--storage-dir", str(single_storage))
        self.assertIn("Profile: only", single.stdout)

        help_result = subprocess.run(
            [project / "run.sh", "help"], cwd=caller, check=True,
            capture_output=True, text=True,
        )
        self.assertIn("quick start:", help_result.stdout)
        self.assertIn("--import-from", help_result.stdout)

    def test_storage_initialization_and_legacy_conversion_are_explicit(self):
        storage = Path(self.temporary.name) / "storage"
        storage.mkdir()
        (storage / "unrelated.txt").write_text("keep me", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Refusing to initialize"):
            analyzer.initialize_storage(storage)
        self.assertEqual((storage / "unrelated.txt").read_text(), "keep me")

        legacy = Path(self.temporary.name) / "legacy"
        (legacy / "inbox").mkdir(parents=True)
        (legacy / "processed/discover").mkdir(parents=True)
        (legacy / "processed/discover/original.csv").write_text(
            "fictional archive", encoding="utf-8",
        )
        (legacy / "data").mkdir()
        (legacy / "rules").mkdir()
        (legacy / "inbox/sample.csv").write_text("fictional", encoding="utf-8")
        analyzer.save_json(legacy / "data/imports.json", [{
            "sha256": "fictional",
            "archived_path": "processed/discover/original.csv",
        }])
        with self.assertRaises(analyzer.LegacyWorkspaceError):
            analyzer.initialize_storage(legacy)

        profile = analyzer.migrate_legacy_workspace(legacy, "alex")
        self.assertEqual(profile, legacy / "profiles/alex")
        self.assertTrue((profile / "imports/sample.csv").exists())
        self.assertTrue((profile / "archive").is_dir())
        self.assertTrue((profile / "data").is_dir())
        self.assertFalse((legacy / "inbox").exists())
        self.assertEqual(analyzer.profile_names(legacy), ["alex"])
        import_record = json.loads((profile / "data/imports.json").read_text())[0]
        self.assertEqual(import_record["archived_path"], "archive/discover/original.csv")
        self.assertTrue((profile / import_record["archived_path"]).exists())

        rollback = Path(self.temporary.name) / "rollback"
        (rollback / "inbox").mkdir(parents=True)
        (rollback / "data").mkdir()
        (rollback / "inbox/sample.csv").write_text("fictional", encoding="utf-8")
        real_move = shutil.move
        move_count = 0

        def fail_second_move(source, destination):
            nonlocal move_count
            move_count += 1
            if move_count == 2:
                raise OSError("fictional move failure")
            return real_move(source, destination)

        with patch(
            "spending_analyzer.workspaces.shutil.move", side_effect=fail_second_move,
        ):
            with self.assertRaisesRegex(OSError, "fictional move failure"):
                analyzer.migrate_legacy_workspace(rollback, "alex")
        self.assertTrue((rollback / "inbox/sample.csv").exists())
        self.assertTrue((rollback / "data").is_dir())
        self.assertFalse((rollback / "profiles").exists())

    def test_interrupted_legacy_conversion_rolls_back_every_move(self):
        real_move = shutil.move

        for timing in ("before", "after"):
            with self.subTest(timing=timing):
                legacy = Path(self.temporary.name) / f"interrupted-{timing}"
                (legacy / "inbox").mkdir(parents=True)
                (legacy / "data").mkdir()
                (legacy / "inbox/sample.csv").write_text("fictional", encoding="utf-8")
                move_count = 0

                def interrupt_second_move(source, destination):
                    nonlocal move_count
                    move_count += 1
                    if move_count == 2 and timing == "before":
                        raise KeyboardInterrupt()
                    result = real_move(source, destination)
                    if move_count == 2 and timing == "after":
                        raise KeyboardInterrupt()
                    return result

                with patch(
                    "spending_analyzer.workspaces.shutil.move",
                    side_effect=interrupt_second_move,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        analyzer.migrate_legacy_workspace(legacy, "alex")
                self.assertTrue((legacy / "inbox/sample.csv").exists())
                self.assertTrue((legacy / "data").is_dir())
                self.assertFalse((legacy / "profiles").exists())

                profile = analyzer.migrate_legacy_workspace(legacy, "alex")
                self.assertTrue((profile / "imports/sample.csv").exists())
                self.assertTrue((profile / "data").is_dir())

    def test_interrupt_after_marker_write_restores_legacy_workspace_for_retry(self):
        legacy = Path(self.temporary.name) / "marker-interrupted"
        (legacy / "inbox").mkdir(parents=True)
        (legacy / "processed/discover").mkdir(parents=True)
        (legacy / "data").mkdir()
        (legacy / "inbox/sample.csv").write_text("fictional import", encoding="utf-8")
        (legacy / "processed/discover/original.csv").write_text(
            "fictional archive", encoding="utf-8",
        )
        original_imports = [{
            "sha256": "fictional",
            "archived_path": "processed/discover/original.csv",
        }]
        analyzer.save_json(legacy / "data/imports.json", original_imports)
        real_save_json = analyzer.save_json

        def interrupt_after_marker(path, value):
            real_save_json(path, value)
            if path.name == analyzer.MARKER_NAME:
                raise KeyboardInterrupt()

        with patch(
            "spending_analyzer.workspaces.save_json",
            side_effect=interrupt_after_marker,
        ):
            with self.assertRaises(KeyboardInterrupt):
                analyzer.migrate_legacy_workspace(legacy, "alex")

        self.assertFalse((legacy / analyzer.MARKER_NAME).exists())
        self.assertTrue((legacy / "inbox/sample.csv").exists())
        self.assertTrue((legacy / "processed/discover/original.csv").exists())
        self.assertEqual(
            json.loads((legacy / "data/imports.json").read_text()),
            original_imports,
        )
        self.assertFalse((legacy / "profiles").exists())
        with self.assertRaises(analyzer.LegacyWorkspaceError):
            analyzer.initialize_storage(legacy)

        profile = analyzer.migrate_legacy_workspace(legacy, "alex")
        migrated_record = json.loads((profile / "data/imports.json").read_text())[0]
        self.assertEqual(
            migrated_record["archived_path"], "archive/discover/original.csv",
        )
        self.assertTrue((profile / migrated_record["archived_path"]).exists())

    def test_storage_rejects_marker_with_legacy_folders(self):
        storage = Path(self.temporary.name) / "inconsistent-storage"
        (storage / "inbox").mkdir(parents=True)
        analyzer.save_json(
            storage / analyzer.MARKER_NAME,
            {"format_version": analyzer.FORMAT_VERSION},
        )

        with self.assertRaisesRegex(analyzer.LegacyWorkspaceError, "both"):
            analyzer.initialize_storage(storage)
        self.assertFalse((storage / "profiles").exists())
        with patch("sys.stderr"):
            self.assertEqual(
                analyzer.main([
                    "--storage-dir", str(storage), "--profile", "alex",
                    "--non-interactive",
                ]),
                1,
            )
        self.assertFalse((storage / "profiles").exists())

    def test_public_process_rejects_legacy_workspace_before_creating_folders(self):
        legacy = Path(self.temporary.name) / "old-api"
        (legacy / "inbox").mkdir(parents=True)
        (legacy / "inbox/sample.csv").write_text("fictional", encoding="utf-8")

        with self.assertRaisesRegex(analyzer.LegacyWorkspaceError, "--migrate-profile"):
            analyzer.process(legacy, non_interactive=True)
        self.assertTrue((legacy / "inbox/sample.csv").exists())
        self.assertFalse((legacy / "imports").exists())
        self.assertFalse((legacy / "archive").exists())

    def test_external_import_source_is_copied_once_and_left_untouched(self):
        source = Path(self.temporary.name) / "external"
        source_file = source / "discover.csv"
        write_csv(
            source_file,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "FICTIONAL STORE", "Amount": "10.00",
                "Category": "Shopping",
            }],
        )
        self.assertEqual(
            analyzer.process(
                self.root, non_interactive=True, external_import_source=source,
            ),
            0,
        )
        self.assertTrue(source_file.exists())
        self.assertEqual(len(list((self.root / "archive/discover").glob("*.csv"))), 1)

        self.assertEqual(
            analyzer.process(
                self.root, non_interactive=True, external_import_source=source,
            ),
            0,
        )
        self.assertTrue(source_file.exists())
        self.assertEqual(len(list((self.root / "archive/discover").glob("*.csv"))), 1)

    def test_failed_external_copy_never_leaves_an_importable_partial_csv(self):
        source = Path(self.temporary.name) / "external"
        source_file = source / "discover.csv"
        write_csv(
            source_file,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "FICTIONAL STORE", "Amount": "10.00",
                "Category": "Shopping",
            }],
        )

        def leave_partial_copy(_source, destination):
            Path(destination).write_text(
                "Trans. Date,Post Date,Description,Amount,Category\n"
                "07/01/2026,07/02/2026,PARTIAL,1.00,Shopping\n",
                encoding="utf-8",
            )
            raise OSError("fictional interrupted copy")

        with patch(
            "spending_analyzer.application.shutil.copy2",
            side_effect=leave_partial_copy,
        ):
            with self.assertRaisesRegex(OSError, "fictional interrupted copy"):
                analyzer.process(
                    self.root, non_interactive=True, external_import_source=source,
                )
        self.assertEqual(list(self.imports.iterdir()), [])
        self.assertTrue(source_file.exists())

        self.assertEqual(
            analyzer.process(
                self.root, non_interactive=True, external_import_source=source,
            ),
            0,
        )
        transactions = analyzer.load_year_shards(self.root / "data/transactions")
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["raw_amount"], "10.00")

    def test_csv_in_workspace_root_is_not_imported(self):
        misplaced = self.root / "misplaced.csv"
        write_csv(
            misplaced,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "FICTIONAL STORE", "Amount": "10.00",
                "Category": "Shopping",
            }],
        )
        with patch("sys.stderr") as stderr:
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        self.assertTrue(misplaced.exists())
        self.assertIn("were not imported", stderr.write.call_args_list[0].args[0])
        self.assertFalse((self.root / "data/transactions/2026.json").exists())

    def test_all_providers_year_sharding_and_duplicate_file(self):
        write_csv(self.imports / "apple.csv", list(analyzer.PROVIDER_HEADERS["apple"]), [
            {"Transaction Date": "07/02/2026", "Clearing Date": "07/03/2026", "Description": "CAFE", "Merchant": "Cafe", "Category": "Restaurants", "Type": "Purchase", "Amount (USD)": "12.34"},
            {"Transaction Date": "07/04/2026", "Clearing Date": "07/05/2026", "Description": "PAYMENT", "Merchant": "Payment", "Category": "Payment", "Type": "Payment", "Amount (USD)": "-50.00"},
        ])
        write_csv(self.imports / "Chase5678_test.CSV", list(analyzer.PROVIDER_HEADERS["chase_credit"]), [
            {"Transaction Date": "06/01/2026", "Post Date": "06/02/2026", "Description": "STORE", "Category": "Shopping", "Type": "Sale", "Amount": "-20.00", "Memo": ""},
            {"Transaction Date": "06/03/2026", "Post Date": "06/03/2026", "Description": "STORE", "Category": "Shopping", "Type": "Return", "Amount": "5.00", "Memo": ""},
        ])
        write_csv(self.imports / "discover.csv", list(analyzer.PROVIDER_HEADERS["discover"]), [
            {"Trans. Date": "07/05/2026", "Post Date": "07/06/2026", "Description": "DINER", "Amount": "22.00", "Category": "Restaurants"},
            {"Trans. Date": "07/07/2026", "Post Date": "07/07/2026", "Description": "INTERNET PAYMENT - THANK YOU", "Amount": "-30.00", "Category": "Payments and Credits"},
        ])
        write_csv(self.imports / "activity.csv", list(analyzer.PROVIDER_HEADERS["amex"]), [
            {"Date": "07/08/2026", "Description": "MARKET", "Amount": "14.00", "Extended Details": "", "Appears On Your Statement As": "MARKET", "Address": "", "City/State": "", "Zip Code": "", "Country": "", "Reference": "r1", "Category": "Groceries"},
            {"Date": "07/09/2026", "Description": "MOBILE PAYMENT - THANK YOU", "Amount": "-14.00", "Extended Details": "", "Appears On Your Statement As": "MOBILE PAYMENT - THANK YOU", "Address": "", "City/State": "", "Zip Code": "", "Country": "", "Reference": "r2", "Category": ""},
        ])
        write_csv(self.imports / "Chase1234_test.CSV", list(analyzer.PROVIDER_HEADERS["chase_checking"]), [
            {"Details": "CREDIT", "Posting Date": "07/10/2026", "Description": "PAYROLL", "Amount": "1000", "Type": "ACH_CREDIT", "Balance": "1000", "Check or Slip #": ""},
            {"Details": "CREDIT", "Posting Date": "07/11/2026", "Description": "ZELLE FROM FRIEND", "Amount": "40", "Type": "PARTNERFI_TO_CHASE", "Balance": "1040", "Check or Slip #": ""},
            {"Details": "DSLIP", "Posting Date": "07/12/2026", "Description": "DEPOSIT", "Amount": "60", "Type": "DEPOSIT", "Balance": "1100", "Check or Slip #": ""},
            {"Details": "DEBIT", "Posting Date": "07/13/2026", "Description": "ATM WITHDRAWAL", "Amount": "-25", "Type": "ATM", "Balance": "1075", "Check or Slip #": ""},
        ])

        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        transactions = json.loads((self.root / "data/transactions/2026.json").read_text())
        kinds = {transaction["description"]: transaction["kind"] for transaction in transactions}
        self.assertEqual(kinds["PAYROLL"], "income")
        self.assertEqual(kinds["ZELLE FROM FRIEND"], "reimbursement")
        self.assertEqual(kinds["DEPOSIT"], "reimbursement")
        self.assertEqual(kinds["ATM WITHDRAWAL"], "expense")
        self.assertEqual(kinds["MOBILE PAYMENT - THANK YOU"], "card_payment")
        self.assertTrue((self.root / "reports/2026-07/spending-report.xlsx").exists())
        self.assertTrue((self.root / "reports/2026-07/spending-report.html").exists())
        self.assertTrue((self.root / "reports/2026-06/spending-report.xlsx").exists())
        self.assertEqual(len(list(self.imports.iterdir())), 0)
        from openpyxl import load_workbook
        workbook = load_workbook(self.root / "reports/2026-07/spending-report.xlsx", data_only=True)
        self.assertEqual(workbook.sheetnames[0], "Dashboard")
        self.assertEqual(workbook.sheetnames, ["Dashboard", "Categories", "People", "Bills", "Transactions"])
        self.assertEqual(workbook["Categories"]["A1"].value, "Category / Person")
        self.assertEqual(workbook["People"]["A1"].value, "Spending by person")
        self.assertEqual(
            workbook["Categories"].cell(workbook["Categories"].max_row, 1).value,
            "GRAND TOTAL",
        )
        transaction_sheet = workbook["Transactions"]
        headers = [cell.value for cell in transaction_sheet[1]]
        self.assertIn("Money Out", headers)
        self.assertIn("Money In", headers)
        self.assertIn("Cash Flow", headers)
        rows = list(transaction_sheet.iter_rows(min_row=2, values_only=True))
        cafe = next(row for row in rows if row[2] == "CAFE")
        self.assertEqual(cafe[6], 12.34)
        self.assertEqual(cafe[8], -12.34)

        write_csv(self.imports / "apple-overlap.csv", list(analyzer.PROVIDER_HEADERS["apple"]), [
            {"Transaction Date": "07/02/2026", "Clearing Date": "07/03/2026", "Description": "CAFE", "Merchant": "Cafe", "Category": "Restaurants", "Type": "Purchase", "Amount (USD)": "12.34"},
            {"Transaction Date": "08/01/2026", "Clearing Date": "08/02/2026", "Description": "SHOP", "Merchant": "Shop", "Category": "Other", "Type": "Purchase", "Amount (USD)": "9.99"},
        ])
        before_overlap = len(transactions)
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        after_overlap = json.loads((self.root / "data/transactions/2026.json").read_text())
        self.assertEqual(len(after_overlap), before_overlap + 1)
        self.assertTrue((self.root / "reports/2026-08/spending-report.xlsx").exists())

        archived = next((self.root / "archive/apple").glob("*.csv"))
        shutil.copy2(archived, self.imports / "same-download.csv")
        before = len(after_overlap)
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        after = len(json.loads((self.root / "data/transactions/2026.json").read_text()))
        self.assertEqual(before, after)
        self.assertEqual(len(list((self.root / "archive/duplicates").glob("*.csv"))), 1)

    def test_failed_file_does_not_persist_partial_transactions(self):
        existing_row = {
            "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
            "Description": "EXISTING", "Amount": "10.00", "Category": "Shopping",
        }
        write_csv(
            self.imports / "initial.csv",
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [existing_row],
        )
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        transaction_path = self.root / "data/transactions/2026.json"
        before = json.loads(transaction_path.read_text())

        failed_path = self.imports / "failed.csv"
        write_csv(
            failed_path,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [
                existing_row,
                {
                    "Trans. Date": "07/03/2026", "Post Date": "07/04/2026",
                    "Description": "NEW ROW", "Amount": "12.00", "Category": "Shopping",
                },
                {
                    "Trans. Date": "NOT A DATE", "Post Date": "07/05/2026",
                    "Description": "BROKEN", "Amount": "9.00", "Category": "Shopping",
                },
            ],
        )
        with patch("sys.stderr"):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)

        self.assertEqual(json.loads(transaction_path.read_text()), before)
        imports = json.loads((self.root / "data/imports.json").read_text())
        self.assertEqual([item["original_filename"] for item in imports], ["initial.csv"])
        self.assertTrue(failed_path.exists())

    def test_archive_failure_is_completed_safely_on_retry(self):
        source_path = self.imports / "discover.csv"
        write_csv(
            source_path,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
            }],
        )
        source_hash = analyzer.sha256_file(source_path)
        real_copy = shutil.copy2

        def leave_partial_archive(copy_source, destination):
            if "archive" in Path(destination).parts:
                Path(destination).write_text("partial", encoding="utf-8")
                raise OSError("archive failed")
            return real_copy(copy_source, destination)

        with (
            patch("spending_analyzer.application.shutil.copy2", side_effect=leave_partial_archive),
            patch("sys.stderr"),
        ):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)

        self.assertEqual(len(analyzer.load_year_shards(self.root / "data/transactions")), 1)
        imports = json.loads((self.root / "data/imports.json").read_text())
        self.assertEqual(len(imports), 1)
        self.assertFalse(source_path.exists())
        self.assertEqual(len(claimed_csvs(self.root)), 1)
        self.assertEqual(len(list((self.root / "data/pending-imports").glob("*.json"))), 1)
        self.assertFalse((self.root / imports[0]["archived_path"]).exists())
        self.assertFalse((self.root / "reports/2026-07/spending-report.html").exists())

        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        self.assertFalse(source_path.exists())
        archived = self.root / imports[0]["archived_path"]
        self.assertEqual(analyzer.sha256_file(archived), source_hash)
        self.assertEqual(list((self.root / "data/pending-imports").glob("*.json")), [])
        self.assertEqual(len(analyzer.load_year_shards(self.root / "data/transactions")), 1)
        self.assertEqual(len(json.loads((self.root / "data/imports.json").read_text())), 1)

    def test_interrupted_persistence_keeps_source_available_for_retry(self):
        source_path = self.imports / "discover.csv"
        write_csv(
            source_path,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
            }],
        )

        with patch("spending_analyzer.application.save_year_shards", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                analyzer.process(self.root, non_interactive=True)

        self.assertFalse(source_path.exists())
        self.assertEqual(len(claimed_csvs(self.root)), 1)
        self.assertEqual(len(list((self.root / "data/pending-imports").glob("*.json"))), 1)
        self.assertEqual(list((self.root / "archive").rglob("*.csv")), [])
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        self.assertFalse(source_path.exists())
        self.assertEqual(len(analyzer.load_year_shards(self.root / "data/transactions")), 1)

    def test_partial_year_save_is_filled_in_without_duplicates_on_retry(self):
        source_path = self.imports / "Chase1234.csv"
        write_csv(
            source_path,
            list(analyzer.PROVIDER_HEADERS["chase_checking"]),
            [
                {
                    "Details": "CREDIT", "Posting Date": "12/31/2025",
                    "Description": "FIRST", "Amount": "10.00", "Type": "DEPOSIT",
                    "Balance": "10.00", "Check or Slip #": "",
                },
                {
                    "Details": "CREDIT", "Posting Date": "01/02/2026",
                    "Description": "SECOND", "Amount": "20.00", "Type": "DEPOSIT",
                    "Balance": "30.00", "Check or Slip #": "",
                },
            ],
        )

        def save_one_then_fail(directory, records):
            first = records[0]
            analyzer.save_json(directory / f"{first['date'][:4]}.json", [first])
            raise OSError("disk full")

        with (
            patch("spending_analyzer.application.save_year_shards", side_effect=save_one_then_fail),
            patch("sys.stderr"),
        ):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)

        self.assertFalse(source_path.exists())
        self.assertEqual(len(claimed_csvs(self.root)), 1)
        self.assertEqual(len(analyzer.load_year_shards(self.root / "data/transactions")), 1)
        analyzer.save_json(
            self.root / "rules/settings.json",
            {"incoming_checking_kinds": {"deposit": "income"}},
        )
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        transactions = analyzer.load_year_shards(self.root / "data/transactions")
        self.assertEqual(len(transactions), 2)
        self.assertEqual(len({transaction["id"] for transaction in transactions}), 2)
        self.assertEqual({transaction["kind"] for transaction in transactions}, {"reimbursement"})
        self.assertEqual(len(json.loads((self.root / "data/imports.json").read_text())), 1)

    def test_history_write_failure_stops_queue_and_recovers_before_duplicate(self):
        row = {
            "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
            "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
        }
        first = self.imports / "a.csv"
        second = self.imports / "b.csv"
        write_csv(first, list(analyzer.PROVIDER_HEADERS["discover"]), [row])
        shutil.copy2(first, second)
        real_save_json = analyzer.save_json
        failed_once = False

        def fail_first_history_write(path, value):
            nonlocal failed_once
            if Path(path).name == "imports.json" and not failed_once:
                failed_once = True
                raise OSError("history unavailable")
            real_save_json(path, value)

        with (
            patch("spending_analyzer.application.save_json", side_effect=fail_first_history_write),
            patch("sys.stderr"),
        ):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)

        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(len(claimed_csvs(self.root)), 1)
        self.assertEqual(len(list((self.root / "data/pending-imports").glob("*.json"))), 1)
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        self.assertEqual(len(analyzer.load_year_shards(self.root / "data/transactions")), 1)
        imports = json.loads((self.root / "data/imports.json").read_text())
        self.assertEqual(len(imports), 1)
        self.assertEqual(len(list((self.root / "archive/duplicates").glob("*.csv"))), 1)

    def test_external_source_restores_incomplete_known_archive(self):
        external = Path(self.temporary.name) / "external"
        source = external / "discover.csv"
        write_csv(
            source,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
            }],
        )
        source_hash = analyzer.sha256_file(source)
        self.assertEqual(
            analyzer.process(self.root, non_interactive=True, external_import_source=external), 0,
        )
        import_record = json.loads((self.root / "data/imports.json").read_text())[0]
        archived = self.root / import_record["archived_path"]
        archived.write_text("partial archive", encoding="utf-8")

        self.assertEqual(
            analyzer.process(self.root, non_interactive=True, external_import_source=external), 0,
        )
        self.assertTrue(source.exists())
        self.assertEqual(analyzer.sha256_file(archived), source_hash)
        self.assertEqual(len(analyzer.load_year_shards(self.root / "data/transactions")), 1)

    def test_external_source_can_restore_missing_pending_source(self):
        external = Path(self.temporary.name) / "external"
        source = external / "discover.csv"
        write_csv(
            source,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
            }],
        )
        real_copy = shutil.copy2

        def fail_archive_copy(copy_source, destination):
            if "archive" in Path(destination).parts:
                Path(destination).write_text("partial", encoding="utf-8")
                raise OSError("archive unavailable")
            return real_copy(copy_source, destination)

        with (
            patch("spending_analyzer.application.shutil.copy2", side_effect=fail_archive_copy),
            patch("sys.stderr"),
        ):
            self.assertEqual(
                analyzer.process(self.root, non_interactive=True, external_import_source=external), 1,
            )

        self.assertFalse((self.imports / "discover.csv").exists())
        claim = claimed_csvs(self.root)[0]
        claim.unlink()
        self.assertEqual(
            analyzer.process(self.root, non_interactive=True, external_import_source=external), 0,
        )
        import_record = json.loads((self.root / "data/imports.json").read_text())[0]
        self.assertEqual(
            analyzer.sha256_file(self.root / import_record["archived_path"]),
            analyzer.sha256_file(source),
        )
        self.assertEqual(list((self.root / "data/pending-imports").glob("*.json")), [])

    def test_staged_rows_and_hash_come_from_same_snapshot(self):
        source = self.imports / "discover.csv"
        headers = list(analyzer.PROVIDER_HEADERS["discover"])
        original_row = {
            "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
            "Description": "ORIGINAL", "Amount": "10.00", "Category": "Shopping",
        }
        replacement_row = original_row | {"Description": "REPLACEMENT", "Amount": "999.99"}
        write_csv(source, headers, [original_row])
        original = source.read_bytes()
        real_read_csv = analyzer.read_csv

        def replace_before_parse(snapshot):
            write_csv(source, headers, [replacement_row])
            return real_read_csv(snapshot)

        with patch("spending_analyzer.application.read_csv", side_effect=replace_before_parse):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)

        self.assertTrue(source.exists())
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        transactions = analyzer.load_year_shards(self.root / "data/transactions")
        self.assertEqual(len(transactions), 2)
        self.assertEqual(
            {(item["description"], item["raw_amount"]) for item in transactions},
            {("ORIGINAL", "10.00"), ("REPLACEMENT", "999.99")},
        )

    def test_archive_cleanup_does_not_delete_replacement_source(self):
        source = self.imports / "discover.csv"
        headers = list(analyzer.PROVIDER_HEADERS["discover"])
        original_row = {
            "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
            "Description": "ORIGINAL", "Amount": "10.00", "Category": "Shopping",
        }
        replacement_row = original_row | {"Description": "REPLACEMENT", "Amount": "20.00"}
        write_csv(source, headers, [original_row])
        real_copy = shutil.copy2

        def replace_after_archive_copy(copy_source, destination):
            result = real_copy(copy_source, destination)
            if "archive" in Path(destination).parts:
                write_csv(source, headers, [replacement_row])
            return result

        with patch(
            "spending_analyzer.application.shutil.copy2",
            side_effect=replace_after_archive_copy,
        ):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)

        self.assertTrue(source.exists())
        first_run = analyzer.load_year_shards(self.root / "data/transactions")
        self.assertEqual([item["description"] for item in first_run], ["ORIGINAL"])
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        transactions = analyzer.load_year_shards(self.root / "data/transactions")
        self.assertEqual(
            {item["description"] for item in transactions}, {"ORIGINAL", "REPLACEMENT"},
        )

    def test_claim_cleanup_never_deletes_new_public_replacement(self):
        source = self.imports / "discover.csv"
        headers = list(analyzer.PROVIDER_HEADERS["discover"])
        original_row = {
            "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
            "Description": "ORIGINAL", "Amount": "10.00", "Category": "Shopping",
        }
        replacement_row = original_row | {"Description": "REPLACEMENT", "Amount": "20.00"}
        write_csv(source, headers, [original_row])
        real_unlink = Path.unlink

        def replace_public_path_before_claim_cleanup(path, *args, **kwargs):
            if path.parent.name == "claims" and path.suffix == ".csv":
                write_csv(source, headers, [replacement_row])
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", replace_public_path_before_claim_cleanup):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)

        self.assertTrue(source.exists())
        self.assertIn("REPLACEMENT", source.read_text(encoding="utf-8"))
        self.assertEqual(
            [item["description"] for item in analyzer.load_year_shards(
                self.root / "data/transactions"
            )],
            ["ORIGINAL"],
        )

    def test_rejected_claim_restores_without_overwriting_new_public_file(self):
        source = self.imports / "discover.csv"
        headers = list(analyzer.PROVIDER_HEADERS["discover"])
        row = {
            "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
            "Description": "ORIGINAL", "Amount": "10.00", "Category": "Shopping",
        }
        write_csv(source, headers, [row])

        def reject_after_replacement(_claimed_source):
            write_csv(source, headers, [row | {"Description": "NEW FILE"}])
            raise ValueError("fictional rejection")

        with (
            patch("spending_analyzer.application.read_csv", side_effect=reject_after_replacement),
            patch("sys.stderr"),
        ):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)

        restored = sorted(self.imports.glob("*.csv"))
        self.assertEqual(len(restored), 2)
        self.assertEqual(list((self.root / "data/pending-imports/claims").iterdir()), [])

    def test_rejected_claim_restore_retries_if_destination_appears(self):
        source = self.imports / "discover.csv"
        headers = list(analyzer.PROVIDER_HEADERS["discover"])
        row = {
            "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
            "Description": "ORIGINAL", "Amount": "10.00", "Category": "Shopping",
        }
        write_csv(source, headers, [row])
        real_link = os.link
        attempted_destination = None

        def add_competing_file_before_restore(claim, destination, **kwargs):
            nonlocal attempted_destination
            if attempted_destination is None:
                attempted_destination = Path(destination)
                attempted_destination.write_text("newer download", encoding="utf-8")
            return real_link(claim, destination, **kwargs)

        with (
            patch("spending_analyzer.application.read_csv", side_effect=ValueError("rejected")),
            patch("spending_analyzer.application.os.link", side_effect=add_competing_file_before_restore),
            patch("sys.stderr"),
        ):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)

        self.assertEqual(attempted_destination.read_text(encoding="utf-8"), "newer download")
        restored = sorted(self.imports.glob("*.csv"))
        self.assertEqual(len(restored), 2)
        self.assertTrue(any("ORIGINAL" in path.read_text(encoding="utf-8") for path in restored))
        self.assertEqual(claimed_csvs(self.root), [])

    def test_pending_archive_symlink_cannot_alias_import_source(self):
        source = self.imports / "discover.csv"
        write_csv(
            source,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
            }],
        )
        real_copy = shutil.copy2

        def fail_archive_copy(copy_source, destination):
            if "archive" in Path(destination).parts:
                raise OSError("archive unavailable")
            return real_copy(copy_source, destination)

        with (
            patch("spending_analyzer.application.shutil.copy2", side_effect=fail_archive_copy),
            patch("sys.stderr"),
        ):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)

        claim = claimed_csvs(self.root)[0]
        import_record = json.loads((self.root / "data/imports.json").read_text())[0]
        destination = self.root / import_record["archived_path"]
        destination.symlink_to(claim)
        with patch("sys.stderr"):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)
        self.assertTrue(claim.exists())
        self.assertTrue(destination.is_symlink())
        self.assertEqual(len(list((self.root / "data/pending-imports").glob("*.json"))), 1)

    def test_pending_recovery_accepts_renamed_uppercase_csv(self):
        source = self.imports / "discover.csv"
        write_csv(
            source,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
            }],
        )
        real_copy = shutil.copy2

        def fail_archive_copy(copy_source, destination):
            if "archive" in Path(destination).parts:
                raise OSError("archive unavailable")
            return real_copy(copy_source, destination)

        with (
            patch("spending_analyzer.application.shutil.copy2", side_effect=fail_archive_copy),
            patch("sys.stderr"),
        ):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)

        claim = claimed_csvs(self.root)[0]
        restored = self.imports / "RESTORED.CSV"
        claim.replace(restored)
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        self.assertFalse(restored.exists())
        self.assertEqual(len(analyzer.load_year_shards(self.root / "data/transactions")), 1)

    def test_interrupted_duplicate_archive_reuses_published_copy(self):
        source = self.imports / "original.csv"
        write_csv(
            source,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
            }],
        )
        original = source.read_bytes()
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        duplicate = self.imports / "extra.csv"
        duplicate.write_bytes(original)
        duplicate_dir = self.root / "archive/duplicates"
        duplicate_dir.mkdir()
        (duplicate_dir / "extra.csv").write_text("different file", encoding="utf-8")
        real_unlink = Path.unlink

        def interrupt_source_cleanup(path, *args, **kwargs):
            if path.parent.name == "claims" and path.suffix == ".csv":
                raise KeyboardInterrupt()
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", interrupt_source_cleanup):
            with self.assertRaises(KeyboardInterrupt):
                analyzer.process(self.root, non_interactive=True)

        self.assertFalse(duplicate.exists())
        self.assertEqual(len(claimed_csvs(self.root)), 1)
        self.assertTrue((duplicate_dir / "extra_2.csv").exists())
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        self.assertFalse(duplicate.exists())
        duplicate_names = sorted(
            path.name for path in (self.root / "archive/duplicates").glob("*.csv")
        )
        self.assertEqual(duplicate_names, ["extra.csv", "extra_2.csv"])

    def test_interruption_before_journal_recovers_claimed_source(self):
        source = self.imports / "discover.csv"
        write_csv(
            source,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
            }],
        )
        with patch("spending_analyzer.application.read_csv", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                analyzer.process(self.root, non_interactive=True)

        self.assertFalse(source.exists())
        self.assertEqual(len(claimed_csvs(self.root)), 1)
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        self.assertEqual(claimed_csvs(self.root), [])
        self.assertEqual(len(analyzer.load_year_shards(self.root / "data/transactions")), 1)

    def test_interruption_immediately_after_claim_rename_keeps_metadata(self):
        source = self.imports / "discover.csv"
        write_csv(
            source,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
            }],
        )
        real_replace = Path.replace

        def interrupt_after_claim(path, target):
            result = real_replace(path, target)
            target = Path(target)
            if target.parent.name == "claims" and target.suffix == ".csv":
                raise KeyboardInterrupt()
            return result

        with patch.object(Path, "replace", interrupt_after_claim):
            with self.assertRaises(KeyboardInterrupt):
                analyzer.process(self.root, non_interactive=True)

        claims = claimed_csvs(self.root)
        self.assertEqual(len(claims), 1)
        self.assertTrue(claims[0].with_suffix(".json").is_file())
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        self.assertEqual(claimed_csvs(self.root), [])

    def test_orphaned_claim_is_reported_and_preserved(self):
        claims = self.root / "data/pending-imports/claims"
        claims.mkdir(parents=True)
        orphan = claims / "fictional.csv"
        orphan.write_text("private fictional data", encoding="utf-8")
        with patch("sys.stderr") as stderr:
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)
        self.assertTrue(orphan.exists())
        self.assertIn("metadata is missing", stderr.write.call_args_list[0].args[0])

    def test_abrupt_stop_before_journal_recovers_lock_and_claim(self):
        source = self.imports / "discover.csv"
        write_csv(
            source,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
            }],
        )
        child = """
import os
from pathlib import Path
import signal
import sys
from spending_analyzer import application

def stop_before_parse(_path):
    os.kill(os.getpid(), signal.SIGKILL)

application.read_csv = stop_before_parse
application.process(Path(sys.argv[1]), non_interactive=True)
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", child, str(self.root)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / ".spending-analyzer.lock").exists())
        self.assertEqual(len(claimed_csvs(self.root)), 1)

        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        self.assertTrue((self.root / ".spending-analyzer.lock").exists())
        self.assertEqual(claimed_csvs(self.root), [])
        self.assertEqual(len(analyzer.load_year_shards(self.root / "data/transactions")), 1)

    def test_managed_folders_cannot_overlap_through_symlinks(self):
        archive_account = self.root / "archive/discover"
        archive_account.mkdir(parents=True)
        self.imports.rmdir()
        self.imports.symlink_to(archive_account, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "cannot be symlinks"):
            analyzer.process(self.root, non_interactive=True)

    def test_existing_lock_marker_and_old_snapshot_are_recovered(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".spending-analyzer.lock").write_text("99999999", encoding="utf-8")
        pending = self.root / "data/pending-imports"
        pending.mkdir(parents=True)
        stale_snapshot = pending / ".source-fictional.tmp"
        stale_snapshot.write_text("fictional private copy", encoding="utf-8")
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        self.assertFalse(stale_snapshot.exists())
        self.assertTrue((self.root / ".spending-analyzer.lock").exists())

    def test_active_profile_lock_blocks_second_run(self):
        with application._profile_lock(self.root):
            with self.assertRaisesRegex(RuntimeError, "Another Spending Analyzer run"):
                analyzer.process(self.root, non_interactive=True)

    def test_archive_symlink_cannot_leave_profile(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        archive = self.root / "archive"
        archive.mkdir(parents=True)
        (archive / "discover").symlink_to(outside, target_is_directory=True)
        source = self.imports / "discover.csv"
        write_csv(
            source,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
            }],
        )

        with patch("sys.stderr"):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)

        self.assertTrue(source.exists())
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(analyzer.load_year_shards(self.root / "data/transactions"), [])

    def test_duplicate_archive_symlink_cannot_leave_profile(self):
        source = self.imports / "discover.csv"
        write_csv(
            source,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
            }],
        )
        original = source.read_bytes()
        self.assertEqual(analyzer.process(self.root, non_interactive=True), 0)
        source.write_bytes(original)
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (self.root / "archive/duplicates").symlink_to(outside, target_is_directory=True)

        with patch("sys.stderr"):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)

        self.assertTrue(source.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_unsupported_transaction_type_stays_unresolved_and_does_not_refresh_reports(self):
        report_path = self.root / "reports/2026-07/spending-report.html"
        report_path.parent.mkdir(parents=True)
        report_path.write_text("existing report", encoding="utf-8")
        source_path = self.imports / "chase.csv"
        write_csv(
            source_path,
            list(analyzer.PROVIDER_HEADERS["chase_credit"]),
            [{
                "Transaction Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "ACCOUNT FEE", "Category": "Fees", "Type": "FEE",
                "Amount": "-10.00", "Memo": "",
            }],
        )

        with patch("sys.stderr", new_callable=StringIO) as stderr:
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)

        self.assertIn("Unsupported Chase credit transaction type 'FEE'", stderr.getvalue())
        self.assertTrue(source_path.exists())
        self.assertEqual(analyzer.load_year_shards(self.root / "data/transactions"), [])
        self.assertEqual(analyzer.load_json(self.root / "data/imports.json", []), [])
        self.assertEqual(report_path.read_text(encoding="utf-8"), "existing report")

    def test_rule_application_does_not_use_bank_category(self):
        transaction = {
            "id": "one", "provider": "apple", "account": "apple", "identifier": "CAFE", "kind": "expense"
        }
        analyzer.apply_rules_and_decisions(
            [transaction], [{"provider": "apple", "account": "apple", "identifier": "CAFE", "category": "Dining"}], {}
        )
        self.assertEqual(transaction["category"], "Dining")
        other = {"id": "two", "provider": "apple", "account": "apple", "identifier": "UNKNOWN", "kind": "expense", "bank_category": "Restaurants"}
        analyzer.apply_rules_and_decisions([other], [], {})
        self.assertEqual(other["category"], "Uncategorized")

    def test_incoming_checking_treatment_is_configurable(self):
        p2p = {
            "Details": "CREDIT", "Description": "ZELLE FROM FRIEND",
            "Type": "PARTNERFI_TO_CHASE",
        }
        deposit = {
            "Details": "DSLIP", "Description": "DEPOSIT", "Type": "DEPOSIT",
        }
        settings = {"incoming_checking_kinds": {"p2p": "income", "deposit": "income"}}
        self.assertEqual(analyzer.classify_kind("chase_checking", p2p, analyzer.money("10"), settings), "income")
        self.assertEqual(analyzer.classify_kind("chase_checking", deposit, analyzer.money("10"), settings), "income")
        self.assertEqual(analyzer.classify_kind("chase_checking", p2p, analyzer.money("10")), "reimbursement")
        unknown_credit = {
            "Details": "CREDIT", "Description": "FICTIONAL CREDIT", "Type": "FUTURE_TYPE",
        }
        unknown_debit = {
            "Details": "DEBIT", "Description": "FICTIONAL DEBIT", "Type": "FUTURE_TYPE",
        }
        self.assertEqual(
            analyzer.classify_kind("chase_checking", unknown_credit, analyzer.money("10"), settings),
            "reimbursement",
        )
        self.assertEqual(
            analyzer.classify_kind("chase_checking", unknown_debit, analyzer.money("-10"), settings),
            "expense",
        )

    def test_other_people_comparison_uses_people_and_configured_categories(self):
        transactions = [
            {"kind": "expense", "raw_amount": "30", "category": "Dining", "responsibility": "Alex"},
            {"kind": "expense", "raw_amount": "20", "category": "Household Group", "responsibility": ""},
            {"kind": "expense", "raw_amount": "40", "category": "Shopping", "responsibility": "Primary"},
            {"kind": "reimbursement", "raw_amount": "35", "category": "Uncategorized", "responsibility": ""},
        ]
        summary = analyzer.month_summary(
            transactions, personless_categories={"Household Group"},
            default_person="Primary", other_people_categories={"Household Group"},
        )
        self.assertEqual(summary["other_people_spending"], analyzer.money("50"))
        self.assertEqual(summary["other_people_after_reimbursements"], analyzer.money("15"))

    def test_excel_bills_lists_dated_bills_under_each_person(self):
        transaction = {
            "provider": "apple", "account": "apple", "date": "2026-07-10",
            "post_date": "2026-07-11", "description": "ELECTRIC COMPANY",
            "kind": "expense", "raw_amount": "85.25", "category": "Bills",
            "responsibility": "Primary", "identifier": "ELECTRIC COMPANY",
            "bank_category": "Utilities", "bank_type": "Purchase", "splits": [],
        }
        destination = self.root / "bill-report.xlsx"
        analyzer.generate_xlsx(
            "2026-07", [transaction], destination, default_person="Primary",
        )
        from openpyxl import load_workbook
        bills = load_workbook(destination, data_only=True)["Bills"]
        values = list(bills.iter_rows(values_only=True))
        self.assertIn(("Primary", None, "1 bill(s)", None, None, 85.25), values)
        self.assertIn((None, "2026-07-10", "ELECTRIC COMPANY", "Apple Card", "Expense", 85.25), values)
        self.assertEqual(values[-1][0], "GRAND TOTAL")

    def test_excel_preserves_imported_and_user_text_without_formulas(self):
        transaction = {
            "provider": "apple", "account": "apple", "date": "2026-07-10",
            "post_date": "2026-07-11", "description": "=1+1",
            "kind": "expense", "raw_amount": "12.34", "category": "=CATEGORY",
            "responsibility": "=PERSON", "identifier": "=IDENTIFIER",
            "bank_category": "=BANK CATEGORY", "bank_type": "=BANK TYPE", "splits": [],
        }
        destination = self.root / "text-report.xlsx"
        analyzer.generate_xlsx(
            "2026-07", [transaction], destination, default_person="=PERSON",
        )

        from openpyxl import load_workbook
        workbook = load_workbook(destination, data_only=False)
        self.addCleanup(workbook.close)
        formula_cells = [
            f"{sheet.title}!{cell.coordinate}"
            for sheet in workbook.worksheets
            for row in sheet.iter_rows()
            for cell in row
            if cell.data_type == "f"
        ]
        self.assertEqual(formula_cells, [])
        transaction_values = [
            cell.value for cell in workbook["Transactions"][2]
        ]
        self.assertEqual(transaction_values[2], "=1+1")
        self.assertEqual(transaction_values[4], "=CATEGORY")
        self.assertEqual(transaction_values[5], "=PERSON")
        self.assertEqual(transaction_values[10:13], ["=IDENTIFIER", "=BANK CATEGORY", "=BANK TYPE"])

    def test_excel_dashboard_is_a_complete_monthly_brief(self):
        def report_transaction(
            index, kind, amount, category="Uncategorized", responsibility="",
        ):
            return {
                "provider": "apple", "account": "apple",
                "date": f"2026-07-{index:02d}", "post_date": f"2026-07-{index:02d}",
                "description": f"FICTIONAL {index}", "kind": kind,
                "raw_amount": str(amount), "category": category,
                "responsibility": responsibility, "identifier": f"FICTIONAL {index}",
                "bank_category": "Test", "bank_type": "Test", "splits": [],
            }

        long_category = "Household supplies and shared essentials"
        transactions = [
            report_transaction(1, "expense", "100", "Groceries", "Primary"),
            report_transaction(2, "refund", "20", "Groceries", "Primary"),
            report_transaction(3, "expense", "50", "Shopping", "Alex"),
            report_transaction(4, "expense", "60", "Gifts", "Primary"),
            report_transaction(5, "refund", "75", "Gifts", "Primary"),
            report_transaction(6, "expense", "40", "Family Group"),
            report_transaction(7, "expense", "25", long_category),
            report_transaction(8, "expense", "30", "Activities"),
            report_transaction(9, "expense", "10", "Transit", "Primary"),
            report_transaction(10, "refund", "10", "Transit", "Primary"),
            report_transaction(11, "income", "1000"),
            report_transaction(12, "reimbursement", "60"),
        ]
        destination = self.root / "monthly-brief.xlsx"
        analyzer.generate_xlsx(
            "2026-07", transactions, destination,
            personless_categories={"Family Group", long_category},
            default_person="Primary", other_people_categories={"Family Group"},
            profile_name="alex",
        )

        from openpyxl import load_workbook
        workbook = load_workbook(destination, data_only=False)
        self.addCleanup(workbook.close)
        dashboard = workbook["Dashboard"]
        self.assertEqual(dashboard["A1"].value, "July 2026 spending")
        self.assertEqual(dashboard["H1"].value, "ALEX")
        self.assertEqual(dashboard["A6"].value, 850)
        self.assertEqual(dashboard["F6"].value, 1060)
        self.assertEqual(dashboard["H6"].value, 210)
        self.assertEqual(dashboard["F5"].value, "INCOME + REIMBURSEMENTS")
        self.assertIn("Income $1,000.00", dashboard["F8"].value)
        self.assertIn("reimbursements $60.00", dashboard["F8"].value)
        self.assertEqual(
            [dashboard[cell].value for cell in ("A12", "D12", "A16", "D16")],
            [65, 90, 25, 30],
        )
        self.assertEqual([dashboard.cell(row, 10).value for row in range(11, 14)], [90, 60, 30])
        self.assertEqual(
            dashboard["G10"].value,
            "Pooled reimbursements were lower than marked spending",
        )
        category_values = {
            dashboard.cell(row, 12).value: dashboard.cell(row, 13).value
            for row in range(2, 7)
        }
        self.assertEqual(category_values[long_category], 25)
        self.assertIn("Gifts: -$15.00", dashboard["A36"].value)
        self.assertIn("Transit: $0.00", dashboard["A36"].value)
        self.assertEqual(len(dashboard._charts), 1)
        self.assertEqual(
            workbook.sheetnames,
            ["Dashboard", "Categories", "People", "Bills", "Transactions"],
        )

    def test_excel_report_handles_an_empty_month(self):
        destination = self.root / "empty-report.xlsx"
        analyzer.generate_xlsx("2026-07", [], destination, profile_name="empty")

        from openpyxl import load_workbook
        workbook = load_workbook(destination, data_only=True)
        self.addCleanup(workbook.close)
        self.assertEqual(workbook["Dashboard"]["A6"].value, 0)
        self.assertEqual(workbook["Dashboard"]["A5"].value, "MONTH RESULT · BALANCED")
        self.assertEqual(workbook["Dashboard"]["G10"].value, "No net spending marked for other people")
        self.assertEqual(workbook["Dashboard"]["A23"].value, "No categories had net purchases this month.")
        self.assertEqual(
            workbook["Dashboard"]["A36"].value,
            "None this month.",
        )
        self.assertEqual(workbook["Dashboard"]._charts, [])
        self.assertEqual(workbook["Transactions"].max_row, 1)

    def test_excel_dashboard_does_not_describe_other_person_refunds_as_reimbursement(self):
        transaction = {
            "provider": "apple", "account": "apple", "date": "2026-07-01",
            "post_date": "2026-07-01", "description": "FICTIONAL REFUND",
            "kind": "refund", "raw_amount": "30", "category": "Shopping",
            "responsibility": "Alex", "identifier": "FICTIONAL REFUND",
            "bank_category": "Test", "bank_type": "Refund", "splits": [],
        }
        destination = self.root / "other-person-refund.xlsx"
        analyzer.generate_xlsx(
            "2026-07", [transaction], destination, default_person="Primary",
        )

        from openpyxl import load_workbook
        workbook = load_workbook(destination, data_only=True)
        self.addCleanup(workbook.close)
        dashboard = workbook["Dashboard"]
        self.assertEqual(
            dashboard["G10"].value,
            "Refunds exceeded spending marked for other people",
        )
        self.assertEqual([dashboard.cell(row, 10).value for row in range(11, 14)], [-30, 0, 0])
        self.assertIn("Refunds or credits exceeded purchases", dashboard["G15"].value)

    def test_excel_dashboard_summarizes_large_category_lists(self):
        transactions = []
        for index in range(1, 41):
            kind = "expense" if index <= 30 else "refund"
            transactions.append({
                "provider": "apple", "account": "apple",
                "date": "2026-07-01", "post_date": "2026-07-01",
                "description": f"FICTIONAL {index}", "kind": kind,
                "raw_amount": str(index), "category": f"Category {index:02d}",
                "responsibility": "Primary", "identifier": f"FICTIONAL {index}",
                "bank_category": "Test", "bank_type": "Test", "splits": [],
            })
        destination = self.root / "large-chart.xlsx"
        analyzer.generate_xlsx(
            "2026-07", transactions, destination, default_person="Primary",
        )

        from openpyxl import load_workbook
        workbook = load_workbook(destination, data_only=True)
        self.addCleanup(workbook.close)
        dashboard = workbook["Dashboard"]
        self.assertEqual(dashboard["L2"].value, "Category 30")
        self.assertEqual(dashboard["L8"].value, "Category 24")
        self.assertIn("23 smaller categories", dashboard["A21"].value)
        self.assertIn("Category 40: -$40.00", dashboard["A36"].value)
        self.assertIn("+6 more on Categories", dashboard["A36"].value)
        self.assertEqual(len(dashboard._charts), 1)

    def test_excel_dashboard_separates_a_dominant_category(self):
        transactions = [
            {
                "provider": "apple", "account": "apple", "date": "2026-07-01",
                "post_date": "2026-07-01", "description": "FICTIONAL RENT",
                "kind": "expense", "raw_amount": "1000", "category": "Housing",
                "responsibility": "Primary", "identifier": "FICTIONAL RENT",
                "bank_category": "Test", "bank_type": "Test", "splits": [],
            },
            {
                "provider": "apple", "account": "apple", "date": "2026-07-02",
                "post_date": "2026-07-02", "description": "FICTIONAL FOOD",
                "kind": "expense", "raw_amount": "100", "category": "Dining",
                "responsibility": "Primary", "identifier": "FICTIONAL FOOD",
                "bank_category": "Test", "bank_type": "Test", "splits": [],
            },
        ]
        destination = self.root / "dominant-category.xlsx"
        analyzer.generate_xlsx(
            "2026-07", transactions, destination, default_person="Primary",
        )

        from openpyxl import load_workbook
        workbook = load_workbook(destination, data_only=True)
        self.addCleanup(workbook.close)
        dashboard = workbook["Dashboard"]
        self.assertEqual(dashboard["A23"].value, "Housing")
        self.assertEqual(dashboard["A25"].value, 1000)
        self.assertEqual(dashboard["L2"].value, "Dining")
        self.assertEqual(dashboard["M2"].value, 100)

    def test_excel_dashboard_handles_only_one_positive_category(self):
        transaction = {
            "provider": "apple", "account": "apple", "date": "2026-07-01",
            "post_date": "2026-07-01", "description": "FICTIONAL FOOD",
            "kind": "expense", "raw_amount": "100", "category": "Dining",
            "responsibility": "Primary", "identifier": "FICTIONAL FOOD",
            "bank_category": "Test", "bank_type": "Test", "splits": [],
        }
        destination = self.root / "one-category.xlsx"
        analyzer.generate_xlsx(
            "2026-07", [transaction], destination, default_person="Primary",
        )

        from openpyxl import load_workbook
        workbook = load_workbook(destination, data_only=True)
        self.addCleanup(workbook.close)
        dashboard = workbook["Dashboard"]
        self.assertEqual(dashboard["A23"].value, "Dining")
        self.assertEqual(dashboard["A25"].value, 100)
        self.assertEqual(dashboard._charts, [])

    def test_rules_are_scoped_to_account(self):
        first = {"id": "one", "provider": "chase_credit", "account": "chase_credit-1111", "identifier": "SAMPLE MERCHANT", "kind": "expense"}
        second = {"id": "two", "provider": "chase_credit", "account": "chase_credit-2222", "identifier": "SAMPLE MERCHANT", "kind": "expense"}
        rules = [{"provider": "chase_credit", "account": "chase_credit-1111", "identifier": "SAMPLE MERCHANT", "category": "Other"}]
        analyzer.apply_rules_and_decisions([first, second], rules, {})
        self.assertEqual(first["category"], "Other")
        self.assertEqual(second["category"], "Uncategorized")

    def test_legacy_rule_migrates_only_with_one_known_account(self):
        rule = {"provider": "chase_credit", "identifier": "SAMPLE MERCHANT", "category": "Dining"}
        transactions = [{"provider": "chase_credit", "account": "chase_credit-1111"}]
        self.assertEqual(analyzer.migrate_legacy_rules([rule], transactions), 1)
        self.assertEqual(rule["account"], "chase_credit-1111")

        ambiguous = {"provider": "apple", "identifier": "CAFE", "category": "Dining"}
        accounts = [
            {"provider": "apple", "account": "apple-one"},
            {"provider": "apple", "account": "apple-two"},
        ]
        self.assertEqual(analyzer.migrate_legacy_rules([ambiguous], accounts), 0)
        self.assertNotIn("account", ambiguous)

    def test_refund_reduces_its_category(self):
        transactions = [
            {"kind": "expense", "raw_amount": "100.00", "category": "Shopping"},
            {"kind": "refund", "raw_amount": "-25.00", "category": "Shopping"},
        ]
        summary = analyzer.month_summary(transactions)
        self.assertEqual(summary["categories"]["Shopping"], analyzer.money("75"))
        self.assertEqual(summary["net_spending"], analyzer.money("75"))

    def test_income_override_moves_credit_out_of_category_spending(self):
        transaction = {
            "id": "credit", "provider": "discover", "account": "discover", "identifier": "CASHBACK",
            "kind": "refund", "raw_amount": "-25.00",
        }
        decisions = {"credit": {"category": "Income", "kind_override": "income"}}
        analyzer.apply_rules_and_decisions([transaction], [], decisions)
        summary = analyzer.month_summary([transaction])
        self.assertEqual(transaction["kind"], "income")
        self.assertEqual(summary["income"], analyzer.money("25"))
        self.assertEqual(summary["spending"], analyzer.money("0"))
        self.assertNotIn("Income", summary["categories"])

    def test_numbered_uncategorized_is_saved_but_enter_is_skip(self):
        def transaction(transaction_id):
            return {
                "id": transaction_id, "provider": "apple", "account": "apple", "identifier": transaction_id,
                "date": "2026-07-03", "post_date": "2026-07-03", "description": transaction_id,
                "raw_amount": "10.00", "kind": "expense", "category": "Uncategorized",
                "responsibility": "", "splits": [], "reference": "", "source_files": [],
                "bank_category": "", "bank_type": "Purchase",
            }
        reviewed, decisions = transaction("reviewed"), {}
        uncategorized_number = str(analyzer.CATEGORIES.index("Uncategorized") + 1)
        with patch("builtins.input", side_effect=[uncategorized_number, "n"]):
            analyzer.prompt_for_categories([reviewed], [], decisions)
        self.assertTrue(decisions["reviewed"]["reviewed"])

        skipped, skipped_decisions = transaction("skipped"), {}
        with patch("builtins.input", side_effect=[""]):
            analyzer.prompt_for_categories([skipped], [], skipped_decisions)
        self.assertNotIn("skipped", skipped_decisions)

    def test_mixed_bills_can_be_assigned_individually(self):
        bills = [
            {"id": "one", "provider": "chase_checking", "account": "chase_checking-1234", "identifier": "UTILITY", "date": "2026-07-01", "description": "UTILITY", "raw_amount": "-40", "kind": "expense", "category": "Bills", "responsibility": ""},
            {"id": "two", "provider": "chase_checking", "account": "chase_checking-1234", "identifier": "UTILITY", "date": "2026-07-02", "description": "UTILITY", "raw_amount": "-60", "kind": "expense", "category": "Bills", "responsibility": ""},
        ]
        decisions = {"one": {"category": "Bills"}, "two": {"category": "Bills"}}
        with patch("builtins.input", side_effect=["n", "Him", "Family"]):
            changed = analyzer.prompt_for_bill_responsibilities(bills, [], decisions)
        self.assertEqual(changed, 2)
        self.assertEqual(decisions["one"]["responsibility"], "Him")
        self.assertEqual(decisions["two"]["responsibility"], "Family")

    def test_purchase_split_uses_exact_remainder_and_summary_categories(self):
        transaction = {
            "id": "split-one", "provider": "apple", "account": "apple", "identifier": "TARGET",
            "date": "2026-07-12", "description": "TARGET", "raw_amount": "80.00",
            "kind": "expense", "category": "Uncategorized", "responsibility": "",
        }
        decisions = {}
        # Groceries, Gifts (fifth option after Groceries is excluded), then first amount.
        with patch("builtins.input", side_effect=["1", "5", "30"]):
            analyzer.prompt_for_split(transaction, decisions)
        self.assertEqual(decisions["split-one"]["splits"], [
            {"category": "Groceries", "amount": "30.00"},
            {"category": "Gifts", "amount": "50.00"},
        ])
        summary = analyzer.month_summary([transaction])
        self.assertEqual(summary["categories"]["Groceries"], analyzer.money("30"))
        self.assertEqual(summary["categories"]["Gifts"], analyzer.money("50"))
        self.assertEqual(summary["spending"], analyzer.money("80"))

    def test_empty_import_folder_can_finish_a_saved_transaction(self):
        transaction = {
            "id": "tx-one", "provider": "apple", "account": "apple", "date": "2026-07-02",
            "post_date": "2026-07-03", "description": "CAFE", "identifier": "CAFE",
            "raw_amount": "12.00", "kind": "expense", "reference": "", "source_files": ["old.csv"],
            "bank_category": "Restaurants", "bank_type": "Purchase"
        }
        analyzer.save_json(self.root / "data/transactions/2026.json", [transaction])
        analyzer.save_json(self.root / "rules/people.json", ["Him"])
        analyzer.save_json(
            self.root / "rules/settings.json",
            {"default_person": "Him", "always_ask_person_categories": ["Bills"]},
        )
        with patch("builtins.input", side_effect=["2", "n"]):
            self.assertEqual(analyzer.process(self.root, non_interactive=False), 0)
        decisions = json.loads((self.root / "data/decisions/2026.json").read_text())
        self.assertEqual(decisions["tx-one"]["category"], "Dining")

    def test_general_contains_words_rule_handles_identifier_variants(self):
        rules = [{
            "provider": "chase_credit", "account": "chase_credit-1111", "match_type": "contains_words",
            "pattern": "SAMPLE MERCHANT", "category": "Dining",
        }]
        variants = [
            {"id": "one", "provider": "chase_credit", "account": "chase_credit-1111", "identifier": "SAMPLE MERCHANT SAN FRANCISCO", "kind": "expense"},
            {"id": "two", "provider": "chase_credit", "account": "chase_credit-1111", "identifier": "HELP SAMPLE BUSINESS MERCHANT ORDER", "kind": "expense"},
            {"id": "three", "provider": "chase_credit", "account": "chase_credit-2222", "identifier": "SAMPLE MERCHANT SAN FRANCISCO", "kind": "expense"},
        ]
        analyzer.apply_rules_and_decisions(variants, rules, {})
        self.assertEqual(variants[0]["category"], "Dining")
        self.assertEqual(variants[1]["category"], "Dining")
        self.assertEqual(variants[2]["category"], "Uncategorized")

    def test_amex_wallet_marker_is_removed_from_identifier(self):
        self.assertEqual(
            analyzer.clean_identifier_value("amex", "APLPAY SAMPLE MERCHANT"), "SAMPLE MERCHANT"
        )
        self.assertEqual(
            analyzer.clean_identifier_value("amex", "SAMPLE APLPAY MERCHANT"), "SAMPLE MERCHANT"
        )

    def test_single_rule_prompt_defaults_exact_or_accepts_contains_words(self):
        exact_rules = []
        with patch("builtins.input", side_effect=[""]):
            analyzer.prompt_to_save_rule("apple", "apple", "SAMPLE MERCHANT", "Shopping", exact_rules)
        self.assertEqual(exact_rules[0]["match_type"], "exact")
        self.assertEqual(exact_rules[0]["pattern"], "SAMPLE MERCHANT")

        contains_rules = []
        with patch("builtins.input", side_effect=["SAMPLE"]):
            analyzer.prompt_to_save_rule("apple", "apple", "SAMPLE MERCHANT", "Shopping", contains_rules)
        self.assertEqual(contains_rules[0]["match_type"], "contains_words")
        self.assertEqual(contains_rules[0]["pattern"], "SAMPLE")

    def test_typed_rule_words_ignore_punctuation_between_words(self):
        rule = {
            "provider": "apple", "account": "apple", "match_type": "contains_words",
            "pattern": "SAMPLE EATS", "category": "Dining",
        }
        transaction = {
            "provider": "apple", "account": "apple", "identifier": "SAMPLE *EATS"
        }
        self.assertTrue(analyzer.rule_matches(transaction, rule))

    def test_category_can_be_added_and_selected_in_one_step(self):
        categories = list(analyzer.CATEGORIES)
        saved = []
        with patch("builtins.input", side_effect=["a", "Pets", "n"]):
            selected = analyzer.ask_category(
                allow_split=True, categories=categories, save_callback=lambda: saved.append(True)
            )
        self.assertEqual(selected, ("Pets", False))
        self.assertIn("Pets", categories)
        self.assertEqual(saved, [True])

    def test_new_category_can_be_personless(self):
        categories = list(analyzer.CATEGORIES)
        personless = set()
        with patch("builtins.input", side_effect=["a", "House Guest", "y"]):
            selected = analyzer.ask_category(
                allow_split=False, categories=categories, personless_categories=personless
            )
        self.assertEqual(selected, ("House Guest", False))
        self.assertEqual(personless, {"House Guest"})

    def test_b_on_next_category_undoes_and_revisits_previous_answer(self):
        def transaction(transaction_id, identifier, date):
            return {
                "id": transaction_id, "provider": "apple", "account": "apple", "identifier": identifier,
                "date": date, "post_date": date, "description": identifier, "raw_amount": "10.00",
                "kind": "expense", "category": "Uncategorized", "responsibility": "", "splits": [],
                "reference": "", "source_files": [], "bank_category": "", "bank_type": "Purchase",
            }
        transactions = [transaction("one", "A MERCHANT", "2026-07-01"), transaction("two", "B MERCHANT", "2026-07-02")]
        decisions, rules = {}, []
        # First answer Groceries; at the next merchant go back; replace with Dining; then make second Gas.
        with patch("builtins.input", side_effect=["1", "n", "b", "2", "n", "3", "n"]):
            analyzer.prompt_for_categories(transactions, rules, decisions)
        self.assertEqual(decisions["one"]["category"], "Dining")
        self.assertEqual(decisions["two"]["category"], "Gas")

    def test_bill_responsibility_is_asked_during_category_selection(self):
        transaction = {
            "id": "bill-one", "provider": "apple", "account": "apple", "identifier": "SAMPLE BILL",
            "date": "2026-07-03", "post_date": "2026-07-03", "description": "SAMPLE BILL",
            "raw_amount": "25.00", "kind": "expense", "category": "Uncategorized",
            "responsibility": "", "splits": [], "reference": "", "source_files": [],
            "bank_category": "", "bank_type": "Purchase",
        }
        decisions, rules = {}, []
        # Bills, Him, then no reusable rule.
        with patch("builtins.input", side_effect=["8", "Him", "n"]):
            analyzer.prompt_for_categories([transaction], rules, decisions)
        self.assertEqual(decisions["bill-one"]["category"], "Bills")
        self.assertEqual(decisions["bill-one"]["responsibility"], "Him")

    def test_category_number_defaults_person_and_p_suffix_asks(self):
        def transaction(transaction_id, identifier):
            return {
                "id": transaction_id, "provider": "apple", "account": "apple", "identifier": identifier,
                "date": "2026-07-03", "post_date": "2026-07-03", "description": identifier,
                "raw_amount": "25.00", "kind": "expense", "category": "Uncategorized",
                "responsibility": "", "splits": [], "reference": "", "source_files": [],
                "bank_category": "", "bank_type": "Purchase",
            }
        default_tx = transaction("default", "A STORE")
        with patch("builtins.input", side_effect=["5", "n"]):
            analyzer.prompt_for_categories(
                [default_tx], [], {}, people=["Primary", "Housemate"], default_person="Primary"
            )
        self.assertEqual(default_tx["responsibility"], "Primary")

        chosen_tx, decisions = transaction("chosen", "B STORE"), {}
        with patch("builtins.input", side_effect=["5p", "2", "n"]):
            analyzer.prompt_for_categories(
                [chosen_tx], [], decisions, people=["Primary", "Housemate"], default_person="Primary"
            )
        self.assertEqual(decisions["chosen"]["responsibility"], "Housemate")

    def test_bill_person_accepts_new_name_or_saved_number(self):
        people, saves = [], []
        with patch("builtins.input", side_effect=["Alex"]):
            self.assertEqual(
                analyzer.ask_bill_responsibility("Who?", people, lambda: saves.append(True)), "Alex"
            )
        self.assertEqual(people, ["Alex"])
        self.assertEqual(saves, [True])
        with patch("builtins.input", side_effect=["1"]):
            self.assertEqual(analyzer.ask_bill_responsibility("Who?", people), "Alex")


if __name__ == "__main__":
    unittest.main()
