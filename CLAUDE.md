# Creator campaign bot

Telegram bot (python-telegram-bot 22, long polling) that sends creator campaigns from a Google Sheet, answers creator questions with Claude from Google Docs/PDFs, and mirrors every conversation into a forum-style Telegram group (one topic per creator) where the team can reply. Operator docs are in README.md.

## Setting this up for a new user

When someone asks you to set this bot up, follow this playbook. Do every step you can yourself; only ask the human for the things marked (human). Never print secrets back to them.

1. **Prerequisites.**
   - Check `python3 --version` is 3.10 or newer. On a Mac, if it's too old, run `brew install python@3.12`.
   - Check `gcloud` exists. On a Mac, if it doesn't, run `brew install --cask gcloud-cli`.
2. **Google Cloud (automated).**
   - (human) Ask them to run `! gcloud auth login`, signing in with the Google account that will own the Sheet. Use a personal Gmail; company Workspace accounts often block service-account keys.
   - Run `./google-setup.sh`. It creates a project, enables the Sheets, Drive and Docs APIs, creates a service account, and saves `service-account.json`.
   - Tell them the service account email it prints.
3. **Credentials.**
   - (human) A Telegram bot token: in @BotFather, send `/newbot`. Give it a neutral name, not a brand, and add a description with `/setdescription`.
   - (human) An OpenRouter key from openrouter.ai/keys, or an Anthropic key.
   - Copy `.env.example` to `.env` and write the values in with an editor. Don't run `./setup.sh` before `.env` has the token, the LLM key and the Sheet ID: it prompts interactively for missing values and will hang.
4. **Sheet and docs.**
   - (human) Ask them to create:
     - a blank Google Sheet
     - a "General FAQ" Google Doc (payment terms, posting rules, anything that applies to every campaign)
     - optionally, a brief doc per campaign
   - (human) They share all of these with the service account email as Editor, untick "Notify people", and paste the links to you.
   - Put the Sheet ID (between `/d/` and `/edit`) in `GOOGLE_SHEET_ID`.
5. **Install and check.** Run `./setup.sh`. With `.env` complete it installs dependencies and runs `python -m bot.check`. Fix anything marked `[fix]`. "No inbox group linked yet" is expected at this point.
6. **Telegram group.** (human) They create a group, turn on Topics, and add the bot as an admin with Manage Topics.
7. **Start the bot.**
   - Run `./run.sh` in the background.
   - Read its output for the `/setup <code>` line, and have them send it in the group.
   - On first start the bot creates the Creators, Campaigns, Assignments and Settings tabs. After that, put the FAQ doc link in Settings > `faq_docs`. You can write it with `bot.sheets.Sheet.update_row("Settings", {"key": "faq_docs"}, {"value": link})`.
8. **First creator.**
   - Add a row to the Creators tab (`id`, `name`, `tags`). The bot fills `invite_code` within a minute.
   - The creator searches for the bot's username in Telegram, taps Start, and sends `/start <invite_code>`.

**Telegram caution.** During development, two brand-new bots on one account were frozen within minutes, and Telegram reset that account's sessions. Each time, the owner had just sent the new bot's `t.me/...?start=` link to someone in a DM. To reduce the risk:
- Don't share clickable bot links from the owner's account right after creating a bot. Give creators the bot's username and their code as plain text instead.
- Start with one or two creators.
- Turn on Two-Step Verification on the owner's account.

## Layout

- `bot/main.py`: wiring. Handlers, the 60s `campaigns.tick` job, bot commands.
- `bot/campaigns.py`: campaign lifecycle (`ready -> awaiting_confirm -> scheduled -> sending -> sent`), targeting, message rendering, preview/confirm buttons, admin commands.
- `bot/creators.py`: creator DMs. `/start <invite_code>`, `/stop`, campaign buttons, and Q&A (`on_creator_message` -> `llm.decide` -> `_act`).
- `bot/inbox.py`: the team group. Topic creation and mirroring, relaying team messages to creators, auto-pause, `/pause` `/resume`, the "save this answer" flow.
- `bot/llm.py`: one structured-output Claude call per creator message. Returns an action: reply, escalate, accepted, declined, post_link or none.
- `bot/sheets.py`: gspread wrapper. Columns are matched by header name; writes locate rows by key columns on a fresh read.
- `bot/docs.py`: Drive/Docs fetch with a 2 minute cache, plus appending Q&A to a Doc.
- `bot/check.py`: `python -m bot.check`, which validates every credential and permission and prints how to fix each failure. Keep it in sync when adding a new external dependency.
- `bot/store.py`: SQLite. Topic mapping, pauses, chat history, escalations, recipient snapshots, and the authoritative assignment state (the Assignments tab is a write-through view).

## Conventions

- **The Sheet is the operator's UI.** Anything the operator should change goes in the Sheet (Settings tab), not code or env. Env is for secrets and IDs only.
- **Inbox mirroring never breaks the creator flow.** `inbox.note` swallows and logs errors.
- **Sending is idempotent.** `send_campaign` skips creators who already have an assignment, so a crash mid-send can resume by re-running. `campaigns._tick_lock` prevents overlapping ticks.
- **Sheet reads are unformatted** (`UNFORMATTED_VALUE`, `SERIAL_NUMBER`), so dates arrive as serial numbers. Always go through `timeutil.parse_sheet_datetime` / `display_value`.
- **Callback data prefixes:**
  - `cr:` creator campaign buttons
  - `cp:` campaign preview buttons
  - `lr:` save-answer buttons
- **Claude calls** use `client.beta.messages.create` with `fallbacks="default"` (beta `server-side-fallback-2026-07-01`) and a JSON schema in `output_config.format`. The model and effort come from `CLAUDE_MODEL` / `CLAUDE_EFFORT`. Any stop reason other than `end_turn`, or any exception, escalates to a human.

## Testing

```bash
.venv/bin/python -m pytest
```

`tests/test_flows.py` drives the real handlers with fake bot/sheet/LLM objects. Add a flow test there for any new handler behavior.

## Moving to serverless (Vercel / Cloudflare)

The current design assumes a long-running process. A port would need:

1. Webhook mode instead of `run_polling`.
2. An external every-minute trigger instead of the job queue.
3. A hosted database instead of the local SQLite file.
4. Idempotent update handling, because Telegram retries webhooks that respond slowly and Claude calls can take several seconds.

On Cloudflare Workers this effectively means a TypeScript rewrite: python-telegram-bot and gspread won't run there.
