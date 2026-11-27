# Tracking, follow-ups and digest (Module 07)

Once a day (and on `/today`) the bot reads the sender Gmail account since the last successful
run, notices which drafts you sent, matches replies by Gmail thread ID, handles bounces and
do-not-contact requests, drafts **one** follow-up after 7 days of silence, marks people and
jobs Ghosted after 14 more days, labels the threads, checks both Gmail tokens and sends one
Telegram report. It never sends mail.

## Rules

- **Follow-ups are drafts in the original thread**, like Module 06. In mail mode draft you
  send them yourself. In mail mode send (5 Nov, the autopilot timetable) the bot also puts
  each follow-up draft in the mail queue and sends it after the same checks in the
  recipient's morning, **Tuesday to Thursday only** (the days cold mail gets the most
  replies), without an attachment; a follow-up you would send at night would arrive in
  their evening and sink. The next daily check sees the draft gone and records Followed up.
  The daily report says `Follow-ups queued, sent by the bot in the recipient's morning (Tue
  to Thu, /mailqueue): Anna Example (Thu 15 Oct 08:00 CEST)`. Notion Config
  `mail.followup_send` = `off` keeps them as drafts for you.
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

**When (5 Nov, the autopilot timetable):** by itself at **07:30 and 21:30 India time**,
once each a day (`track/timed.py`; Notion Config `schedule.tracking_times`, and
`schedule.tracking` = `false` turns it off), on the laptop (long polling) and on Cloud Run
(`je-daily-prod` at `30 7,21`). Before, the laptop ran it only on /today, and Cloud Run only
at 07:30. The 07:30 check records last night's applications and queues the day's
follow-ups; the 21:30 check, after the recipients' office closes, records the day's replies
and rejections before the 21:45 report and the 22:00 apply nudge. The once-a-day messages
of the Cloud Run task come with the 07:30 call only. `/autopilot when` names the times.
/today still runs it at any time.

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

## What works, in the weekly digest (S6, 23 Nov, your request)

The Sunday weekly digest ends with **📈 What works** (`src/jobengine/track/what_works.py`),
also sent at any time with `/stats works` (menu `/stats_works`). From every job in Notion it
counts found > approved > applied > answered (and interviews), split by source (Board),
country and screening verdict, at most 6 lines each (the rest folded into "others"), with the
share approved. "Speed" is the median number of days from posting (else the day /fetch found
the job) to applying, and how many were applied within 2 days. Two tips: the source with the
most approvals for what it finds (at least 10 found), and the sources with at least 30 found
and none approved: candidates to drop (FETCH 11). LOG-15 (24 Nov): the tip says the threshold
and each source's count ("Jooble (31 found): candidates to drop (30 found or more)"), and a
source with 10 to 29 found and none approved is named as "None approved yet: Pracuj.pl (29
found); a source is a candidate to drop from 30 found." "Others" is several small sources
added up, so it is never named.

## Reply analysis, in the weekly digest (S9, 23 Nov, your request)

Every reply the daily check reads is classified as before (see "Reply classification"); each
one is now also counted per day in four groups (`src/jobengine/track/reply_analysis.py`, day
ledger `reply.<group>`): **interview** (a screening call, an interview, an offer),
**rejection** (a no, or "do not contact"), **automatic** (a confirmation or an out-of-office)
and **question** (a positive or neutral answer, a question, anything else).

The Sunday weekly digest gets **📬 Replies**: this week's replies by group, then, from every job
you applied to, which resume headline (your approved title the job's role names, the same rule
as the resume header) and which skills the jobs asked for (their Tech stack, at least 2
applications each, the best 6) got answers: "- DevOps Engineer: 2 of 3 answered (67%), 0
rejected". A last line names the headline with the most answers, or, after 10 or more
applications and no answer, says to look at the headline and the first lines of the resume.

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

