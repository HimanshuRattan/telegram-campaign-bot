"""The Google Sheet is the operator's control panel. Columns are looked up by
header name, so the operator can reorder or add columns freely."""

import asyncio
from dataclasses import dataclass

import gspread
from gspread.utils import rowcol_to_a1

TABS: dict[str, list[str]] = {
    "Creators": ["id", "name", "tags", "status", "chat_id", "telegram_username", "invite_code", "invite_link", "joined_at", "notes"],
    "Campaigns": ["id", "name", "message", "brief_docs", "target", "send_at", "deadline", "status", "notes"],
    "Assignments": ["campaign_id", "creator_id", "creator_name", "status", "sent_at", "updated_at", "post_link"],
    "Settings": ["key", "value", "description"],
}

DEFAULT_SETTINGS = [
    ("brand_name", "Our team", "Name the bot uses when introducing itself to creators"),
    ("faq_docs", "", "Google Doc or PDF links (one per line) with info that applies to every campaign"),
    ("admin_signature", "", "If set, your replies are tagged with (this name) when relayed to creators"),
    ("auto_resume_hours", "12", "After you reply in a creator's topic, the bot stays quiet with them for this many hours"),
]


@dataclass
class Row:
    row: int  # 1-based row number in the sheet
    data: dict[str, str]

    def get(self, key: str) -> str:
        return (self.data.get(key) or "").strip()


def parse_rows(values: list[list]) -> list[Row]:
    if not values:
        return []
    headers = [str(h).strip().lower() for h in values[0]]
    rows = []
    for number, raw in enumerate(values[1:], start=2):
        raw = ["" if cell is None else str(cell) for cell in raw]
        if not any(cell.strip() for cell in raw):
            continue
        data = {h: (raw[i] if i < len(raw) else "") for i, h in enumerate(headers) if h}
        rows.append(Row(number, data))
    return rows


class Sheet:
    def __init__(self, client: gspread.Client, sheet_id: str):
        self._client = client
        self._sheet_id = sheet_id
        self._book: gspread.Spreadsheet | None = None
        self._worksheets: dict[str, gspread.Worksheet] = {}
        # gspread is synchronous; serialize calls and run them off the event loop.
        self._lock = asyncio.Lock()
        self.creators: list[Row] = []
        self.campaigns: list[Row] = []
        self.settings: dict[str, str] = {}

    def _open(self) -> gspread.Spreadsheet:
        if self._book is None:
            self._book = self._client.open_by_key(self._sheet_id)
        return self._book

    def _ws(self, tab: str) -> gspread.Worksheet:
        if tab not in self._worksheets:
            self._worksheets[tab] = self._open().worksheet(tab)
        return self._worksheets[tab]

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # Reads

    async def refresh(self) -> None:
        def read():
            # Unformatted so date cells come back as serial numbers regardless of the
            # sheet's locale and display format (see timeutil.parse_sheet_datetime).
            result = self._open().values_batch_get(
                ["Creators", "Campaigns", "Settings"],
                params={"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER"},
            )
            return [vr.get("values", []) for vr in result["valueRanges"]]

        creators, campaigns, settings = await self._run(read)
        self.creators = [r for r in parse_rows(creators) if r.get("id")]
        self.campaigns = [r for r in parse_rows(campaigns) if r.get("id")]
        self.settings = {r.get("key"): r.get("value") for r in parse_rows(settings) if r.get("key")}

    def setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key) or default

    def creator(self, creator_id: str) -> Row | None:
        return next((c for c in self.creators if c.get("id") == creator_id), None)

    def creator_by_chat(self, chat_id: int) -> Row | None:
        return next((c for c in self.creators if c.get("chat_id") == str(chat_id)), None)

    def creator_by_invite(self, code: str) -> Row | None:
        return next((c for c in self.creators if code and c.get("invite_code") == code), None)

    def campaign(self, campaign_id: str) -> Row | None:
        return next((c for c in self.campaigns if c.get("id") == campaign_id), None)

    # Writes

    async def update_rows(self, tab: str, updates: list[tuple[dict[str, str], dict[str, str]]]) -> int:
        """Apply (match, changes) pairs. Rows are located by the match columns on a
        fresh read, so edits the operator made since the last refresh are safe.
        Returns how many rows were updated."""

        def write():
            ws = self._ws(tab)
            values = ws.get_all_values()
            if not values:
                return 0
            headers = [h.strip().lower() for h in values[0]]
            rows = parse_rows(values)
            cells = []
            matched = 0
            for match, changes in updates:
                row = next((r for r in rows if all(r.get(k) == v for k, v in match.items())), None)
                if row is None:
                    continue
                matched += 1
                for key, value in changes.items():
                    if key in headers:
                        cells.append({"range": rowcol_to_a1(row.row, headers.index(key) + 1), "values": [[value]]})
                        row.data[key] = value
            if cells:
                ws.batch_update(cells, value_input_option="RAW")
            return matched

        return await self._run(write)

    async def update_row(self, tab: str, match: dict[str, str], changes: dict[str, str]) -> bool:
        return await self.update_rows(tab, [(match, changes)]) > 0

    async def append_rows(self, tab: str, rows: list[dict[str, str]]) -> None:
        if not rows:
            return

        def write():
            ws = self._ws(tab)
            headers = [h.strip().lower() for h in ws.row_values(1)]
            ws.append_rows([[row.get(h, "") for h in headers] for row in rows], value_input_option="RAW")

        await self._run(write)

    async def ensure_tabs(self) -> list[str]:
        """Create missing tabs with headers and default settings. Returns created tab names."""

        def write():
            book = self._open()
            existing = {ws.title for ws in book.worksheets()}
            created = []
            for tab, headers in TABS.items():
                if tab in existing:
                    continue
                ws = book.add_worksheet(title=tab, rows=200, cols=max(len(headers), 10))
                ws.append_row(headers, value_input_option="RAW")
                ws.freeze(rows=1)
                if tab == "Settings":
                    ws.append_rows([list(s) for s in DEFAULT_SETTINGS], value_input_option="RAW")
                created.append(tab)
            return created

        return await self._run(write)
