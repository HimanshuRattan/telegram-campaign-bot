# Creator campaign bot

A Telegram bot for running creator campaigns. You plan campaigns in a Google Sheet. The bot DMs the selected creators at the scheduled time, answers their questions from your docs, and hands anything it can't answer to you. Every conversation is mirrored into a private Telegram group, one topic per creator, and you can reply from there.

## How it works day to day

1. Write the campaign brief in a Google Doc.
2. Add a row to the **Campaigns** tab: name, message, brief doc link, target creators, send time. Set `status` to `ready`.
3. Within a minute the bot posts a preview in your inbox group. Tap **Confirm**.
4. At the send time, each creator gets the message with **I'm in / Pass / Ask a question** buttons.
5. Creator questions are answered from the brief and your general FAQ. If the answer isn't there, the creator gets "let me check with the team", and the question shows up in that creator's topic. Reply in the topic and the bot passes it on.
6. Track responses in the **Assignments** tab or with `/status <campaign id>`.

## Quick start (Mac or Linux)

You need four things:
- a Telegram bot token from @BotFather
- an OpenRouter key from openrouter.ai/keys
- a Google Sheet
- a Telegram group with Topics turned on and your bot added as an admin with Manage Topics

```bash
./google-setup.sh   # creates the bot's Google account (needs the gcloud CLI); skip if you already have a key file
./setup.sh          # installs everything, asks for your credentials, checks them
./run.sh            # starts the bot; keep the window open
```

On first start the bot prints a line like `/setup 3fa91c`. Send it in your Telegram group and you're done.

The sections below explain each step in detail.

## One-time setup

### 1. Create the bot

1. In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot` and follow the prompts.
2. Copy the token it gives you.

### 2. Create the inbox group

1. Create a new Telegram group (just you, plus any teammates).
2. In the group settings, turn on **Topics**.
3. Add your bot to the group and make it an **admin** with the **Manage topics** permission.

### 3. Set up Google

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project.
2. Enable the **Google Sheets API**, **Google Drive API** and **Google Docs API** (APIs & Services > Library).
3. Create a **service account** (IAM & Admin > Service Accounts). Open it, go to **Keys > Add key > JSON**, and save the file as `service-account.json` in this folder.
4. Create an empty Google Sheet. Share it with the service account's email (it looks like `name@project.iam.gserviceaccount.com`) as **Editor**.
5. Share your brief docs and FAQ docs with the same email. The easiest way is to put them all in one Drive folder and share the folder. Give it **Editor** access if you want the "Save this answer" button to work. **Viewer** is enough for answering questions.

### 4. Get an Anthropic API key

Create one at [console.anthropic.com](https://console.anthropic.com).

### 5. Configure, check and run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in the four values at the top
.venv/bin/python -m bot.check   # tests every credential and tells you what to fix
.venv/bin/python -m bot
```

On first start:

1. The bot creates the **Creators**, **Campaigns**, **Assignments** and **Settings** tabs in your Sheet.
2. It prints a line like `/setup 3fa91c`.
3. Send that line in your inbox group. The bot checks that Topics is on and that it has the permissions it needs, then links the group. There's nothing to restart.

## The Sheet

### Creators

| Column | Who fills it | Notes |
|---|---|---|
| id | you | Any short unique id, e.g. `c001` |
| name | you | Used in messages: "Hi Priya!" |
| tags | you | Comma separated, e.g. `fitness, 10k+, hindi` |
| status | bot | `active` once connected, `opted_out` if they send /stop |
| chat_id, telegram_username, joined_at | bot | Filled when the creator connects |
| invite_code, invite_link | bot | Send each creator **their own** link. `/invites` lists them. |
| notes | you | Free text |

A creator connects by tapping their invite link and pressing **Start**. Each link works for one Telegram account only.

### Campaigns

| Column | Notes |
|---|---|
| id | Short unique id, e.g. `nike1` |
| name | Shown to creators as `{campaign}` |
| message | What creators receive. Placeholders: `{name}`, `{campaign}`, `{deadline}`. If left empty, a default message is used. |
| brief_docs | Links to the Google Docs or PDFs the bot answers from (one per line) |
| target | `all`, `tags: fitness, 10k+` (must have every tag), `any: fitness, food` (has at least one), or ids like `c001, c004` |
| send_at | A date cell, or text like `2026-10-16 15:00`, in the `TIMEZONE` from `.env` |
| deadline | Optional, shown to creators and given to the bot as context |
| status | You set `draft` or `ready`. The bot moves it through `awaiting_confirm`, `scheduled`, `sending` and `sent` (also `cancelled` and `error`). |

### Assignments

One row per creator per campaign, written by the bot. Status is one of `sent`, `accepted`, `declined`, `posted` (with `post_link`) or `blocked`.

### Settings

| key | What it does |
|---|---|
| brand_name | How the bot refers to your team |
| faq_docs | Doc links with info that applies to every campaign (rates, payment terms, posting rules) |
| admin_signature | If set, your replies reach creators tagged with `(Name)` |
| auto_resume_hours | After you reply in a topic, the bot stays quiet with that creator for this long |

## The inbox group

- **Each creator has a topic.** It shows their messages (forwarded), the bot's replies, button taps and campaign sends.
- **Anything you type in a topic goes to that creator.** That includes photos and files. A 👍 reaction confirms it was delivered.
- **Internal notes:** start a message with `//` and it won't be sent.
- **Pausing:** when you reply, the bot pauses auto-replies for that creator for `auto_resume_hours`. `/pause` stops it until you send `/resume`.
- **Saving answers:** after you answer an escalated question, the bot offers to save the Q&A into the campaign brief or the general FAQ, so it can answer the next creator who asks.
- **Commands:** `/campaigns`, `/status <id>`, `/sendnow <id>`, `/cancel <id>`, `/invites`, `/refresh`, `/help`.

## Testing it yourself

You can play both roles with one Telegram account:

1. Add yourself as a creator row and open your invite link.
2. Create a campaign that targets your id, set its `send_at` to a minute from now, and set its status to `ready`.
3. Confirm the preview in the group. The campaign arrives in your DM with the bot.
4. Ask questions there, and watch them appear in your topic.

## Hosting

The bot is one long-running process. It uses long polling, so it needs no domain or open port, and it checks the Sheet every minute. It keeps a small SQLite file in `data/`, which must survive restarts.

- **For testing:** run it on your laptop.
- **Free 24/7:**
  - **Oracle Cloud Always Free VM:** check that their idle-instance reclaim policy doesn't apply to you.
  - **Google Cloud's free e2-micro VM:** US regions only.
  - Check the current free-tier terms of either before relying on it.
- **About $5/month, least hassle:** any small VPS (Hetzner, DigitalOcean) or Railway with a volume mounted at `/app/data`.

With Docker:

```bash
docker build -t campaign-bot .
docker run -d --restart unless-stopped --env-file .env \
  -e GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service-account.json)" \
  -v campaign-bot-data:/app/data campaign-bot
```

Serverless platforms (Vercel, Cloudflare Workers) don't fit the current design. See `CLAUDE.md` for what would need to change.

## Costs

- **Telegram, Google APIs:** free at this scale.
- **Claude:**
  - Each creator question is one API call that includes that creator's campaign docs. The docs are prompt-cached, so repeat questions about the same campaign are cheaper.
  - The default model is Claude Opus 5.5 at low effort. For lower cost, set `CLAUDE_MODEL=claude-haiku-5-5` in `.env`. Spot-check its answers before keeping it.

## License

MIT. See [LICENSE](LICENSE).

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
