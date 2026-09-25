"""The Cold Mail Templates page (Notion) parsed into templates. Pure apart from loading.

Each template is a heading `<n>. <name> : APPROVED <date>`, then a `Subject:` paragraph, then
a code block with the body. A section without APPROVED in its heading is never used.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jobengine.settings import ROOT_DIR

if TYPE_CHECKING:
    from jobengine.notion_repo import NotionClient

FIXTURES = ROOT_DIR / "fixtures" / "mail"
HEADINGS = ("heading_1", "heading_2", "heading_3")
SUBJECT_PREFIX = "Subject:"
# Heading word -> template key, checked in this order.
KEYS = (("hiring manager", "hiring"), ("recruiter", "recruiter"), ("peer", "peer"),
        ("follow-up", "followup"), ("follow up", "followup"))
# Contacts Type -> template key.
TYPE_TEMPLATE = {"Peer engineer": "peer", "Recruiter/TA": "recruiter", "Hiring": "hiring"}
MARKDOWN_ESCAPE = re.compile(r"\\([{}_*`\[\]\\])")


class TemplateError(Exception):
    """The templates page cannot be read. The message is shown to you."""


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    approved: bool
    subject: str
    body: str


def rich_text(block: dict[str, Any]) -> str:
    inner = block.get(block.get("type") or "") or {}
    return "".join(p.get("plain_text") or (p.get("text") or {}).get("content", "")
                   for p in inner.get("rich_text") or [])


def unescape(text: str) -> str:
    """Notion markdown escapes, e.g. `\\{role\\}` -> `{role}`."""
    return MARKDOWN_ESCAPE.sub(r"\1", text)


def template_key(heading: str) -> str | None:
    lowered = heading.casefold()
    return next((key for word, key in KEYS if word in lowered), None)


def parse_templates(blocks: list[dict[str, Any]]) -> dict[str, Template]:
    """Templates by key. When a key appears twice, an approved section wins over one that is
    not approved, and the first approved one wins over later ones."""
    sections: list[tuple[str, list[dict[str, Any]]]] = []
    for block in blocks:
        if block.get("type") in HEADINGS:
            sections.append((rich_text(block).strip(), []))
        elif sections:
            sections[-1][1].append(block)
    out: dict[str, Template] = {}
    for heading, content in sections:
        key = template_key(heading)
        if key is None:
            continue
        subject = next((rich_text(b).strip() for b in content
                        if rich_text(b).strip().startswith(SUBJECT_PREFIX)), "")
        body = next((rich_text(b) for b in content if b.get("type") == "code"), "")
        name = heading.split(":", 1)[0].strip()
        template = Template(
            key=key, name=name, approved="APPROVED" in heading,
            subject=unescape(subject.removeprefix(SUBJECT_PREFIX).strip()),
            body=body.strip("\n"),
        )
        if key not in out or (template.approved and not out[key].approved):
            out[key] = template
    return out


def approved(templates: dict[str, Template], key: str) -> Template | None:
    """The template for `key` when it exists, is approved and has a subject and a body."""
    t = templates.get(key)
    return t if t and t.approved and t.subject and t.body else None


def load_templates(client: NotionClient, page_id: str) -> dict[str, Template]:
    from jobengine.resume.master import fetch_blocks

    if not page_id:
        raise TemplateError("Config has no notion.pages.cold_mail_templates page.")
    return parse_templates(fetch_blocks(client, page_id))


def fake_templates(path: Path = FIXTURES / "cold_mail_templates_page.json") -> dict[str, Template]:
    return parse_templates(json.loads(path.read_text(encoding="utf-8")))
