"""Environment configuration. Secrets and IDs live here; anything the operator
changes day to day lives in the Google Sheet instead."""

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    telegram_token: str
    admin_group_id: int | None
    sheet_id: str
    google_credentials_file: str | None
    google_credentials_json: str | None
    timezone: ZoneInfo
    db_path: str
    claude_model: str
    claude_effort: str
    openrouter_api_key: str | None


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable {name}. See .env.example.")
    return value


def load_config() -> Config:
    load_dotenv()
    group = os.environ.get("ADMIN_GROUP_ID", "").strip()
    creds_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip() or None
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip() or None
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip() or None
    if not openrouter_key and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise SystemExit("Set OPENROUTER_API_KEY or ANTHROPIC_API_KEY in .env.")
    if not creds_file and not creds_json:
        raise SystemExit(
            "Set GOOGLE_SERVICE_ACCOUNT_FILE (path to the key file) or "
            "GOOGLE_SERVICE_ACCOUNT_JSON (the key file contents)."
        )
    return Config(
        telegram_token=_required("TELEGRAM_BOT_TOKEN"),
        admin_group_id=int(group) if group else None,
        sheet_id=_required("GOOGLE_SHEET_ID"),
        google_credentials_file=creds_file,
        google_credentials_json=creds_json,
        timezone=ZoneInfo(os.environ.get("TIMEZONE", "Asia/Kolkata")),
        db_path=os.environ.get("DB_PATH", "data/bot.db"),
        # OpenRouter names models "anthropic/claude-opus-5.5"; the Anthropic API uses "claude-opus-5-5".
        claude_model=os.environ.get("CLAUDE_MODEL") or ("anthropic/claude-opus-5.5" if openrouter_key else "claude-opus-5-5"),
        claude_effort=os.environ.get("CLAUDE_EFFORT", "low"),
        openrouter_api_key=openrouter_key,
    )
