"""Rules and interactive transaction categorization."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
import re
from typing import Any, Callable

from .constants import CATEGORIES
from .providers import account_label, clean_text
from .storage import money

def apply_rules_and_decisions(transactions: list[dict[str, Any]], rules: list[dict[str, str]], decisions: dict[str, Any]) -> None:
    for transaction in transactions:
        if "original_kind" not in transaction:
            transaction["original_kind"] = transaction["kind"]
        transaction["kind"] = transaction["original_kind"]
        decision = decisions.get(transaction["id"], {})
        matching_rule = best_matching_rule(transaction, rules)
        kind_override = decision.get("kind_override") or (
            matching_rule.get("kind_override", "") if matching_rule else ""
        )
        if kind_override:
            transaction["kind"] = kind_override
        if decision.get("category"):
            transaction["category"] = decision["category"]
        elif transaction["kind"] in {"expense", "refund"} and matching_rule:
            transaction["category"] = matching_rule["category"]
        else:
            transaction["category"] = "Uncategorized"
        transaction["reviewed"] = bool(decision.get("reviewed") or (
            matching_rule.get("reviewed") if matching_rule else False
        ))
        transaction["splits"] = decision.get("splits", []) if transaction["category"] == "Split" else []
        if transaction["kind"] in {"expense", "refund"} and transaction["category"] != "Uncategorized":
            if "responsibility" in decision:
                transaction["responsibility"] = decision["responsibility"]
            else:
                transaction["responsibility"] = (
                    matching_rule.get("responsibility", "") if matching_rule else ""
                )
        else:
            transaction["responsibility"] = ""


def rule_pattern(rule: dict[str, str]) -> str:
    return (rule.get("pattern") or rule.get("identifier") or "").upper()


def rule_matches(transaction: dict[str, Any], rule: dict[str, str]) -> bool:
    if rule.get("account") != transaction["account"] or rule.get("provider") != transaction["provider"]:
        return False
    candidate = transaction["identifier"].upper()
    pattern = rule_pattern(rule)
    match_type = rule.get("match_type", "exact")
    if not pattern:
        return False
    if match_type == "exact":
        return candidate == pattern
    if match_type == "starts_with":
        return candidate.startswith(pattern)
    if match_type == "contains":
        return pattern in candidate
    if match_type == "contains_words":
        candidate_words = set(re.findall(r"[A-Z0-9]+", candidate))
        pattern_words = set(re.findall(r"[A-Z0-9]+", pattern))
        return bool(pattern_words) and pattern_words <= candidate_words
    return False


def best_matching_rule(transaction: dict[str, Any], rules: list[dict[str, str]]) -> dict[str, str] | None:
    priority = {"exact": 4, "starts_with": 3, "contains": 2, "contains_words": 1}
    matches = [rule for rule in rules if rule_matches(transaction, rule)]
    if not matches:
        return None
    return max(matches, key=lambda rule: (priority.get(rule.get("match_type", "exact"), 0), len(rule_pattern(rule))))


def migrate_legacy_rules(rules: list[dict[str, str]], transactions: list[dict[str, Any]]) -> int:
    accounts_by_provider: dict[str, set[str]] = defaultdict(set)
    for transaction in transactions:
        accounts_by_provider[transaction["provider"]].add(transaction["account"])
    migrated = 0
    for rule in rules:
        if rule.get("account"):
            continue
        accounts = accounts_by_provider.get(rule.get("provider", ""), set())
        if len(accounts) == 1:
            rule["account"] = next(iter(accounts))
            migrated += 1
    return migrated


def add_custom_category(
    categories: list[str], save_callback: Callable[[], None] | None = None,
    personless_categories: set[str] | None = None,
) -> str | None:
    personless_categories = personless_categories if personless_categories is not None else set()
    name = clean_text(input("New category name (Enter cancels): "))
    if not name:
        return None
    existing = next((category for category in categories if category.casefold() == name.casefold()), None)
    if existing:
        print(f"Category already exists: {existing}")
        return existing
    if name.casefold() == "split":
        print("“Split” is reserved; choose another category name.")
        return None
    insert_at = categories.index("Uncategorized") if "Uncategorized" in categories else len(categories)
    categories.insert(insert_at, name)
    personless = input(f"Should {name} skip person assignment? [y/N]: ").strip().lower()
    if personless in {"y", "yes"}:
        personless_categories.add(name)
    if save_callback:
        save_callback()
    print(f"Added category: {name}")
    return name


def ask_category(
    allow_split: bool, allow_back: bool = False, allow_review_each: bool = False,
    categories: list[str] | None = None,
    save_callback: Callable[[], None] | None = None,
    personless_categories: set[str] | None = None,
) -> tuple[str, bool]:
    categories = categories if categories is not None else CATEGORIES
    personless_categories = personless_categories if personless_categories is not None else set()
    while True:
        for number, category in enumerate(categories, 1):
            print(f"  {number}. {category}")
        split_number = len(categories) + 1
        if allow_split:
            print(f"  {split_number}. Split purchase")
        if allow_review_each:
            print("  R. Review these transactions individually")
        print("  Add P after a number to choose who it was for (example: 5p)")
        print("  A. Add a category")
        if allow_back:
            print("  B. Undo previous answer and go back")
        answer = input("Category number (Enter reviews later): ").strip().lower()
        if answer == "a":
            added = add_custom_category(categories, save_callback, personless_categories)
            if added:
                return added, False
            continue
        if answer == "b" and allow_back:
            return "__back__", False
        if answer == "r" and allow_review_each:
            return "__review_each__", False
        if not answer:
            return "__skip__", False
        choose_person = answer.endswith("p")
        number_text = answer[:-1] if choose_person else answer
        if number_text.isdigit() and 1 <= int(number_text) <= len(categories):
            return categories[int(number_text) - 1], choose_person
        if allow_split and number_text == str(split_number):
            return "Split", choose_person
        print("Please enter one of the listed choices.")


def ask_split_category(
    label: str, excluded: str | None = None, categories: list[str] | None = None,
    save_callback: Callable[[], None] | None = None,
    personless_categories: set[str] | None = None,
) -> tuple[str, bool]:
    categories = categories if categories is not None else CATEGORIES
    personless_categories = personless_categories if personless_categories is not None else set()
    while True:
        choices = [category for category in categories if category != "Uncategorized" and category != excluded]
        print(label)
        for number, category in enumerate(choices, 1):
            print(f"  {number}. {category}")
        print("  A. Add a category")
        answer = input("Category number: ").strip()
        if answer.lower() == "a":
            added = add_custom_category(categories, save_callback, personless_categories)
            if added and added != excluded:
                return added, False
            continue
        choose_person = answer.lower().endswith("p")
        number_text = answer[:-1] if choose_person else answer
        if number_text.isdigit() and 1 <= int(number_text) <= len(choices):
            return choices[int(number_text) - 1], choose_person
        print("Please enter one of the listed numbers.")


def prompt_for_split(
    transaction: dict[str, Any], decisions: dict[str, Any], save_callback: Callable[[], None] | None = None,
    categories: list[str] | None = None, people: list[str] | None = None, default_person: str = "",
    category_person_defaults: dict[str, str] | None = None,
    personless_categories: set[str] | None = None,
) -> None:
    categories = categories if categories is not None else CATEGORIES
    people = people if people is not None else []
    category_person_defaults = category_person_defaults if category_person_defaults is not None else {}
    personless_categories = personless_categories if personless_categories is not None else set()
    total = expense_amount(transaction)
    while True:
        print(f"\nSplit {transaction['date']} | ${total:,.2f} | {transaction['description']}")
        (first, first_choose_person) = ask_split_category(
            "Choose the first category:", categories=categories, save_callback=save_callback,
            personless_categories=personless_categories,
        )
        (second, second_choose_person) = ask_split_category(
            "Choose the second category:", excluded=first, categories=categories, save_callback=save_callback,
            personless_categories=personless_categories,
        )
        while True:
            entered = input(f"Approximately how much of ${total:,.2f} belongs to {first}? $").strip()
            try:
                first_amount = money(entered)
            except ValueError:
                print("Please enter a dollar amount, such as 20 or 20.50.")
                continue
            if first_amount <= 0 or first_amount >= total:
                print(f"Enter an amount greater than $0.00 and less than ${total:,.2f}.")
                continue
            break
        second_amount = total - first_amount
        print(f"Split: ${first_amount:,.2f} {first}; ${second_amount:,.2f} {second}")
        break
    splits = [{"category": first, "amount": f"{first_amount:.2f}"}, {"category": second, "amount": f"{second_amount:.2f}"}]
    for split, choose_person in zip(splits, (first_choose_person, second_choose_person)):
        if split["category"] in personless_categories:
            split.pop("responsibility", None)
        elif choose_person or split["category"] == "Bills":
            split["responsibility"] = ask_bill_responsibility(
                f"Who is the ${money(split['amount']):,.2f} {split['category']} portion for?", people, save_callback
            )
        else:
            assigned_person = category_person_defaults.get(split["category"], default_person)
            if assigned_person:
                split["responsibility"] = assigned_person
            else:
                split.pop("responsibility", None)
    decisions[transaction["id"]] = {
        "category": "Split", "splits": splits, "responsibility": "",
    }
    transaction["category"], transaction["splits"] = "Split", splits
    if save_callback:
        save_callback()
    print("Split saved.")


def _assign_single_transaction_category(
    transaction: dict[str, Any], category: str, choose_person: bool,
    decisions: dict[str, Any], save_callback: Callable[[], None] | None,
    categories: list[str], people: list[str], default_person: str,
    always_ask_categories: set[str], category_person_defaults: dict[str, str],
    personless_categories: set[str],
) -> None:
    original_kind = transaction.get("original_kind", transaction["kind"])
    if category == "Split":
        staged_transaction = deepcopy(transaction)
        staged_transaction["kind"] = original_kind
        staged_decisions: dict[str, Any] = {}
        prompt_for_split(
            staged_transaction, staged_decisions, None, categories, people,
            default_person, category_person_defaults, personless_categories,
        )
        staged_decision = staged_decisions[transaction["id"]]
        staged_decision["kind_override"] = original_kind
        decisions[transaction["id"]] = staged_decision
        transaction["kind"] = original_kind
        transaction["category"] = "Split"
        transaction["splits"] = staged_transaction["splits"]
        transaction["responsibility"] = ""
        transaction["reviewed"] = False
        if save_callback:
            save_callback()
        return

    decision = deepcopy(decisions.get(transaction["id"], {}))
    decision["category"] = category
    decision.pop("splits", None)
    decision["kind_override"] = original_kind
    decision.pop("reviewed", None)
    if category == "Income":
        decision["kind_override"] = "income"
    elif category == "Uncategorized":
        decision["reviewed"] = True

    person = ""
    if category not in personless_categories and category not in {"Income", "Uncategorized"}:
        if choose_person or category in always_ask_categories:
            person = ask_bill_responsibility("Who was this for?", people, save_callback)
        else:
            person = category_person_defaults.get(category, default_person)
    if category in personless_categories:
        decision["responsibility"] = ""
    elif category in {"Income", "Uncategorized"}:
        decision.pop("responsibility", None)
    else:
        decision["responsibility"] = person
    decisions[transaction["id"]] = decision
    transaction["kind"] = decision["kind_override"]
    transaction["category"] = category
    transaction["splits"] = []
    transaction["reviewed"] = category == "Uncategorized"
    transaction["responsibility"] = person
    if save_callback:
        save_callback()


def prompt_for_individual_categories(
    transactions: list[dict[str, Any]], decisions: dict[str, Any],
    save_callback: Callable[[], None] | None, categories: list[str],
    people: list[str], default_person: str, always_ask_categories: set[str],
    category_person_defaults: dict[str, str], personless_categories: set[str],
) -> int:
    print("Reviewing each transaction separately. No reusable rule will be added for this group.")
    changed = 0
    for transaction in sorted(transactions, key=lambda value: value["date"]):
        amount = expense_amount(transaction) or credit_amount(transaction)
        print(
            f"\n{transaction['date']} | {transaction['kind'].title()} | "
            f"${amount:,.2f} | {transaction['description']}"
        )
        category, choose_person = ask_category(
            allow_split=transaction["kind"] == "expense",
            categories=categories,
            save_callback=save_callback,
            personless_categories=personless_categories,
        )
        if category == "__skip__":
            continue
        _assign_single_transaction_category(
            transaction, category, choose_person, decisions, save_callback,
            categories, people, default_person, always_ask_categories,
            category_person_defaults, personless_categories,
        )
        changed += 1
    return changed


def prompt_to_save_rule(
    provider: str, account: str, identifier: str, category: str, rules: list[dict[str, str]],
    save_callback: Callable[[], None] | None = None, responsibility: str = "",
) -> None:
    entered = input(
        f"Rule for “{identifier}”: type contains text, Enter=exact, M=more options, N=no rule: "
    ).strip()
    if entered.lower() in {"n", "no"}:
        return
    if not entered:
        match_type, pattern = "exact", identifier
    elif entered.lower() not in {"m", "more"}:
        match_type, pattern = "contains_words", clean_text(entered).upper()
    else:
        print("How should future identifiers match?")
        print("  1. Starts with")
        print("  2. Contains exact text, including punctuation and spacing")
        while True:
            choice = input("Match type: ").strip()
            if choice in {"1", "2"}:
                break
            print("Please enter 1 or 2.")
        match_type = {"1": "starts_with", "2": "contains"}[choice]
        while True:
            pattern = clean_text(input("Text to match: ")).upper()
            if pattern:
                break
            print("Match text cannot be blank.")
    new_rule = {"provider": provider, "account": account, "match_type": match_type, "pattern": pattern, "category": category}
    if category == "Income":
        new_rule["kind_override"] = "income"
    if category == "Uncategorized":
        new_rule["reviewed"] = True
    if responsibility:
        new_rule["responsibility"] = responsibility
    rules[:] = [rule for rule in rules if not (
        rule.get("provider") == provider and rule.get("account") == account
        and rule.get("match_type", "exact") == match_type and rule_pattern(rule) == pattern
    )]
    rules.append(new_rule)
    if save_callback:
        save_callback()
    print(f"Saved {match_type.replace('_', ' ')} rule for “{pattern}”.")


def prompt_for_categories(
    transactions: list[dict[str, Any]], rules: list[dict[str, str]], decisions: dict[str, Any],
    save_callback: Callable[[], None] | None = None, categories: list[str] | None = None,
    people: list[str] | None = None, default_person: str = "", always_ask_categories: set[str] | None = None,
    category_person_defaults: dict[str, str] | None = None,
    personless_categories: set[str] | None = None,
) -> int:
    categories = categories if categories is not None else CATEGORIES
    people = people if people is not None else []
    always_ask_categories = always_ask_categories if always_ask_categories is not None else {"Bills"}
    category_person_defaults = category_person_defaults if category_person_defaults is not None else {}
    personless_categories = personless_categories if personless_categories is not None else set()
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for transaction in transactions:
        if (
            transaction["kind"] in {"expense", "refund"}
            and transaction["category"] == "Uncategorized"
            and not transaction.get("reviewed")
        ):
            groups[(transaction["provider"], transaction["account"], transaction["identifier"])].append(transaction)
    changed = 0
    if not groups:
        return changed
    print(f"\n{sum(len(value) for value in groups.values())} expense/refund transaction(s) need categories.")
    group_list = sorted(groups.items(), key=lambda item: item[0])
    history: list[tuple[int, dict[str, Any], list[dict[str, str]], int]] = []
    index = 0
    while index < len(group_list):
        (provider, account, identifier), items = group_list[index]
        apply_rules_and_decisions(items, rules, decisions)
        if all(item["category"] != "Uncategorized" for item in items):
            index += 1
            continue
        expenses = sum((expense_amount(item) for item in items), Decimal("0"))
        refunds = sum((credit_amount(item) for item in items), Decimal("0"))
        print(f"\n{account_label(items[0])} | {identifier}")
        print(f"{len(items)} transaction(s): ${expenses:,.2f} purchases, ${refunds:,.2f} refunds/credits")
        for number, item in enumerate(sorted(items, key=lambda value: value["date"]), 1):
            amount = expense_amount(item) or credit_amount(item)
            print(f"  Transaction {number}: {item['date']} | {item['kind'].title()} | ${amount:,.2f}")
        category, choose_person = ask_category(
            allow_split=len(items) == 1 and items[0]["kind"] == "expense",
            allow_back=bool(history), allow_review_each=len(items) > 1,
            categories=categories, save_callback=save_callback, personless_categories=personless_categories,
        )
        if category == "__back__":
            previous_index, previous_decisions, previous_rules, previous_changed = history.pop()
            decisions.clear()
            decisions.update(previous_decisions)
            rules[:] = previous_rules
            changed = previous_changed
            apply_rules_and_decisions(transactions, rules, decisions)
            if save_callback:
                save_callback()
            index = previous_index
            continue
        if category == "__skip__":
            index += 1
            continue
        snapshot = (index, deepcopy(decisions), deepcopy(rules), changed)
        if category == "__review_each__":
            changed += prompt_for_individual_categories(
                items, decisions, save_callback, categories, people, default_person,
                always_ask_categories, category_person_defaults, personless_categories,
            )
            history.append(snapshot)
            index += 1
            continue
        if category == "Split":
            prompt_for_split(
                items[0], decisions, save_callback, categories, people,
                default_person, category_person_defaults, personless_categories,
            )
            changed += 1
            history.append(snapshot)
            index += 1
            continue
        for item in items:
            decisions.setdefault(item["id"], {})["category"] = category
            decisions[item["id"]].pop("splits", None)
            decisions[item["id"]].pop("kind_override", None)
            decisions[item["id"]].pop("reviewed", None)
            if category == "Income":
                decisions[item["id"]]["kind_override"] = "income"
                item["kind"] = "income"
            elif category == "Uncategorized":
                decisions[item["id"]]["reviewed"] = True
            item["category"] = category
            item["splits"] = []
            changed += 1
        person = ""
        if category in personless_categories or category in {"Income", "Uncategorized"}:
            person = ""
        elif choose_person or category in always_ask_categories:
            review_each_person = False
            if len(items) > 1:
                same_answer = input(f"Use the same person for all {len(items)} transactions? [Y/n]: ").strip().lower()
                review_each_person = same_answer in {"n", "no"}
            if review_each_person:
                for item in sorted(items, key=lambda value: value["date"]):
                    amount = expense_amount(item) or credit_amount(item)
                    item_person = ask_bill_responsibility(
                        f"{item['date']} | ${amount:,.2f} | {item['description']}: who was this for?",
                        people, save_callback,
                    )
                    decisions[item["id"]]["responsibility"] = item_person
                    item["responsibility"] = item_person
            else:
                person = ask_bill_responsibility("Who was this for?", people, save_callback)
        else:
            person = category_person_defaults.get(category, default_person)
        if person:
            for item in items:
                decisions[item["id"]]["responsibility"] = person
                item["responsibility"] = person
        elif category in personless_categories:
            for item in items:
                decisions[item["id"]]["responsibility"] = ""
        if save_callback:
            save_callback()
        prompt_to_save_rule(
            provider, account, identifier, category, rules, save_callback, responsibility=person
        )
        history.append(snapshot)
        index += 1
    return changed


def _select_saved_transaction(
    transactions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    eligible = [
        transaction for transaction in transactions
        if transaction.get("original_kind", transaction["kind"])
        in {"expense", "refund", "income", "reimbursement"}
    ]
    if not eligible:
        print("No saved expenses, refunds, income, or reimbursements are available to review.")
        return None

    months = sorted({transaction["date"][:7] for transaction in eligible}, reverse=True)
    print("\nSaved months:")
    for number, month in enumerate(months, 1):
        count = sum(transaction["date"].startswith(month) for transaction in eligible)
        print(f"  {number}. {month} ({count} transaction{'s' if count != 1 else ''})")
    while True:
        answer = input("Month number (Enter cancels): ").strip()
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(months):
            month = months[int(answer) - 1]
            break
        print("Please enter one of the listed month numbers.")

    month_transactions = [
        transaction for transaction in eligible if transaction["date"].startswith(month)
    ]
    while True:
        query = clean_text(input(
            "Search description, account, category, person, or amount "
            "(* lists all; Enter cancels): "
        ))
        if not query:
            return None
        if query == "*":
            matches = month_transactions
        else:
            search = query.casefold()
            matches = [
                transaction for transaction in month_transactions
                if search in " ".join((
                    transaction.get("description", ""),
                    transaction.get("identifier", ""),
                    account_label(transaction),
                    transaction.get("category", ""),
                    transaction.get("responsibility", ""),
                    transaction.get("raw_amount", ""),
                    " ".join(
                        str(value)
                        for split in transaction.get("splits", [])
                        for value in (
                            split.get("category", ""),
                            split.get("responsibility", ""),
                            split.get("amount", ""),
                        )
                    ),
                )).casefold()
            ]
        if not matches:
            print("No matching transactions. Try another search.")
            continue
        if len(matches) > 50 and query != "*":
            print(f"{len(matches)} transactions matched. Use a more specific search or * to list all.")
            continue
        break

    matches = sorted(
        matches,
        key=lambda transaction: (
            transaction["date"], transaction["description"], transaction["id"],
        ),
        reverse=True,
    )
    print("\nMatching transactions:")
    for number, transaction in enumerate(matches, 1):
        amount = abs(money(transaction["raw_amount"]))
        category = transaction.get("category") or "Uncategorized"
        person = transaction.get("responsibility") or "No person"
        if transaction.get("splits"):
            split_categories = list(dict.fromkeys(
                split.get("category", "") for split in transaction["splits"]
                if split.get("category")
            ))
            split_people = list(dict.fromkeys(
                split.get("responsibility", "") for split in transaction["splits"]
                if split.get("responsibility")
            ))
            if split_categories:
                category = f"Split ({' / '.join(split_categories)})"
            if split_people:
                person = " / ".join(split_people)
        print(
            f"  {number}. {transaction['date']} | {account_label(transaction)} | "
            f"{transaction['description']} | {transaction['kind'].replace('_', ' ').title()} | "
            f"${amount:,.2f} | {category} | {person}"
        )
    while True:
        answer = input("Transaction number (Enter cancels): ").strip()
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(matches):
            return matches[int(answer) - 1]
        print("Please enter one of the listed transaction numbers.")


def prompt_for_saved_transaction_corrections(
    transactions: list[dict[str, Any]], rules: list[dict[str, str]],
    decisions: dict[str, Any], save_callback: Callable[[], None] | None = None,
    categories: list[str] | None = None, people: list[str] | None = None,
    default_person: str = "", always_ask_categories: set[str] | None = None,
    category_person_defaults: dict[str, str] | None = None,
    personless_categories: set[str] | None = None,
) -> int:
    categories = categories if categories is not None else CATEGORIES
    people = people if people is not None else []
    always_ask_categories = (
        always_ask_categories if always_ask_categories is not None else {"Bills"}
    )
    category_person_defaults = (
        category_person_defaults if category_person_defaults is not None else {}
    )
    personless_categories = (
        personless_categories if personless_categories is not None else set()
    )
    apply_rules_and_decisions(transactions, rules, decisions)
    corrected = 0
    while True:
        transaction = _select_saved_transaction(transactions)
        if transaction is None:
            return corrected

        print("\nThis changes only the selected transaction.")
        if best_matching_rule(transaction, rules):
            print("A reusable rule also matches it. That rule will remain unchanged.")
        else:
            print("Reusable rules will remain unchanged.")

        original_kind = transaction.get("original_kind", transaction["kind"])
        if original_kind in {"expense", "refund"}:
            category, choose_person = ask_category(
                allow_split=original_kind == "expense",
                categories=categories,
                save_callback=save_callback,
                personless_categories=personless_categories,
            )
            if category == "__skip__":
                print("No correction was saved.")
                return corrected
            _assign_single_transaction_category(
                transaction, category, choose_person, decisions, save_callback,
                categories, people, default_person, always_ask_categories,
                category_person_defaults, personless_categories,
            )
        else:
            print("  1. Income")
            print("  2. Estimated reimbursement")
            while True:
                answer = input("Money-in type (Enter cancels): ").strip()
                if not answer:
                    print("No correction was saved.")
                    return corrected
                if answer in {"1", "2"}:
                    break
                print("Please enter 1 or 2.")
            decision = decisions.setdefault(transaction["id"], {})
            decision.clear()
            decision["kind_override"] = "income" if answer == "1" else "reimbursement"

        apply_rules_and_decisions([transaction], rules, decisions)
        if save_callback:
            save_callback()
        corrected += 1
        print("Saved correction. Reusable rules were not changed.")
        another = input("Review another saved transaction? [y/N]: ").strip().lower()
        if another not in {"y", "yes"}:
            return corrected


def ask_bill_responsibility(
    prompt: str, people: list[str] | None = None, save_callback: Callable[[], None] | None = None
) -> str:
    people = people if people is not None else []
    while True:
        print(prompt)
        if people:
            print("Saved people:")
            for number, person in enumerate(people, 1):
                print(f"  {number}. {person}")
        answer = clean_text(input("Enter a saved number or a new name: "))
        if answer.isdigit():
            if 1 <= int(answer) <= len(people):
                return people[int(answer) - 1]
            print("That saved-person number does not exist.")
            continue
        if not answer:
            print("Please enter a name or a saved number.")
            continue
        existing = next((person for person in people if person.casefold() == answer.casefold()), None)
        if existing:
            return existing
        people.append(answer)
        if save_callback:
            save_callback()
        print(f"Saved person: {answer}")
        return answer


def prompt_for_bill_responsibilities(
    transactions: list[dict[str, Any]], rules: list[dict[str, str]], decisions: dict[str, Any],
    save_callback: Callable[[], None] | None = None, offer_rule: bool = True,
    people: list[str] | None = None,
    personless_categories: set[str] | None = None,
) -> int:
    people = people if people is not None else []
    personless_categories = personless_categories if personless_categories is not None else set()
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for transaction in transactions:
        if (
            transaction["kind"] in {"expense", "refund"}
            and transaction.get("category") not in {None, "", "Uncategorized", "Split"}
            and transaction.get("category") not in personless_categories
            and not transaction.get("responsibility")
        ):
            groups[(transaction["provider"], transaction["account"], transaction["identifier"])].append(transaction)
    changed = 0
    for (provider, account, identifier), items in sorted(groups.items(), key=lambda item: item[0]):
        category = items[0]["category"]
        print(f"\nWho was this for? {account_label(items[0])} | {identifier} | {category}")
        review_each = False
        if len(items) > 1:
            answer = input(f"Use the same person for all {len(items)} bills? [Y/n]: ").strip().lower()
            review_each = answer in {"n", "no"}
        if review_each:
            for item in sorted(items, key=lambda value: value["date"]):
                amount = expense_amount(item) or credit_amount(item)
                responsibility = ask_bill_responsibility(
                    f"{item['date']} | ${amount:,.2f} | {item['description']}: who is this for?",
                    people, save_callback,
                )
                decisions.setdefault(item["id"], {})["responsibility"] = responsibility
                item["responsibility"] = responsibility
                changed += 1
                if save_callback:
                    save_callback()
            continue

        responsibility = ask_bill_responsibility("Who is this bill for?", people, save_callback)
        for item in items:
            decisions.setdefault(item["id"], {})["responsibility"] = responsibility
            item["responsibility"] = responsibility
            changed += 1
        if save_callback:
            save_callback()
        if offer_rule:
            prompt_to_save_rule(
                provider, account, identifier, category, rules, save_callback, responsibility=responsibility
            )
    return changed


def expense_amount(transaction: dict[str, Any]) -> Decimal:
    return abs(money(transaction["raw_amount"])) if transaction["kind"] == "expense" else Decimal("0")


def credit_amount(transaction: dict[str, Any]) -> Decimal:
    return abs(money(transaction["raw_amount"])) if transaction["kind"] == "refund" else Decimal("0")
