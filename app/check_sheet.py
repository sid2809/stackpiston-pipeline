"""Phase 0 checkpoint: `python -m app.check_sheet` — can the pipeline read and write the sheet?"""
from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread

from .config import ConfigError, SheetConfig
from .sheets import OPTIONAL, append_log, missing_headers, norm, open_sheet


def main() -> int:
    try:
        cfg = SheetConfig.from_env()
    except ConfigError as e:
        print(f"FAIL  {e}")
        return 1
    print(f"OK  key loaded for {cfg.service_account.get('client_email')}")
    try:
        sh = open_sheet(cfg)
    except ValueError:
        print("FAIL  the key inside GOOGLE_SERVICE_ACCOUNT_JSON_B64 is damaged. Re-run the base64 step and paste again.")
        return 1
    except gspread.exceptions.SpreadsheetNotFound:
        print("FAIL  sheet not found. Check SHEET_ID, and share the sheet with the service account email as Editor.")
        return 1
    except gspread.exceptions.APIError as e:
        msg = str(e)
        if "has not been used" in msg or "disabled" in msg:
            print("FAIL  Google Sheets API is not enabled in the Google Cloud project.")
        elif "PERMISSION_DENIED" in msg or "403" in msg:
            print("FAIL  no access. Share the sheet with the service account email as Editor.")
        else:
            print(f"FAIL  Google API error: {msg[:300]}")
        return 1
    print(f"OK  opened '{sh.title}'")
    problems = missing_headers(sh)
    for p in problems:
        print(f"FAIL  {p}")
    if problems:
        return 1
    print("OK  all 4 tabs and required columns found")
    for tab, cols in OPTIONAL.items():
        have = {norm(h) for h in sh.worksheet(tab).row_values(1)}
        for c in cols:
            if norm(c) not in have:
                print(f"NOTE  optional column '{c}' not in tab '{tab}' (needed only for launches with bundles)")
    now = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M")
    try:
        append_log(sh, [now, "", "", "connection test", "OK", "Pipeline can write to this sheet."])
    except gspread.exceptions.APIError as e:
        print(f"FAIL  could not write (is the service account an Editor?): {str(e)[:200]}")
        return 1
    print("OK  wrote a test line to the Logs tab")
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
