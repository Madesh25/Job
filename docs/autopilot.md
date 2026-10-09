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
   - Write Gmail drafts, one per contact (DRY RUN: a preview, nothing created). In mail mode
     send, check them and send the ones that pass; a message per job lists what was sent and
     what was kept as a draft, and why.
6. **One summary**: the list of jobs with what was done for each, then each resume PDF with
   an **I applied** button.

A job whose resume cannot be built (for example no description yet: paste it with `/jd`)
goes back to `/pending` and does not use one of the day's approvals. A failure in one job
never stops the others.

## Sending it again

- While its batch works, `/autopilot` does not fetch or send a second batch; it only checks
  the batch and carries on when it has ended.
- Once the day's approvals are used, `/autopilot` still fetches and screens, and the new
  jobs wait for tomorrow's approvals (or for you in `/pending`).
- After a bot restart, the waiting run (Bot State `autopilot.run`) is picked up again.

## Every morning by itself (scheduled)

`/autopilot` can run once a day without a command. It is **off** until you set Notion Config
`schedule.autopilot` to `true` (set it back to `false` to stop it). Send `/autopilot when` to
see whether it is on and when it runs next.

- **When**: Monday to Friday at 07:00 Europe/Warsaw (10:30 in India), the time European
  recruiters start their day, so your applications and mails are among the first. The time
  zone covers Poland and the Netherlands; Ireland is one hour behind.
- **Change the time**: Notion Config `schedule.autopilot_time` (`HH:MM`, Europe/Warsaw). The
  days, the time zone and the latest start time are in `config/base.yaml` under
  `screening.autopilot_schedule`.
- **Once a day**: the first check at or after the start time runs it (Bot State
  `autopilot.scheduled` keeps the date). A bot that was off in the morning starts it until
  the latest start time (10:00), never later: send `/autopilot` yourself after that.
- **Long polling** (the bot on your laptop): checked every
  `screening.autopilot_check_minutes` (5), together with a waiting batch.
- **Cloud Run**: Cloud Scheduler job `je-autopilot-prod` calls `POST /tasks/autopilot` every
  30 minutes in the morning ([deploy.md](deploy.md) B4). The first call starts the run; later
  calls carry on with its half-price batch once it has answered. The call is answered at
  once and the run reports in Telegram (a failure too).
- It is the same run as `/autopilot`: at most `screening.autopilot_approvals` (10) approvals
  a day, and the mail mode (`/mailmode`) decides between drafts and sending. The summary
  starts with "Scheduled autopilot (07:00 Europe/Warsaw)".

## Afternoon check (apply early)

Jobs posted after the morning run would wait until the next day, and the first applicants
get the most replies. With Notion Config `schedule.check` = `true` the bot runs `/fetch` and
then `/screen` by itself at each time in `schedule.check_times` (default `15:00`, Europe/Warsaw,
the autopilot days) and sends the summary with an Apply high card for each new strong match.
Nothing is approved, built or mailed by itself. Each time runs once a day, up to 2 hours after
it (a bot that was off then does not run it in the evening), and never while another
`/fetch`, `/screen` or `/autopilot` runs. `/autopilot when` shows it. On Cloud Run the
scheduler job `je-check-prod` calls `POST /tasks/check` ([deploy.md](deploy.md) B4).
