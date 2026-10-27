# Apply pack (PR 11)

When a resume is approved (the **Approve resume** tap, or `/autopilot`), the bot sends one
more message with ready answers for the job's portal form, and saves the same text on the
job's Notion page under `Apply pack (<date>)`. `/applypack <job URL or page id>` sends it again.
No AI: every answer is yours.

## What it holds

- **Work permit:** "Authorized to work without sponsorship? No", "Need visa sponsorship? Yes",
  and the country's line (Poland: type A work permit; Netherlands: Highly Skilled Migrant with
  a recognised IND sponsor; Ireland: General Employment Permit, no labour market test because
  DevOps is on the Critical Skills Occupations List).
- **Salary:** "Open, any range at or above the visa minimum", the visa minimum for that country
  (2026: Netherlands EUR 4,357 a month gross under 30; Ireland EUR 36,605 a year; Poland none),
  the salary the posting states, and "Current salary: Prefer not to say".
- **Notice and start:** 90 days, can be shortened to 30 to 40 days; start about 3 months after
  the offer, including the permit.
- **About you:** experience, education, English (C1, only where a level is required), other
  languages, relocation, interview availability, gender and other diversity questions, and
  the LinkedIn, GitHub and website links from Config `profile.*`.
- **Why this company:** "The role's focus on {detail} is close to my day-to-day work at
  Xerago.", with the job's own stored specific detail. Without one, it asks you to write it.
- **Resume for this job:** the headline and location line the resume used.

## Where the answers live

`config/base.yaml` under `apply_pack`. A Notion Config key `apply.<name>` wins over each one
(for example `apply.notice_period`), so you can change an answer without a code change. A
missing answer shows as `(not set: add Config apply.<name>)`, never a guess.

The visa minimums change every year (IND in January, DETE in March): update
`apply_pack.countries` then. The Irish Critical Skills Employment Permit needs EUR 68,911
without an ICT degree, so the pack uses the General Employment Permit line.

## The answer starter pack (28 Oct)

`/answers start` asks the questions forms ask most, once, one at a time in Telegram
(`apply/starter.py`): expected salary for the Netherlands, Ireland and Poland; total years of
experience; years with AWS, Azure, GCP, Kubernetes, Docker, Terraform, Ansible, Linux,
Python, Jenkins and GitLab; highest education; current title and employer; nationality; visa
status; languages; how you heard about a job; office or hybrid; travel; driving licence;
security clearance; interview availability. Reply with the answer, "skip" or "stop"; a
question waits one hour. Only open questions are asked, so `/answers start` again continues.

Each answer is saved like `/answer` (Bot State `apply.answers`) and as a Notion Answer bank
row (Source "You"), so the dry runs and auto-submit fill those fields from your own words.
The salaries are saved per country: a form for a Dutch job gets the Dutch salary, the Apply
pack and the autofill code list only that country's, and a job elsewhere keeps the profile
answer. Answer only what is true: a tool you never used is "0" or "skip".

## The resume header per job

`resume/header.py`, applied before the resume is rendered (the skills and bullets and their
integrity gate are unchanged):

- **Headline:** the whole headline becomes the job's title when the role names one of your
  approved titles (`resume.headline_titles`: DevOps Engineer, Site Reliability Engineer,
  Platform Engineer, Cloud Engineer, Infrastructure Engineer, Kubernetes Engineer; "SRE"
  counts as Site Reliability Engineer). It is only the title ("Cloud Engineer"), never
  "Cloud Engineer | Cloud & Platform Engineering". The approved title is used, never the
  posting's words, so "Senior Site Reliability Engineer (m/f/d)" gives "Site Reliability
  Engineer". Otherwise the headline stays as in your master.
