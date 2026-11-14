"""Monthly summaries and CSV, HTML, and Excel reports."""

from __future__ import annotations

from collections import defaultdict
from copy import copy
import csv
from datetime import datetime
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
        from openpyxl.chart.label import DataLabelList
        from openpyxl.formatting.rule import CellIsRule
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
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
    detail_header = "4A403A"
    detail_section = "E4B5A4"
    detail_section_text = "6B3F34"
    detail_pale, detail_light, line = "F2E4DC", "FCF8F4", "E4D7CF"
    text, muted, outflow, inflow = "3E342E", "786C64", "A95846", "537C7B"
    currency_format = '$#,##0.00;-$#,##0.00;$0.00'
    thin_gray = Side(style="thin", color=line)

    def fill_range(sheet, cell_range: str, color: str) -> None:
        for row in sheet[cell_range]:
            for cell in row:
                cell.fill = PatternFill("solid", fgColor=color)

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

    def style_data_sheet(sheet, widths: list[int]) -> None:
        sheet.freeze_panes = "A2"
        sheet.sheet_view.showGridLines = False
        sheet.auto_filter.ref = sheet.dimensions
        sheet.row_dimensions[1].height = 24
        for cell in sheet[1]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = PatternFill("solid", fgColor=detail_header)
            cell.alignment = Alignment(vertical="center")
        for index, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        for row_number in range(2, sheet.max_row + 1):
            if row_number % 2 == 0:
                for cell in sheet[row_number]:
                    cell.fill = PatternFill("solid", fgColor=detail_light)
        set_print_layout(sheet)

    dashboard_cream = "FAF6EF"
    dashboard_text = "3E342E"
    dashboard_muted = "786C64"
    dashboard_salmon = "C9785F"
    dashboard_salmon_dark = "965642"
    dashboard_salmon_soft = "F2D9CE"
    dashboard_neutral_soft = "EFE6DF"
    dashboard_line = "E6D8CD"
    dashboard_white = "FFFCF8"

    allocation_totals = {
        default_person or "Default person": Decimal("0"),
        "Other people": Decimal("0"),
        "Unassigned" if default_person else "Unassigned / no default person": Decimal("0"),
        "No person needed": Decimal("0"),
    }
    for transaction in transactions:
        for category, amount, responsibility in category_entries(transaction):
            is_other = category in other_people_categories or (
                responsibility and default_person
                and responsibility.casefold() != default_person.casefold()
            )
            if is_other:
                allocation_totals["Other people"] += amount
            elif category in personless_categories:
                allocation_totals["No person needed"] += amount
            elif default_person and responsibility.casefold() == default_person.casefold():
                allocation_totals[default_person] += amount
            else:
                key = "Unassigned" if default_person else "Unassigned / no default person"
                allocation_totals[key] += amount

    positive_categories = sorted(
        (
            (name, amount) for name, amount in summary["categories"].items()
            if amount > 0
        ),
        key=lambda item: (-item[1], item[0].casefold()),
    )
    nonpositive_categories = sorted(
        (
            (name, amount) for name, amount in summary["categories"].items()
            if amount <= 0
        ),
        key=lambda item: (item[1], item[0].casefold()),
    )

    dominant_category: tuple[str, Decimal] | None = None
    chart_categories = positive_categories
    if positive_categories:
        largest = positive_categories[0]
        second_amount = positive_categories[1][1] if len(positive_categories) > 1 else Decimal("0")
        if len(positive_categories) == 1 or largest[1] >= second_amount * 2:
            dominant_category = largest
            chart_categories = positive_categories[1:]

    chart_limit = 7
    displayed_categories = chart_categories[:chart_limit]
    remaining_categories = chart_categories[chart_limit:]

    dashboard.sheet_view.showGridLines = False
    dashboard.sheet_view.zoomScale = 90
    dashboard.sheet_properties.tabColor = dashboard_salmon
    fill_range(dashboard, "A1:J42", dashboard_cream)
    for column, width in enumerate([19, 12, 12, 12, 3, 19, 12, 12, 12, 12], 1):
        dashboard.column_dimensions[get_column_letter(column)].width = width

    try:
        month_label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        month_label = month
    dashboard.merge_cells("A1:G2")
    dashboard["A1"] = f"{month_label} spending"
    dashboard["A1"].font = Font(size=25, bold=True, color=dashboard_text)
    dashboard["A1"].alignment = Alignment(vertical="center")
    dashboard.merge_cells("H1:J1")
    dashboard["H1"] = (profile_name or "Monthly overview").upper()
    dashboard["H1"].font = Font(size=10, bold=True, color=dashboard_salmon)
    dashboard["H1"].alignment = Alignment(horizontal="right", vertical="center")
    dashboard.merge_cells("H2:J2")
    dashboard["H2"] = "SPENDING ANALYZER"
    dashboard["H2"].font = Font(size=9, bold=True, color=dashboard_muted)
    dashboard["H2"].alignment = Alignment(horizontal="right", vertical="center")
    dashboard.row_dimensions[1].height = 30
    dashboard.row_dimensions[2].height = 22

    dashboard.merge_cells("A3:J3")
    dashboard["A3"] = (
        "Month result = total money in - net spending. Positive means more money came in than went out."
        + (
            f"  {summary['uncategorized_count']} uncategorized entr"
            f"{'y' if summary['uncategorized_count'] == 1 else 'ies'} still need review."
            if summary["uncategorized_count"] else ""
        )
    )
    dashboard["A3"].font = Font(size=9, color=dashboard_muted)

    def soft_card(cell_range: str, color: str) -> None:
        fill_range(dashboard, cell_range, color)
        for row in dashboard[cell_range]:
            for cell in row:
                cell.border = Border(bottom=Side(style="thin", color=dashboard_line))

    def set_metric(
        label_range: str, value_range: str, label: str, value: Decimal,
        *, fill: str, value_size: int = 20,
        label_color: str = dashboard_muted, value_color: str = dashboard_text,
        number_format: str = currency_format,
    ) -> None:
        dashboard.merge_cells(label_range)
        dashboard.merge_cells(value_range)
        card_range = f"{label_range.split(':')[0]}:{value_range.split(':')[-1]}"
        soft_card(card_range, fill)
        label_cell = dashboard[label_range.split(":")[0]]
        value_cell = dashboard[value_range.split(":")[0]]
        label_cell.value = label.upper()
        label_cell.font = Font(size=9, bold=True, color=label_color)
        label_cell.alignment = Alignment(vertical="bottom")
        value_cell.value = float(value)
        value_cell.number_format = number_format
        value_cell.font = Font(size=value_size, bold=True, color=value_color)
        value_cell.alignment = Alignment(vertical="center")

    month_result = summary["net_after_spending"]
    total_money_in = summary["income"] + summary["reimbursements"]
    month_result_label = (
        "Month result · more in than out"
        if month_result >= 0 else "Month result · more out than in"
    )
    set_metric(
        "A5:E5", "A6:E8", month_result_label, month_result,
        fill=dashboard_salmon_dark,
        value_size=28, label_color="FFFFFF", value_color="FFFFFF",
        number_format='+$#,##0.00;-$#,##0.00;$0.00',
    )
    set_metric(
        "F5:G5", "F6:G7", "Total money in", total_money_in,
        fill=dashboard_neutral_soft, value_size=17,
    )
    set_metric(
        "H5:J5", "H6:J7", "Net spending", summary["net_spending"],
        fill=dashboard_salmon_soft,
    )
    metric_notes = [
        (
            "F8:G8",
            f"Income ${summary['income']:,.2f} + reimbursements ${summary['reimbursements']:,.2f}",
            dashboard_neutral_soft,
        ),
        (
            "H8:J8",
            f"Purchases ${summary['spending']:,.2f} - refunds ${summary['refunds']:,.2f}",
            dashboard_salmon_soft,
        ),
    ]
    for cell_range, note, fill in metric_notes:
        dashboard.merge_cells(cell_range)
        soft_card(cell_range, fill)
        note_cell = dashboard[cell_range.split(":")[0]]
        note_cell.value = note
        note_cell.font = Font(size=8, color=dashboard_muted)
        note_cell.alignment = Alignment(wrap_text=True, vertical="center")
    dashboard.row_dimensions[5].height = 23
    dashboard.row_dimensions[6].height = 27
    dashboard.row_dimensions[7].height = 19
    dashboard.row_dimensions[8].height = 26

    dashboard.merge_cells("A10:F10")
    dashboard["A10"] = "Who the spending was for"
    dashboard["A10"].font = Font(size=12, bold=True, color=dashboard_text)
    estimated_unreimbursed = max(summary["other_people_after_reimbursements"], Decimal("0"))
    dashboard.merge_cells("G10:J10")
    if not summary["other_people_spending"]:
        dashboard["G10"] = "No spending marked for other people"
    elif estimated_unreimbursed:
        dashboard["G10"] = "Not all spending for others was reimbursed"
    else:
        dashboard["G10"] = "Spending for others was covered by reimbursements"
    dashboard["G10"].font = Font(size=10, bold=True, color=dashboard_text)

    allocation_cards = [
        ("A11:C11", "A12:C13", default_person or "Default person", allocation_totals[default_person or "Default person"]),
        ("D11:F11", "D12:F13", "Spent for other people", allocation_totals["Other people"]),
        ("A15:C15", "A16:C17", "No person needed", allocation_totals["No person needed"]),
        (
            "D15:F15", "D16:F17",
            "Needs a person" if default_person else "No default person",
            allocation_totals["Unassigned" if default_person else "Unassigned / no default person"],
        ),
    ]
    for label_range, value_range, label, value in allocation_cards:
        set_metric(label_range, value_range, label, value, fill=dashboard_white, value_size=15)

    soft_card("G11:J17", dashboard_neutral_soft)
    comparison = [
        ("Spent for other people", summary["other_people_spending"]),
        ("Reimbursements received", summary["reimbursements"]),
        ("Still not reimbursed (estimate)", estimated_unreimbursed),
    ]
    for row, (label, amount) in enumerate(comparison, 11):
        dashboard.merge_cells(start_row=row, start_column=7, end_row=row, end_column=9)
        dashboard.cell(row, 7, label)
        dashboard.cell(row, 7).font = Font(size=9, color=dashboard_muted)
        dashboard.cell(row, 10, float(amount))
        dashboard.cell(row, 10).number_format = currency_format
        dashboard.cell(row, 10).font = Font(
            size=11, bold=row == 13,
            color=dashboard_salmon if row == 13 and estimated_unreimbursed else dashboard_text,
        )
        dashboard.cell(row, 10).alignment = Alignment(horizontal="right")
    dashboard.merge_cells("G15:J17")
    if not summary["other_people_spending"]:
        dashboard["G15"] = (
            "No spending was marked for other people. Reimbursements are pooled, "
            "not linked to individual people yet."
        )
    elif estimated_unreimbursed:
        dashboard["G15"] = (
            f"About ${estimated_unreimbursed:,.2f} more was spent for other people than received back. "
            "Reimbursements are pooled, not linked to individual people yet."
        )
    else:
        dashboard["G15"] = (
            "Recorded reimbursements cover all marked spending for other people. "
            "Reimbursements are pooled, not linked to individual people yet."
        )
    dashboard["G15"].font = Font(size=8, italic=True, color=dashboard_muted)
    dashboard["G15"].alignment = Alignment(wrap_text=True, vertical="center")

    dashboard.merge_cells("A20:J20")
    dashboard["A20"] = "Net spending by category"
    dashboard["A20"].font = Font(size=14, bold=True, color=dashboard_text)
    dashboard.merge_cells("A21:J21")
    if remaining_categories:
        dashboard["A21"] = (
            f"Top {chart_limit} shown here. {len(remaining_categories)} smaller "
            f"categor{'y is' if len(remaining_categories) == 1 else 'ies are'} on the Categories sheet."
        )
    else:
        dashboard["A21"] = "Positive net categories. Refund-heavy totals are listed below."
    dashboard["A21"].font = Font(size=8, color=dashboard_muted)

    chart_anchor = "A22"
    chart_width = 26
    if dominant_category:
        dashboard.merge_cells("A22:D22")
        dashboard["A22"] = "LARGEST CATEGORY"
        dashboard["A22"].font = Font(size=9, bold=True, color=dashboard_salmon)
        dashboard.merge_cells("A23:D24")
        dashboard["A23"] = dominant_category[0]
        dashboard["A23"].font = Font(size=16, bold=True, color=dashboard_text)
        dashboard["A23"].alignment = Alignment(vertical="center")
        dashboard.merge_cells("A25:D27")
        dashboard["A25"] = float(dominant_category[1])
        dashboard["A25"].number_format = currency_format
        dashboard["A25"].font = Font(size=24, bold=True, color=dashboard_text)
        dashboard["A25"].alignment = Alignment(vertical="center")
        dashboard.merge_cells("A29:D31")
        dashboard["A29"] = (
            "Shown separately so smaller categories stay readable. See Categories for exact rows."
        )
        dashboard["A29"].font = Font(size=8, color=dashboard_muted)
        dashboard["A29"].alignment = Alignment(wrap_text=True, vertical="top")
        soft_card("A22:D31", dashboard_salmon_soft)
        chart_anchor = "E22"
        chart_width = 16

    dashboard["L1"], dashboard["M1"] = "Category", "Net amount"
    for row, (name, amount) in enumerate(displayed_categories, 2):
        dashboard.cell(row, 12, name)
        dashboard.cell(row, 13, float(amount))

    if displayed_categories:
        chart = BarChart()
        chart.type = "bar"
        chart.style = 10
        chart.legend = None
        chart.add_data(
            Reference(
                dashboard, min_col=13, min_row=1,
                max_row=len(displayed_categories) + 1,
            ),
            titles_from_data=True,
        )
        chart.set_categories(
            Reference(
                dashboard, min_col=12, min_row=2,
                max_row=len(displayed_categories) + 1,
            ),
        )
        chart.visible_cells_only = False
        chart.height = min(6.8, max(3.4, 2.4 + len(displayed_categories) * 0.55))
        chart.width = chart_width
        chart.varyColors = False
        chart.y_axis.scaling.min = 0
        chart.y_axis.scaling.max = float(
            max(amount for _, amount in displayed_categories) * Decimal("1.5")
        )
        chart.y_axis.majorGridlines = None
        chart.y_axis.delete = True
        chart.dataLabels = DataLabelList()
        chart.dataLabels.showVal = True
        chart.dataLabels.showCatName = False
        chart.dataLabels.showSerName = False
        chart.dataLabels.showLegendKey = False
        chart.dataLabels.showPercent = False
        chart.dataLabels.position = "outEnd"
        chart.dataLabels.numFmt = '$#,##0'
        chart.series[0].graphicalProperties.solidFill = dashboard_salmon
        chart.series[0].graphicalProperties.line.solidFill = dashboard_salmon
        dashboard.add_chart(chart, chart_anchor)
    elif not dominant_category:
        dashboard.merge_cells("A23:J27")
        dashboard["A23"] = "No categories had net purchases this month."
        dashboard["A23"].font = Font(size=12, italic=True, color=dashboard_muted)
        dashboard["A23"].alignment = Alignment(vertical="center")

    dashboard.merge_cells("A35:J35")
    dashboard["A35"] = "Refund-heavy and zero categories"
    dashboard["A35"].font = Font(size=10, bold=True, color=dashboard_text)
    dashboard.merge_cells("A36:J37")
    if nonpositive_categories:
        shown_nonpositive = nonpositive_categories[:4]
        def display_money(amount: Decimal) -> str:
            return f"-${abs(amount):,.2f}" if amount < 0 else f"${amount:,.2f}"
        nonpositive_text = "   |   ".join(
            f"{name}: {display_money(amount)}" for name, amount in shown_nonpositive
        )
        if len(nonpositive_categories) > len(shown_nonpositive):
            nonpositive_text += (
                f"   |   +{len(nonpositive_categories) - len(shown_nonpositive)} more on Categories"
            )
        dashboard["A36"] = nonpositive_text
    else:
        dashboard["A36"] = "None this month."
    dashboard["A36"].font = Font(size=9, color=dashboard_muted)
    dashboard["A36"].alignment = Alignment(wrap_text=True, vertical="top")

    dashboard.merge_cells("A39:J40")
    dashboard["A39"] = (
        "Negative category totals mean refunds or credits exceeded purchases. "
        "Income and estimated reimbursements are tracked separately from spending. "
        "No person needed means that category is configured to skip person tracking. "
        "Exact rows remain available on the Categories, People, Bills, and Transactions sheets."
    )
    dashboard["A39"].font = Font(size=8, color=dashboard_muted)
    dashboard["A39"].alignment = Alignment(wrap_text=True, vertical="top")
    dashboard.row_dimensions[39].height = 24
    dashboard.row_dimensions[40].height = 24

    for column in ("L", "M"):
        dashboard.column_dimensions[column].hidden = True
    dashboard.freeze_panes = "A4"
    dashboard.print_area = "A1:J40"
    set_print_layout(dashboard)
    dashboard.page_setup.fitToHeight = 1

    category_person_totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal("0"))
    )
    category_detail_rows: dict[str, list[tuple[dict[str, Any], Decimal, str]]] = defaultdict(list)
    for transaction in transactions:
        for category, amount, responsibility in category_entries(transaction):
            if category in personless_categories:
                person = "No person needed"
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
        2 if person == "No person needed" else 1,
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
        sheet["A1"].fill = PatternFill("solid", fgColor=detail_header)
        fill_range(sheet, "A1:E1", detail_header)
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
                heading.font = Font(bold=True, color=detail_section_text, size=12)
                heading.fill = PatternFill("solid", fgColor=detail_section)
                sheet.cell(row_cursor, column + 1).fill = PatternFill("solid", fgColor=detail_section)
                heading.alignment = Alignment(wrap_text=True, vertical="center")
                if len(name) > 36:
                    sheet.row_dimensions[row_cursor].height = 32
                for offset, (label, amount) in enumerate(lines, 1):
                    label_cell = sheet.cell(row_cursor + offset, column, label)
                    amount_cell = sheet.cell(row_cursor + offset, column + 1, float(amount))
                    amount_cell.number_format = currency_format
                    label_cell.fill = amount_cell.fill = PatternFill("solid", fgColor=detail_light)
                total_row = row_cursor + len(lines) + 1
                total_label = sheet.cell(total_row, column, "Total")
                total_value = sheet.cell(total_row, column + 1, float(total))
                total_label.font = total_value.font = Font(bold=True, color=detail_section_text)
                total_label.fill = total_value.fill = PatternFill("solid", fgColor=detail_pale)
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
    categories.sheet_properties.tabColor = dashboard_salmon
    categories.sheet_view.showGridLines = False
    categories.sheet_properties.outlinePr.summaryBelow = False
    categories.append([
        "Category / Person", "Date", "Description", "Account", "Type",
        "Net Amount",
    ])
    for cell in categories[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor=detail_header)
    for category in matrix_categories:
        entries = sorted(
            category_detail_rows[category],
            key=lambda item: (item[2], item[0]["date"], item[0]["provider"], item[0]["description"]),
        )
        category_row = categories.max_row + 1
        category_total = sum((amount for _transaction, amount, _person in entries), Decimal("0"))
        categories.append([category, None, f"{len(entries)} categorized item(s)", None, None, float(category_total)])
        for cell in categories[category_row]:
            cell.font = Font(bold=True, color=detail_section_text)
            cell.fill = PatternFill("solid", fgColor=detail_section)
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
                    cell.font = Font(bold=True, color=detail_section_text)
                    cell.fill = PatternFill("solid", fgColor=detail_pale)
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
        cell.fill = PatternFill("solid", fgColor=detail_header)
    categories.cell(category_grand_total_row, 6).number_format = currency_format
    categories.freeze_panes = "A2"
    for index, width in enumerate([26, 12, 48, 20, 16, 25], 1):
        categories.column_dimensions[get_column_letter(index)].width = width
    categories.print_title_rows = "1:1"
    set_print_layout(categories)

    people_cards = []
    for person in person_columns:
        if person == "No person needed":
            continue
        lines = [
            (category, category_person_totals[category][person])
            for category in matrix_categories if category_person_totals[category][person]
        ]
        lines.sort(key=lambda item: (-item[1], item[0]))
        people_cards.append((person, lines, sum((amount for _category, amount in lines), Decimal("0"))))
    for category in matrix_categories:
        amount = category_person_totals[category].get("No person needed", Decimal("0"))
        if amount:
            people_cards.append((category, [], amount))
    people_sheet = workbook.create_sheet("People")
    people_sheet.sheet_properties.tabColor = dashboard_salmon
    build_card_sheet(people_sheet, "Spending by person", people_cards)
    people_sheet.merge_cells("A2:E2")
    people_sheet["A2"] = (
        "Reimbursements are pooled, so this sheet shows spending by person or group, but not repayment by person."
    )
    people_sheet["A2"].font = Font(size=9, italic=True, color=muted)
    people_sheet["A2"].alignment = Alignment(wrap_text=True, vertical="center")
    people_sheet.row_dimensions[2].height = 28

    bills = workbook.create_sheet("Bills")
    bills.sheet_properties.tabColor = dashboard_salmon
    bills.sheet_view.showGridLines = False
    bills.sheet_properties.outlinePr.summaryBelow = False
    bills.append(["For", "Date", "Bill", "Account", "Type", "Net Amount"])
    for cell in bills[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor=detail_header)
    bill_entries_by_person: dict[str, list[tuple[dict[str, Any], Decimal]]] = defaultdict(list)
    for transaction, amount, person in category_detail_rows.get("Bills", []):
        bill_entries_by_person[person or "Unassigned"].append((transaction, amount))
    for person, entries in sorted(bill_entries_by_person.items()):
        entries.sort(key=lambda item: (item[0]["date"], item[0]["description"]))
        person_row = bills.max_row + 1
        total = sum((amount for _transaction, amount in entries), Decimal("0"))
        bills.append([person, None, f"{len(entries)} bill(s)", None, None, float(total)])
        for cell in bills[person_row]:
            cell.font = Font(bold=True, color=detail_section_text)
            cell.fill = PatternFill("solid", fgColor=detail_pale)
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
        cell.fill = PatternFill("solid", fgColor=detail_header)
    bills.cell(bills_total_row, 6).number_format = currency_format
    bills.freeze_panes = "A2"
    for index, width in enumerate([24, 12, 48, 20, 16, 18], 1):
        bills.column_dimensions[get_column_letter(index)].width = width
    bills.print_title_rows = "1:1"
    set_print_layout(bills)

    sheet = workbook.create_sheet("Transactions")
    sheet.sheet_properties.tabColor = dashboard_salmon
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
    style_data_sheet(sheet, [12, 20, 46, 19, 24, 20, 15, 15, 15, 21, 38, 25, 18, 12])
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
