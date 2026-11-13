"""Command-line entry point."""

import argparse
from pathlib import Path
import re
import sys

from .application import process
from .constants import __version__
from .workspaces import (
    LegacyWorkspaceError,
    initialize_profile,
    initialize_storage,
    migrate_legacy_workspace,
    profile_names,
)


PROFILE_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
RESERVED_PROFILE_NAMES = {"archive", "data", "imports", "profiles", "reports", "rules"}


def profile_name(value: str) -> str:
    name = value.strip().lower()
    if not PROFILE_NAME_PATTERN.fullmatch(name):
        raise argparse.ArgumentTypeError(
            "profile names use letters, numbers, hyphens, or underscores (example: alex)"
        )
    if name in RESERVED_PROFILE_NAMES:
        raise argparse.ArgumentTypeError(f"{name!r} is reserved for spending-analyzer data")
    return name


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="spending-analyzer",
        description="Import local bank CSVs and generate monthly spending reports.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""quick start:
  spending-analyzer
      On the first interactive run, choose a profile name.

  spending-analyzer --profile alex
      Create or use a specific profile.

  spending-analyzer --profile alex --import-from /path/to/exports
      Copy new CSVs from an external folder without changing that folder.

Run "spending-analyzer help" or "spending-analyzer --help" to see this guide.
""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--storage-dir", type=Path, default=Path.cwd() / "spending-data",
        help="Folder containing all profiles (default: ./spending-data)",
    )
    parser.add_argument(
        "--data-dir", type=Path, dest="storage_dir", default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--profile", type=profile_name, metavar="NAME",
        help="Create or use NAME under the storage directory",
    )
    parser.add_argument(
        "--import-from", type=Path, metavar="FOLDER",
        help="Copy new CSVs from an external folder; originals remain untouched",
    )
    parser.add_argument(
        "--migrate-profile", type=profile_name, metavar="NAME",
        help="Convert an older root workspace into the named profile, then stop",
    )
    parser.add_argument("--non-interactive", action="store_true", help="Do not ask for categories; leave new expenses uncategorized")
    parser.add_argument("command", nargs="?", choices=["help"], help=argparse.SUPPRESS)
    args = parser.parse_args(arguments)
    if args.command == "help":
        parser.print_help()
        return 0
    try:
        storage_dir = args.storage_dir.expanduser().resolve()
        if args.migrate_profile:
            if args.profile or args.import_from:
                parser.error("--migrate-profile cannot be combined with --profile or --import-from")
            profile_dir = migrate_legacy_workspace(storage_dir, args.migrate_profile)
            print(f"Converted the older workspace into profile: {args.migrate_profile}")
            print(f"Profile folder: {profile_dir}")
            print(f"Next: run this command again to use profile {args.migrate_profile}.")
            return 0

        storage_created = initialize_storage(storage_dir)
        profiles = profile_names(storage_dir)
        selected = args.profile
        if not selected and not profiles:
            if args.non_interactive:
                raise ValueError(
                    "No profiles exist yet. Run interactively, or add --profile NAME."
                )
            selected = profile_name(input("No profiles exist yet. Choose a profile name: "))
        elif not selected and len(profiles) == 1:
            selected = profiles[0]
        elif not selected:
            raise ValueError(
                "More than one profile exists. Choose one with --profile NAME. "
                f"Available profiles: {', '.join(profiles)}"
            )

        profile_dir, profile_created = initialize_profile(storage_dir, selected)
        if storage_created:
            print(f"Initialized Spending Analyzer storage: {storage_dir}")
        if profile_created:
            print(f"Created profile: {selected}")
        print(f"Storage directory: {storage_dir}")

        external_source = None
        if args.import_from:
            external_source = args.import_from.expanduser().resolve()
            if not external_source.is_dir():
                raise ValueError(f"External import source is not a folder: {external_source}")
            if external_source == storage_dir or storage_dir in external_source.parents:
                raise ValueError(
                    "--import-from must be outside Spending Analyzer storage"
                )

        misplaced = [
            *storage_dir.glob("*.csv"),
            *(storage_dir / "profiles").glob("*.csv"),
        ]
        if misplaced:
            print(
                f"WARNING: {len(misplaced)} CSV file(s) outside a profile were not imported.\n"
                f"Move them to the Import folder: {profile_dir / 'imports'}",
                file=sys.stderr,
            )

        return process(
            profile_dir,
            args.non_interactive,
            profile_name=selected,
            external_import_source=external_source,
        )
    except LegacyWorkspaceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Completed work was saved when possible; run again to continue.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
