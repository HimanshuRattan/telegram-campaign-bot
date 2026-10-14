"""Campaign lifecycle, driven from the Campaigns tab:

draft -> ready (operator) -> awaiting_confirm (bot posts preview) -> scheduled
(operator taps Confirm) -> sending -> sent. Also: cancelled, error.
"""

import asyncio
import logging
import re
import secrets
from collections import Counter

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden
from telegram.ext import ContextTypes

from . import inbox
from .services import Services, services
from .sheets import Row
from .timeutil import display_value, fmt_time, now, parse_sheet_datetime, stamp

log = logging.getLogger(__name__)

DEFAULT_MESSAGE = (
    "Hi {name}! We're running a new campaign: {campaign}.\n\n"
    "Let us know below if you're in, or ask me anything about it."
)
APPEND_BATCH = 10
# The minute tick and /sendnow both run tick(); never let two overlap and double-send.
_tick_lock = asyncio.Lock()


# Targeting and rendering (pure functions, unit tested)


def _tags(creator: Row) -> set[str]:
    return {t.strip().lower() for t in creator.get("tags").split(",") if t.strip()}


def resolve_targets(target: str, creators: list[Row]) -> tuple[list[Row], list[Row]]:
    """Return (creators who will receive it, matched creators who haven't joined the bot).

    target forms: "all", "tags: a, b" (has all tags), "any: a, b" (has any tag),
    or a list of creator ids ("c001, c004").
    """
    target = (target or "").strip()
    lowered = target.lower()
    if lowered == "all":
        matched = list(creators)
    elif lowered.startswith(("tags:", "any:")):
        wanted = {t.strip().lower() for t in target.split(":", 1)[1].split(",") if t.strip()}
        if lowered.startswith("tags:"):
            matched = [c for c in creators if wanted and wanted <= _tags(c)]
        else:
            matched = [c for c in creators if wanted & _tags(c)]
    else:
        ids = [i for i in re.split(r"[\s,]+", target) if i]
        by_id = {c.get("id"): c for c in creators}
        matched = [by_id[i] for i in ids if i in by_id]
    matched = [c for c in matched if c.get("status").lower() != "opted_out"]
    ready = [c for c in matched if c.get("chat_id") and c.get("status").lower() == "active"]
    not_joined = [c for c in matched if c not in ready]
    return ready, not_joined


class _KeepMissing(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render_message(campaign: Row, creator: Row, tz) -> str:
    template = campaign.get("message") or DEFAULT_MESSAGE
    deadline = display_value(campaign.get("deadline"), tz)
    text = template.format_map(
        _KeepMissing(name=creator.get("name") or "there", campaign=campaign.get("name"), deadline=deadline)
    )
    if deadline and "{deadline}" not in template:
        text += f"\n\nDeadline: {deadline}"
    return text


def creator_keyboard(campaign_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("I'm in", callback_data=f"cr:{campaign_id}:in"),
                InlineKeyboardButton("Pass", callback_data=f"cr:{campaign_id}:out"),
            ],
            [InlineKeyboardButton("Ask a question", callback_data=f"cr:{campaign_id}:ask")],
        ]
    )


async def set_assignment_status(svc: Services, campaign_id: str, creator_id: str, status: str, post_link: str = "") -> bool:
    if svc.store.get_assignment(campaign_id, creator_id) is None:
        return False
    svc.store.update_assignment(campaign_id, creator_id, status, post_link or None)
    changes = {"status": status, "updated_at": stamp(svc.cfg.timezone)}
    if post_link:
        changes["post_link"] = post_link
    try:
        await svc.sheet.update_row("Assignments", {"campaign_id": campaign_id, "creator_id": creator_id}, changes)
    except Exception:
        log.exception("Could not update Assignments row for %s/%s", campaign_id, creator_id)
    return True


# The scheduler tick (runs every minute)


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    async with _tick_lock:
        await _tick(context)


