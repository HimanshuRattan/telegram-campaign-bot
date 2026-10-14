"""Fetches knowledge-base files (Google Docs, PDFs, text files) from Google
Drive, and appends answered questions back to a Google Doc."""

import asyncio
import re
import time
from dataclasses import dataclass

from google.auth.transport.requests import AuthorizedSession

DRIVE = "https://www.googleapis.com/drive/v3/files"
DOCS = "https://docs.googleapis.com/v1/documents"
GOOGLE_DOC = "application/vnd.google-apps.document"

_ID_PATTERNS = [
    re.compile(r"/d/([a-zA-Z0-9_-]{20,})"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]{20,})"),
    re.compile(r"^([a-zA-Z0-9_-]{25,})$"),
]


def parse_file_ids(cell: str) -> list[str]:
    """Extract Drive file IDs from a cell holding links separated by newlines, commas or spaces."""
    ids = []
    for part in re.split(r"[\s,]+", cell or ""):
        for pattern in _ID_PATTERNS:
            match = pattern.search(part)
            if match:
                if match.group(1) not in ids:
                    ids.append(match.group(1))
                break
    return ids


@dataclass
class Doc:
    file_id: str
    name: str
    is_google_doc: bool
    text: str | None = None  # Google Docs and text files
    pdf: bytes | None = None  # PDFs, sent to Claude as document blocks


class DocFetcher:
    def __init__(self, session: AuthorizedSession, ttl_seconds: int = 120):
        self._session = session
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, Doc]] = {}

    async def fetch(self, file_id: str) -> Doc:
        cached = self._cache.get(file_id)
        if cached and time.time() - cached[0] < self._ttl:
            return cached[1]
        doc = await asyncio.to_thread(self._fetch, file_id)
        self._cache[file_id] = (time.time(), doc)
        return doc

    async def fetch_all(self, cell: str) -> list[Doc]:
        return [await self.fetch(file_id) for file_id in parse_file_ids(cell)]

    def _fetch(self, file_id: str) -> Doc:
        meta = self._get(f"{DRIVE}/{file_id}", params={"fields": "name,mimeType", "supportsAllDrives": "true"}).json()
        name, mime = meta["name"], meta["mimeType"]
        if mime == GOOGLE_DOC:
            text = self._get(f"{DRIVE}/{file_id}/export", params={"mimeType": "text/plain"}).content.decode("utf-8-sig")
            return Doc(file_id, name, is_google_doc=True, text=text)
        content = self._get(f"{DRIVE}/{file_id}", params={"alt": "media", "supportsAllDrives": "true"}).content
        if mime == "application/pdf":
            return Doc(file_id, name, is_google_doc=False, pdf=content)
        if mime.startswith("text/"):
            return Doc(file_id, name, is_google_doc=False, text=content.decode("utf-8", errors="replace"))
        raise ValueError(f"'{name}' is a {mime} file. Use a Google Doc, PDF or text file.")

    def _get(self, url: str, params: dict):
        response = self._session.get(url, params=params, timeout=30)
        if response.status_code == 404:
            raise ValueError(
                f"Drive file {url.split('/files/')[1].split('/')[0]} not found. "
                "Is it shared with the bot's service account email?"
            )
        response.raise_for_status()
        return response

    async def append_qa(self, file_id: str, question: str, answer: str) -> None:
        """Append a Q&A pair to the end of a Google Doc."""

        def write():
            text = f"\n\nQ: {question.strip()}\nA: {answer.strip()}\n"
            response = self._session.post(
                f"{DOCS}/{file_id}:batchUpdate",
                json={"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text}}]},
                timeout=30,
            )
            response.raise_for_status()

        await asyncio.to_thread(write)
        self._cache.pop(file_id, None)
