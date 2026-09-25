"""Shared test helpers for the resume builder: the fake master for Alex Example."""

from jobengine.config_store import ConfigStore
from jobengine.resume.master import fake_blocks, load_master
from jobengine.resume.models import Plan, PlanRow

CONFIG = ConfigStore.fake()


def master():
    return load_master(fake_blocks(), CONFIG)


def plan(m, **changes):
    """The unchanged plan with some fields replaced. Rows are (label, [items])."""
    base = Plan.unchanged(m)
    data = base.model_dump()
    for key in ("skills_main", "skills_also"):
        if key in changes:
            changes[key] = [PlanRow(label=label, items=items).model_dump()
                            for label, items in changes[key]]
    data.update(changes)
    return Plan.model_validate(data)
