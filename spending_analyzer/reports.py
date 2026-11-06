"""Monthly summaries and CSV, HTML, and Excel reports."""

from __future__ import annotations

from collections import defaultdict
import csv
from decimal import Decimal
import html
from pathlib import Path
from typing import Any

from .categorization import credit_amount, expense_amount
from .constants import PROVIDER_LABELS
from .providers import account_label
from .storage import money


def _preserve_excel_text(workbook: Any) -> None:
    """Keep report text beginning with '=' from becoming a formula."""
    # Report totals are calculated in Python, so worksheet formula cells are
    # always imported or user-entered text that openpyxl inferred incorrectly.
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"


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
<body><h1>Spending report - {month}</h1><p class="note">Local report. Card payments and transfers are excluded from spending and income.</p>
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
    dashboard["A1"] = f"Spending Dashboard - {month}"
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

    _preserve_excel_text(workbook)
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
