"""Command-line entry point."""

import argparse
from pathlib import Path
import sys

from .application import process
from .constants import __version__

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
    parser.add_argument("--non-interactive", action="store_true", help="Do not ask for categories; leave new expenses uncategorized")
    args = parser.parse_args()
    try:
        return process(args.data_dir.expanduser().resolve(), args.non_interactive)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Files already archived were imported safely; run again to continue.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
