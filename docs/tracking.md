# Tracking, follow-ups and digest (Module 07)

Once a day (and on `/today`) the bot reads the sender Gmail account since the last successful
run, notices which drafts you sent, matches replies by Gmail thread ID, handles bounces and
do-not-contact requests, drafts **one** follow-up after 7 days of silence, marks people and
jobs Ghosted after 14 more days, labels the threads, checks both Gmail tokens and sends one
Telegram report. It never sends mail.

## Rules

- **Never send.** Follow-ups are drafts in the original thread, like Module 06.
- **One follow-up per contact, ever.** Never to `Do not contact`, `Bounced` or `Replied`
  contacts, and never while a reply waits for your decision. The Notes line
  `follow-up drafted <date>` makes sure a second one is never made, even if you delete the
  first draft.
- **Status only moves forward** (below). `Offer`, `Rejected`, `Withdrawn`, `Declined`,
  `Ghosted` and `Expired` are never changed by the run.
- **The LLM is advisory.** A reply classified below Config `tracking.min_confidence` (0.8)
  changes nothing; it comes to Telegram as a card with buttons.
- **Window:** from `bot_state["tracking.last_success"]` minus one day (14 days on the first
  run). The marker moves only after a run without errors, so a failed day is never lost.
- **Labels are applied, never created.** A missing label is reported. No label changes in
  DRY_RUN. The names are in `config/base.yaml` under `tracking.labels`:
  `JobSearch/Replies`, `JobSearch/Applications`, `JobSearch/Interviews`. Create them in both
  Gmail accounts.
- **Deleting old contacts needs your tap,** and happens only in prod (Notion trash).

## The daily run

0. Token health: both Gmail tokens are refreshed. A failure is the first line of the report;
   when the sender token fails, steps 2 to 8 are skipped and the report is still sent.
