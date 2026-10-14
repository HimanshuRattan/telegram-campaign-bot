import time
from zoneinfo import ZoneInfo

from bot.campaigns import render_message, resolve_targets
from bot.docs import parse_file_ids
from bot.llm import CampaignContext, _knowledge_base
from bot.docs import Doc
from bot.sheets import Row, parse_rows
from bot.store import Store
from bot.timeutil import display_value, parse_sheet_datetime

TZ = ZoneInfo("Asia/Kolkata")
DOC_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def creator(cid, name="", tags="", status="active", chat_id="1"):
    return Row(2, {"id": cid, "name": name or cid, "tags": tags, "status": status, "chat_id": chat_id})


def test_parse_rows_maps_headers_and_skips_blank_rows():
    rows = parse_rows([["ID", " Name "], ["c1", "Priya"], ["", ""], ["c2"]])
    assert [(r.row, r.get("id"), r.get("name")) for r in rows] == [(2, "c1", "Priya"), (4, "c2", "")]


def test_parse_rows_stringifies_unformatted_values():
    rows = parse_rows([["key", "value"], ["auto_resume_hours", 12]])
    assert rows[0].get("value") == "12"


def test_parse_file_ids_handles_links_and_separators():
    cell = f"https://docs.google.com/document/d/{DOC_ID}/edit\nhttps://drive.google.com/open?id={DOC_ID[::-1]}, junk"
    assert parse_file_ids(cell) == [DOC_ID, DOC_ID[::-1]]
    assert parse_file_ids(f"{DOC_ID} {DOC_ID}") == [DOC_ID]
    assert parse_file_ids("") == []


def test_resolve_targets_by_tags_all_and_any():
    creators = [
        creator("c1", tags="fitness, 10k+"),
        creator("c2", tags="fitness"),
        creator("c3", tags="Food, 10k+"),
        creator("c4", tags="fitness, 10k+", chat_id="", status="invited"),
        creator("c5", tags="fitness, 10k+", status="opted_out"),
    ]
    ready, not_joined = resolve_targets("tags: fitness, 10K+", creators)
    assert [c.get("id") for c in ready] == ["c1"]
    assert [c.get("id") for c in not_joined] == ["c4"]
    ready, _ = resolve_targets("any: food, fitness", creators)
    assert [c.get("id") for c in ready] == ["c1", "c2", "c3"]


def test_resolve_targets_by_ids_and_all():
    creators = [creator("c1"), creator("c2"), creator("c3", chat_id="")]
    ready, not_joined = resolve_targets("c2, c3 c9", creators)
    assert [c.get("id") for c in ready] == ["c2"]
    assert [c.get("id") for c in not_joined] == ["c3"]
    assert len(resolve_targets("ALL", creators)[0]) == 2
    assert resolve_targets("", creators) == ([], [])


def test_render_message_fills_placeholders_and_appends_deadline():
    campaign = Row(2, {"name": "Nike launch", "message": "Hey {name}, {campaign} is live! {unknown}", "deadline": "Friday"})
    text = render_message(campaign, creator("c1", name="Priya"), TZ)
    assert text == "Hey Priya, Nike launch is live! {unknown}\n\nDeadline: Friday"


def test_render_message_default_template():
    text = render_message(Row(2, {"name": "Spotify"}), creator("c1", name="Ali"), TZ)
    assert text.startswith("Hi Ali! We're running a new campaign: Spotify.")
    assert "Deadline" not in text


def test_parse_sheet_datetime_text_and_serial():
    dt = parse_sheet_datetime("2026-10-16 15:00", TZ)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.tzinfo) == (2026, 10, 16, 15, TZ)
    # 46311.625 is 2026-10-16 15:00 as a Sheets serial number
    serial = parse_sheet_datetime("46311.625", TZ)
    assert (serial.month, serial.day, serial.hour, serial.minute) == (10, 16, 15, 0)
    assert parse_sheet_datetime("15", TZ) is None
    assert parse_sheet_datetime("next friday", TZ) is None


def test_display_value():
    assert display_value("46311", TZ) == "Fri 16 Oct"
    assert display_value("46311.625", TZ) == "Fri 16 Oct, 3:00 pm"
    assert display_value("end of month", TZ) == "end of month"


def test_store_pause_and_resume(tmp_path):
    store = Store(str(tmp_path / "bot.db"))
    store.pause("c1", 60)
    assert store.is_paused("c1") and not store.is_paused_manually("c1")
    store.pause("c1")
    assert store.is_paused_manually("c1")
    store.resume("c1")
    assert not store.is_paused("c1")
    store.pause("c2", -1)
    assert not store.is_paused("c2")


def test_store_assignments_and_escalations(tmp_path):
    store = Store(str(tmp_path / "bot.db"))
    store.add_assignment("k1", "c1", 101)
    store.add_assignment("k2", "c1", 102)
    store.update_assignment("k2", "c1", "declined")
    assert [a.campaign_id for a in store.active_assignments("c1")] == ["k1"]
    assert store.campaign_for_brief_message("c1", 102) == "k2"
    store.update_assignment("k1", "c1", "posted", "https://x.com/p/1")
    assert store.get_assignment("k1", "c1").post_link == "https://x.com/p/1"

    esc = store.add_escalation("c1", "k1", "Can I post on Sunday?")
    assert store.open_escalation("c1").id == esc
    store.answer_escalation(esc, "Yes")
    assert store.open_escalation("c1") is None
    assert store.get_escalation(esc).answer == "Yes"


def test_store_history_order_and_threads(tmp_path):
    store = Store(str(tmp_path / "bot.db"))
    for i in range(5):
        store.add_history("c1", "creator", f"m{i}")
    assert [t for _, t in store.recent_history("c1", limit=3)] == ["m2", "m3", "m4"]
    store.set_thread("c1", 77)
    assert store.creator_for_thread(77) == "c1"
    store.forget_thread("c1")
    assert store.get_thread("c1") is None


def test_knowledge_base_includes_campaign_details():
    kb = _knowledge_base(
        [Doc("f", "FAQ", True, text="We pay within 30 days.")],
        [CampaignContext("k1", "Nike", "Fri 16 Oct", "Hi Priya!", "accepted", [Doc("b", "Brief", True, text="Use #justdoit")])],
    )
    assert "We pay within 30 days." in kb
    assert '<campaign id="k1" name="Nike">' in kb and "Use #justdoit" in kb and "Deadline: Fri 16 Oct" in kb
    assert "no active campaigns" in _knowledge_base([], [])
