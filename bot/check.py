"""Checks every credential and permission the bot needs, and says how to fix
what's missing. Run with: python -m bot.check"""

import sys

import anthropic
import gspread
import requests

from .config import load_config
from .docs import DocFetcher, parse_file_ids
from .google_api import http_session, load_credentials, sheets_client
from .sheets import parse_rows
from .store import Store

failures = 0


def ok(text: str) -> None:
    print(f"  [ok]  {text}")


def fix(text: str) -> None:
    global failures
    failures += 1
    print(f"  [fix] {text}")


def _google_hint(error_text: str, api: str) -> str:
    if "has not been used" in error_text or "is disabled" in error_text:
        return f"Enable the {api} in your Google Cloud project (APIs & Services > Library)."
    return error_text[:300]


def main() -> None:
    cfg = load_config()
    print("\nChecking your setup...\n")

    response = requests.get(f"https://api.telegram.org/bot{cfg.telegram_token}/getMe", timeout=15).json()
    if response.get("ok"):
        ok(f"Telegram bot: @{response['result']['username']}")
    else:
        fix("TELEGRAM_BOT_TOKEN was rejected by Telegram. Copy it again from @BotFather.")

    try:
        creds = load_credentials(cfg)
        email = creds.service_account_email
        ok(f"Google service account: {email}")
    except Exception as e:
        fix(f"Couldn't load the Google service account key: {e}")
        return _finish()

    book = None
    try:
        book = sheets_client(creds).open_by_key(cfg.sheet_id)
        ok(f"Google Sheet: {book.title}")
    except gspread.SpreadsheetNotFound:
        fix(f"Can't open the Sheet. Share it with {email} as Editor, and check GOOGLE_SHEET_ID.")
    except gspread.exceptions.APIError as e:
        fix(_google_hint(str(e), "Google Sheets API"))

    if book is not None:
        links = []
        tabs = {ws.title for ws in book.worksheets()}
        if "Settings" in tabs:
            settings = {r.get("key"): r.get("value") for r in parse_rows(book.worksheet("Settings").get_all_values())}
            links += [("general FAQ", f) for f in parse_file_ids(settings.get("faq_docs", ""))]
        if "Campaigns" in tabs:
            for row in parse_rows(book.worksheet("Campaigns").get_all_values()):
                if row.get("status").lower() not in ("sent", "cancelled"):
                    links += [(f"campaign {row.get('id')}", f) for f in parse_file_ids(row.get("brief_docs"))]
        docs = DocFetcher(http_session(creds))
        for label, file_id in links:
            try:
                doc = docs._fetch(file_id)
                ok(f"Doc for {label}: {doc.name}")
            except requests.HTTPError as e:
                fix(f"Doc for {label}: " + _google_hint(e.response.text, "Google Drive API"))
            except ValueError as e:
                fix(f"Doc for {label}: {e}")
        if not links:
            ok("No docs linked yet (add them in Settings > faq_docs and Campaigns > brief_docs)")

    if cfg.openrouter_api_key:
        _check_openrouter(cfg)
    else:
        try:
            anthropic.Anthropic().models.retrieve(cfg.claude_model)
            ok(f"Anthropic API key works, model {cfg.claude_model} available")
        except anthropic.AuthenticationError:
            fix("ANTHROPIC_API_KEY was rejected. Create a key at console.anthropic.com.")
        except anthropic.NotFoundError:
            fix(f"Model {cfg.claude_model} not found. Check CLAUDE_MODEL.")
        except anthropic.APIError as e:
            fix(f"Anthropic API error: {e}")

    group = cfg.admin_group_id or Store(cfg.db_path).get_value("admin_group_id")
    if group:
        _check_group(cfg, group)
    else:
        fix("No inbox group linked yet. Start the bot and send the /setup command from its log in your group.")

    _finish()


def _check_group(cfg, group) -> None:
    api = f"https://api.telegram.org/bot{cfg.telegram_token}/"
    chat = requests.get(api + "getChat", params={"chat_id": group}, timeout=15).json()
    if not chat.get("ok"):
        fix(f"The bot can't see inbox group {group}. Add the bot to the group.")
        return
    if not chat["result"].get("is_forum"):
        fix(f"Turn on Topics in the settings of '{chat['result'].get('title')}'.")
    bot_id = cfg.telegram_token.split(":")[0]
    member = requests.get(api + "getChatMember", params={"chat_id": group, "user_id": bot_id}, timeout=15).json()
    result = member.get("result", {})
    if result.get("status") != "administrator" or not result.get("can_manage_topics"):
        fix("Make the bot an admin of the inbox group with the 'Manage topics' permission.")
    elif chat["result"].get("is_forum"):
        ok(f"Inbox group: {chat['result'].get('title')}")


def _check_openrouter(cfg) -> None:
    key = requests.get(
        "https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {cfg.openrouter_api_key}"}, timeout=15
    )
    if key.status_code != 200:
        fix("OPENROUTER_API_KEY was rejected. Create a key at openrouter.ai/keys.")
        return
    models = requests.get("https://openrouter.ai/api/v1/models", timeout=15).json()["data"]
    model = next((m for m in models if m["id"] == cfg.claude_model), None)
    if model is None:
        fix(f"OpenRouter has no model called {cfg.claude_model}. Check CLAUDE_MODEL (e.g. anthropic/claude-opus-5.5).")
    elif not {"structured_outputs", "response_format"} & set(model.get("supported_parameters", [])):
        fix(f"{cfg.claude_model} doesn't support structured outputs on OpenRouter. Pick another model.")
    else:
        ok(f"OpenRouter key works, model {cfg.claude_model} available")


def _finish() -> None:
    print("\nAll good. Start the bot with: python -m bot\n" if not failures else f"\n{failures} thing(s) to fix.\n")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