1. Window (above), Gmail query `after:<epoch> -in:chats`.
2. Sent detection, for Contacts with Status `Drafted`: the draft still exists (nothing to do);
   the draft is gone and its thread has a `SENT` message (Status `Contacted`, `Last
   contacted` = send date, draft ID cleared, the job's `Last activity date` set); the draft is
   gone and nothing was sent (you deleted it: back to `Verified`, or `Unverified` when it was,
   IDs cleared, Notes `draft deleted <date>`). The same for a follow-up draft: sent means
   `Followed up` (and the job `Followed up` when it was `Applied`).
3. Replies in cold-mail threads: a message after your last sent message that is not yours, not
   an auto-reply (`Auto-Submitted`, `X-Autoreply`, subjects like `Automatic reply` or
   `Out of office`) and not a bounce. The contact becomes `Replied` (or `Do not contact` for an
   opt-out), a pending follow-up draft is reported as no longer needed, the job moves per the
   table below, and the thread gets `labels.replies`.
4. Job-level threads (`Gmail thread ID` on the job): new inbound messages are classified and
   applied; label `labels.applications`, plus `labels.interviews` for an interview.
4b. Confirmation mails (Autopilot A, 26 Oct): a mail in the window that says "Thank you for
   applying", "We have received your application" (and the Polish and Dutch ways to say it;
   not when it also says "unfortunately" or "not moving forward") and names the company of a
   job you approved (`Approved` or `Resume built`) marks that job `Applied` (Applied date = the
   mail's date, its thread linked, `labels.applications`), also when you applied outside the
   bot and never tapped `I applied`. With several jobs at that company the mail must name the
   role; when it still fits more than one, the report asks you to tap `I applied` yourself.
   The report gets a card with a `Contacts: <company>` button per job, so contacts and mails
   follow.
4c. Rejection mails (Autopilot I4, 26 Oct): a mail in the window that says "Unfortunately",
   "not moving forward", "other candidates" and the like (also Polish "niestety" and Dutch
   "helaas") and names the company of a job you applied to (`Applied` up to `Interview`) sets it
   `Rejected`, also when it came outside the linked thread. Several fits are listed for you.
   Contacts of a job that is `Rejected` (or otherwise closed) get no follow-up any more.
5. Linking: for `Applied` jobs without a thread, messages since the Applied date from the
   company domain (Target Companies `Domain`, or a domain you gave in Telegram) or a known ATS
   (`tracking.ats_sender_domains`) whose subject or snippet names the company. One thread is
   linked; several come to Telegram with `Link` and `Not this`.
6. Bounces from `mailer-daemon@` or `postmaster@`: `X-Failed-Recipients`, or the
   `Final-Recipient` line of the delivery report. The contact becomes `Bounced`.
7. Follow-ups: `Contacted`, silent for Config `followup.days` (7) days, no follow-up yet. Made
   from the approved `followup` template as a reply in the same thread (`In-Reply-To`,
   `References`, `Re:` subject), with the signature and no attachment. Not in DRY_RUN.
8. Ghosted: a contact `Followed up` and silent for Config `ghosted.days` (14) days; a job
   `Applied` or `Followed up` for `followup.days + ghosted.days` days whose contacts are all
   `Ghosted`, `Bounced` or `Do not contact` (or it has none).
9. Retention: contacts that never replied, found more than Config `contacts.retention_months`
   (12) months ago, are listed with one `Delete N old contacts` button. `Do not contact` rows
   are kept so they are never mailed again.
10. The marker is written and the report sent.

## Claude API cost in the digest

The weekly digest has a line with the month's Claude API cost so far, what it went on, the
pace for the whole month and last month's total, for example:

```
AI in October so far: about $1.50 in 214 calls (screening $0.90, resumes $0.60); on pace for about $4.65 this month. Last month: about $3.10.
```

Every LLM call adds its estimated cost (the same prices as the "AI used" lines, half price for
batches, web searches at $10 per 1,000) to Bot State `llm.cost_month`
(`src/jobengine/track/costs.py`). A new month starts from zero. It is an estimate from the
token counts: the Anthropic console (and Google's, for Gemini) has the exact bill.

## Instant reply ping

Between daily checks, a reply should not wait until the next morning. Every
`tracking.reply_check_minutes` (15, `config/base.yaml`) the inbox is searched once for new
mail since the last check (`src/jobengine/track/ping.py`), and each new message is sent to
Telegram at once when it is:

- in the thread of a mail you sent to a contact (Contacted, Followed up or Replied), or
- in a job's application thread, or from the company's own domain of a job you applied to
  (Applied, Screening, Interview).

```
📩 New reply from Piotr Example (Vistula Cloud)
From: piotr.example@vistula.example.com
Subject: Re: DevOps Engineer on your team at Vistula Cloud
Hi Alex, Thanks for reaching out. Could we set up a technical interview next week? ...
It may be about an interview or a call: answer soon.
The daily check records it; send /today to record it now.
```

- Nothing is written to Notion and no AI is used; the daily check (or `/today`) still
  classifies and records the reply as before.
- Each message is pinged once (Bot State `tracking.pinged`: the last message IDs and the
  time of the last check; the first check looks back one day).
- Automatic replies, bounces, your own mails and mail from anyone else are never pinged.
- A Gmail or Notion failure is only logged; the daily check reports token problems.
- Long polling checks it with the 5-minute loop; Cloud Run gets `POST /tasks/replies` from
  Cloud Scheduler job `je-replies-prod` ([deploy.md](deploy.md) B4).

### Draft answer (flow feature 3)

When the new part of the reply asks for a call, your notice period, salary, visa or work
permit, start date or relocation, the ping ends with a **draft answer** to copy into your
Gmail reply (`src/jobengine/track/reply_draft.py`):

- One sentence per question, in that order, from `reply_drafts` in `config/base.yaml` (a
  Notion Config key `reply.<name>` wins), filled with your Apply pack answers
  (`apply.interviews`, `apply.notice_period`, `apply.expected_salary`,
  `apply.earliest_start`, `apply.relocation`) and the job country's permit line. A call ask
  offers the next three working days.
- Only the new part of their mail is read: your own quoted mail is cut first.
- An answer that is not set is left out (that question stays for you); a mail that asks none
  of these gets no draft. No AI, nothing sent: edit it and send it yourself.


Autopilot D (26 Oct): when the reply may be about an interview or a call, the ping is followed by the `/prep` pack for that job (likely questions with hints from your resume, your gaps, questions to ask them): "🎯 Prepare for it now:". Once a day per job (Bot State `autoprep.done`); one AI call; a failure only loses the pack, never the ping.
## Status rules

Job order: `New < Screened < Approved < Resume built < Applied < Followed up < Replied <
Screening < Interview < Offer`. `Rejected`, `Ghosted`, `Withdrawn`, `Declined`, `Expired` are
terminal.

| Event | Job | Contact |
|---|---|---|
| reply `positive` or `neutral` | `Replied` if `Applied` or `Followed up` | `Replied` |
| reply `screening` | `Screening` | `Replied` |
| reply `interview` | `Interview` | `Replied` |
| reply `offer` | `Offer` | `Replied` |
| reply `rejection` | `Rejected` | `Replied` |
| reply `opt_out` | no change | `Do not contact` |
| application confirmation (`ack`) | `Last activity date` only | none |
| follow-up sent | `Followed up` if `Applied` | `Followed up` |

## Contacts memory: Replied, Mails sent, Dead end

All contacts stay in the one Contacts database (DEV and prod); **Source** says which site
gave the email (Apollo, Hunter, Snov, Prospeo, Tomba, GitHub, Job posting), and Notes say
"found with Hunter-2" for a second account. Three fields remember how each person answered:

- **Mails sent**: every first mail and follow-up the daily run sees sent, for all jobs. A new
  draft for someone you mailed before (another job at the company) counts too when you send
  it; deleting that draft only clears it.
- **Replied** (checkbox): ticked when the person answers (any reply but an opt-out). They are
  then reused first for the company's next jobs, however long ago they were found, and are
  never marked Dead end or listed for deletion.
- **Status "Dead end"**: after `dead_end_after` (5) mails with no answer. A Dead end contact
  is never drafted again and never picked from the cache; it is kept so it is never found
  and mailed again. A late answer still turns it into Replied. Notion Config
  `contacts.dead_end_after` changes the number.

## Reply classification

Only the subject, the sender's domain and the new part of the reply (quoted history cut,
at most 3000 characters) go to the LLM (stage `score`). The answer must quote the text, or its
confidence is set to 0. Before any LLM call, auto-reply headers give `auto_reply` (ignored) and
the phrases in `tracking.opt_out_phrases` (`do not contact`, `remove me`, `unsubscribe`, ...)
give `opt_out` with confidence 1.

## Daily report and failure mails (Autopilot H1 and H3)

Every evening at 21:45 India time the bot sends **📊 Daily report** in Telegram and the same
text by email. It comes even when nothing ran, so a stopped bot shows the same day:

```
📊 Daily report, Thu 01 Oct
🔎 Fetch: 2 run(s) (07:02, 19:31), 16 new job(s) saved. Found: Email alerts 38, Adzuna 9, Company sites 2.
🧪 Screening: 16 screened (2 apply high, 5 apply normal, 9 skip).
📄 Resumes: 3 built, keyword match on average 8 of 12 (lowest 6 of 12).
👥 Contacts found: 4. ✉️ Gmail drafts: 3, mails sent: 1. 📩 Replies: 1.
💰 AI today: about $0.12. AI in October so far: about $2.10 ...
💳 Credits: Hunter 3/50 ...
🔑 Gmail token m02: 5 day(s) left
📌 Waiting for you: 4 job(s) in /pending, 2 need a description (/jd).
```

On a day with no fetch and no screening it starts with `Nothing ran today: 0 new jobs, 0
screened.` `/report` shows today's report at any time.

**Warnings** (Autopilot H2) come first, each starting with ⚠️, when today looks wrong next to
the last 7 days of the day ledger:

- a scheduled autopilot time (07:00, 19:30) passed its 3-hour window without a run;
- a source that usually finds 3 or more jobs a day found 0 today (alert parser, Gmail or the
  site broken);
- the skip rate is 25 points or more above the week's (at least 5 screened today);
- the resume keyword match is 15 points or more below the week's;
- a Gmail token stops within 2 days, or has expired (local);
- a contact provider has no credit left this month on any of its accounts.

A new install has no week to compare with: the source, skip and match warnings start once
the ledger has days.

The numbers come from the day ledger (`src/jobengine/track/day_ledger.py`, Bot State
`day.ledger`, the last 8 days): every /fetch, screened row, resume, Gmail draft, sent mail,
contact found, reply and AI cost adds to today's entry while it runs. Bot State `report.sent`
keeps the day of the last report, so it is sent once a day (also after a restart).

Config (all optional): `schedule.report` (`false` turns it off), `schedule.report_time`
(default `21:45`), `schedule.report_timezone` (default `Asia/Kolkata`). The report is sent at
that time or up to 3 hours after it (long polling checks every few minutes; on Cloud Run the
`je-report-prod` Scheduler job calls `POST /tasks/report`).

**A failed scheduled run** (Autopilot, the afternoon check, the mail queue, the reply check,
the daily report, any `POST /tasks/*`) is also mailed to you at once, with the error line and
what to send Claude Code (the error and the last 40 lines of the bot log, never the .env or a
token). The same failure is mailed at most once an hour (Bot State `alerts.mailed`).

Both mails go to your own address only (`safety.redirect_to`, `safety.owner_mail_target`),
from the sender mailbox, also when `DRY_RUN` is true (`src/jobengine/alerts.py`). If the mail
cannot be sent, the Telegram report says why.

## Weekly spot check (Autopilot H5)

With the autopilot building resumes and cover letters by itself you no longer check each
one, so every Sunday at 20:00 India time (with the weekly digest) the bot sends **2 of the
week's approved resumes**, picked at random (the newest revision of each job,
`src/jobengine/track/spot_check.py`):

