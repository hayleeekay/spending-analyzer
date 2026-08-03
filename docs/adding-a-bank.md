# Adding a bank CSV format

Bank CSV exports differ in headers, signs, date fields, and transaction-type names. Even two exports from the same company may differ by product or download page. Add a layout deliberately rather than trying to infer an unknown format.

All provider integration points are currently in `spending_analyzer.py`.

## 1. Add the identifying headers

Add an entry to `PROVIDER_HEADERS` containing the smallest set of headers that uniquely identifies the export.

```python
PROVIDER_HEADERS = {
    # Existing providers...
    "example_bank": {"Date", "Description", "Amount", "Transaction Type"},
}
```

Then add a readable name to `PROVIDER_LABELS`.

## 2. Normalize the merchant identifier

Update `clean_identifier()` if the useful merchant field is not `Description`. Provider-only noise removal belongs in `clean_identifier_value()`.

Cleanup should remove changing reference text while preserving enough merchant text for safe rules. Do not add household merchant names or personal rules here.

## 3. Classify transaction types

Add a branch to `classify_kind()`. Every row must become one of:

- `expense`
- `refund`
- `income`
- `reimbursement`
- `card_payment`
- `transfer`
- `unknown`

Use both the provider's documented sign convention and its transaction-type column. Do not assume positive always means income: several supported credit-card exports use positive amounts for purchases.

## 4. Map dates and amounts

Update `normalize_row()` with the provider's transaction date, posting date, and amount column names. The normalized transaction must use ISO dates (`YYYY-MM-DD`) and a two-decimal string amount.

If filenames contain an account suffix, update `account_id()` so rules remain scoped to the correct account.

## 5. Add tests

Add fictional rows to `tests/test_spending_analyzer.py` covering at least:

- a purchase;
- a refund or credit;
- a payment, if applicable;
- the provider's amount signs;
- header recognition; and
- an overlapping export.

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Never commit a real statement as a fixture. Reduce examples to invented merchants, dates, amounts, and account suffixes.
