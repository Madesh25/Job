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
Claude API in October so far: about $1.50 in 214 calls (screening and replies $0.90, resumes $0.60); on pace for about $4.65 this month. Last month: about $3.10.
```

Every LLM call adds its estimated cost (the same prices as the "AI used" lines, half price for
batches, web searches at $10 per 1,000) to Bot State `llm.cost_month`
(`src/jobengine/track/costs.py`). A new month starts from zero. It is an estimate from the
token counts: the Anthropic console has the exact bill.

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
`/sources`, `/health`, `/digest`. The weekly digest (Module 09 sends it on Sunday 20:00
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