- the resume PDF, its keyword match line and the cover letter saved on the job's page;
- **👍 Looks fine** and **⚠️ Something is wrong**. Something is wrong asks what, in a reply
  to that message ("says 5 years of Terraform, I have 3"); the note is kept in Bot State
  `spotcheck.notes` (the last 30).

`/spotcheck` sends a spot check at any time; `/spotcheck notes` lists your notes: send them to
Claude Code when you have a few, so the cause gets fixed. Once a week (Bot State
`spotcheck.sent`), long polling and the Cloud Run digest task (`POST /tasks/digest`) alike.

## Weekly link check (Autopilot I1)

Once a week, from Sunday 09:00 India time (or the first day after, when the bot was off), the
bot opens the job link of every open job (Status Screened, Approved or Resume built) with the
same check as the Approve button (`apply/link.py` `closed_reason`): HTTP 404 or 410, a closed
posting text in English, Dutch or Polish, a schema.org `validThrough` in the past, or
Greenhouse's `?error=true`. A job whose Expires date has passed counts as closed without
opening the link. Closed jobs get Status Expired, so /pending only shows live jobs.

- LinkedIn and blocked sites are never opened. Anything unclear (a refused page, a time-out)
  counts as open.
- At most 80 links a week (one plain GET each); the rest wait for next week.
- One message lists what was marked: `🧹 Weekly link check: N of M open jobs are closed and now
  Expired:` with the reason for each. Wrong? Set the Status back in Notion.
