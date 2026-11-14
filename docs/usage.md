# Using Local Spending Analyzer

This guide covers the regular command-line workflow. Start with the [main README](../README.md) for the project overview and installation.

## Everyday workflow

1. Put new bank CSV exports in the selected profile's `imports/` folder.
2. Run `./run.sh`.
3. Answer questions for unfamiliar transaction identifiers.
4. Open the newest report in the profile's `reports/` folder.

Successfully imported CSVs are renamed and preserved in `archive/`. Exact duplicate files are detected by content and archived without being imported twice. Overlapping exports are merged at the transaction level.

To import and report without answering categorization questions:

```bash
./run.sh --non-interactive
```

To show the installed version:

```bash
./run.sh --version
```

For a short command guide:

```bash
./run.sh --help
```

## Profiles and folders

A profile is one isolated financial workspace with its own imports, archive, transactions, rules, decisions, and reports. People tracked together can share a profile. Use separate profiles when source files and custom rules should not mix.

The first interactive run asks for a profile name. You can also choose one directly:

```bash
./run.sh --profile alex
```

The default storage layout is:

```text
spending-data/
├── .spending-analyzer.json
└── profiles/
    └── alex/
        ├── imports/     New CSVs go here
        ├── archive/     Imported originals are preserved here
        ├── data/        Normalized transactions and one-time decisions
        ├── rules/       Categories, people, settings, and reusable rules
        └── reports/     Excel and HTML reports
```

With one profile, `./run.sh` selects it automatically. With more than one, choose a profile on every run:

```bash
./run.sh --profile alex
./run.sh --profile jordan
```

Profile names start with a letter or number, use only letters, numbers, hyphens, or underscores, and are normalized to lowercase. Profile folders and managed subfolders cannot be symbolic links.

To keep all analyzer files somewhere else, select a storage directory. Profiles and all managed data are created inside it:

```bash
./run.sh --storage-dir "/Volumes/Finance" --profile alex
```

The program initializes a missing or empty storage directory. It refuses to initialize a populated unrelated directory. Pass the same `--storage-dir` on later runs so the analyzer opens the same profiles.

## Importing from an external folder

An automation can point the analyzer at a separate read-only source folder:

```bash
./run.sh --profile alex --import-from "/path/to/bank-exports"
```

New CSVs are copied into the managed workflow. Files in the external folder are never moved, renamed, or deleted, and files already imported are not copied again. Subfolders are not scanned. The profile's regular `imports/` folder remains available.

The external source must be outside the selected Spending Analyzer storage directory.

CSV files placed at the storage root or directly inside `profiles/` are not imported. The program warns about them and prints the correct Import folder.

## Categorization controls

For an unfamiliar expense or refund, the program shows the date, description, account, and amount. You can:

- choose or add a category;
- split one purchase between two categories;
- assign it to the default person or choose another person;
- save an account-specific matching rule; or
- leave it uncategorized for later.

At the rule prompt:

- Enter saves an exact identifier match.
- Typing stable words saves a contains-all-words match.
- `M` opens starts-with and literal-contains choices.
- `N` saves only the current decision, without a reusable rule.

Completed answers are written immediately. If you stop halfway through, the next run continues with the remaining transactions. At the following category question, `B` undoes the previous completed answer.

Provider-supplied categories are kept for reference but never determine the analyzer's category.

## People and groups

The first interactive run that has transactions asks for a default person. A normal category selection assigns spending to that person. Add `P` to a category number, such as `5p`, to choose another saved person.

Categories can be configured to:

- always ask who the transaction was for;
- skip person assignment; or
- count as spending on others for pooled reimbursement comparisons.

A named group category such as `Roommates` or `Work expenses` can appear in reports without pretending it belongs to one person.

## Income and estimated reimbursements

The default Chase-checking policy treats:

- incoming P2P payments as estimated reimbursements;
- deposits as estimated reimbursements;
- ACH credits as income; and
- other incoming credits as estimated reimbursements.

These choices affect newly imported checking transactions. They are configurable in the selected profile's `rules/settings.json`. Use [settings.example.json](../settings.example.json) as the shape. Each incoming type accepts `income` or `reimbursement`.

Chase checking uses amount direction plus recognized descriptions and types. An unfamiliar incoming credit follows the configured `other_credit` policy, while an unfamiliar outgoing debit is treated as an expense. Card exports with an unsupported explicit transaction type stay in the Import folder for review rather than being guessed.

Estimated reimbursements are pooled. The analyzer compares their total with spending assigned to other people, but it does not link a payment to a specific person or purchase.

## Upgrading an older workspace

Older versions stored `inbox`, `processed`, `data`, `rules`, and `reports` directly under `spending-data`. The analyzer detects that layout but never moves it automatically. Convert it explicitly:

```bash
./run.sh --migrate-profile alex
```

This keeps the workspace together while renaming `inbox` to `imports` and `processed` to `archive`. The command stops after conversion so you can review the new location before processing anything.

## Privacy reminders

All financial data lives under the selected storage directory. The repository's `.gitignore` covers its default `spending-data/` folder. A custom storage folder elsewhere is not ignored automatically.

Before publishing a fork or sharing a bug report, check for original statements, generated reports, JSON state, names, account identifiers, and saved rules. Use fictional transactions in examples and tests.