async def _tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    try:
        await svc.sheet.refresh()
    except Exception:
        log.exception("Sheet refresh failed")
        return
    await ensure_invite_links(context.bot, svc)
    if not svc.group_id:
        return  # previews and confirmations need the inbox group; see /setup
    current = now(svc.cfg.timezone)
    for campaign in list(svc.sheet.campaigns):
        status = campaign.get("status").lower()
        try:
            if status == "ready":
                await send_preview(context.bot, svc, campaign)
            elif status in ("scheduled", "sending"):
                when = parse_sheet_datetime(campaign.get("send_at"), svc.cfg.timezone)
                if when and when <= current:
                    await send_campaign(context.bot, svc, campaign)
        except Exception as e:
            log.exception("Campaign %s failed", campaign.get("id"))
            try:
                await svc.sheet.update_row("Campaigns", {"id": campaign.get("id")}, {"status": "error"})
                await inbox.post_general(
                    context.bot, svc, f"Campaign {campaign.get('id')} hit an error and was set to 'error': {e}"
                )
            except Exception:
                log.exception("Could not report the failure of campaign %s", campaign.get("id"))


async def ensure_invite_links(bot: Bot, svc: Services) -> None:
    updates = []
    for creator in svc.sheet.creators:
        if creator.get("invite_code"):
            continue
        code = secrets.token_urlsafe(8)
        changes = {"invite_code": code, "invite_link": f"https://t.me/{bot.username}?start={code}"}
        creator.data.update(changes)
        updates.append(({"id": creator.get("id")}, changes))
    if updates:
        await svc.sheet.update_rows("Creators", updates)


async def send_preview(bot: Bot, svc: Services, campaign: Row) -> None:
    campaign_id = campaign.get("id")
    tz = svc.cfg.timezone
    when = parse_sheet_datetime(campaign.get("send_at"), tz)
    problems = []
    if when is None:
        problems.append("send_at is empty or unreadable. Use a date cell or text like 2026-10-16 15:00.")
    ready, not_joined = resolve_targets(campaign.get("target"), svc.sheet.creators)
    if not ready:
        problems.append(f"target '{campaign.get('target')}' matches no connected creators.")
    if problems:
        await svc.sheet.update_row("Campaigns", {"id": campaign_id}, {"status": "error"})
        await inbox.post_general(
            bot, svc, f"Campaign {campaign_id} can't be scheduled:\n- " + "\n- ".join(problems) +
            "\n\nFix the row and set status back to 'ready'."
        )
        return

    warnings = []
    if not campaign.get("brief_docs"):
        warnings.append("No brief_docs set, so the bot can only answer from the general FAQ.")
    else:
        try:
            await svc.docs.fetch_all(campaign.get("brief_docs"))
        except Exception as e:
            warnings.append(f"Couldn't read the brief doc: {e}")

    lines = [
        f"Campaign {campaign_id}: {campaign.get('name')}",
        f"Sends: {fmt_time(when)}" + (" (time has passed, so it sends right after you confirm)" if when <= now(tz) else ""),
        f"Recipients: {len(ready)} creators",
    ]
    if not_joined:
        lines.append(f"Matched but not connected to the bot (won't receive it): {len(not_joined)}")
    lines += [f"Warning: {w}" for w in warnings]
    lines += ["", f"Message preview for {ready[0].get('name')}:", "", render_message(campaign, ready[0], tz)]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data=f"cp:ok:{campaign_id}"),
                InlineKeyboardButton("Show list", callback_data=f"cp:list:{campaign_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"cp:no:{campaign_id}"),
            ]
        ]
    )
    await svc.sheet.update_row("Campaigns", {"id": campaign_id}, {"status": "awaiting_confirm"})
    campaign.data["status"] = "awaiting_confirm"
    await inbox.post_general(bot, svc, "\n".join(lines), reply_markup=keyboard)