- Notion Config `schedule.expired_check` = `false` switches it off (default on). Bot State
  `expired.check` keeps the date of the last check. On Cloud Run the Sunday digest task runs it.

## Weekly job board check (28 Oct, your request)

Every Sunday from 10:00 India time (or the first day after, when the bot was off) the bot runs
the job board check of `python -m jobengine.sweep --check-boards` by itself (`sweep/boards.py`;
it reads only and writes nothing to Notion) and compares it with last week's
(`sweep/board_watch.py`):

- 🔴 a board that worked and now fails (HTTP 404, a moved site);
- 🟠 a board that had jobs in Poland, the Netherlands or Ireland and now has none;
- 🟠 a board whose jobs fell to half or less (from 10 jobs or more);
- 🟢 a board that failed and works again.

A message comes only when something changed; the first run sends one short summary. The
weekly audit shows the same result. On by default only on the prod bot (after go-live);
Notion Config `schedule.board_check` = `true` or `false` overrides that. Bot State
`boards.weekly` keeps the last result. On Cloud Run the Sunday digest task runs it too.

## Weekly self-audit (27 Oct, your request)

Every Sunday from 20:00 India time (with the digest; on the first day after when the bot was
off) the bot writes one row in the Notion table **Weekly Audit**, with the full report in the
page, and sends a short summary with the link to Telegram. It cannot be switched off. `/audit`
writes it now. The first audit ever comes on the first Sunday evening after the deploy.

