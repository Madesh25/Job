"""PR 15: the job link in the referral ask ({job_link})."""

import base64
import dataclasses
from email import message_from_bytes, policy

import pytest
from test_mail_drafter import deps_for, settings

from jobengine.config_store import ConfigStore
from jobengine.gmail_client import FakeGmail
from jobengine.mail import drafter
from jobengine.mail.fill import FillError, MailJob, fill, job_link, placeholder_values
from jobengine.mail.templates import Template, fake_templates

LINK_LINE = "This is the role: {job_link}"


@pytest.mark.parametrize("url, link", [
    ("https://jobs.example.com/devops?utm_source=adzuna&utm_medium=x&id=7",
     "https://jobs.example.com/devops?id=7"),
    ("https://boards.greenhouse.io/vistula/jobs/2?gh_src=abc",
     "https://boards.greenhouse.io/vistula/jobs/2?gh_src=abc"),
    ("https://www.linkedin.com/jobs/view/123/?trk=eml&refId=9",
     "https://www.linkedin.com/jobs/view/123/"),
    ("http://jobs.example.com/a#apply", "http://jobs.example.com/a"),
    ("", ""),
    ("jobs.example.com/a", ""),
    ("mailto:jobs@example.com", ""),
    ("javascript:alert(1)", ""),
])
def test_job_link(url, link):
    assert job_link(url) == link


def job(url="https://jobs.example.com/devops?utm_source=adzuna"):
    return MailJob(page_id="p", company="Vistula Cloud", role="DevOps Engineer", city="Krakow",
                   country="Poland", url=url)


def peer_with_link():
    peer = fake_templates()["peer"]
    return dataclasses.replace(peer, body=f"{peer.body}\n\n{LINK_LINE}")


def test_the_template_decides_where_the_link_goes():
    values = placeholder_values(job(), "Piotr Example", ConfigStore.fake(), "Terraform")
    assert values["job_link"] == "https://jobs.example.com/devops"
    filled = fill(peer_with_link(), values)
    assert filled.body.endswith("This is the role: https://jobs.example.com/devops")
    # A template without {job_link} is used word for word: no link is added.
    plain = fill(fake_templates()["peer"], values)
    assert "https://" not in plain.body


def test_no_link_no_referral_ask():
    values = placeholder_values(job(url=""), "Piotr Example", ConfigStore.fake(), "x")
    with pytest.raises(FillError, match=r"unfilled placeholder \{job_link\}"):
        fill(peer_with_link(), values)
    template = Template(key="peer", name="Peer", approved=True, subject="Hi",
                        body="Hello {first_name}")
    assert fill(template, values).body == "Hello Piotr"  # others are not affected


def test_drafts_carry_the_link(tmp_path):
    gmail = FakeGmail()
    deps = deps_for(settings("dev"), tmp_path, gmail=gmail)
    templates = {**fake_templates(), "peer": peer_with_link()}
    deps.templates = lambda: templates
    result = drafter.create_drafts(deps, "fixture-clean-pl")
    assert result.ok
    bodies = []
    for draft in gmail.drafts:
        message = message_from_bytes(base64.urlsafe_b64decode(draft["raw"]),
                                     policy=policy.default)
        bodies.append(message.get_body(("plain",)).get_content())
    with_link = [b for b in bodies if "This is the role: https://jobs.example.com/" in b]
    peers = [line for line in result.drafted if line.template == "peer"]
    assert len(with_link) == len(peers) > 0
