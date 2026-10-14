"""End-to-end flows through the real handlers, with Telegram, the Sheet and
Claude replaced by in-memory fakes."""

import asyncio
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from telegram.error import Forbidden

from bot import campaigns, creators, inbox
from bot.llm import Decision
from bot.services import Services
from bot.sheets import Row, Sheet
from bot.store import Store

GROUP = -100123


class FakeBot:
    username = "TestCampaignBot"

    def __init__(self, blocked=()):
        self.sent = []  # (chat_id, text, thread)
        self.forwarded = []
        self.copied = []
        self.topics = 0
        self.blocked = set(blocked)

    async def send_message(self, chat_id, text, message_thread_id=None, reply_markup=None, **_):
        if chat_id in self.blocked:
            raise Forbidden("bot was blocked by the user")
        self.sent.append((chat_id, text, message_thread_id))
        return SimpleNamespace(message_id=len(self.sent) + 1000)

    async def create_forum_topic(self, chat_id, name):
        self.topics += 1
        return SimpleNamespace(message_thread_id=500 + self.topics)

    async def forward_message(self, chat_id, from_chat_id, message_id, message_thread_id=None):
        self.forwarded.append((chat_id, from_chat_id, message_thread_id))

    async def copy_message(self, chat_id, from_chat_id, message_id):
        self.copied.append((chat_id, from_chat_id, message_id))

    async def send_chat_action(self, chat_id, action):
        pass


class FakeSheet(Sheet):
    def __init__(self, creators, campaigns, settings):
        super().__init__(None, "sheet")
        self.creators, self.campaigns, self.settings = creators, campaigns, settings
        self.appended = []
        self.updates = []

    async def refresh(self):
        pass

    async def update_rows(self, tab, updates):
        self.updates += [(tab, m, c) for m, c in updates]
        rows = self.creators if tab == "Creators" else self.campaigns if tab == "Campaigns" else []
        for match, changes in updates:
            for row in rows:
                if all(row.get(k) == v for k, v in match.items()):
                    row.data.update(changes)
        return len(updates)

    async def append_rows(self, tab, rows):
        self.appended += [(tab, r) for r in rows]


class FakeLLM:
    def __init__(self):
        self.next = Decision(action="none")
        self.calls = []

    async def decide(self, **kwargs):
        self.calls.append(kwargs)
        return self.next


class FakeDocs:
    async def fetch_all(self, cell):
        return []


def make_services(tmp_path, blocked=()):
    creator_rows = [
        Row(2, {"id": "c1", "name": "Priya", "tags": "fitness", "status": "active", "chat_id": "11", "invite_code": "x"}),
        Row(3, {"id": "c2", "name": "Ali", "tags": "fitness", "status": "active", "chat_id": "22", "invite_code": "y"}),
        Row(4, {"id": "c3", "name": "Sam", "tags": "fitness", "status": "", "chat_id": "", "invite_code": ""}),
    ]
    campaign_rows = [Row(2, {"id": "k1", "name": "Nike launch", "target": "tags: fitness", "status": "scheduled",
                             "send_at": "2020-01-01 10:00", "message": "Hi {name}, {campaign}!"})]
    svc = Services(
        cfg=SimpleNamespace(admin_group_id=GROUP, timezone=ZoneInfo("Asia/Kolkata")),
        sheet=FakeSheet(creator_rows, campaign_rows, {"admin_signature": "Rahul", "auto_resume_hours": "2"}),
        docs=FakeDocs(),
        store=Store(str(tmp_path / "bot.db")),
        llm=FakeLLM(),
    )
    bot = FakeBot(blocked)
    context = SimpleNamespace(bot=bot, application=SimpleNamespace(bot_data={"svc": svc}), args=[])
    return svc, bot, context


class FakeMessage(SimpleNamespace):
    async def reply_text(self, text, **_):
        self.replies = getattr(self, "replies", []) + [text]

    async def set_reaction(self, emoji):
        self.reaction = emoji


def creator_msg(chat_id, text, reply_to=None):
    return FakeMessage(chat_id=chat_id, message_id=9, text=text, caption=None, reply_to_message=reply_to)


