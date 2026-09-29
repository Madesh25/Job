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
