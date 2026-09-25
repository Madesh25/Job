# Resume builder (Module 04)

When you tap **Approve** on a job in `/pending`, the bot builds a one-page resume tailored to
that job from the Golden Master, checks it, renders the PDF and sends it to you as a preview.
You approve it, ask for a rebuild, or reply with a correction. On approval it saves the final
PDF (Google Drive, or `out/` in DRY_RUN), ticks the Resume Log row and gives you the apply link.

## What may change (and what never does)

Only two things change per job (Resume Build Spec section 1):

1. **Technical Skills tables**: remove, reorder, move items between the main table and "Also
   worked with", rename a label to a name that still contains it ("CI/CD" to "CI/CD
   Pipelines") or to an Active Term Map JD term for it, and add an item only when Skills
   Inventory has it as Production or Hands-on, or an Active Term Map row backs it.
2. **A few words inside Experience bullets**: at most +8 words per bullet and +25 in total,
   only backed terms plus small connector words (Config `resume.gate.connector_words`).

Everything else is byte-identical to the Golden Master: header (the links come from Config
`profile.*`), summary, companies, titles, dates, numbers, education, fonts, margins, spacing.
The integrity gate (`resume/gate.py`) enforces all of it and compares the rendered HTML with
both editable zones blanked against the master.

The Golden Master and all real resume text are read from Notion at runtime (the code block
starting with `<!DOCTYPE html>` on the Resume Build Spec page). Nothing real is stored in the
repo; tests use a synthetic resume for Alex Example.

## The flow

```
Approve in /pending
  -> "Building resume for <Company>..."
  -> plan (LLM stage tailor) -> integrity gate (up to 2 retries with the errors)
  -> render, measure, fit (one page, fill 88 to 96 percent)
  -> Resume Log row (one per revision, plan JSON in its body), Job Status "Resume built"
  -> PDF preview with [Approve resume] [Rebuild]
Reply to the preview with a correction ("drop Oracle") -> next revision
Approve resume -> re-render from the stored plan, save to Drive (or out/), tick Approved
  -> "Resume approved and saved. Apply here: <URL>" [I applied]
I applied -> Status "Applied", Applied date and Last activity date today
```

- A second Approve tap shows the latest preview; it does not build again.
- Approving an older revision answers "A newer revision exists" and shows the latest.
- A correction the rules do not allow ("add Istio") is answered with the reason, nothing is
  built.
- After Config `resume.max_revisions` (5) revisions the bot asks you to fix the rules
  (Skills Inventory, Term Map) or skip the job.
- Jobs with verdict Skip, or with a Status other than Approved or Resume built, are refused.

## Fit fallback

Fonts, margins and spacing are never changed and no bullet is ever dropped.

- **Overflow** (2 pages or fill above 96%): revert bullet edits, least important first, then
  remove the plan's low-relevance skill items (never below 5 main rows or 1 "also" row).
- **Underfill** (below 88%): restore removed skill rows, then removed items.
- Still not fitting: no PDF, and the message says why.

## Sections

From the Resume Sections table: Certifications (after Experience; needs a real date instead of
`<Month Year>`, and shrinks the job in Config `resume.shrink_job_when_certs` to the single
bullet `resume.ncr_short_bullet`), Languages, and the GDPR line (Poland only, 8pt italic,
centred). Projects are not supported yet: the build stops with a message asking you to untick
it.

## Files and Drive

- Name: Config `resume.filename` (`Madeshwaran_Devops_<Company>.pdf`). When the company already
  has an approved resume for another job, `_<Role>` is added (letters and digits only, role at
  most 40 characters).
- DRY_RUN (the default): the final PDF goes to `out/` and Resume Log `File` is
  `DRY RUN: out/<file>`. Nothing is uploaded.
- `DRY_RUN=false`: uploaded to the Drive folder Config `drive.resume_folder` (with ` (DEV)`
  outside prod) using the sender token with the `drive.file` scope only. Existing files are
  never overwritten (a ` (2)` suffix is added); nothing is deleted or shared.

## Running it

```bash
# Fake Golden Master (Alex Example), fake LLM, in-memory Notion and Drive
python -m jobengine.resume --fake --job fixture-clean-pl

# Real: render and show the changes for one job, write nothing
python -m jobengine.resume --job <page_id> --no-write

# Real: build the next revision with a correction
python -m jobengine.resume --job <page_id> --correction "drop Oracle"
```

In the fake bot (`python -m jobengine.telegram_bot --fake`), `tap ap:pl-clean` builds a
preview, previews are saved to `out/fake/`, and `reply <message number> <text>` replies to a
bot message (the message number is printed with each document).

## Rendering on Windows

WeasyPrint needs Pango, which Windows does not have by default. Render in WSL (Ubuntu) or
Docker:

```bash
sudo apt-get install -y fonts-lato libpango-1.0-0 libpangoft2-1.0-0
python -m jobengine.resume --fake --job fixture-clean-pl
```

The Lato font must be installed: if the PDF would use another font the build fails instead of
rendering in a fallback font. The pure tests (gate, sections, master, builder with a fake
renderer) run anywhere; the tests marked `render` are skipped when Pango or Lato is missing
(CI runs them with `REQUIRE_RENDER=1`).