async def on_preview_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    query = update.callback_query
    if query.message.chat_id != svc.group_id:
        await query.answer()
        return
    _, action, campaign_id = query.data.split(":", 2)
    await svc.sheet.refresh()
    campaign = svc.sheet.campaign(campaign_id)
    if campaign is None:
        await query.answer("That campaign is no longer in the sheet.", show_alert=True)
        return
    status = campaign.get("status").lower()
    ready, not_joined = resolve_targets(campaign.get("target"), svc.sheet.creators)

    if action == "list":
        text = "Will receive it:\n" + "\n".join(f"- {c.get('name')} ({c.get('id')})" for c in ready)
        if not_joined:
            text += "\n\nNot connected yet:\n" + "\n".join(f"- {c.get('name')} ({c.get('id')})" for c in not_joined)
        await query.answer()
        await query.message.reply_text(text[:4000])
        return

    if status != "awaiting_confirm":
        await query.answer(f"Status is now '{status}', so this preview is out of date.", show_alert=True)
        await query.edit_message_reply_markup(None)
        return

    who = query.from_user.first_name
    if action == "ok":
        svc.store.confirm_campaign(campaign_id, [c.get("id") for c in ready])
        await svc.sheet.update_row("Campaigns", {"id": campaign_id}, {"status": "scheduled"})
        await query.answer("Scheduled.")
        await query.edit_message_text(f"{query.message.text}\n\nConfirmed by {who}. Scheduled for {len(ready)} creators.")
    else:
        await svc.sheet.update_row("Campaigns", {"id": campaign_id}, {"status": "cancelled"})
        await query.answer("Cancelled.")
        await query.edit_message_text(f"{query.message.text}\n\nCancelled by {who}.")


async def send_campaign(bot: Bot, svc: Services, campaign: Row) -> None:
    campaign_id = campaign.get("id")
    tz = svc.cfg.timezone
    await svc.sheet.update_row("Campaigns", {"id": campaign_id}, {"status": "sending"})

    recipient_ids = svc.store.confirmed_recipients(campaign_id)
    if recipient_ids is None:  # status was set to 'scheduled' by hand, skipping the preview
        recipient_ids = [c.get("id") for c in resolve_targets(campaign.get("target"), svc.sheet.creators)[0]]

    pending_rows: list[dict[str, str]] = []
    sent = blocked = skipped = 0
    for creator_id in recipient_ids:
        if svc.store.get_assignment(campaign_id, creator_id):
            continue  # already sent (resuming after a restart)
        creator = svc.sheet.creator(creator_id)
        if creator is None or not creator.get("chat_id") or creator.get("status").lower() != "active":
            skipped += 1
            continue
        text = render_message(campaign, creator, tz)
        status = "sent"
        try:
            message = await bot.send_message(int(creator.get("chat_id")), text, reply_markup=creator_keyboard(campaign_id))
            svc.store.add_assignment(campaign_id, creator_id, message.message_id)
            svc.store.add_history(creator_id, "bot", text)
            await inbox.note(bot, svc, creator, f"Campaign sent: {campaign.get('name')} ({campaign_id})\n\n{text}")
            sent += 1
        except Forbidden:
            status = "blocked"
            svc.store.add_assignment(campaign_id, creator_id, None, status=status)
            blocked += 1
        pending_rows.append(
            {
                "campaign_id": campaign_id,
                "creator_id": creator_id,
                "creator_name": creator.get("name"),
                "status": status,
                "sent_at": stamp(tz),
                "updated_at": stamp(tz),
            }
        )
        if len(pending_rows) >= APPEND_BATCH:
            await svc.sheet.append_rows("Assignments", pending_rows)
            pending_rows = []
    await svc.sheet.append_rows("Assignments", pending_rows)
    await svc.sheet.update_row("Campaigns", {"id": campaign_id}, {"status": "sent"})

    summary = f"Campaign {campaign_id} ({campaign.get('name')}) sent to {sent} creators."
    if blocked:
        summary += f" {blocked} have blocked the bot."
    if skipped:
        summary += f" {skipped} skipped (no longer connected or opted out)."
    await inbox.post_general(bot, svc, summary)