- **Location line** (`p.reloc`): `resume.relocation_template`, one line, by default
  `Chennai, India | Open to relocate to {country}` (the job's country). Placeholders:
  `{country}`, `{city}` and `{place}` (city and country). A `\n` in the template (also in the
  Notion Config value) becomes ` | `, so the line never breaks in two.

Config keys `resume.headline_titles` and `resume.relocation_template` win when set.

## Cover letter (flow feature 4)

`apply/cover.py`. After Approve resume one AI call writes 150 to 200 words from the job
description, the approved resume PDF and your own answers (experience, notice period,
earliest start, relocation; `apply/facts.py`). Before it is shown: "N+ years" becomes
"N years", a sentence saying you can start immediately becomes your notice period, and the
letter is left out (with the reason) when it gives a year count that is not in your
experience answer or names a gap, also one short term of a gap ("Lambda" in "Dynamo, Lambda,
Bedrock") that your resume does not show (Phase 9, P9-23).

## Auto-apply dry run (Autopilot H8, step 1)

`apply/form_bot.py`, `/dryrun <job URL or page id>` (or `/dryrun <job> <form link>` when the
form is on another page). The bot opens the job's application form in a browser, fills it
and sends you a screenshot and the list of answers. **It never submits.** This is the dry run
of your decision of 25 Oct: rounds of dry runs until you say the answers are good; only then
a later step may submit.

- **Answers:** your saved answers (`/answer`) first, matched to the question's words, then
  your profile and Apply pack answers (the same as the autofill code: name, email, phone,
  links, permit, sponsorship, salary, notice, start, relocation, English, why this company).
- **AI for the rest (step 2, `apply/form_ai.py`):** the questions still open go to the AI in
  one call per form (Config `model.apply`; plan C: DeepSeek, Haiku when not set or when it
  fails). It gets your Apply pack facts, your saved answers (your own voice), your Skills
  Inventory, your resume text and the job, and must answer in your voice from those facts
  only, or give nothing. Before an AI answer is typed, code checks it: a year count must be
  in your experience answer ("N+ years" becomes "N years"), "immediately" becomes your notice
  period, a skill the job asks for that is not in your resume or Skills Inventory drops the
  answer, and a dropdown answer must be one of its options. AI answers are marked
  "🤖 AI from your facts: check it" in the report; a dropped one says why.
- A question nobody could answer stays empty and is listed with the `/answer` line to save
  it once; that is how your own natural answers are collected.
- **Dropdowns and radio buttons:** the option with the same text, else one starting with it,
  else Yes or No by the answer's first word. When no option matches it is left empty.
- **Left to you:** consent and privacy boxes (never ticked) and file fields other than the
  resume. The job's **approved resume** (the newest approved Resume Log revision) is
  attached to the resume field (step 4); without one, the field is listed for you.
- **Nothing is sent:** every request the page makes that is not a plain GET (autosave,
  tracking, the form itself) is blocked and counted in the report. No button is pressed, no
  key is pressed (`tests/test_repo_rules.py` checks the code).
- **Never opened:** LinkedIn, http, IP addresses and private hosts
  (`safety.assert_page_fetch_allowed`, checked for every request of the page too).
- **Captcha or login:** the dry run stops and says so; the bot does not go past it.
- **Screenshots:** `out/dryrun/`, deleted 3 days after you approve the run, 7 days when you
  do not review it, never saved in Notion.
- **Your review (step 3, `apply/form_review.py`):** under each report, **✅ Answers OK** and
  **❌ Something is wrong**.
  - Answers OK: each AI answer becomes one of your saved answers (the next form gets it
    without AI) and a row of the Notion **Answer bank** (Source "AI approved"). The bot
    counts the approved dry runs (the gate for auto-submit later).
  - Something is wrong: the bot lists the answers with numbers; reply `3 = the right answer`
    (saved as your answer and in the Answer bank, Source "Dry run correction") or one
    sentence ("it missed the visa question", "the form was not read"). Every reply becomes
    a row of the Notion **Auto-apply issues** table (Status "To fix") for the weekend fix.
  - The Answer bank's approved rows are read by every dry run, together with your `/answer`
    list (the `/answer` list wins on the same question). You can edit or add rows in
    Notion.
  - Tables: Answer bank (DEV) and Auto-apply issues (DEV) in the DEV Sandbox (Claude made
    them on 25 Oct). Prod gets its own two tables with your OK at go-live.

Setup on the machine that runs it (once): `pip install -e ".[formbot]"` then
`python -m playwright install chromium`. In Docker, build with
`docker build --build-arg FORM_BOT=true -t jobengine .` (about 400 MB more). Without it
`/dryrun` answers that Playwright is missing. `JOBENGINE_CHROMIUM` may point to a Chromium the
machine already has.

### The queue: laptop first, 3 forms at a time (step 4, `apply/queue.py`)

- `/autoapply` queues the jobs whose resume is built (Status `Resume built`) for a dry run,
  at most **50 a day** (Config `apply.daily_limit`; 30 until 27 Oct). With Config `apply.auto` = `true` the
  bot queues them by itself at every check. `/autoapply status` shows the queue.
- **Where the browser runs** (your idea of 25 Oct): the bot on your laptop (long polling) is
  the laptop worker: at every check it marks itself as seen and fills up to **3 forms at
  once** (one browser each) for free. The bot on Cloud Run asks **"💻 Is your laptop on?"**
  when jobs wait and the laptop was not seen in the last 10 minutes: **Yes** leaves them to
  the laptop for 15 minutes; **No**, no answer, or a laptop that has not taken them in 15
  minutes: Cloud Run fills them, also 3 at a time. A job taken by a worker that stopped is
  free again after 20 minutes. Cloud Run: Scheduler job `je-apply-prod` calls
  `POST /tasks/apply` ([deploy.md](deploy.md)); Cloud Run needs the image built with
  `FORM_BOT=true` (until then it answers that Playwright is missing and only the laptop
  fills).
- Every result is a normal dry run report with Answers OK and Something is wrong.
- **Auto-submit** (step 5 below) is closed until its gate opens; `/autoapply status` shows
  where it stands. Until then every form is a dry run.

### Auto-submit (step 5, 27 Oct, your OK; `apply/form_submit.py`)

The autopilot applies by itself on the forms it can fill completely; the rest come to you as
**📝 Apply by hand** with the reason, the link and the filled dry run (Answers OK / Something
is wrong, so the next form learns).

**The gate** (all of them, checked before every form): the prod bot with `DRY_RUN=false`;
Notion Config `apply.submit` = `true` (you set it); at least `apply.approvals_needed` (10) dry
runs approved and none marked wrong in the last 10; today's submits under
`apply.daily_limit` (**50** a day since 27 Oct, was 30). `/dryrun` never submits; only the
queue (`/autoapply`, or the autopilot with `apply.auto` = `true`) may.

**Each form is submitted only when every check passes:**
- every required question has an answer from your saved answers, the Answer bank or your
  profile;
- no AI answer you have not approved yet (a form that needed a new AI answer comes to you;
  once you tap Answers OK, that answer is yours and the next form with it can go);
- the resume field has the job's approved resume;
- a required consent box only when it is a privacy or data-protection one and Notion Config
  `apply.tick_privacy` = `true` (off unless you set it: your decision of 25 Oct was never to
  tick them); marketing, newsletter, talent-pool and "future roles" boxes are never ticked;
- exactly one visible Submit button (Submit, Apply, Send, Wyślij, Aplikuj, Solliciteer,
  Verstuur ...);
- no captcha and no login (the dry run already stops there; the bot never goes past one).

**After the press** the page is watched for up to 15 seconds for a confirmation ("thank you
for applying", "application received", "dziękujemy za aplikację", "bedankt voor je
sollicitatie" ...):
- **✅ Applied by the bot**: confirmed. The job is marked Applied with today's date, the
  screenshot of the confirmation comes to Telegram, and Autopilot A2 adds the company when
  Config allows it. Contacts and mails follow as for any applied job.
- **⚠️ No confirmation**: Submit was pressed but the page said nothing clear. The job is not
  marked Applied; the "thank you for applying" mail marks it (Autopilot A), or you check the
  form yourself.

Only while Submit is pressed does the page get to send (the guard opens `Sending` for that
form only); every dry run still blocks every request that is not a plain GET.
`tests/test_repo_rules.py` checks that `form_bot.py` presses nothing and that
`form_submit.py` presses exactly one button, after its checks. Bot State `apply.submitted`
counts today's submits.

## Form autofill (flow feature 9)

`apply/autofill.py`. The bookmark never submits anything; only `/dryrun` (above) opens a form.

- After each Apply pack comes one more message: the **autofill code** for that job, a line
  that starts with `JE1:`. It holds your answers for the job: name (from Config `profile.name`;
  Config `apply.first_name` and `apply.last_name` win), email, phone, links, town
  (`apply_pack.city`), permit, sponsorship, salary, notice, start, relocation, English, gender,
  the "why" line, the cover letter and your `/answer` list. A value that is not set is left
  out, so that field stays empty for you.
- `/autofill` sends the **bookmark** and how to add it once per browser (bookmarks bar, Add
  page, paste the `javascript:` message as the URL).
- On the form page (Greenhouse, Lever, Workday and most others) click the bookmark and paste
  the whole autofill message. It fills only **empty** fields whose label it recognises (your
  saved answers first, then the standard questions), chooses Yes/No and similar options in
  dropdowns and radio buttons, marks every filled field in orange, and says how many required
  fields are still empty. It never clicks a button and never submits; you check every field,
  attach the resume and press Submit yourself.
- A form shown inside a company page (an iframe from another site): the bookmark offers to
  open the form on its own page; click it again there.
- Workday has several pages: click the bookmark on each page; it offers the same code again
  for 2 hours in that tab.
- The code is at most 3,600 characters (one Telegram message): when your saved answers are
  many, the oldest are left out first, then the cover letter (it is in its own message).
