"""Google Sheet access. Columns are always found by header name, never by position."""
from __future__ import annotations

import gspread

from .config import SheetConfig

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TABS = {
    "Launches": ["Status", "Product name", "JV doc / JV page URL", "Sales page URL", "FE affiliate link",
                 "OTO links", "Cart open", "Cart close", "Timezone", "Mode", "Publish at", "Days tested",
                 "Testing notes", "Video URL", "Rewrite", "WP post ID", "Slug", "Preview link", "Last run",
                 "Messages", "Post-launch done", "Method note used"],
    "Bonuses": ["Type", "Title", "Description", "Value ($)", "Active"],
    "Method notes": ["Note"],
    "Logs": ["Timestamp (IST)", "Row", "Slug", "Step", "Result", "Details"],
}
MAX_CELL = 49_000  # Google Sheets cell limit is 50,000 characters


def norm(header: str) -> str:
    """Header match ignores capitals and extra spaces: 'REWRITE ' == 'Rewrite'."""
    return " ".join(str(header).split()).lower()


def open_sheet(cfg: SheetConfig) -> gspread.Spreadsheet:
    gc = gspread.service_account_from_dict(cfg.service_account, scopes=SCOPES)
    return gc.open_by_key(cfg.sheet_id)


def missing_headers(sh: gspread.Spreadsheet) -> list[str]:
    problems = []
    names = {ws.title: ws for ws in sh.worksheets()}
    for tab, needed in TABS.items():
        ws = names.get(tab)
        if ws is None:
            problems.append(f"Tab '{tab}' is missing.")
            continue
        have = {norm(h) for h in ws.row_values(1)}
        for h in needed:
            if norm(h) not in have:
                problems.append(f"Tab '{tab}' has no column '{h}'.")
    return problems


def append_log(sh: gspread.Spreadsheet, row: list) -> None:
    row = [str(x)[:MAX_CELL] if x is not None else "" for x in row]
    sh.worksheet("Logs").append_row(row, value_input_option="RAW")
