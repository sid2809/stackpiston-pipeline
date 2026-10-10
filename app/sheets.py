"""Google Sheet access. Columns are always found by header name, never by position."""
from __future__ import annotations

import gspread

from .config import SheetConfig

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TABS = {
    "Launches": ["Status", "Product name", "Sales page URL", "FE affiliate link",
                 "OTO links", "Cart open", "Cart close", "Timezone", "Mode", "Publish at", "Days tested",
                 "Testing notes", "Video URL", "Rewrite", "WP post ID", "Slug", "Preview link", "Last run",
                 "Messages", "Post-launch done", "Method note used"],
    "Bonuses": ["Type", "Title", "Description", "Value ($)", "Active"],
    "Method notes": ["Note"],
    "Logs": ["Timestamp (IST)", "Row", "Slug", "Step", "Result", "Details"],
}
OPTIONAL = {"Launches": ["JV page URL", "JV doc URL", "Bundle links", "Notes for AI", "Main image URL",
                         "Bonus link", "Rebuild bonus"]}
JV_COLUMNS = ["JV page URL", "JV doc URL", "JV doc / JV page URL"]  # at least one must exist
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
        if tab == "Launches" and not any(norm(c) in have for c in JV_COLUMNS):
            problems.append("Tab 'Launches' needs a 'JV page URL' or 'JV doc URL' column.")
    return problems


def append_log(sh: gspread.Spreadsheet, row: list) -> None:
    row = [str(x)[:MAX_CELL] if x is not None else "" for x in row]
    sh.worksheet("Logs").append_row(row, value_input_option="RAW")


# ---------------------------------------------------------------- Phase 5: row-level reading and writing

from dataclasses import dataclass  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from gspread.utils import DateTimeOption, ValueRenderOption, rowcol_to_a1  # noqa: E402


@dataclass
class Row:
    number: int            # sheet row number (header is row 1)
    values: dict           # normalized header -> raw cell value

    def get(self, header: str, default=""):
        v = self.values.get(norm(header), default)
        return default if v is None else v

    def text(self, header: str) -> str:
        return str(self.get(header, "")).strip()

    def lines(self, header: str) -> list[str]:
        return [x.strip() for x in str(self.get(header, "")).replace("\r", "\n").split("\n") if x.strip()]

    def links(self, header: str) -> list[str]:
        """URLs in a cell, one per line, ignoring labels like 'OTO 1:' or 'Bundle -'."""
        import re
        return re.findall(r"https?://[^\s,;]+", str(self.get(header, "")))

    def checked(self, header: str) -> bool:
        v = self.get(header, False)
        return v is True or str(v).strip().upper() in ("TRUE", "YES", "Y", "1")


def _raw_rows(ws) -> list[list]:
    """Raw cell values: dates come back as serial numbers, so the sheet's locale can't confuse them."""
    return ws.get_values(value_render_option=ValueRenderOption.unformatted,
                         date_time_render_option=DateTimeOption.serial_number)


class SheetIO:
    def __init__(self, sh):
        self.sh = sh
        self.ws = sh.worksheet("Launches")
        self._headers: list[str] = []

    def launches(self) -> list[Row]:
        rows = _raw_rows(self.ws)
        if not rows:
            return []
        self._headers = [norm(h) for h in rows[0]]
        out = []
        for i, r in enumerate(rows[1:], start=2):
            vals = {h: (r[j] if j < len(r) else "") for j, h in enumerate(self._headers) if h}
            if any(str(v).strip() for v in vals.values()):
                out.append(Row(i, vals))
        return out

    def bonuses(self) -> list[dict]:
        rows = _raw_rows(self.sh.worksheet("Bonuses"))
        if not rows:
            return []
        hdr = [norm(h) for h in rows[0]]
        out = []
        for r in rows[1:]:
            d = {h: (r[j] if j < len(r) else "") for j, h in enumerate(hdr)}
            if str(d.get("active", "")).strip().upper() == "Y" and str(d.get("title", "")).strip():
                try:
                    value = float(d.get("value ($)") or 0)
                except (TypeError, ValueError):
                    value = 0.0
                out.append({"type": str(d.get("type", ""))[:14], "title": str(d["title"]).strip()[:40],
                            "description": str(d.get("description", "")).strip()[:140], "value": value})
        return out

    def method_notes(self) -> list[str]:
        rows = _raw_rows(self.sh.worksheet("Method notes"))
        return [str(r[0]).strip() for r in rows[1:] if r and str(r[0]).strip()]

    def update(self, row: int, values: dict) -> None:
        """Write several cells of one row in a single API call. Unknown headers are skipped."""
        if not self._headers:
            self.launches()
        data = []
        for header, value in values.items():
            h = norm(header)
            if h in self._headers:
                col = self._headers.index(h) + 1
                v = value if isinstance(value, (bool, int, float)) else str(value)[:MAX_CELL]
                data.append({"range": rowcol_to_a1(row, col), "values": [[v]]})
        if data:
            self.ws.batch_update(data, raw=True)

    def log(self, row: list) -> None:
        append_log(self.sh, row)


# ---------------------------------------------------------------- sheet date/time values

def sheet_datetime(value) -> datetime | None:
    """A sheet cell -> naive local datetime. Accepts real dates (serial numbers) or text like
    '2026-10-05 11:00'. Text in dd/mm or mm/dd form is refused because it is ambiguous."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Round to the minute: 11:00 is stored as 0.4583333…, which would otherwise read as 10:59:59.99.
        minutes = round(float(value) * 24 * 60)
        return datetime(1899, 12, 30) + timedelta(minutes=minutes)
    s = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"'{value}' is not a date I can read. Use the date picker or type it as 2026-10-05 11:00.")


def ist_now() -> str:
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M")
