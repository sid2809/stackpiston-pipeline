# StackPiston pipeline

Turns a Google Sheet row into a validated StackPiston review on WordPress.
Full plan: see the "StackPiston Pipeline: Build Plan & Phases" doc.

## Status
- [x] Phase 2: validator (schema + exact port of the theme's checks + link rules) — tests pass
- [~] Phase 0: setup — sheet check: `python -m app.check_sheet`
- [x] Phase 1: WordPress client — live check passed on staging
- [ ] Phases 3–8

## Run the tests
    pip install -r requirements.txt
    python -m pytest -q
