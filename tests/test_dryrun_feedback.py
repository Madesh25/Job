"""/dryrun says at once that it runs and /end stops it (LOG-01, 24 Nov: a /dryrun sent at
12:39 gave no answer for minutes and looked stuck). Made-up forms and jobs."""

import pytest
from test_form_bot import FORM, SAVED, VALUES, _serve, browser
from test_telegram_bot import Clock, settings

from jobengine import telegram_bot as tb
from jobengine.apply import form_bot
from jobengine.apply.form_bot import Result
from jobengine.jobctl import CONTROL, Stopped
from jobengine.screen.desk import Reply, fake_desk
from jobengine.sweep.fakes import FAKE_TODAY


def test_dryrun_says_it_started_and_passes_the_stop(monkeypatch):
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    said = []
    d.notify = said.append
    got = {}

    def run(s, url, values, saved, **kw):
        got["stop"] = kw.get("stop")
        return Result(url=url, stopped="captcha")

    monkeypatch.setattr(form_bot, "dry_run", run)
    stop = object()
    d.dryrun_command("pl-clean", stop=stop)
    assert said[0].text == ("🧪 Dry run started: Vistula Cloud, DevOps Engineer. The bot opens "
                            "the form in a browser and fills it; it never submits. About 1 to 3 "
                            "minutes; /end stops it.")
    assert got["stop"] is stop
    said.clear()
    d.dry_run_job("pl-clean", submit=True)  # the auto-apply queue: no start message, no stop
    assert said == [] and got["stop"] is None


def test_end_stops_the_dryrun_command():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())

    def slow(args, stop=None):
        assert CONTROL.running == "/dryrun"
        CONTROL.request_stop()  # /end sent while it runs
        stop()
        return [Reply("not reached")]

    d.dryrun_command = slow
    [reply] = tb._reported("/dryrun", lambda: tb._dry_run_command(d, "pl-clean"), d)
    assert reply.text == "/dryrun stopped by /end. Nothing was sent."
    assert d.run_log_table.created[-1]["Result"] == "Failed"
    assert d.run_log_table.created[-1]["Failure"] == "stopped by /end"
    assert CONTROL.running is None


@browser
def test_each_step_can_stop_the_browser_run(tmp_path, monkeypatch):
    monkeypatch.setattr(form_bot, "SETTLE_MS", 200)
    steps = []

    def stop():
        steps.append(1)
        if len(steps) == 2:  # after the page opened
            raise Stopped("/dryrun")

    with pytest.raises(Stopped):
        form_bot.dry_run(settings(), "https://jobs.example.com/apply", VALUES, SAVED,
                         name="Northwind Cloud", folder=tmp_path, prepare=_serve(FORM),
                         stop=stop)
    assert len(steps) == 2
    result = form_bot.dry_run(settings(), "https://jobs.example.com/apply", VALUES, SAVED,
                              name="Northwind Cloud", folder=tmp_path, prepare=_serve(FORM),
                              stop=lambda: steps.append(1))
    assert not result.stopped and len(steps) == 2 + 4  # opening, opened, fields read, filled