def run(coro):
    return asyncio.run(coro)


def test_send_campaign_records_assignments_and_handles_blocked(tmp_path):
    svc, bot, context = make_services(tmp_path, blocked={22})
    run(campaigns.send_campaign(bot, svc, svc.sheet.campaigns[0]))

    assert svc.store.get_assignment("k1", "c1").status == "sent"
    assert svc.store.get_assignment("k1", "c2").status == "blocked"
    assert svc.store.get_assignment("k1", "c3") is None  # not connected
    assert (11, "Hi Priya, Nike launch!", None) in bot.sent
    assert [r["status"] for tab, r in svc.sheet.appended if tab == "Assignments"] == ["sent", "blocked"]
    assert svc.sheet.campaigns[0].get("status") == "sent"
    assert any(thread for _, text, thread in bot.sent if text.startswith("Campaign sent"))  # mirrored to a topic

    # Running again (e.g. resuming after a restart) must not double-send.
    before = len([s for s in bot.sent if s[0] == 11])
    run(campaigns.send_campaign(bot, svc, svc.sheet.campaigns[0]))
    assert len([s for s in bot.sent if s[0] == 11]) == before


def test_escalation_relay_and_pause(tmp_path):
    svc, bot, context = make_services(tmp_path)
    run(campaigns.send_campaign(bot, svc, svc.sheet.campaigns[0]))

    # Creator asks something the docs don't cover -> holding reply + escalation in their topic.
    svc.llm.next = Decision(action="escalate", campaign_id="k1", question_summary="Can I post on Sunday?")
    run(creators.on_creator_message(SimpleNamespace(effective_message=creator_msg(11, "can i post sunday?")), context))
    assert bot.sent[-2][:2] == (11, creators.HOLDING_REPLY)
    thread = svc.store.get_thread("c1")
    assert bot.sent[-1][0] == GROUP and bot.sent[-1][2] == thread and "Can I post on Sunday?" in bot.sent[-1][1]
    assert bot.forwarded[-1] == (GROUP, 11, thread)
    llm_kwargs = svc.llm.calls[-1]
    assert [c.id for c in llm_kwargs["campaigns"]] == ["k1"]
    assert llm_kwargs["history"][-1] == ("bot", "Hi Priya, Nike launch!")

    # Team replies in the topic -> relayed with signature, bot pauses, learn prompt shown.
    team = FakeMessage(is_topic_message=True, message_thread_id=thread, text="Yes, Sunday works", caption=None,
                       chat_id=GROUP, message_id=77)
    run(inbox.on_team_message(SimpleNamespace(effective_message=team), context))
    assert bot.sent[-1] == (11, "Yes, Sunday works\n\n(Rahul)", None)
    assert team.reaction == "👍"
    assert svc.store.is_paused("c1") and not svc.store.is_paused_manually("c1")
    assert "Save this answer" in team.replies[0]
    assert svc.store.get_escalation(1).answer == "Yes, Sunday works"

    # While paused, creator messages are mirrored but the LLM isn't called.
    calls = len(svc.llm.calls)
    run(creators.on_creator_message(SimpleNamespace(effective_message=creator_msg(11, "great thanks")), context))
    assert len(svc.llm.calls) == calls

    # Internal notes stay internal.
    sent = len(bot.sent)
    note = FakeMessage(is_topic_message=True, message_thread_id=thread, text="// she's reliable", caption=None,
                       chat_id=GROUP, message_id=78)
    run(inbox.on_team_message(SimpleNamespace(effective_message=note), context))
    assert len(bot.sent) == sent


def test_post_link_updates_assignment(tmp_path):
    svc, bot, context = make_services(tmp_path)
    run(campaigns.send_campaign(bot, svc, svc.sheet.campaigns[0]))
    svc.llm.next = Decision(action="post_link", campaign_id="k1", post_url="https://instagram.com/p/abc", reply="Thanks!")
    run(creators.on_creator_message(SimpleNamespace(effective_message=creator_msg(11, "posted! https://instagram.com/p/abc")), context))
    assignment = svc.store.get_assignment("k1", "c1")
    assert (assignment.status, assignment.post_link) == ("posted", "https://instagram.com/p/abc")
    assert (11, "Thanks!", None) in bot.sent
    assert any("Post submitted (Nike launch)" in text for _, text, _ in bot.sent)


