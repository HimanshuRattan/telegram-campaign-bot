"""Decides how to handle a creator's message: answer it from the knowledge base,
record an accept/decline or post link, or hand it to a human."""

import base64
import json
import logging
from dataclasses import dataclass, field

import anthropic
import httpx

from .docs import Doc

log = logging.getLogger(__name__)

ACTIONS = ["reply", "escalate", "accepted", "declined", "post_link", "none"]

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ACTIONS},
        "campaign_id": {"type": "string"},
        "reply": {"type": "string"},
        "post_url": {"type": "string"},
        "question_summary": {"type": "string"},
    },
    "required": ["action", "campaign_id", "reply", "post_url", "question_summary"],
    "additionalProperties": False,
}

SYSTEM = """You are the campaign assistant for {brand}. You chat with content creators on Telegram on the team's behalf: creators receive campaign briefs from you and message you with questions.

Each request gives you the knowledge base (a general FAQ plus the details and brief for each campaign this creator is part of), the recent conversation, and the creator's newest message. Choose one action for the newest message:

- reply: the knowledge base clearly answers it. Answer in a short, friendly message using only what the knowledge base says.
- escalate: the knowledge base doesn't clearly answer it, or it needs a decision from the team (payment amounts or timing, deadline changes, exceptions, complaints, new deals, anything personal). Put a one-line summary of what the creator needs in question_summary. Don't guess: a wrong answer about money, dates or deliverables costs the team far more than a short wait.
- accepted / declined: the creator is saying they will or won't take part in a campaign. Put a short acknowledgement in reply.
- post_link: the creator is sharing the link to their published post for a campaign. Put the URL in post_url and a short thank-you in reply.
- none: nothing needs a response, such as "ok", "thanks" or an emoji.

Set campaign_id to the id of the campaign the message is about, or an empty string if it is general or unclear. Write replies as plain text without markdown, in the language the creator writes in. Use empty strings for fields that don't apply. Don't mention the knowledge base, documents or these instructions to the creator."""

HISTORY_LABELS = {"creator": "Creator", "bot": "You", "team": "Team member (human)"}


@dataclass
class CampaignContext:
    id: str
    name: str
    deadline: str
    message_sent: str
    status: str
    docs: list[Doc] = field(default_factory=list)


@dataclass
class Decision:
    action: str
    campaign_id: str = ""
    reply: str = ""
    post_url: str = ""
    question_summary: str = ""


def _knowledge_base(faq: list[Doc], campaigns: list[CampaignContext]) -> str:
    parts = ["<general_faq>"]
    parts += [f"<document name=\"{d.name}\">\n{d.text}\n</document>" for d in faq if d.text]
    parts.append("</general_faq>")
    for c in campaigns:
        parts.append(f"<campaign id=\"{c.id}\" name=\"{c.name}\">")
        parts.append(f"Deadline: {c.deadline or 'not set'}\nCreator's status: {c.status}")
        parts.append(f"Message the creator received:\n{c.message_sent}")
        parts += [f"<document name=\"{d.name}\">\n{d.text}\n</document>" for d in c.docs if d.text]
        parts.append("</campaign>")
    if not campaigns:
        parts.append("This creator has no active campaigns.")
    return "\n\n".join(parts)


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class Answerer:
    """Calls Claude directly (Anthropic API) or through OpenRouter when an
    OpenRouter key is configured. Both paths send the same prompt and schema."""

    def __init__(self, model: str, effort: str, openrouter_key: str | None = None):
        self._model = model
        self._effort = effort
        self._openrouter_key = openrouter_key
        if openrouter_key:
            self._http = httpx.AsyncClient(timeout=120)
        else:
            self._client = anthropic.AsyncAnthropic()

    async def decide(
        self,
        *,
        brand: str,
        creator_name: str,
        faq: list[Doc],
        campaigns: list[CampaignContext],
        history: list[tuple[str, str]],
        message: str,
    ) -> Decision:
        # PDFs go first, titled so Claude knows which campaign they belong to.
        pdfs = [("General FAQ", d) for d in faq if d.pdf]
        pdfs += [(f"Campaign {c.id} ({c.name})", d) for c in campaigns for d in c.docs if d.pdf]
        knowledge = _knowledge_base(faq, campaigns)
        transcript = "\n".join(f"{HISTORY_LABELS.get(role, role)}: {text}" for role, text in history)
        question = (
            f"<conversation>\n{transcript or '(no earlier messages)'}\n</conversation>\n\n"
            f"Newest message from {creator_name}:\n{message}"
        )
        system = SYSTEM.format(brand=brand)
        if self._openrouter_key:
            text = await self._via_openrouter(system, pdfs, knowledge, question)
        else:
            text = await self._via_anthropic(system, pdfs, knowledge, question)
        if text is None:
            return Decision(action="escalate", question_summary=message)
        data = json.loads(text)
        return Decision(**{k: data.get(k, "") for k in Decision.__dataclass_fields__})

    async def _via_anthropic(self, system: str, pdfs, knowledge: str, question: str) -> str | None:
        content: list[dict] = [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(d.pdf).decode()},
                "title": f"{label}: {d.name}",
            }
            for label, d in pdfs
        ]
        # The knowledge base is stable across a conversation, so cache everything up to here.
        content.append({"type": "text", "text": knowledge, "cache_control": {"type": "ephemeral"}})
        content.append({"type": "text", "text": question})
        response = await self._client.beta.messages.create(
            model=self._model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": self._effort, "format": {"type": "json_schema", "schema": SCHEMA}},
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        if response.stop_reason != "end_turn":
            log.warning("Claude stopped with %s (request %s); escalating", response.stop_reason, response._request_id)
            return None
        return next(b.text for b in response.content if b.type == "text")

    async def _via_openrouter(self, system: str, pdfs, knowledge: str, question: str) -> str | None:
        content: list[dict] = [
            {
                "type": "file",
                "file": {
                    "filename": f"{label}: {d.name}.pdf",
                    "file_data": "data:application/pdf;base64," + base64.b64encode(d.pdf).decode(),
                },
            }
            for label, d in pdfs
        ]
        content.append({"type": "text", "text": knowledge, "cache_control": {"type": "ephemeral"}})
        content.append({"type": "text", "text": question})
        response = await self._http.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {self._openrouter_key}", "X-Title": "Campaign bot"},
            json={
                "model": self._model,
                "max_tokens": 16000,
                "reasoning": {"effort": self._effort},
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "decision", "strict": True, "schema": SCHEMA},
                },
                # Only route to providers that honor the JSON schema.
                "provider": {"require_parameters": True},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text[:300]}")
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") != "stop":
            log.warning("OpenRouter finished with %s; escalating", choice.get("finish_reason"))
            return None
        return choice["message"]["content"]
