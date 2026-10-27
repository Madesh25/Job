# /autopilot

One Telegram command takes new postings all the way to Gmail drafts. In mail mode draft (the
default) nothing is sent: you read the drafts, send them yourself, apply on the job page and
tap **I applied**. In mail mode send (`/mailmode send`) the drafts that pass every check are
sent (see "Mail mode" in [gmail.md](gmail.md)).

## What it does

1. **Fetch**: the same sweep as `/fetch`.
2. **Screen at half price**: the free checks run first (their skips are written at once),
   then the other Unscreened jobs go to the AI as one half-price batch, within the daily
   screening limit (see "Half-price batch" in [screening.md](screening.md)).
3. **Wait for the answers**: Anthropic answers most batches within an hour (at most 24 hours).
   - Long polling (the bot on your laptop): the bot checks the batch every
     `screening.autopilot_check_minutes` (5) and carries on by itself. It keeps answering
     other commands in the meantime.
   - Webhook (Cloud Run): send `/autopilot` again later; it carries on without a new fetch.
4. **Approve**: the best Apply high and Apply normal jobs, in the `/pending` order, at most
   `screening.autopilot_approvals` (10) a day (Bot State `autopilot.day`). Apply low and
   Needs review are never approved by autopilot; they wait in `/pending`.
5. **For each approved job**:
   - Build the resume and save it, the same as tapping **Approve resume** (Drive in prod,
     `out/` when Drive writes are off).
   - Find contacts within this week's outreach budget ([outreach.md](outreach.md)): paid
     lookups only for jobs the budget covers, and only in prod with `DRY_RUN=false`. When the
     company's email domain is unknown, the bot asks; reply with the domain as usual.
   - Add your LinkedIn connections at the company (Autopilot B, from the 🤝 LinkedIn
     Connections table). A connection with an email who was not asked in the last 30 days
     becomes a Peer contact of the job and is marked Asked, so the drafts include the
     referral ask (the referral template, word for word). The others get a LinkedIn message
     to send yourself, in a message after the summary (the bot never opens LinkedIn). In mail
     mode send, no connection is mailed by itself: they are all listed as LinkedIn messages
     (Referral asks > Mail them still works by hand).
   - Write Gmail drafts, one per contact (DRY RUN: a preview, nothing created). In mail mode
     send, check them and send the ones that pass; a message per job lists what was sent and
     what was kept as a draft, and why.
6. **One summary**: the list of jobs with what was done for each, then per job its LinkedIn
   messages to your connections and its resume PDF with an **I applied** button.

A job whose resume cannot be built (for example no description yet: paste it with `/jd`)
goes back to `/pending` and does not use one of the day's approvals. A failure in one job
never stops the others.

## Sending it again

- While its batch works, `/autopilot` does not fetch or send a second batch; it only checks
  the batch and carries on when it has ended.
- Once the day's approvals are used, `/autopilot` still fetches and screens, and the new
  jobs wait for tomorrow's approvals (or for you in `/pending`).
- After a bot restart, the waiting run (Bot State `autopilot.run`) is picked up again.

## Twice a day by itself (scheduled)

`/autopilot` can run by itself, without a command. It is **off** until you set Notion Config
`schedule.autopilot` to `true` (set it back to `false` to stop it). Send `/autopilot when` to
see whether it is on and when it runs next.

- **When** (your decision of 25 Oct, Autopilot H6): at **07:00 and 19:30 India time, every
  day**, weekends too. Fetch, screening, resumes and contacts are then ready when you start
  applying at 22:00, and the weekend can be a catch-up apply day. Mails you send still go out
  at the recipient's working time (`/mailqueue`).
- **Change the times**: Notion Config `schedule.autopilot_time` (`HH:MM`, or several:
  `07:00, 19:30`). The days, the time zone and the window are in `config/base.yaml` under
  `screening.autopilot_schedule`.
- **Once per time**: the first check at or after a start time runs it (Bot State
  `autopilot.scheduled` keeps the date and the times done). A bot that was off then starts it
  up to `window_hours` (3) later, never after that: send `/autopilot` yourself then.
- **Long polling** (the bot on your laptop): checked every
  `screening.autopilot_check_minutes` (5), together with a waiting batch.
- **Cloud Run**: Cloud Scheduler job `je-autopilot-prod` calls `POST /tasks/autopilot` every
  30 minutes after each start time ([deploy.md](deploy.md) B4). The first call starts the
  run; later calls carry on with its half-price batch once it has answered. The call is
  answered at once and the run reports in Telegram (a failure too, also by email).
- It is the same run as `/autopilot`: at most `screening.autopilot_approvals` (10) approvals
  a day for both runs together, and the mail mode (`/mailmode`) decides between drafts and
  sending. The summary starts with "Scheduled autopilot (07:00 Asia/Kolkata)".

## Morning check before the run (Autopilot C)

