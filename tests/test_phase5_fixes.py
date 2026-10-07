"""Phase 5 fixes (7 Oct): Approve resume never waits on the shared out folder, /restart says
so when a job is stuck, a register page that drops every sponsor changes nothing, and an
expired Gmail token is told in Telegram once a day."""

import time

import pytest
from test_end_restart import FakeTelegram, update
from test_resume_builder import JOB, make
from test_telegram_bot import Clock, settings

from jobengine import telegram_bot as tb
from jobengine.resume import builder
from jobengine.screen import desk as desk_module
from jobengine.screen.desk import fake_desk
from jobengine.settings import load_settings
from jobengine.strategy import ind_refresh
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import ping

S = load_settings("local", {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "111222333"})


@pytest.fixture(autouse=True)
def no_stuck_saves():
    builder.STUCK_SAVES.clear()
    yield
    builder.STUCK_SAVES.clear()


def test_approve_resume_goes_on_when_the_out_folder_does_not_answer(tmp_path, monkeypatch):
    deps, _ = make(tmp_path)
    out = builder.build_resume(deps, JOB)
    monkeypatch.setattr(builder, "LOCAL_SAVE_SECONDS", 0.2)

    def slow(deps, name, pdf):
        time.sleep(1)
        raise OSError("never reached")

    monkeypatch.setattr(builder, "_save_local", slow)
    started = time.monotonic()
    result = builder.finalise(deps, out.log_id)
    assert time.monotonic() - started < 1
    assert result.status == "approved"
    assert deps.resume_log.get(out.log_id)["File"] == (
        "DRY RUN: not saved locally (the out folder did not answer in 0 s); the PDF is in "
        "Telegram")
    assert builder.STUCK_SAVES == ["Alex_Devops_VistulaCloud.pdf"]


def test_an_existing_pdf_is_never_written_over(tmp_path):
    deps, _ = make(tmp_path)
    (tmp_path / "Alex_Devops_VistulaCloud.pdf").write_bytes(b"open in a viewer")
    out = builder.build_resume(deps, JOB)
    builder.finalise(deps, out.log_id)
    assert (tmp_path / "Alex_Devops_VistulaCloud.pdf").read_bytes() == b"open in a viewer"
    assert (tmp_path / "Alex_Devops_VistulaCloud_2.pdf").exists()
    assert builder.free_name(tmp_path, "x") == "x"


def test_restart_is_refused_while_a_save_is_stuck():
    builder.STUCK_SAVES.append("stuck.pdf")
    restarted = []
    # the second batch is the one "confirm /restart" reads
    fake = FakeTelegram([[update(7, "/restart")], [], [update(8, "/help")]])
    tb.run(S, tb.TelegramClient(fake), max_polls=3, background=True,
           restart=lambda: restarted.append(1))
    assert restarted == []
    assert fake.sent[1] == f"[LOCAL] {tb.STUCK_RESTART}"
    assert "Job Engine commands" in fake.sent[-1]  # the bot kept running


def companies(verified):
    return [ind_refresh.Company(row_id=f"r{i}", name=f"Gone {i} B.V.", region="Netherlands",
                                tier=None, ind_sponsor="Verified") for i in range(verified)]


def test_a_register_page_that_drops_every_sponsor_changes_nothing():
    deps = ind_refresh.fake_deps(load_settings("prod", {}), writer=lambda *a: pytest.fail())
    deps.companies = lambda: companies(28)
    report = ind_refresh.refresh(deps)
    assert report.changes == [] and not report.written
    assert report.text().startswith("IND register check failed: 28 of 28 Verified companies "
                                    "would become Not listed at once (")
    assert report.text().endswith("Nothing was changed.")


def test_a_normal_register_run_says_how_many_names_were_read():
    report = ind_refresh.refresh(ind_refresh.fake_deps(load_settings("local", {})))
    assert report.names > 0
    assert f"({report.names} names read from the register.)" in report.text()


def test_an_expired_gmail_token_is_told_once_a_day(monkeypatch):
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())

    def expired(track, now):
        raise RuntimeError("('invalid_grant: Token has been expired or revoked.')")

    monkeypatch.setattr(ping, "check", expired)
    monkeypatch.setattr(ping, "due", lambda track, now: True)
    first = [r.text for r in d.reply_ping_tick()]
    assert first == [desk_module.TOKEN_ALERT]
    assert d.reply_ping_tick() == []  # same day: once
    monkeypatch.setattr(ping, "check", lambda track, now: (_ for _ in ()).throw(
        RuntimeError("timeout")))
    assert d.reply_ping_tick() == []  # other errors stay in the log
