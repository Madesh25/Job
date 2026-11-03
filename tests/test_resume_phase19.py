"""Phase 19 (3 Nov), the resume: "Add skills" after "Add 8 keywords" lost the 8, a skill in
your Skills Inventory was listed as "not in your skills", and a gap you added with "Add
skills" stayed in the job's Gaps."""

from types import SimpleNamespace

from jobengine.reference import Reference, Skill
from jobengine.resume import builder
from jobengine.resume.models import Plan, PlanRow

REF = Reference(skills=[Skill("1", "Kubernetes", "Production"),
                        Skill("2", "CloudFormation", "Hands-on"),
                        Skill("3", "CDK", "Hands-on")])


class Master:
    def all_items(self):
        return ["Kubernetes", "Docker"]


CTX = SimpleNamespace(master=Master(), reference=REF)


def test_a_rebuild_keeps_what_the_last_version_added():
    previous = Plan(skills_main=[PlanRow(label="Cloud", items=["Kubernetes", "Docker",
                                                               "CloudFormation", "Istio"])],
                    skills_also=[PlanRow(label="Also", items=["Perl"])],
                    forced_skills=["Perl"])
    # CloudFormation: yours. Perl: your "Add anyway". Istio: neither, so it is not kept.
    assert builder.added_skills(previous, CTX) == ["CloudFormation", "Perl"]


def test_gaps_you_own_now_leave_the_job():
    gaps = "CDK; CloudFormation; Datadog; Perl; experience with large scale systems design"
    assert builder.owned_removed(gaps, REF, ["Perl"]) == (
        "Datadog; Perl; experience with large scale systems design")
