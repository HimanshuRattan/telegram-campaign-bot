"""Everything a creator does in their private chat with the bot."""

import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from . import inbox
from .campaigns import render_message, set_assignment_status
from .llm import CampaignContext, Decision
from .services import Services, services
from .sheets import Row
from .timeutil import display_value, stamp

log = logging.getLogger(__name__)

NOT_INVITED = "Hi! This bot is for creators we work with. If you were expecting access, ask the team for your personal invite link."
HOLDING_REPLY = "Good question. Let me check with the team and get back to you shortly."


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    brand = svc.sheet.setting("brand_name", "our team")

    existing = svc.sheet.creator_by_chat(chat.id)
    if existing:
        if existing.get("status").lower() != "active":
            await _update_creator(svc, existing, {"status": "active"})
            await inbox.note(context.bot, svc, existing, f"{existing.get('name')} rejoined with /start.")
        await msg.reply_text(f"You're connected to {brand}'s campaign assistant. Campaign briefs will show up here.")
        return

    code = context.args[0] if context.args else ""
    creator = svc.sheet.creator_by_invite(code)
    if creator is None and code:
        await svc.sheet.refresh()  # the link may be for a row added in the last minute
        creator = svc.sheet.creator_by_invite(code)
    if creator is None:
        await msg.reply_text(NOT_INVITED)
        return
    if creator.get("chat_id") and creator.get("chat_id") != str(chat.id):
        await msg.reply_text("This invite link has already been used. Ask the team for a new one.")
        return

    await _update_creator(
        svc,
        creator,
        {
            "chat_id": str(chat.id),
            "status": "active",
            "telegram_username": f"@{user.username}" if user.username else "",
            "joined_at": stamp(svc.cfg.timezone),
        },
    )
    welcome = (
        f"Hi {creator.get('name')}! You're now connected to {brand}'s campaign assistant. "
        "You'll get campaign briefs here, and you can ask questions about them anytime. "
        "Our team can see these chats and will jump in when needed.\n\n"
        "Send /stop if you'd rather not receive campaign messages."
    )
    await msg.reply_text(welcome)
    svc.store.add_history(creator.get("id"), "bot", welcome)
    await inbox.note(context.bot, svc, creator, f"{creator.get('name')} connected to the bot.")


async def on_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    creator = svc.sheet.creator_by_chat(update.effective_chat.id)
    if creator is None:
        return
    await _update_creator(svc, creator, {"status": "opted_out"})
    await update.effective_message.reply_text("Done. You won't receive campaign messages. Send /start anytime to rejoin.")
    await inbox.note(context.bot, svc, creator, f"{creator.get('name')} opted out with /stop.")


async def _update_creator(svc: Services, creator: Row, changes: dict[str, str]) -> None:
    creator.data.update(changes)
    await svc.sheet.update_row("Creators", {"id": creator.get("id")}, changes)


async def on_campaign_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    query = update.callback_query
    _, campaign_id, choice = query.data.split(":", 2)
    creator = svc.sheet.creator_by_chat(query.message.chat_id)
    if creator is None:
        await query.answer()
        return
    if choice == "ask":
        await query.answer()
        await query.message.reply_text("Sure, type your question here and I'll help.")
        return

    status = "accepted" if choice == "in" else "declined"
    await set_assignment_status(svc, campaign_id, creator.get("id"), status)
    await query.answer("Got it!")
    await query.edit_message_reply_markup(None)
    reply = (
        "You're in! Ask me here if anything about the brief is unclear, and send me the link once your post is live."
        if status == "accepted"
        else "No problem, thanks for letting us know."
    )
    await query.message.reply_text(reply)
    svc.store.add_history(creator.get("id"), "creator", f"[tapped {'I am in' if status == 'accepted' else 'Pass'}]")
    svc.store.add_history(creator.get("id"), "bot", reply)
    campaign = svc.sheet.campaign(campaign_id)
    name = campaign.get("name") if campaign else campaign_id
    await inbox.note(context.bot, svc, creator, f"{creator.get('name')} tapped {'I am in' if status == 'accepted' else 'Pass'} for {name}.")


