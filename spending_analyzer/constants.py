"""Shared defaults and supported bank export layouts."""

__version__ = "1.1.0"

CATEGORIES = [
    "Groceries",
    "Dining",
    "Gas",
    "Transportation",
    "Shopping",
    "Gifts",
    "Activities",
    "Bills",
    "Cash",
    "Other",
    "Income",
    "Uncategorized",
]

DEFAULT_SETTINGS = {
    "default_person": "",
    "always_ask_person_categories": ["Bills"],
    "personless_categories": ["Income"],
    "other_people_categories": [],
    "incoming_checking_kinds": {
        "p2p": "reimbursement",
        "deposit": "reimbursement",
        "ach_credit": "income",
        "other_credit": "reimbursement",
    },
}

PROVIDER_HEADERS = {
    "apple": {"Transaction Date", "Clearing Date", "Merchant", "Type", "Amount (USD)"},
    "chase_credit": {"Transaction Date", "Post Date", "Description", "Type", "Amount", "Memo"},
    "discover": {"Trans. Date", "Post Date", "Description", "Amount", "Category"},
    "amex": {"Date", "Description", "Amount", "Extended Details", "Appears On Your Statement As", "Reference"},
    "chase_checking": {"Details", "Posting Date", "Description", "Amount", "Type", "Balance", "Check or Slip #"},
}

PROVIDER_LABELS = {
    "apple": "Apple Card",
    "chase_credit": "Chase credit",
    "discover": "Discover",
    "amex": "Amex",
    "chase_checking": "Chase checking",
}

SUPPORTED_PROVIDERS = tuple(PROVIDER_HEADERS)
