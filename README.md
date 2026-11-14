# Local Spending Analyzer

A local, terminal-based tool for turning bank CSV exports into consistent monthly spending reports.

It imports overlapping exports safely, remembers categorization rules, supports split purchases and responsibility tracking, and generates an Excel dashboard plus a phone-friendly HTML report. Processing happens entirely on your computer: there are no bank logins, cloud APIs, analytics, or AI services.

## Tested CSV layouts

Version 1 has been tested with the consumer export layouts used by:

- Apple Card
- American Express
- Chase credit cards
- Chase checking accounts
- Discover credit cards

This does not guarantee that every account or export offered by those companies has the same layout. Formats may differ by product, country, download page, or future bank update. The importer checks for specific required headers; column order does not matter and extra columns are fine, but missing or renamed required columns are rejected instead of guessed.

The tested header sets are listed in `spending_analyzer/constants.py`. An unrecognized file stays in the profile's Import folder, and the terminal prints both the received headers and the layouts it knows. See [Adding a bank](docs/adding-a-bank.md) to support another variation.

## What it produces

For each month found in a profile's imported data:

```text
spending-data/profiles/alex/reports/YYYY-MM/
├── spending-report.xlsx
└── spending-report.html
```

The Excel workbook contains:

- **Dashboard:** spending for the default person, spending assigned to others, reimbursements, income, and comparison charts.
- **Categories:** category totals, person subtotals, dated transaction detail, and a grand total.
- **People:** compact cards showing each person or named group by category.
- **Bills:** exact dated bills grouped beneath each responsible person.
- **Transactions:** a filterable normalized ledger. Money out is negative cash flow; money in is positive.

Card payments and recognized internal transfers remain visible in Transactions but are excluded from spending totals to avoid double counting.

## Setup on macOS

Python 3.11 or newer is recommended.

```bash
git clone https://github.com/hayleeekay/spending-analyzer.git
cd spending-analyzer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Run the tool once. The first run asks for a profile name and creates its local folders:

```bash
./run.sh
```

## Everyday use

1. Put new bank CSV exports in the profile's `imports/` folder shown by the program.
2. Run `./run.sh`.
3. Answer questions for new transaction identifiers.
4. Open the newest report in that profile's `reports/` folder.

Successfully imported CSVs are renamed and preserved in the profile's `archive/` folder. Exact duplicate files are detected by content and archived without being imported twice. Overlapping exports are merged at the transaction level.

To import and report without answering questions:

```bash
./run.sh --non-interactive
```

To show the installed version:

```bash
./run.sh --version
```

## Separate profiles

Every transaction belongs to a profile. A profile is one isolated financial workspace with its own imports, rules, decisions, and reports. People tracked together can share a profile; use separate profiles when their source files and custom rules should not mix.

On the first interactive run, enter a profile name when prompted. You can also name it directly:

```bash
./run.sh --profile alex
```

The storage directory contains every profile:

```text
spending-data/
├── .spending-analyzer.json
└── profiles/
    └── alex/
        ├── imports/     New CSVs go here
        ├── archive/     Imported originals are preserved here
        ├── data/        Normalized transactions and decisions
        ├── rules/       Categories, people, settings, and rules
        └── reports/     Excel and HTML reports
