#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ ! -x "$project_dir/.venv/bin/python" ]; then
    echo "First-time setup is required. Run:"
    echo "  cd $project_dir"
    echo "  python3 -m venv .venv"
    echo "  .venv/bin/python -m pip install -r requirements.txt"
    exit 1
fi

exec "$project_dir/.venv/bin/python" "$project_dir/spending_analyzer.py" \
    --data-dir "$project_dir/spending-data" "$@"
