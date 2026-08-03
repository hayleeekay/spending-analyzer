# Contributing

This is a small project, so the contribution process is simple:

1. Create a branch from `main`.
2. Add or update tests for behavior changes.
3. Run `.venv/bin/python -m unittest discover -s tests -v`.
4. Confirm no files under `spending-data/` are staged.
5. Explain the user-visible behavior and any CSV assumptions in the pull request.

Please use fictional transaction data in tests and documentation. Do not submit real statements, account numbers, names, or locally generated reports.