**Send it in their morning (5 Nov, the autopilot timetable):** an answer you send at 23:00
India time arrives in the recipient's evening and sinks. A ping with a draft answer has two
buttons: **✉️ Send in their morning** writes the answer as a Gmail draft in the same thread
(you can still edit it in Gmail) and queues it for the recipient's next window (08:00 to
11:00 their time, first in the queue; at once when their morning is now); **Send now**
writes it and sends it at once, for an urgent answer such as an interview time. Both go
through the same checks as every mail (Gmail's copy, To, subject, signature, no
attachment) and count in the daily cap; in DRY RUN nothing is written. The daily check
records the thread as before.

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
- **LOG-18 (25 Nov, your rules): a new person after a silence or a no.** A **Ghosted** contact
  (no answer 14 days after the follow-up) is never picked from the cache or drafted again, so
  the company's next job gets a new person. A reply saying they **cannot help** or are not the
  right person (reply class `cant_help`, also a "Can't help" button on a low-confidence card)
  makes the contact **Dead end** with a note, even after an earlier friendly answer; the job's
  own status is not changed. A late answer from a Ghosted person still turns them Replied.

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
📊 Today: Found 16 · Screened 16 · Resumes 3 · Forms 0 · Mails 1 · Replies 1 · Problems 0
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
- The date is written when the check ends (23 Nov: the bot was stopped 35 s into Monday's
  check, and the date written at the start made it wait a week). A check that fails with an
  error is marked too (no retry every few minutes); a check cut by stopping the bot runs again
  after the restart.

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
5. **Fast rejections** (#12, 28 Oct, `track/rejections.py`). A rejection within 3 days of
   applying is usually an automatic filter. The fast rejections of the last 8 weeks are
   compared with the other applications of that time; a cause is named (🔴, with what to
   try) when 3 or more fast rejections exist, at least half share it and it is 20 points
   more common than in the other applications: 4+ years asked, 2+ missing skills, Polish or
   Dutch needed, no sponsorship sign, a Senior title, a weak screening verdict, one country
   or one job board. Up to 8 of the jobs are listed with their years, country, verdict and
   gaps.
6. **Fetch and filters: jobs a filter may have got wrong** (28 Oct). Every `/fetch` keeps,
   for its week, what it read and dropped (`sweep/filter_watch.py`, Bot State
   `sweep.filter_weeks`). The audit lists: postings read and saved per source; the drop
   reasons; 🟠 titles in your countries that are not one of your titles but have a near word
   (cloud, infrastructure, platform, kubernetes, terraform ...), the ones a few changed words
   can hide; 🔵 DevOps-type titles left out by an excluded word (mostly seniority on purpose,
   sometimes a wrong hit like "Staff Augmentation"); 🔵 jobs dropped for too few of your
   skills; and 🟠 the other way round, saved jobs that were not your profile (screening said
   Skip, or you declined them), with their titles. The bot never changes the filters: Opus may
   suggest a title to add or a word to leave out, and you decide.
7. **Job boards (weekly check)**: the result of the Sunday board check above.
8. **Last week's actions**: the reply rate and applications before and after, and the
   Actions you wrote on last week's row.
9. **For Opus**: a ready prompt. Open the page, give it to Opus, then set the row's Status
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

## Run Log (18 Nov, your request)

One Notion row per scheduled run, so the days after go-live show what ran, when, for how long,
how it went and what it did. A run is one scheduled step on your PC (`telegram_bot._scheduled`:
Autopilot, Mail queue, Reply check ...) or one Cloud Run task (`web/app.py` `desk_tasks`).
Since 20 Nov also /fetch, /screen and /autopilot sent by hand: Step is the command ("/fetch
10:11"), and one stopped by /end is Failed with "stopped by /end".

While a run goes on, the numbers the day ledger records (new jobs, screened, applied, mails
sent, AI cost) and the bot's own WARNING and ERROR lines are counted for it in memory
(`track/run_log.py`), so the Run Log costs no Notion read. When it ends one row is written:

- Result: **OK**, **Problems** (an error was logged) or **Failed** (the step stopped with an
  error, or a Cloud Run task answered `ok: false`);
- Step, Where (PC or Cloud Run), Started, Seconds, the numbers, Errors, Warnings;
- Summary: the first 4 lines of what the run reported; Failure: why it failed.
- The row's page holds the full report, one line per paragraph (keys and emails
  blanked). 23 Nov: /fetch sends Telegram a short message (new jobs per country, own job
  links found, closed jobs, seen before, not for you, and "For you": jobs waiting for /jd,
  jobs Notion refused, no alert email read, sources not checked) ending with
  "Full report in Notion: Run Log, row /fetch HH:MM"; the long report is that page.
- S10 (23 Nov, your ask): the same for the other long messages. Telegram gets the short one
  ending with "📝 Full report in Notion: Run Log, row <name>"; the row's page holds the full
  one (and what else the run sent that has no full report of its own):
  - the daily check (07:30, 21:30 and /today): the counts in one line (sent, replies and
    interviews, applications updated, bounced, follow-ups, ghosted), then only what needs
    you: failed tokens, "Needs your call", the notes, the first error. The cards follow as
    before;
  - the daily report (21:45 and /report): the day's line, the warnings and what waits for
    you. The email copy is still the full report;
  - the morning check: what blocks the run and what needs you soon in full, the checks that
    passed as one count ("✅ 5 other check(s) passed.");
  - /health: "🩺 Health: N problem(s), M check(s) OK", each problem, and the Environment line;
  - the weekly digest (Sunday and /digest): the week in numbers (its first block) and the
    one-line tip of each analysis ("Put more effort where it works", "Most approvals for
    what it finds", "Many found, none approved", "Most answers");
  - the autopilot summary (scheduled and /autopilot): the counts and only the jobs that need
    you (no resume built, or not approved); the resumes, apply packs and questions follow
    as before;
  - each dry run report (/dryrun and the auto-apply queue, 3 forms at once): what is left
    for you (❗ and ❓ fields, the /answer line, blocked requests); the list of filled
    fields is in the page and the screenshot shows them;
  - the weekly audit was already short (its report is the Weekly Audit page).

  /today, /report, /health, /digest and /dryrun now get a Run Log row too (Step is the
  command). Where no Run Log row is written (no Run Log table), the full message comes as
  before. In the page, a status word after "token:" or "key:" (OK, FAILED, missing) is
  kept; a real key or token is still blanked.

A run that did nothing (most ticks: "not due yet") writes no row. Email addresses, keys,
tokens and the query part of links are blanked. Writing the row never fails the run.

Sites blocked: the sites that refused the bot (403) or asked it to slow down (429) in the run,
each site once (LOG-09, 24 Nov: every refused request was counted, 64 against the 30 the page
listed). A `/fetch` writes the number its page lists under "Blocked us (N)".

Tables: Run Log (DEV) (`notion.write.run_log` in dev and local) and Run Log in prod.

## Daily scorecard (S1, 23 Nov, your request)

One line a day that says what the bot did, so a testing day after go-live is reading one
line (`src/jobengine/track/scorecard.py`). It opens the daily report (line 2 above):

```
📊 Today: Found 19 · Screened 11 · Resumes 2 · Forms 1 · Mails 0 · Replies 0 · Problems 1
❌ Failed or with errors: Link check 10:15 (Run Log has why)
```

- Found: new jobs saved by /fetch. Screened: every verdict. Resumes, Mails, Replies: as in the
  report. Forms: forms the form bot filled (dry run or auto-apply; a captcha or login stop is
  not counted).
- Problems: runs whose Run Log Result is Failed or Problems; the ❌ line names them with
  their time, the same name as their Run Log row, where the reason is.
- The same numbers are one row a day in Notion **Daily Scorecard** (`notion.write.
  daily_scorecard`; Daily Scorecard (DEV) under DEV Sandbox in dev and local), updated after
  every run that did something or had a problem. An idle tick writes nothing.
- A scorecard problem is only logged: it never stops the run it counts.

## Error Log (18 Nov, your request)

One Notion row per kind of problem per day (`track/error_log.py`), so you can look back and
ask Claude Code to "read the Error Log rows with Status New and fix them".

- What counts: every ERROR and WARNING line of the bot's own loggers, and the site problems
  the bot logs as INFO because the run goes on (403, 429, 404/410, timeouts, 5xx): Level
  Error, Warning or Site problem.
- One kind: an error by its message (links, ids and numbers blanked); a warning or a site
  problem by the line in the code that logged it and its likely cause. "careers page not
  read" for 30 sites is one row, Count 30, with the sites in Example.
- Each row: Problem, Day, First seen, Last seen, Count, Stage (by the module that logged it),
  Level, Likely cause (Rate limited (429), Refuses the bot (403), Key expired or wrong (401),
  Page gone (404), Timeout, Network, Server error (5xx), Bot bug, Other), Where, Status (New),
  Example (emails, keys, tokens and link queries blanked) and Key (do not edit).
- Written at the end of every scheduled run (PC and Cloud Run), so short Cloud Run tasks lose
  nothing; since 20 Nov also at the end of every command (/fetch, /screen ...), every 5
  minutes while one runs (a /fetch that hangs still leaves its rows) and when the bot is
  stopped (Ctrl+C, docker stop): the rows of the last 8 days are read once, today's row of a problem gets the new
  count, a new one is added. Your Status, Notes and Fix PR are never overwritten. A row
  written again the same day keeps the sites or companies it named and adds the new ones
  (20 Nov).
- A new kind of error or warning (no row in the last 7 days) comes to Telegram once, the
  others of the same run in the same message (at most 5 named). Site problems never ping.
- Writing the rows or the message never fails the run; rows that could not be written are
  kept for the next run.
- Not a problem (19 Nov): a retry still going on (a Telegram message sent on the second try,
  a page asked again), and the DRY RUN lines saying what the dry run did not do (they do not
  count as warnings in the Run Log either). Lost Telegram polls are a Warning only after
  12 in a row ("Telegram not reached 12 times in a row"); a single lost poll is not.
- Sites in Example are host names from the line (small letters); "Page.goto" in a browser
  error is no site.

Tables: Error Log (DEV) (`notion.write.error_log` in dev and local) and Error Log in prod.

## Report a problem from Telegram (S2, 23 Nov, your request)

When a bot message is wrong, reply to it with `wrong: what is wrong` (or `bug: ...`), or send
`/bug what is wrong`. The bot saves one Error Log row (`src/jobengine/track/reports.py`):
Problem "You: <your words>", Level **Reported by you**, Stage Reported, Likely cause Your
report, Status New, and in Example your words, then the message you replied to with its time
(emails, keys, tokens and link queries blanked). No logs or screenshots need pasting: Claude
Code reads the rows with Status New and fixes them like the bot's own problems. A report never
pings (you sent it). Without a reply the row says no message was attached; `/bug` alone answers
how to write one. When the Error Log cannot be written (DRY RUN for Notion, or Notion down) the
answer says "Not saved" and why.

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