def test_unknown_user_gets_not_invited(tmp_path):
    svc, bot, context = make_services(tmp_path)
    msg = creator_msg(999, "hello")
    run(creators.on_creator_message(SimpleNamespace(effective_message=msg), context))
    assert msg.replies == [creators.NOT_INVITED]
    assert svc.llm.calls == []


def test_start_with_invite_code_connects_creator(tmp_path):
    svc, bot, context = make_services(tmp_path)
    svc.sheet.creators[2].data["invite_code"] = "sam-code"
    context.args = ["sam-code"]
    msg = FakeMessage()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=33), effective_user=SimpleNamespace(username="samcreates"),
                             effective_message=msg)
    run(creators.on_start(update, context))
    sam = svc.sheet.creator("c3")
    assert (sam.get("chat_id"), sam.get("status"), sam.get("telegram_username")) == ("33", "active", "@samcreates")
    assert msg.replies[0].startswith("Hi Sam!")

    # Someone else reusing the link is refused.
    other = FakeMessage()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=44), effective_user=SimpleNamespace(username=None),
                             effective_message=other)
    run(creators.on_start(update, context))
    assert "already been used" in other.replies[0]


def test_setup_links_inbox_group(tmp_path):
    from bot.main import InboxGroup, cmd_setup

    svc, bot, context = make_services(tmp_path)
    svc.cfg.admin_group_id = None
    context.application.bot_data["setup_code"] = "abc123"
    bot.id = 1
    member = SimpleNamespace(status="administrator", can_manage_topics=True)

    async def get_chat_member(chat_id, user_id):
        return member

    async def set_my_commands(commands, scope):
        pass

    bot.get_chat_member, bot.set_my_commands = get_chat_member, set_my_commands

    def setup(code, is_forum=True):
        msg = FakeMessage()
        context.args = [code] if code else []
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=-100777, is_forum=is_forum), effective_message=msg)
        run(cmd_setup(update, context))
        return msg.replies[0]

    assert "code" in setup("wrong")
    assert "Topics" in setup("abc123", is_forum=False)
    member.can_manage_topics = False
    assert "Manage topics" in setup("abc123")
    member.can_manage_topics = True
    assert setup("abc123").startswith("All set")
    assert svc.group_id == -100777
    assert InboxGroup(svc).filter(SimpleNamespace(chat_id=-100777))
    assert not InboxGroup(svc).filter(SimpleNamespace(chat_id=11))
    assert "already the inbox" in setup("abc123")


def test_openrouter_request_shape():
    import json

    import httpx

    from bot.docs import Doc
    from bot.llm import Answerer

    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["Authorization"]
        content = json.dumps({"action": "reply", "campaign_id": "k1", "reply": "Use #NikeTest", "post_url": "", "question_summary": ""})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

    answerer = Answerer("anthropic/claude-opus-5.5", "low", openrouter_key="sk-or-test")
    answerer._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    decision = asyncio.run(
        answerer.decide(
            brand="Test Brand", creator_name="Priya", faq=[Doc("f", "FAQ", False, pdf=b"%PDF-1.4")], campaigns=[],
            history=[("bot", "Hi!")], message="what hashtag?",
        )
    )
    assert (decision.action, decision.reply) == ("reply", "Use #NikeTest")
    body = seen["body"]
    assert seen["auth"] == "Bearer sk-or-test"
    assert body["model"] == "anthropic/claude-opus-5.5" and body["reasoning"] == {"effort": "low"}
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["messages"][0] == {"role": "system", "content": body["messages"][0]["content"]}
    parts = body["messages"][1]["content"]
    assert parts[0]["type"] == "file" and parts[0]["file"]["file_data"].startswith("data:application/pdf;base64,")
    assert parts[1]["cache_control"] == {"type": "ephemeral"} and "what hashtag?" in parts[2]["text"]
