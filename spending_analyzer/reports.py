"""Monthly summaries and CSV, HTML, and Excel reports."""

from __future__ import annotations

from collections import defaultdict
from copy import copy
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
    profile_name: str = "",
) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.chart import BarChart, Reference
        from openpyxl.formatting.rule import CellIsRule
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.table import Table, TableStyleInfo
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
    navy, accent, accent_dark = "243447", "4E718B", "36566C"
    pale, light, line = "EAF0F4", "F6F8FA", "D7DEE5"
    text, muted, outflow, inflow = "202A33", "5D6872", "A44A3F", "3D6E78"
    currency_format = '$#,##0.00;-$#,##0.00;$0.00'
    thin_gray = Side(style="thin", color=line)

    def fill_range(sheet, cell_range: str, color: str) -> None:
        for row in sheet[cell_range]:
            for cell in row:
                cell.fill = PatternFill("solid", fgColor=color)

    def border_range(sheet, cell_range: str) -> None:
        for row in sheet[cell_range]:
            for cell in row:
                cell.border = Border(
                    left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray,
                )

    def set_print_layout(sheet, orientation: str = "landscape") -> None:
        sheet.page_setup.orientation = orientation
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_margins.left = 0.35
        sheet.page_margins.right = 0.35
        sheet.page_margins.top = 0.5
        sheet.page_margins.bottom = 0.5
        sheet.oddFooter.center.text = f"Spending Analyzer | {month}"
        sheet.oddFooter.right.text = "Page &P of &N"

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
        set_print_layout(sheet)

    dashboard.sheet_view.showGridLines = False
    dashboard.sheet_view.zoomScale = 85
    dashboard.sheet_properties.tabColor = accent
    dashboard.merge_cells("A1:H1")
    dashboard["A1"] = "Monthly spending brief"
    dashboard["A1"].font = Font(size=24, bold=True, color="FFFFFF")
    dashboard["A1"].fill = PatternFill("solid", fgColor=navy)
    dashboard["A1"].alignment = Alignment(vertical="center")
    fill_range(dashboard, "A1:H1", navy)
    dashboard.row_dimensions[1].height = 42
    dashboard.merge_cells("A2:H2")
    context = f"Month: {month}"
    if profile_name:
        context += f"  |  Profile: {profile_name}"
    dashboard["A2"] = context
    dashboard["A2"].font = Font(size=11, bold=True, color=accent_dark)
    dashboard.merge_cells("A3:H3")
    dashboard["A3"] = (
        "Purchases minus refunds equals net spending. Card payments and transfers are "
        "excluded from spending and income totals."
    )
    dashboard["A3"].font = Font(italic=True, color=muted)
    dashboard["A3"].alignment = Alignment(wrap_text=True, vertical="top")
    dashboard.row_dimensions[3].height = 30

    def metric_block(
        label_range: str, value_range: str, label: str, value: Decimal,
        *, hero: bool = False,
    ) -> None:
        dashboard.merge_cells(label_range)
        dashboard.merge_cells(value_range)
        fill_range(dashboard, label_range, pale if hero else light)
        fill_range(dashboard, value_range, pale if hero else light)
        border_range(dashboard, label_range)
        border_range(dashboard, value_range)
        label_cell = dashboard[label_range.split(":")[0]]
        value_cell = dashboard[value_range.split(":")[0]]
        label_cell.value = label.upper()
        label_cell.font = Font(size=10, bold=True, color=accent_dark)
        label_cell.alignment = Alignment(vertical="center")
        value_cell.value = float(value)
        value_cell.number_format = currency_format
        value_cell.font = Font(size=24 if hero else 18, bold=True, color=text)
        value_cell.alignment = Alignment(vertical="center")

    metric_block("A5:C5", "A6:C7", "Net spending", summary["net_spending"], hero=True)
    metric_block("D5:F5", "D6:F7", "Purchases", summary["spending"])
    metric_block("G5:H5", "G6:H7", "Refunds and credits", summary["refunds"])
    dashboard.row_dimensions[6].height = 28
    dashboard.row_dimensions[7].height = 20

    allocation_totals = {
        default_person or "Default person": Decimal("0"),
        "Other people / groups": Decimal("0"),
        "Unassigned" if default_person else "Unassigned / no default person": Decimal("0"),
        "Category-only": Decimal("0"),
    }
    for transaction in transactions:
        for category, amount, responsibility in category_entries(transaction):
            is_other = category in other_people_categories or (
                responsibility and default_person
                and responsibility.casefold() != default_person.casefold()
            )
            if is_other:
                allocation_totals["Other people / groups"] += amount
            elif category in personless_categories:
                allocation_totals["Category-only"] += amount
            elif default_person and responsibility.casefold() == default_person.casefold():
                allocation_totals[default_person] += amount
            else:
                key = "Unassigned" if default_person else "Unassigned / no default person"
                allocation_totals[key] += amount

    def section_title(cell_range: str, title: str) -> None:
        dashboard.merge_cells(cell_range)
        start = cell_range.split(":")[0]
        fill_range(dashboard, cell_range, accent_dark)
        dashboard[start] = title
        dashboard[start].font = Font(size=11, bold=True, color="FFFFFF")
        dashboard[start].alignment = Alignment(vertical="center")

    section_title("A9:D9", "Where net spending was assigned")
    dashboard["A10"], dashboard["B10"] = "Allocation", "Net amount"
    for cell in dashboard[10][:2]:
        cell.font = Font(bold=True, color=accent_dark)
        cell.fill = PatternFill("solid", fgColor=pale)
    for row, (label, amount) in enumerate(allocation_totals.items(), 11):
        dashboard.cell(row, 1, label)
        dashboard.cell(row, 2, float(amount))
        dashboard.cell(row, 2).number_format = currency_format
    dashboard["A15"], dashboard["B15"] = "Total net spending", float(sum(allocation_totals.values()))
    dashboard["A15"].font = dashboard["B15"].font = Font(bold=True, color=text)
    dashboard["B15"].number_format = currency_format
    border_range(dashboard, "A10:B15")

    section_title("E9:H9", "Spending for others and reimbursements")
    comparison = [
        ("Spending assigned to others", summary["other_people_spending"]),
        ("Estimated reimbursements", summary["reimbursements"]),
        ("Difference", summary["other_people_after_reimbursements"]),
    ]
    for row, (label, amount) in enumerate(comparison, 10):
        dashboard.cell(row, 5, label)
        dashboard.cell(row, 8, float(amount))
        dashboard.cell(row, 8).number_format = currency_format
    dashboard["E12"].font = dashboard["H12"].font = Font(bold=True, color=text)
    border_range(dashboard, "E10:H12")
    dashboard.merge_cells("E14:H15")
    dashboard["E14"] = (
        "Estimate only. The difference is not a debt balance, confirmed reimbursement, "
        "or money saved."
    )
    dashboard["E14"].font = Font(size=9, italic=True, color=muted)
    dashboard["E14"].alignment = Alignment(wrap_text=True, vertical="top")

    section_title("A17:D17", "Money in and monthly net flow")
    flow_rows = [
        ("Income", summary["income"]),
        ("Estimated reimbursements", summary["reimbursements"]),
        ("Income + reimbursements - net spending", summary["net_after_spending"]),
    ]
    for row, (label, amount) in enumerate(flow_rows, 18):
        dashboard.cell(row, 1, label)
        dashboard.cell(row, 4, float(amount))
        dashboard.cell(row, 4).number_format = currency_format
    dashboard["A20"].font = dashboard["D20"].font = Font(bold=True, color=text)
    border_range(dashboard, "A18:D20")

    section_title("E17:H17", "How to read this report")
    dashboard.merge_cells("E18:H20")
    dashboard["E18"] = (
        "Net flow describes transactions in this report. It is not a bank balance, "
        "savings total, or amount guaranteed to be available. Exact rows are on the "
        "Categories, People, Bills, and Transactions sheets."
    )
    dashboard["E18"].font = Font(size=9, color=muted)
    dashboard["E18"].alignment = Alignment(wrap_text=True, vertical="top")

    positive_categories = sorted(
        (
            (name, amount) for name, amount in summary["categories"].items()
            if amount > 0
        ),
        key=lambda item: (item[1], item[0].casefold()),
    )
    nonpositive_categories = sorted(
        (
            (name, amount) for name, amount in summary["categories"].items()
            if amount <= 0
        ),
        key=lambda item: (item[1], item[0].casefold()),
    )
    category_items = positive_categories + nonpositive_categories
    dashboard["J1"], dashboard["K1"] = "Category", "Net amount"
    for row, (name, amount) in enumerate(category_items, 2):
        dashboard.cell(row, 10, name)
        dashboard.cell(row, 11, float(amount))

    section_title("A22:H22", "Net spending by category")
    if positive_categories:
        chart = BarChart()
        chart.type = "bar"
        chart.title = "Categories with net purchases"
        chart.y_axis.title = "Net spending"
        chart.legend = None
        chart.add_data(
            Reference(
                dashboard, min_col=11, min_row=1,
                max_row=len(positive_categories) + 1,
            ),
            titles_from_data=True,
        )
        chart.set_categories(
            Reference(
                dashboard, min_col=10, min_row=2,
                max_row=len(positive_categories) + 1,
            ),
        )
        chart.visible_cells_only = False
        chart.height = min(13, max(8, 5 + len(positive_categories) * 0.55))
        chart.width = 22
        dashboard.add_chart(chart, "A23")
    else:
        dashboard["A24"] = "No categories had net purchases this month."
        dashboard["A24"].font = Font(italic=True, color=muted)

    section_title("A42:H42", "Refund-heavy and zero categories")
    dashboard["A43"], dashboard["B43"] = "Category", "Net amount"
    dashboard["A43"].font = dashboard["B43"].font = Font(
        bold=True, color=accent_dark,
    )
    dashboard["A43"].fill = dashboard["B43"].fill = PatternFill(
        "solid", fgColor=pale,
    )
    if nonpositive_categories:
        for row, (name, amount) in enumerate(nonpositive_categories, 44):
            dashboard.cell(row, 1, name)
            dashboard.cell(row, 2, float(amount))
            dashboard.cell(row, 2).number_format = currency_format
        nonpositive_end = 43 + len(nonpositive_categories)
    else:
        dashboard["A44"] = "None"
        dashboard["B44"] = 0
        dashboard["B44"].number_format = currency_format
        nonpositive_end = 44
    border_range(dashboard, f"A43:B{nonpositive_end}")
    note_row = nonpositive_end + 2
    dashboard.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=8)
    dashboard.cell(note_row, 1).value = (
        "These values are listed separately so refunds are not mistaken for spending bars. "
        "A negative total means refunds or credits exceeded purchases in that category."
    )
    dashboard.cell(note_row, 1).font = Font(size=9, italic=True, color=muted)
    dashboard.cell(note_row, 1).alignment = Alignment(wrap_text=True)
    for column in ("J", "K"):
        dashboard.column_dimensions[column].hidden = True
    for column, width in enumerate([25, 16, 4, 18, 25, 4, 22, 17], 1):
        dashboard.column_dimensions[get_column_letter(column)].width = width
    dashboard.freeze_panes = "A4"
    dashboard.print_area = f"A1:H{note_row}"
    set_print_layout(dashboard)
    dashboard.page_setup.fitToHeight = 1

    category_person_totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal("0"))
    )
    category_detail_rows: dict[str, list[tuple[dict[str, Any], Decimal, str]]] = defaultdict(list)
    for transaction in transactions:
        for category, amount, responsibility in category_entries(transaction):
            if category in personless_categories:
                person = "Category-only"
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
        2 if person == "Category-only" else 1,
        person.casefold(),
    ))
    matrix_categories = sorted(
        category_person_totals,
        key=lambda category: (-sum(category_person_totals[category].values(), Decimal("0")), category),
    )

    def build_card_sheet(sheet, title: str, cards: list[tuple[str, list[tuple[str, Decimal]], Decimal]]) -> None:
        sheet.sheet_view.showGridLines = False
        sheet.merge_cells("A1:E1")
        sheet["A1"] = title
        sheet["A1"].font = Font(size=20, bold=True, color="FFFFFF")
        sheet["A1"].fill = PatternFill("solid", fgColor=navy)
        fill_range(sheet, "A1:E1", navy)
        sheet["A1"].alignment = Alignment(vertical="center")
        sheet.row_dimensions[1].height = 34
        for column in ("A", "D"):
            sheet.column_dimensions[column].width = 30
        for column in ("B", "E"):
            sheet.column_dimensions[column].width = 15
        for column in ("C",):
            sheet.column_dimensions[column].width = 3
        row_cursor = 3
        for start in range(0, len(cards), 2):
            card_row = cards[start:start + 2]
            height = max((len(lines) + 2 for _name, lines, _total in card_row), default=2)
            for index, (name, lines, total) in enumerate(card_row):
                column = 1 + index * 3
                sheet.merge_cells(
                    start_row=row_cursor, start_column=column,
                    end_row=row_cursor, end_column=column + 1,
                )
                heading = sheet.cell(row_cursor, column, name)
                heading.font = Font(bold=True, color="FFFFFF", size=12)
                heading.fill = PatternFill("solid", fgColor=accent)
                sheet.cell(row_cursor, column + 1).fill = PatternFill("solid", fgColor=accent)
                heading.alignment = Alignment(wrap_text=True, vertical="center")
                if len(name) > 36:
                    sheet.row_dimensions[row_cursor].height = 32
                for offset, (label, amount) in enumerate(lines, 1):
                    label_cell = sheet.cell(row_cursor + offset, column, label)
                    amount_cell = sheet.cell(row_cursor + offset, column + 1, float(amount))
                    amount_cell.number_format = currency_format
                    label_cell.fill = amount_cell.fill = PatternFill("solid", fgColor=light)
                total_row = row_cursor + len(lines) + 1
                total_label = sheet.cell(total_row, column, "Total")
                total_value = sheet.cell(total_row, column + 1, float(total))
                total_label.font = total_value.font = Font(bold=True, color=navy)
                total_label.fill = total_value.fill = PatternFill("solid", fgColor=pale)
                total_value.number_format = currency_format
                for row in range(row_cursor, total_row + 1):
                    for col in range(column, column + 2):
                        sheet.cell(row, col).border = Border(
                            left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray
                        )
            row_cursor += height + 2
        sheet.freeze_panes = "A3"
        set_print_layout(sheet, "portrait")

    categories = workbook.create_sheet("Categories")
    categories.sheet_properties.tabColor = accent
    categories.sheet_view.showGridLines = False
    categories.sheet_properties.outlinePr.summaryBelow = False
    categories.append([
        "Category / Person", "Date", "Description", "Account", "Type",
        "Net Amount",
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
            cell.fill = PatternFill("solid", fgColor=accent)
        categories.cell(category_row, 1).alignment = Alignment(
            wrap_text=True, vertical="center",
        )
        categories.row_dimensions[category_row].height = 30
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
                    cell.fill = PatternFill("solid", fgColor=pale)
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
    categories.print_title_rows = "1:1"
    set_print_layout(categories)

    people_cards = []
    for person in person_columns:
        if person == "Category-only":
            continue
        lines = [
            (category, category_person_totals[category][person])
            for category in matrix_categories if category_person_totals[category][person]
        ]
        lines.sort(key=lambda item: (-item[1], item[0]))
        people_cards.append((person, lines, sum((amount for _category, amount in lines), Decimal("0"))))
    for category in matrix_categories:
        amount = category_person_totals[category].get("Category-only", Decimal("0"))
        if amount:
            people_cards.append((f"Category-only: {category}", [], amount))
    people_sheet = workbook.create_sheet("People")
    people_sheet.sheet_properties.tabColor = accent
    build_card_sheet(people_sheet, "People and category-only groups", people_cards)

    bills = workbook.create_sheet("Bills")
    bills.sheet_properties.tabColor = accent
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
            cell.fill = PatternFill("solid", fgColor=pale)
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
    bills.print_title_rows = "1:1"
    set_print_layout(bills)

    sheet = workbook.create_sheet("Transactions")
    sheet.sheet_properties.tabColor = accent
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
    sheet.page_setup.fitToWidth = 2
    for row in range(2, sheet.max_row + 1):
        for column in (7, 8, 9):
            sheet.cell(row, column).number_format = currency_format
    if sheet.max_row >= 2:
        sheet.conditional_formatting.add(
            f"I2:I{sheet.max_row}",
            CellIsRule(operator="lessThan", formula=["0"], font=Font(color=outflow)),
        )
        sheet.conditional_formatting.add(
            f"I2:I{sheet.max_row}",
            CellIsRule(operator="greaterThan", formula=["0"], font=Font(color=inflow)),
        )

    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                updated_font = copy(cell.font)
                updated_font.name = "Arial"
                cell.font = updated_font
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
