"""Generate reports from a small set of fictional transactions."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any

from spending_analyzer.reports import generate_html, generate_xlsx, write_month_csv


MONTH = "2026-10"


def transaction(
    number: int,
    date: str,
    description: str,
    kind: str,
    amount: str,
    *,
    category: str = "",
    responsibility: str = "",
) -> dict[str, Any]:
    checking = kind in {"income", "reimbursement"}
    provider = "chase_checking" if checking else "chase_credit"
    return {
        "id": f"fictional-{number}",
        "provider": provider,
        "account": provider,
        "date": date,
        "post_date": date,
        "description": description,
        "identifier": description.upper(),
        "raw_amount": amount,
        "kind": kind,
        "original_kind": kind,
        "category": category or "Uncategorized",
        "responsibility": responsibility,
        "splits": [],
        "reference": "",
        "source_files": ["fictional-demo.csv"],
        "bank_category": "",
        "bank_type": "",
    }


def main() -> None:
    output = Path(tempfile.mkdtemp(prefix="spending-analyzer-demo-"))
    report_dir = output / "reports" / MONTH
    report_dir.mkdir(parents=True)
    csv_dir = output / "data" / "by-month"
    csv_dir.mkdir(parents=True)

    transactions = [
        transaction(1, "2026-10-01", "Fictional paycheck", "income", "2400.00"),
        transaction(
            2, "2026-10-02", "Neighborhood market", "expense", "-86.40",
            category="Groceries", responsibility="Alex",
        ),
        transaction(
            3, "2026-10-05", "Apartment rent", "expense", "-900.00",
            category="Bills", responsibility="Alex",
        ),
        transaction(
            4, "2026-10-10", "Birthday book", "expense", "-42.00",
            category="Gifts", responsibility="Alex",
        ),
        transaction(
            5, "2026-10-14", "Dinner with a friend", "expense", "-96.00",
            category="Dining", responsibility="Jordan",
        ),
        transaction(6, "2026-10-17", "Friend paid part back", "reimbursement", "60.00"),
        transaction(
            7, "2026-10-20", "Market return", "refund", "18.25",
            category="Groceries", responsibility="Alex",
        ),
    ]

    personless = {"Income"}
    generate_html(
        MONTH,
        transactions,
        report_dir / "spending-report.html",
        personless,
        "Alex",
        set(),
    )
    generate_xlsx(
        MONTH,
        transactions,
        report_dir / "spending-report.xlsx",
        personless,
        "Alex",
        set(),
        profile_name="Fictional demo",
    )
    csv_path = csv_dir / f"{MONTH}.csv"
    write_month_csv(transactions, csv_path)

    print(f"Fictional reports created in: {output}")
    print(f"Open: {report_dir / 'spending-report.html'}")
    print(f"Open: {report_dir / 'spending-report.xlsx'}")
    print(f"Open: {csv_path}")


if __name__ == "__main__":
    main()