async def on_creator_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    msg = update.effective_message
    creator = svc.sheet.creator_by_chat(msg.chat_id)
    if creator is None:
        await msg.reply_text(NOT_INVITED)
        return
    creator_id = creator.get("id")
    text = msg.text or msg.caption or ""

    await inbox.mirror_creator_message(context.bot, svc, creator, msg)
    history = svc.store.recent_history(creator_id)
    svc.store.add_history(creator_id, "creator", text or "[sent an attachment]")

    if svc.store.is_paused(creator_id):
        return
    if not text:
        await inbox.note(context.bot, svc, creator, "The bot can't read this kind of message. Reply here if it needs a response.")
        return

    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    campaigns = await _campaign_contexts(context, svc, creator, msg)
    faq = []
    if svc.sheet.setting("faq_docs"):
        try:
            faq = await svc.docs.fetch_all(svc.sheet.setting("faq_docs"))
        except Exception as e:
            log.exception("Could not read FAQ docs")
            await inbox.post_general(context.bot, svc, f"Couldn't read the general FAQ doc: {e}")

    try:
        decision = await svc.llm.decide(
            brand=svc.sheet.setting("brand_name", "our team"),
            creator_name=creator.get("name"),
            faq=faq,
            campaigns=campaigns,
            history=history,
            message=text,
        )
    except Exception:
        log.exception("Claude call failed for %s", creator_id)
        decision = Decision(action="escalate", question_summary=text)
    await _act(context, svc, creator, decision, text, campaigns)


async def _campaign_contexts(context, svc: Services, creator: Row, msg) -> list[CampaignContext]:
    creator_id = creator.get("id")
    assignments = svc.store.active_assignments(creator_id)
    # If the creator replied to a specific brief, focus on that campaign.
    if msg.reply_to_message:
        replied = svc.store.campaign_for_brief_message(creator_id, msg.reply_to_message.message_id)
        assignments = [a for a in assignments if a.campaign_id == replied] or assignments

    contexts = []
    for assignment in assignments:
        campaign = svc.sheet.campaign(assignment.campaign_id)
        if campaign is None:
            continue
        docs = []
        if campaign.get("brief_docs"):
            try:
                docs = await svc.docs.fetch_all(campaign.get("brief_docs"))
            except Exception as e:
                log.exception("Could not read brief for %s", assignment.campaign_id)
                await inbox.note(context.bot, svc, creator, f"Couldn't read the brief doc for {assignment.campaign_id}: {e}")
        contexts.append(
            CampaignContext(
                id=assignment.campaign_id,
                name=campaign.get("name"),
                deadline=display_value(campaign.get("deadline"), svc.cfg.timezone),
                message_sent=render_message(campaign, creator, svc.cfg.timezone),
                status=assignment.status,
                docs=docs,
            )
        )
    return contexts


async def _act(context, svc: Services, creator: Row, decision: Decision, text: str, campaigns: list[CampaignContext]) -> None:
    bot, creator_id, chat_id = context.bot, creator.get("id"), int(creator.get("chat_id"))
    known = {c.id: c for c in campaigns}
    campaign = known.get(decision.campaign_id)
    label = f" ({campaign.name})" if campaign else ""

    if decision.action == "none":
        return

    if decision.action == "escalate":
        await bot.send_message(chat_id, HOLDING_REPLY)
        svc.store.add_history(creator_id, "bot", HOLDING_REPLY)
        question = decision.question_summary or text
        svc.store.add_escalation(creator_id, campaign.id if campaign else "", question)
        await inbox.note(
            bot, svc, creator,
            f"Needs your answer{label}: {question}\n\n"
            f"Bot replied: \"{HOLDING_REPLY}\"\nReply in this topic and it goes to {creator.get('name')}.",
        )
        return

    if decision.action in ("accepted", "declined", "post_link") and campaign:
        status = "posted" if decision.action == "post_link" else decision.action
        await set_assignment_status(svc, campaign.id, creator_id, status, decision.post_url)

    reply = decision.reply.strip()
    if reply:
        await bot.send_message(chat_id, reply)
        svc.store.add_history(creator_id, "bot", reply)
    summary = ""
    if campaign:
        summary = {
            "accepted": f"Marked as accepted{label}.",
            "declined": f"Marked as declined{label}.",
            "post_link": f"Post submitted{label}: {decision.post_url}",
        }.get(decision.action, "")
    mirrored = "\n\n".join(p for p in [summary, f"Bot: {reply}" if reply else ""] if p)
    if mirrored:
        await inbox.note(bot, svc, creator, mirrored)