At 06:30 India time (Notion Config `schedule.health_time`; `schedule.health` = `false` turns it
off) one message comes before the 07:00 run (`track/health_report.py`):

- ⛔ **Blocks the run, fix first:** a Notion table the bot cannot read (HTTP 404: share it with
  the integration), a Gmail token that does not work (`/gmailtoken`), an Anthropic key that is
  missing or refused, the `/fetch` gate closed (`/update`).
- ⚠️ **Needs you soon:** a Gmail token that stops within 2 days, `/update` due within 5 days, a
  contact provider with no credit left, the LinkedIn export older than 30 days, the autopilot
  schedule switched off.
- ✅ The rest: the Notion tables reachable, the `/fetch` gate days, credits, AI cost this
  month, the last `/fetch`.

Long polling checks it every few minutes; Cloud Run gets `POST /tasks/health`
([deploy.md](deploy.md)).

## Afternoon check (apply early)

Jobs posted after the morning run would wait until the next day, and the first applicants
get the most replies. With Notion Config `schedule.check` = `true` the bot runs `/fetch` and
then `/screen` by itself at each time in `schedule.check_times` (default `15:00`, in the
autopilot's time zone, India time since H6, on the autopilot days) and sends the summary with an Apply high card for each new strong match.
Nothing is approved, built or mailed by itself. Each time runs once a day, up to 2 hours after
it (a bot that was off then does not run it in the evening), and never while another
`/fetch`, `/screen` or `/autopilot` runs. `/autopilot when` shows it. On Cloud Run the
scheduler job `je-check-prod` calls `POST /tasks/check` ([deploy.md](deploy.md) B4).

## Apply nudge from 22:00 (Autopilot H7)

Your decision of 25 Oct: you start applying at 22:00 India time, and a gentle push helps.
`src/jobengine/track/nudge.py`.

- **22:00**: "🎯 N job(s) ready to apply, about M minutes" (ready = Status `Resume built`:
  the resume is done and you have not applied yet) with **🪫 Low (1 easy one)**, **🙂 OK (3)**
  and **⚡ High (all)**, plus **😴 Snooze 1 hour** and **✅ Done for today**.
- After the energy button: one job at a time (link, `/applypack` for the resume, Apply pack
  and autofill code, `/dryrun` to let the bot fill the form first). **I applied** brings the
  next one; **Not applying** asks why, then the next one.
- **Reminders** every 30 minutes until the goal is reached or you tap Done for today, at most
  5 an evening (until about 00:30). Snooze skips the next hour.
- **Streak**: evenings with at least one application in a row ("🔥 3-evening streak"). A
  missed evening is never mentioned; the next one simply starts a new streak.
- **Weekend**: the first message offers to catch up on everything left this week (High).
- Notion Config (all optional): `schedule.nudge` (`false` turns it off),
  `schedule.nudge_time` (default `22:00`), `apply.minutes_per_job` (default 10).
- Long polling checks every few minutes. Cloud Run: Scheduler job `je-nudge-prod` calls
  `POST /tasks/nudge` every 30 minutes from 22:00 to 00:30 ([deploy.md](deploy.md) B4).


## /help, Next lines and /next (Autopilot H)

- `/help` lists the commands in the five groups of the Command guide page in Notion, in the
  order of the day: Find and screen, Review and apply, Contacts and mails, Replies and
  interviews, Strategy, health and control. It fits in one Telegram message. The `/` menu
  starts with /start, /next, /status, /fetch and /autopilot, then follows the same groups.
- Commands that ended without a next step now end with `👉 Next: ...`: /credits, /outreach,
  /stats, /keywords, /connections, /agencies (the list), /mailmode (once a mode is set),
  /mailqueue, /digest, /report, /status, /answer and /answers. An answer that already says
  what comes next (buttons, its own "Next:", a question) gets none.
- `/next` adds an "Also due" part when a weekly or monthly step is due: /stats and /gaps a
  week after you last sent them, /keywords a month after, /update when strategy tips wait or
  /fetch closes within 5 days, and the LinkedIn export 30 days after the last one. Bot State
  `next.used` keeps the day you last sent /stats, /gaps and /keywords.

## Holiday mode: /pause and /resume (Autopilot I8)

`/pause 5d` (5 days) or `/pause 2026-11-05` (the day you are back; at most 60 days) turns it
on. While it is on:

- The autopilot and the afternoon check still fetch and screen, so nothing is missed, but
  the autopilot approves nothing and builds no resume. Its summary says how many Apply high
  and Apply normal jobs wait in `/pending`.
- No mail is sent by itself: the mail queue waits, the daily check writes no follow-up
  drafts, auto-apply and the evening apply nudge stop.
- Your own commands still work. `/pause` alone says when it ends.
- The morning check lists it under "Needs you soon".

It ends by itself on the day you are back (that day's first autopilot summary starts with a
welcome back), or at once with `/resume`. Bot State `holiday` keeps the day you are back.
