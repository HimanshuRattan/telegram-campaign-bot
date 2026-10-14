import logging
import secrets

from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, Message, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import campaigns, creators, inbox
from .config import load_config
from .docs import DocFetcher
from .google_api import http_session, load_credentials, sheets_client
from .llm import Answerer
from .services import Services, services
from .sheets import Sheet
from .store import Store

log = logging.getLogger(__name__)

HELP = """Campaign bot commands (use them in this group):

/campaigns - upcoming and active campaigns
/status <id> - responses for a campaign
/sendnow <id> - send a confirmed campaign right away
/cancel <id> - cancel a campaign that hasn't gone out
/invites - invite links for creators who haven't connected
/refresh - reload the Google Sheet now

Inside a creator's topic:
- Anything you write is sent to the creator by the bot.
- Start a message with // to keep it as an internal note.
- /pause stops the bot auto-replying to them; /resume turns it back on.

To schedule a campaign: add a row in the Campaigns tab and set its status to 'ready'. The bot posts a preview here within a minute for you to confirm."""

ADMIN_COMMANDS = [
    BotCommand("campaigns", "Upcoming and active campaigns"),
    BotCommand("status", "Responses for a campaign"),
    BotCommand("sendnow", "Send a confirmed campaign now"),
    BotCommand("cancel", "Cancel a campaign"),
    BotCommand("invites", "Invite links for unconnected creators"),
    BotCommand("pause", "Stop auto-replies in this topic"),
    BotCommand("resume", "Resume auto-replies in this topic"),
    BotCommand("refresh", "Reload the Google Sheet"),
    BotCommand("help", "How this bot works"),
]


class InboxGroup(filters.MessageFilter):
    """Matches messages in the team inbox group, which can be linked at runtime with /setup."""

    def __init__(self, svc: Services):
        super().__init__(name="InboxGroup")
        self.svc = svc

    def filter(self, message: Message) -> bool:
        return message.chat_id == self.svc.group_id


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    chat, msg = update.effective_chat, update.effective_message
    if svc.group_id == chat.id:
        await msg.reply_text("This group is already the inbox. Send /help to see what you can do.")
        return
    if svc.group_id:
        await msg.reply_text("This bot is already linked to another inbox group.")
        return
    if not context.args or context.args[0] != context.application.bot_data["setup_code"]:
        await msg.reply_text("Send /setup followed by the code shown in the bot's startup log.")
        return
    if not chat.is_forum:
        await msg.reply_text("Turn on Topics in this group's settings first, then send /setup again.")
        return
    me = await context.bot.get_chat_member(chat.id, context.bot.id)
    if me.status != ChatMemberStatus.ADMINISTRATOR or not getattr(me, "can_manage_topics", False):
        await msg.reply_text("Make me an admin with the 'Manage topics' permission, then send /setup again.")
        return
    svc.store.set_value("admin_group_id", str(chat.id))
    await context.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat.id))
    await msg.reply_text(
        "All set. This group is now the inbox: every creator gets a topic here.\n\n"
        "Next: add creators to the Creators tab of your Sheet, then send /invites to get their invite links."
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"This chat's id is {update.effective_chat.id}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP)


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await services(context).sheet.refresh()
    await update.effective_message.reply_text("Sheet reloaded.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)
    svc = context.application.bot_data.get("svc")
    if svc and svc.group_id:
        try:
            await context.bot.send_message(
                svc.group_id, f"Bot error: {type(context.error).__name__}: {str(context.error)[:500]}"
            )
        except Exception:
            log.exception("Could not report error to the inbox group")


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config()
    creds = load_credentials(cfg)
    svc = Services(
        cfg=cfg,
        sheet=Sheet(sheets_client(creds), cfg.sheet_id),
        docs=DocFetcher(http_session(creds)),
        store=Store(cfg.db_path),
        llm=Answerer(cfg.claude_model, cfg.claude_effort, cfg.openrouter_api_key),
    )

    async def post_init(app: Application) -> None:
        app.bot_data["svc"] = svc
        app.bot_data["setup_code"] = secrets.token_hex(3)
        created = await svc.sheet.ensure_tabs()
        if created:
            log.info("Created sheet tabs: %s", ", ".join(created))
        await svc.sheet.refresh()
        await app.bot.set_my_commands([], scope=BotCommandScopeAllPrivateChats())
        if svc.group_id:
            await app.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(svc.group_id))
        log.info("Bot @%s is running", app.bot.username)
        if not svc.group_id:
            log.warning(
                "\n\n  No inbox group linked yet. In your Telegram group (Topics on, bot is admin), send:\n\n"
                "      /setup %s\n",
                app.bot_data["setup_code"],
            )

    app = Application.builder().token(cfg.telegram_token).rate_limiter(AIORateLimiter(max_retries=3)).post_init(post_init).build()

    private = filters.ChatType.PRIVATE
    group = InboxGroup(svc)
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("setup", cmd_setup, filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("start", creators.on_start, private))
    app.add_handler(CommandHandler("stop", creators.on_stop, private))
    app.add_handler(CallbackQueryHandler(creators.on_campaign_button, pattern=r"^cr:"))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, creators.on_creator_message))

    for name, handler in [
        ("help", cmd_help),
        ("refresh", cmd_refresh),
        ("campaigns", campaigns.cmd_campaigns),
        ("status", campaigns.cmd_status),
        ("cancel", campaigns.cmd_cancel),
        ("sendnow", campaigns.cmd_sendnow),
        ("invites", campaigns.cmd_invites),
        ("pause", inbox.cmd_pause),
        ("resume", inbox.cmd_resume),
    ]:
        app.add_handler(CommandHandler(name, handler, group))
    app.add_handler(CallbackQueryHandler(campaigns.on_preview_button, pattern=r"^cp:"))
    app.add_handler(CallbackQueryHandler(inbox.on_learn_button, pattern=r"^lr:"))
    app.add_handler(MessageHandler(group & ~filters.COMMAND & ~filters.StatusUpdate.ALL, inbox.on_team_message))
    app.job_queue.run_repeating(campaigns.tick, interval=60, first=5)

    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
