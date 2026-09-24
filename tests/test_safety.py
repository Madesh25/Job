import pytest

from jobengine.safety import (
    SafetyError,
    check_startup,
    gmail_write_allowed,
    notion_write_target,
    paid_api_allowed,
    resolve_model,
    route_recipients,
    telegram_text,
)
from jobengine.settings import load_settings

FAKE_SANDBOX = {
    "job_opportunities": "11111111-1111-4111-8111-111111111111",
    "contacts": "22222222-2222-4222-8222-222222222222",
    "resume_log": "33333333-3333-4333-8333-333333333333",
}
PROD_JOB_OPPORTUNITIES = "0228ce56-475a-4b60-8d6b-fd2a59297b24"


def dev(**updates):
    base = load_settings("dev", {}).model_copy(update={"notion_write": dict(FAKE_SANDBOX)})
    return base.model_copy(update=updates)


def prod(**updates):
    return load_settings("prod", {}).model_copy(update=updates)


def test_check_startup_passes_for_local():
    check_startup(load_settings("local", {}))


def test_check_startup_passes_for_dev_with_sandbox_ids():
    check_startup(dev())


def test_check_startup_passes_for_prod():
    check_startup(load_settings("prod", {}))


def test_check_startup_fails_when_dev_writes_to_prod_id():
    s = dev(notion_write={**FAKE_SANDBOX, "job_opportunities": PROD_JOB_OPPORTUNITIES})
    with pytest.raises(SafetyError, match="production"):
        check_startup(s)


def test_check_startup_fails_on_placeholder():
    s = dev(notion_write={**FAKE_SANDBOX, "contacts": "<sandbox ID from Part B>"})
    with pytest.raises(SafetyError, match="placeholder"):
        check_startup(s)


def test_check_startup_fails_when_dev_uses_prod_sender():
    s = dev(gmail_sender="madeshwaran.manikam@gmail.com")
    with pytest.raises(SafetyError, match="sender"):
        check_startup(s)


def test_check_startup_fails_when_prod_uses_dev_sender():
    with pytest.raises(SafetyError, match="sender"):
        check_startup(prod(gmail_sender="madeshwaranm02@gmail.com"))


def test_check_startup_fails_when_prod_forces_model():
    with pytest.raises(SafetyError, match="force_model"):
        check_startup(prod(force_model="claude-haiku-4-5"))


def test_route_recipients_dev_redirects_and_drops_cc():
    to, cc, subject = route_recipients(
        ["a@example.com"], ["b@example.com"], "Hello", dev()
    )
    assert to == ["madeshwaranm02@gmail.com"]
    assert cc == []
    assert subject == "[DEV] to a@example.com, b@example.com | Hello"


def test_route_recipients_local_uses_local_label():
    _, _, subject = route_recipients(["a@example.com"], [], "Hi", load_settings("local", {}))
    assert subject == "[LOCAL] to a@example.com | Hi"


def test_route_recipients_prod_unchanged():
    to, cc, subject = route_recipients(["a@example.com"], ["b@example.com"], "Hello", prod())
    assert to == ["a@example.com"]
    assert cc == ["b@example.com"]
    assert subject == "Hello"


def test_gmail_write_allowed_follows_dry_run():
    assert gmail_write_allowed(dev(dry_run=True)) is False
    assert gmail_write_allowed(dev(dry_run=False)) is True
    assert gmail_write_allowed(prod(dry_run=True)) is False
    assert gmail_write_allowed(prod(dry_run=False)) is True


@pytest.mark.parametrize("provider", ["apollo", "hunter", "snov"])
def test_paid_api_allowed_only_in_prod_without_dry_run(provider):
    assert paid_api_allowed(provider, dev(dry_run=False)) is False
    assert paid_api_allowed(provider, dev(dry_run=True)) is False
    assert paid_api_allowed(provider, load_settings("local", {"DRY_RUN": "false"})) is False
    assert paid_api_allowed(provider, prod(dry_run=True)) is False
    assert paid_api_allowed(provider, prod(dry_run=False)) is True


def test_resolve_model():
    assert resolve_model("claude-opus-4-1", dev()) == "claude-haiku-4-5"
    assert resolve_model("claude-opus-4-1", prod()) == "claude-opus-4-1"


def test_notion_write_target():
    assert notion_write_target("target_companies", dev()) is None
    assert notion_write_target("target_companies", prod()) == "064bf98d-ab49-498d-9b94-2ecc41640230"
    assert notion_write_target("contacts", dev()) == FAKE_SANDBOX["contacts"]


def test_notion_write_target_never_returns_prod_id_outside_prod():
    s = dev(notion_write={**FAKE_SANDBOX, "job_opportunities": PROD_JOB_OPPORTUNITIES})
    assert notion_write_target("job_opportunities", s) is None


def test_telegram_text_prefix():
    assert telegram_text("hi", dev()) == "[DEV] hi"
    assert telegram_text("hi", load_settings("local", {})) == "[LOCAL] hi"
    assert telegram_text("hi", prod()) == "hi"
