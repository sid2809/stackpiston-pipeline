# StackPiston pipeline

Turns a Google Sheet row into a validated StackPiston review on WordPress.
Full plan: see the "StackPiston Pipeline: Build Plan & Phases" doc.

## Status
- [x] Phase 2: validator (schema + exact port of the theme's checks + link rules) — tests pass
- [~] Phase 0: setup — checks: `python -m app.check_wp`, `python -m app.check_sheet` (passed), `python -m app.check_llm`
- [x] Phase 1: WordPress client — live check passed on staging
- [~] Phase 3: read JV sources — check: `python -m app.check_extract <JV URL> [sales URL]`
- [x] Theme 1.1.0 support: bundles, coupons, vendor bonuses (validator matches theme exactly)
- [~] Phase 4: write review — dry run: `python -m app.check_write <JV URL> [sales URL]`
- [~] Phase 5: run from the sheet — `python -m app.main poll` (all rows with Status = Run) or `python -m app.main run --row N`
- [ ] Phases 6–8

Email notifications are deferred: while SMTP settings are empty, results appear only in the sheet.

## Run the tests
    pip install -r requirements-dev.txt
    python -m pytest -q
