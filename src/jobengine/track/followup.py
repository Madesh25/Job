"""Follow-up drafts (spec section 5): the approved `followup` template, as a reply in the
original thread, with the Config signature and no attachment. Never sent by the bot."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from jobengine.config_store import ConfigStore
from jobengine.mail.compose import Mail, compose, signature
from jobengine.mail.fill import MailJob, fill_text, placeholder_values
from jobengine.mail.templates import Template, approved
from jobengine.safety import route_recipients
from jobengine.settings import Settings
from jobengine.track.models import Message

NOTE = "follow-up drafted"
DEV_PREFIX = re.compile(r"^\[[A-Z]+\] to .*? \| ")


class FollowupError(Exception):
    """The follow-up cannot be built. It is listed in the report and not drafted."""


@dataclass
class Followup:
    mail: Mail
    thread_id: str


def reply_subject(subject: str) -> str:
    return subject if subject.casefold().startswith("re:") else f"Re: {subject}"


def my_messages(thread: list[Message], mine: set[str]) -> list[Message]:
    return [m for m in thread if m.sent or m.sender in mine]


def build(contact: dict[str, Any], job: MailJob, thread: list[Message], mine: set[str],
          templates: dict[str, Template], config: ConfigStore, s: Settings) -> Followup:
    template = approved(templates, "followup")
    if template is None:
        raise FollowupError("the followup template is missing or not APPROVED")
    sent = my_messages(thread, mine)
    if not sent:
        raise FollowupError("no sent message in the thread")
    original, last = sent[0], sent[-1]
    if not last.message_id:
        raise FollowupError("the last sent message has no Message-ID")
    values = placeholder_values(job, contact.get("Name") or "", config, None)
    body = fill_text(template.body, values)
    subject = DEV_PREFIX.sub("", original.subject)  # routing adds the prefix again
    to, _cc, routed = route_recipients([contact.get("Email") or ""], [],
                                       reply_subject(subject), s)
    mail = compose(to[0], routed, body, signature(config), None)
    mail.message["In-Reply-To"] = last.message_id
    mail.message["References"] = last.message_id
    return Followup(mail=mail, thread_id=last.thread_id)
