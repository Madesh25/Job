import base64
import json
from email import message_from_bytes, policy

import pytest

from jobengine.config_store import ConfigStore
from jobengine.mail.compose import (
    LONG_DASHES,
    ComposeError,
    body_html,
    check_attachment,
    compose,
    signature,
)
from jobengine.mail.templates import FIXTURES

SIGS = json.loads((FIXTURES / "signature.json").read_text(encoding="utf-8"))
PDF = b"%PDF-1.4 invented test bytes"
BODY = "Hi Ola,\n\nFirst paragraph with <b> & more.\nSecond line.\n\nLast one."


def test_body_paragraphs_and_escaping():
    html = body_html(BODY)
    assert html.count("<p>") == 3
    assert "&lt;b&gt; &amp; more.<br>Second line." in html
    assert "<a" not in html


def test_signature_links_only_config_urls_and_plain_phone():
    config = ConfigStore.fake()
    sig = signature(config)
    assert 'href="https://www.linkedin.com/in/alex-example/"' in sig.html
    assert 'href="https://alex.example.com"' in sig.html
    assert "+1 555 010 0000 |" in sig.html
    assert "tel:" not in sig.html
    assert sig.html.count("<a ") == 2
    assert "+1 555 010 0000 | https://www.linkedin.com/in/alex-example/" in sig.text
    assert "https://alex.example.com" in sig.text
    assert "<" not in sig.text


def test_extra_link_aborts():
    with pytest.raises(ComposeError, match="tracker.example.org"):
        signature(ConfigStore.fake(), SIGS["extra_link"])


def test_phone_link_aborts():
    with pytest.raises(ComposeError):
        signature(ConfigStore.fake(), SIGS["tel_link"])


def test_missing_profile_key_aborts():
    config = ConfigStore.from_values({"profile.name": "Alex Example"})
    with pytest.raises(ComposeError, match="profile.phone"):
        signature(config, SIGS["valid"])


def test_message_structure():
    mail = compose("madeshwaranm02@gmail.com", "[DEV] to ola@example.com | Hi", BODY,
                   signature(ConfigStore.fake()), ("Alex_Devops_Vistula.pdf", PDF))
    parsed = message_from_bytes(base64.urlsafe_b64decode(mail.raw()), policy=policy.default)
    assert parsed.get_content_type() == "multipart/mixed"
    assert parsed["To"] == "madeshwaranm02@gmail.com"
    assert parsed["From"] is None and parsed["Bcc"] is None and parsed["Cc"] is None
    alt, pdf = parsed.get_payload()
    assert alt.get_content_type() == "multipart/alternative"
    plain, html = alt.get_payload()
    assert plain.get_content_type() == "text/plain" and html.get_content_type() == "text/html"
    assert html.get_content_charset() == "utf-8"
    assert pdf.get_content_type() == "application/pdf"
    assert pdf.get_filename() == "Alex_Devops_Vistula.pdf"
    assert pdf.get_content() == PDF
    assert "Last one." in plain.get_content() and "Alex Example" in plain.get_content()
    assert "Attachment: Alex_Devops_Vistula.pdf" in mail.preview()


@pytest.mark.parametrize("dash", LONG_DASHES)
def test_long_dash_anywhere_aborts(dash):
    sig = signature(ConfigStore.fake())
    with pytest.raises(ComposeError, match="dash"):
        compose("a@example.com", "Hi", f"Body {dash} text", sig, None)
    with pytest.raises(ComposeError, match="dash"):
        compose("a@example.com", f"Role {dash} Company", "Body", sig, None)


def test_attachment_size_limit():
    check_attachment(b"x" * 250 * 1024, 250)
    with pytest.raises(ComposeError, match="over the 250 KB limit"):
        compose("a@example.com", "Hi", "Body", signature(ConfigStore.fake()),
                ("r.pdf", b"x" * (251 * 1024)), max_kb=250)