The report, counted from your own data with no AI:

1. **Scorecard**: applications this week against last week; the reply rate of the "settled"
   group (jobs applied 7 to 35 days ago, since replies take a week or more), interviews,
   rejections and ghosted; what the bot did each day (day ledger: new jobs, screened, Apply
   high or normal, resumes, contacts, drafts, mails sent, replies, AI cost).
2. **Where it went wrong this week** (🔴 serious, 🟠 to fix, 🔵 for information): approved
   jobs with a resume not applied after 2 days; jobs that expired after their resume was
   built; applications without any contact; Gmail drafts older than 3 days not sent; bounces
   by the source that gave the address; rejections, with how many came within 3 days (usually
   an automatic filter); resumes you marked wrong in the spot check; wrong answers in dry-run
   forms; days the bot did not run; and the warnings and errors the bot logged, the most
   frequent first.
3. **Done again and again without results** (last 4 weeks, this week left out): any country,
   board, screen verdict, sponsorship or role family with 8 or more applications and no
   reply; mails to one kind of person (recruiters, hiring managers, engineers) with 8 or more
   sent and no reply; 8 or more follow-ups unanswered; a source with 20 or more jobs and none
   Apply high or normal.
4. **What works**: the reply rate per group with at least 5 settled applications.
5. **Fetch and filters: jobs a filter may have got wrong** (28 Oct). Every `/fetch` keeps,
   for its week, what it read and dropped (`sweep/filter_watch.py`, Bot State
   `sweep.filter_weeks`). The audit lists: postings read and saved per source; the drop
   reasons; 🟠 titles in your countries that are not one of your titles but have a near word
   (cloud, infrastructure, platform, kubernetes, terraform ...), the ones a few changed words
   can hide; 🔵 DevOps-type titles left out by an excluded word (mostly seniority on purpose,
   sometimes a wrong hit like "Staff Augmentation"); 🔵 jobs dropped for too few of your
   skills; and 🟠 the other way round, saved jobs that were not your profile (screening said
   Skip, or you declined them), with their titles. The bot never changes the filters: Opus may
   suggest a title to add or a word to leave out, and you decide.
6. **Job boards (weekly check)**: the result of the Sunday board check above.
7. **Last week's actions**: the reply rate and applications before and after, and the
   Actions you wrote on last week's row.
8. **For Opus**: a ready prompt. Open the page, give it to Opus, then set the row's Status
   (New, Reviewed, Actions set) and write the Actions you decide. The next audit reports on
   them.

