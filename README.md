# StackPiston pipeline

Turns a Google Sheet row into a validated StackPiston review on WordPress.
Full plan: see the "StackPiston Pipeline: Build Plan & Phases" doc.

## Status
- [x] Phase 2: validator (schema + exact port of the theme's checks + link rules) — tests pass
- [ ] Phase 0: setup
- [~] Phase 1: WordPress client (code + unit tests done; live check pending: `python -m app.check_wp`)
- [ ] Phases 3–8

## Run the tests
    pip install -r requirements.txt
    python -m pytest -q
