import csv
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import spending_analyzer as analyzer


def write_csv(path, headers, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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
        self.assertEqual(workbook["People"]["A1"].value, "People and groups by category")
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

    def test_archive_failure_does_not_commit_staged_transactions(self):
        source_path = self.imports / "discover.csv"
        write_csv(
            source_path,
            list(analyzer.PROVIDER_HEADERS["discover"]),
            [{
                "Trans. Date": "07/01/2026", "Post Date": "07/02/2026",
                "Description": "STORE", "Amount": "10.00", "Category": "Shopping",
            }],
        )

        with (
            patch("spending_analyzer.application.shutil.move", side_effect=OSError("archive failed")),
            patch("sys.stderr"),
        ):
            self.assertEqual(analyzer.process(self.root, non_interactive=True), 1)

        self.assertEqual(analyzer.load_year_shards(self.root / "data/transactions"), [])
        self.assertEqual(json.loads((self.root / "data/imports.json").read_text()), [])
        self.assertTrue(source_path.exists())

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