The morning check names an audit that is missing on Tuesday (a blocker: send `/audit`) and an
audit still New 3 days after its week ended.

The warnings and errors come from `track/issue_log.py`: every WARNING or ERROR of the bot's
own loggers is counted in memory under a short pattern (numbers, ids and links blanked) and
written to Bot State `audit.issues` at most every 30 minutes; counts of a run that stops
before that write are lost. Bot State `audit.done` keeps the last audited week.

Tables: Weekly Audit (DEV) in DEV Sandbox (`notion.write.weekly_audit` in dev and local).
There is no prod table yet: in prod the audit comes in Telegram only, with a DRY RUN line,
until you OK the prod table.

## Heartbeat (Autopilot H4)

The daily report and the failure mails come from the bot itself, so when the PC, the
container or Cloud Run is down, nothing comes. The heartbeat is the alarm from outside
(`src/jobengine/heartbeat.py`): after each daily report the bot pings your free
healthchecks.io check. When no ping comes for about 26 hours, healthchecks.io emails you.
A failed scheduled run also pings `<url>/fail` (at most once an hour), so the check turns
red at once.

Set it up once (about 2 minutes):

1. Sign up at healthchecks.io (free plan) with your own email.
2. Add a check named `Job Engine`: Period **1 day**, Grace time **2 hours**, time zone
   Asia/Kolkata. Its email integration is on by default.
3. Copy the check's ping URL (`https://hc-ping.com/...`). It works like a password: never
   paste it in a chat.
4. Local: put it in `.env` as `HEALTHCHECK_URL=` and restart the container. Cloud Run: create
   the secret `healthcheck-url` in Secret Manager (prod; the deploy mounts it when it
   exists).
5. The next daily report pings it; the check turns green.

Without `HEALTHCHECK_URL` nothing is sent. A ping is one GET to `hc-ping.com` (on
`safety.allowed_hosts`); a failed ping is only logged.

## Telegram

```
Daily check 2026-10-15 (since 2026-10-01 08:00)
Sent: 2 mails detected (Anna Example, Zofia Example (follow-up))
Replies: Piotr Example (Vistula Cloud) -> Interview
Applications: Canal Payments -> Rejected; Northwind Cloud linked
Bounced: 2 (bob.example@vistula.example.com, olga.example@northwind.example.org)
Follow-ups drafted: 2 (send them from Gmail > Drafts): Anna Example, Tomek Example
Ghosted: 1 contact, 1 job
Needs your call: 3 (see below)
Tokens: alerts OK, sender OK
```

Commands: `/today`, `/followups`, `/status` (now with job and contact counts), `/stats`,
`/sources`, `/health`, `/digest`, `/report`, `/spotcheck`. The weekly digest (Module 09 sends it on Sunday 20:00
Asia/Kolkata) compares the last 7 days with the week before and lists what waits for you.

It ends with **What gets replies (last 30 days)** (`track/stats.replies_report`, free, no
AI): replies and interviews by board, by country and by mail (referral ask to engineers,
cold mail to HR, hiring managers or mailboxes), best first, then a tip such as
`Put more effort where it works: board: most replies from Company site (67%), fewest from
Adzuna (0%)`. A tip needs at least 3 applications (or mails) in two groups; until then it
says there is not enough data yet. `/digest` shows it any time.

## Notion prerequisites

- Contacts (DEV): `Follow-up draft ID` (text) and the Status option `Ghosted` (done). Prod gets
  them in Module 09.
- Config: `followup.days` (7), `ghosted.days` (14), `contacts.retention_months` (12),
  `gmail.token_healthcheck`, `tracking.min_confidence` (0.8).
- Gmail: the three labels above, in both accounts.

## Run it

```bash
python -m jobengine.track daily --fake --now 2026-10-15T08:00:00+05:30   # fixtures
python -m jobengine.track digest --fake
python -m jobengine.track daily --no-write      # real data, report only, nothing written
```
