"""The team inbox: a private Telegram group with Topics enabled. Each creator
gets one topic, every message with them is mirrored there, and anything the
team writes in a topic is relayed to that creator by the bot."""

import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes

from .docs import parse_file_ids
from .services import Services, services
from .sheets import Row

log = logging.getLogger(__name__)

NOTE_PREFIX = "//"


async def _thread_for(bot: Bot, svc: Services, creator: Row) -> int:
    creator_id = creator.get("id")
    thread = svc.store.get_thread(creator_id)
    if thread is None:
        name = f"{creator.get('name') or 'Creator'} ({creator_id})"[:128]
        topic = await bot.create_forum_topic(chat_id=svc.group_id, name=name)
        thread = topic.message_thread_id
        svc.store.set_thread(creator_id, thread)
    return thread


async def _in_topic(bot: Bot, svc: Services, creator: Row, send):
    """Run send(thread_id), recreating the topic once if the team deleted it."""
    for attempt in range(2):
        thread = await _thread_for(bot, svc, creator)
        try:
            return await send(thread)
        except BadRequest as e:
            if attempt == 0 and "thread not found" in str(e).lower():
                svc.store.forget_thread(creator.get("id"))
                continue
            raise


async def note(bot: Bot, svc: Services, creator: Row, text: str, reply_markup=None) -> Message | None:
    """Post a message from the bot into the creator's topic. Never raises: the
    inbox is a mirror, so a failure here must not break the creator's flow."""
    if not svc.group_id:
        return None
    try:
        return await _in_topic(
            bot,
            svc,
            creator,
            lambda thread: bot.send_message(
                svc.group_id, text, message_thread_id=thread, reply_markup=reply_markup
            ),
        )
    except Exception:
        log.exception("Could not post to inbox topic for %s", creator.get("id"))
        return None


async def mirror_creator_message(bot: Bot, svc: Services, creator: Row, message: Message) -> None:
    """Forward the creator's own message so the topic shows who sent it."""
    if not svc.group_id:
        return
    try:
        await _in_topic(
            bot,
            svc,
            creator,
            lambda thread: bot.forward_message(
                svc.group_id, message.chat_id, message.message_id, message_thread_id=thread
            ),
        )
    except Exception:
        log.exception("Could not mirror message from %s", creator.get("id"))


async def post_general(bot: Bot, svc: Services, text: str, reply_markup=None) -> Message | None:
    if not svc.group_id:
        log.warning("No ADMIN_GROUP_ID set; dropping inbox message: %s", text)
        return None
    return await bot.send_message(svc.group_id, text, reply_markup=reply_markup)


# Team messages in a topic -> relayed to the creator


async def on_team_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    msg = update.effective_message
    if not msg or not msg.is_topic_message:
        return
    creator_id = svc.store.creator_for_thread(msg.message_thread_id)
    if creator_id is None:
        return
    text = msg.text or msg.caption or ""
    if text.startswith(NOTE_PREFIX):
        return  # internal note for the team
    creator = svc.sheet.creator(creator_id)
    if creator is None or not creator.get("chat_id"):
        await msg.reply_text("This creator isn't connected to the bot, so nothing was sent.")
        return

    chat_id = int(creator.get("chat_id"))
    try:
        if msg.text:
            signature = svc.sheet.setting("admin_signature")
            await context.bot.send_message(chat_id, f"{msg.text}\n\n({signature})" if signature else msg.text)
        else:
            await context.bot.copy_message(chat_id, from_chat_id=msg.chat_id, message_id=msg.message_id)
    except Forbidden:
        await msg.reply_text("Not delivered: this creator has blocked the bot.")
        return

    try:
        await msg.set_reaction("👍")  # shows the team the message went out
    except BadRequest:
        pass

    svc.store.add_history(creator_id, "team", text or "[sent an attachment]")
    if not svc.store.is_paused_manually(creator_id):
        hours = float(svc.sheet.setting("auto_resume_hours", "12") or 12)
        svc.store.pause(creator_id, hours * 3600)

    escalation = svc.store.open_escalation(creator_id)
    if escalation and msg.text:
        svc.store.answer_escalation(escalation.id, msg.text)
        buttons = []
        if escalation.campaign_id:
            buttons.append(InlineKeyboardButton("Save to campaign brief", callback_data=f"lr:{escalation.id}:c"))
        buttons.append(InlineKeyboardButton("Save to general FAQ", callback_data=f"lr:{escalation.id}:g"))
        buttons.append(InlineKeyboardButton("Skip", callback_data=f"lr:{escalation.id}:s"))
        await msg.reply_text(
            "Save this answer so the bot can handle the question next time?\n\n"
            f"Q: {escalation.question}\nA: {msg.text}",
            reply_markup=InlineKeyboardMarkup([buttons[:-1], buttons[-1:]]),
        )


async def on_learn_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    query = update.callback_query
    if query.message.chat_id != svc.group_id:
        await query.answer()
        return
    _, escalation_id, choice = query.data.split(":")
    escalation = svc.store.get_escalation(int(escalation_id))
    if escalation is None or escalation.status != "answered":
        await query.answer("Already handled.")
        await query.edit_message_reply_markup(None)
        return
    if choice == "s":
        svc.store.close_escalation(escalation.id, "skipped")
        await query.answer("Skipped.")
        await query.edit_message_reply_markup(None)
        return

    if choice == "c":
        campaign = svc.sheet.campaign(escalation.campaign_id)
        cell, target = (campaign.get("brief_docs") if campaign else ""), "the campaign brief"
    else:
        cell, target = svc.sheet.setting("faq_docs"), "the general FAQ"
    doc_id = None
    for file_id in parse_file_ids(cell):
        try:
            if (await svc.docs.fetch(file_id)).is_google_doc:
                doc_id = file_id
                break
        except Exception:
            log.exception("Could not read %s", file_id)
    if doc_id is None:
        await query.answer(f"No Google Doc found for {target}.", show_alert=True)
        return
    try:
        await svc.docs.append_qa(doc_id, escalation.question, escalation.answer)
    except Exception as e:
        log.exception("Could not append to %s", doc_id)
        await query.answer(
            f"Couldn't edit the doc ({e.__class__.__name__}). Give the service account Editor access.",
            show_alert=True,
        )
        return
    svc.store.close_escalation(escalation.id, "saved")
    await query.answer("Saved.")
    await query.edit_message_text(f"{query.message.text}\n\nSaved to {target}.")


# Topic commands


def _topic_creator(update: Update, svc: Services) -> str | None:
    msg = update.effective_message
    if not msg.is_topic_message:
        return None
    return svc.store.creator_for_thread(msg.message_thread_id)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    creator_id = _topic_creator(update, svc)
    if creator_id is None:
        await update.effective_message.reply_text("Use /pause inside a creator's topic.")
        return
    svc.store.pause(creator_id)
    await update.effective_message.reply_text("Paused. The bot won't auto-reply to this creator until you send /resume.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    creator_id = _topic_creator(update, svc)
    if creator_id is None:
        await update.effective_message.reply_text("Use /resume inside a creator's topic.")
        return
    svc.store.resume(creator_id)
    await update.effective_message.reply_text("Resumed. The bot will answer this creator's questions again.")
