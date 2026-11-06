"""Command-line entry point."""

import argparse
from pathlib import Path
import re
import sys

from .application import process
from .constants import __version__


PROFILE_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
RESERVED_PROFILE_NAMES = {"data", "inbox", "processed", "profiles", "reports", "rules"}


def profile_name(value: str) -> str:
    name = value.strip().lower()
    if not PROFILE_NAME_PATTERN.fullmatch(name):
        raise argparse.ArgumentTypeError(
            "profile names use letters, numbers, hyphens, or underscores (example: alex)"
        )
    if name in RESERVED_PROFILE_NAMES:
        raise argparse.ArgumentTypeError(f"{name!r} is reserved for spending-analyzer data")
    return name


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="spending-analyzer",
        description="Import local bank CSVs and generate monthly spending reports.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--data-dir", type=Path, default=Path.cwd() / "spending-data",
        help="Data directory (default: spending-data in the current directory)",
    )
    parser.add_argument(
        "--profile", type=profile_name, metavar="NAME",
        help="Use an isolated NAME subfolder inside the data directory",
    )
    parser.add_argument("--non-interactive", action="store_true", help="Do not ask for categories; leave new expenses uncategorized")
    args = parser.parse_args()
    try:
        data_dir = args.data_dir.expanduser()
        if args.profile:
            data_dir /= args.profile
        return process(data_dir.resolve(), args.non_interactive, profile_name=args.profile)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Files already archived were imported safely; run again to continue.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