```

With one profile, `./run.sh` selects it automatically. With more than one, specify the profile on every run:

```bash
./run.sh --profile alex
./run.sh --profile jordan
```

Profile names start with a letter or number, use only letters, numbers, hyphens, or underscores, and are normalized to lowercase. Profile folders cannot be symbolic links to other locations.

To keep all Spending Analyzer files somewhere else, select a storage directory. All profile folders, imported data, and reports are created inside it:

```bash
./run.sh --storage-dir "/Volumes/Finance" --profile alex
```

The program initializes a missing or empty storage directory. It refuses to initialize a populated unrelated directory.

### Importing from an external folder

An automation can provide a separate, read-only source folder:

```bash
./run.sh --profile alex --import-from "/path/to/bank-exports"
```

New CSVs are copied into the profile's managed workflow. Files in the external folder are never moved, renamed, or deleted, and already imported files are not copied again. Subfolders are not scanned. The normal `imports/` folder remains available at the same time.

### Upgrading an older workspace

Older versions stored `inbox`, `processed`, `data`, `rules`, and `reports` directly under `spending-data`. The analyzer detects that layout but never moves it automatically. Convert it explicitly:

```bash
./run.sh --migrate-profile alex
```

This keeps the workspace together while renaming `inbox` to `imports` and `processed` to `archive`. The command stops after conversion so you can review the new location before processing anything.

For a short command guide, run `./run.sh help` or `./run.sh --help`.

## Categorization workflow

For an unfamiliar expense or refund, the program shows the date, description, account, and amount. You can:

- choose a category;
- add a category;
- split a purchase between two categories;
- assign it to the default person or choose another person;
- save an account-specific matching rule; or
- leave it uncategorized for later.

At the rule prompt:

- Enter saves an exact identifier match.
- Typing stable words saves a contains-all-words match.
- `M` opens starts-with and literal-contains choices.
- `N` saves only the current decision, without a reusable rule.

Completed answers are written immediately. If you stop halfway through, completed transactions remain saved and the next run continues with the rest. At the following category question, `B` undoes the previous completed answer.

Provider-supplied categories are retained only for reference. They never determine the analyzer's category.

## People and groups

The first interactive run asks for a default person. A normal category selection assigns spending to that person. Add `P` to a category number, for example `5p`, to choose another saved person.

Some categories can be configured to:

- always ask who the transaction was for;
- skip person assignment; or
- count as spending on others for reimbursement comparisons.

These are ordinary settings, not household-specific code. A named group category such as “Roommates” or “Work expenses” can therefore appear in reports without pretending it belongs to one person.

## Income and reimbursements

The default Chase-checking policy treats:

- incoming P2P payments as estimated reimbursements;
- deposits as estimated reimbursements;
- ACH credits as income; and
- other incoming credits as estimated reimbursements.

These assumptions affect newly imported checking transactions and are configurable in the selected profile's `rules/settings.json`. Copy the shape from [settings.example.json](settings.example.json). Each incoming type accepts `income` or `reimbursement`.

Chase checking uses the amount direction plus recognized descriptions and types. An unfamiliar incoming credit follows the configurable `other_credit` policy, while an unfamiliar outgoing debit is treated as an expense. Card exports with an unsupported explicit transaction type stay in the Import folder for review instead of being guessed.

## Local data and privacy

All user data lives under the selected storage directory, including:

- original and archived statements;
- normalized transactions;
- one-time decisions;
- people, categories, rules, and settings; and
- generated reports.

That directory is excluded by `.gitignore`. The repository contains no real transactions, names, account numbers, or personal rules. Keep `spending-data/` ignored when publishing a fork.

If `--storage-dir` points somewhere else inside a Git repository, that custom folder is not automatically ignored. Add it to that repository's `.gitignore` before importing financial data.

JSON state is divided into yearly files, so the program does not rewrite one unlimited all-time file. No database server is required.

## Project layout

```text
spending_analyzer/
├── application.py       Import and processing workflow
├── categorization.py    Rules and interactive questions
├── constants.py         Defaults and supported CSV headers
├── providers.py         Bank CSV normalization
├── reports.py           Monthly summaries and reports
├── storage.py           Local JSON persistence
└── workspaces.py        Profile setup and legacy conversion
run.sh                    macOS launcher
settings.example.json     Documented configuration shape
tests/                    Tests with fictional transactions
docs/adding-a-bank.md     Provider extension guide
```

The package is split by responsibility, while `run.sh` keeps everyday use to one command. Start with `application.py` to follow the overall workflow, then open the focused module for the part you want to change.

## Testing

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The tests use temporary directories and fictional CSV rows. They do not touch `spending-data/`.

## Known limitations

- Only the tested CSV layouts described above are recognized.
- Provider export formats can change; a changed header or transaction type may require an adapter update.
- Incoming P2P payments and deposits are estimates until the user changes the configured policy.
- Merchant matching is deterministic text matching, not fuzzy matching or machine learning.
- Profiles isolate separate financial workspaces, but multiple same-provider accounts inside one profile may still need explicit account support.
- v1 targets macOS. The Python code is portable, but a Windows launcher is not included yet.

## License

This project uses the [MIT License](LICENSE). It costs nothing and does not require registration. The license lets people use, modify, and share the code, including in commercial projects, as long as they keep the copyright and license notice. It also says the code is provided without a warranty.