# Admin commands (only registered for the inbox group)


async def cmd_campaigns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    await svc.sheet.refresh()
    lines = []
    for c in svc.sheet.campaigns:
        status = c.get("status").lower() or "draft"
        if status in ("sent", "cancelled"):
            continue
        when = parse_sheet_datetime(c.get("send_at"), svc.cfg.timezone)
        lines.append(f"{c.get('id')} {c.get('name')}: {status}" + (f", {fmt_time(when)}" if when else ""))
    await update.effective_message.reply_text("\n".join(lines) or "No upcoming campaigns.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    if not context.args:
        await update.effective_message.reply_text("Usage: /status <campaign id>")
        return
    campaign_id = context.args[0]
    assignments = svc.store.campaign_assignments(campaign_id)
    if not assignments:
        await update.effective_message.reply_text(f"Nothing has been sent for {campaign_id} yet.")
        return
    counts = Counter(a.status for a in assignments)
    order = ["sent", "accepted", "declined", "posted", "blocked"]
    parts = [f"{counts[s]} {s}" for s in order if counts[s]]
    parts += [f"{n} {s}" for s, n in counts.items() if s not in order]
    lines = [f"{campaign_id}: {len(assignments)} total. " + ", ".join(parts)]
    waiting = [a.creator_id for a in assignments if a.status == "sent"]
    if waiting:
        names = [(svc.sheet.creator(cid).get("name") if svc.sheet.creator(cid) else cid) for cid in waiting[:30]]
        lines.append("No response yet: " + ", ".join(names) + (" ..." if len(waiting) > 30 else ""))
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    if not context.args:
        await update.effective_message.reply_text("Usage: /cancel <campaign id>")
        return
    campaign_id = context.args[0]
    await svc.sheet.refresh()
    campaign = svc.sheet.campaign(campaign_id)
    if campaign is None:
        await update.effective_message.reply_text(f"No campaign with id {campaign_id}.")
        return
    if campaign.get("status").lower() in ("sending", "sent"):
        await update.effective_message.reply_text("That campaign has already gone out.")
        return
    await svc.sheet.update_row("Campaigns", {"id": campaign_id}, {"status": "cancelled"})
    await update.effective_message.reply_text(f"Cancelled {campaign_id}.")


async def cmd_sendnow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    if not context.args:
        await update.effective_message.reply_text("Usage: /sendnow <campaign id>")
        return
    campaign_id = context.args[0]
    await svc.sheet.refresh()
    campaign = svc.sheet.campaign(campaign_id)
    if campaign is None:
        await update.effective_message.reply_text(f"No campaign with id {campaign_id}.")
        return
    if campaign.get("status").lower() not in ("awaiting_confirm", "scheduled"):
        await update.effective_message.reply_text(
            "Only campaigns that are awaiting confirmation or scheduled can be sent now. "
            "Set the status to 'ready' first to get a preview."
        )
        return
    if svc.store.confirmed_recipients(campaign_id) is None:
        ready, _ = resolve_targets(campaign.get("target"), svc.sheet.creators)
        svc.store.confirm_campaign(campaign_id, [c.get("id") for c in ready])
    await svc.sheet.update_row(
        "Campaigns", {"id": campaign_id}, {"status": "scheduled", "send_at": stamp(svc.cfg.timezone)}
    )
    await update.effective_message.reply_text(f"Sending {campaign_id} now.")
    context.job_queue.run_once(tick, 0)


async def cmd_invites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    await svc.sheet.refresh()
    await ensure_invite_links(context.bot, svc)
    pending = [c for c in svc.sheet.creators if not c.get("chat_id")]
    if not pending:
        await update.effective_message.reply_text("Every creator in the sheet is connected.")
        return
    lines = [f"{c.get('name')} ({c.get('id')}): {c.get('invite_link')}" for c in pending]
    await update.effective_message.reply_text(
        "Not connected yet. Send each creator their own link:\n\n" + "\n".join(lines),
        disable_web_page_preview=True,
    )
