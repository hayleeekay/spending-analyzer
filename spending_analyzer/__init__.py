"""Public API for the local spending analyzer."""

from .application import process
from .categorization import (
    add_custom_category,
    apply_rules_and_decisions,
    ask_bill_responsibility,
    ask_category,
    ask_split_category,
    best_matching_rule,
    credit_amount,
    expense_amount,
    migrate_legacy_rules,
    prompt_for_bill_responsibilities,
    prompt_for_categories,
    prompt_for_split,
    prompt_to_save_rule,
    rule_matches,
    rule_pattern,
)
from .cli import main
from .constants import (
    CATEGORIES,
    DEFAULT_SETTINGS,
    PROVIDER_HEADERS,
    PROVIDER_LABELS,
    SUPPORTED_PROVIDERS,
    __version__,
)
from .providers import (
    account_id,
    account_label,
    classify_kind,
    clean_identifier,
    clean_identifier_value,
    clean_text,
    detect_provider,
    normalize_row,
    read_csv,
    transaction_base_key,
    unique_destination,
)
from .reports import (
    category_display,
    category_entries,
    generate_html,
    generate_xlsx,
    inflow_amount,
    month_summary,
    person_display,
    write_month_csv,
)
from .storage import (
    load_decision_shards,
    load_json,
    load_settings,
    load_year_shards,
    money,
    parse_date,
    save_decision_shards,
    save_json,
    save_year_shards,
    sha256_file,
    stable_hash,
)
from .workspaces import (
    FORMAT_VERSION,
    LEGACY_FOLDER_NAMES,
    MARKER_NAME,
    PROFILE_FOLDERS,
    LegacyWorkspaceError,
    has_legacy_workspace,
    initialize_profile,
    initialize_storage,
    migrate_legacy_workspace,
    profile_names,
)

__all__ = [name for name in globals() if not name.startswith("_")]
