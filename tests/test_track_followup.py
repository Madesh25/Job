import pytest

from jobengine.config_store import ConfigStore
from jobengine.mail.fill import MailJob
from jobengine.mail.templates import fake_templates
from jobengine.settings import load_settings
from jobengine.track.fakes import FIXTURES, load_threads
from jobengine.track.followup import FollowupError, build, reply_subject

THREADS = load_threads()
JOB = MailJob("job-vistula", "Vistula Cloud", "DevOps Engineer", "Kraków", "Poland")
CONTACT = {"Name": "Tomek Example", "Email": "tomek.example@vistula.example.com"}
MINE = {"madeshwaranm02@gmail.com"}


def templates():
    return fake_templates(FIXTURES / "cold_mail_templates_page.json")


def test_reply_subject():
    assert reply_subject("Hello") == "Re: Hello"
    assert reply_subject("RE: Hello") == "RE: Hello"


def test_prod_followup_headers():
    s = load_settings("prod", {"DRY_RUN": "false"})
    f = build(CONTACT, JOB, THREADS["t-tomek"], MINE, templates(), ConfigStore.fake(), s)
    assert f.thread_id == "t-tomek"
    assert f.mail.message["To"] == "tomek.example@vistula.example.com"
    assert f.mail.message["Subject"] == "Re: DevOps Engineer role at Vistula Cloud Poland"
    assert f.mail.message["In-Reply-To"] == "<m-tomek-1@mail.example.com>"
    assert f.mail.body.startswith("Hi Tomek,\n\nJust following up on my note about the "
                                  "DevOps Engineer role at Vistula Cloud.")
    assert f.mail.attachment is None


def test_dev_prefix_is_not_doubled():
    s = load_settings("local", {"DRY_RUN": "false"})
    f = build({"Name": "Anna Example", "Email": "anna.example@vistula.example.com"}, JOB,
              THREADS["t-anna"], MINE, templates(), ConfigStore.fake(), s)
    assert f.mail.message["Subject"] == ("[LOCAL] to anna.example@vistula.example.com | Re: "
                                         "DevOps Engineer role at Vistula Cloud Poland")


def test_unapproved_followup_template_is_refused():
    s = load_settings("local", {})
    with pytest.raises(FollowupError, match="not APPROVED"):
        build(CONTACT, JOB, THREADS["t-tomek"], MINE, fake_templates(), ConfigStore.fake(), s)
