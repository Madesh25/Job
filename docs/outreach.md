# Outreach budget (Module 10)

The sweep can find 100 jobs a week, but the contact providers' free credits (Apollo, Hunter,
Snov) cover only a few dozen lookups a month. So every approved job is **applied to**, and
**outreach** (the paid contact lookup and the cold mail drafts) goes only to the best-ranked
jobs the month's credits can cover.

## Two lanes

| | Apply lane (every approved job) | Outreach lane (limited) |
|---|---|---|
| Tailored resume, apply link, tracking | yes | yes |
| Contacts from the Contacts cache and emails in the job description (free) | yes | yes |
| Paid lookup (Apollo, Hunter, Snov) and the domain question | no | yes |
| Cold mail drafts | only to the free contacts found | yes (4 contacts, Config `contacts.mix`) |

## Who gets outreach

When you approve a resume, the bot ranks the job:

- **A**: Apply high, plus sponsorship stated, a Tier 1 or 2 target company, or the IND register
- **B**: other Apply high
- **C**: Apply normal
- **D**: Apply low, Needs review

A and B get outreach while this week's allowance lasts. C gets it only while more slots are
left than the reserve kept for Apply high jobs later in the week (a third of the allowance, at
least 1). D is apply-only. Every apply-only job gets a **Find contacts anyway** button; tapping
it (or sending `/contacts <job>`) runs the paid lookup and counts against the week.

## The weekly allowance

At the start of each week (ISO week) the bot takes the credits left this month
(`credits.apollo`, `credits.hunter`, `credits.snov` in Config, after the monthly reset),
divides each by the credits one job's lookup costs (Config `outreach.cost_per_job`, default
`apollo=4, hunter=1, snov=4`), adds them up, and spreads the result over the weeks left in
the month. Example: 75 + 25 + 50 credits on the 1st of a 31-day month is about 55 jobs, 11 a
week. The week's usage is kept in `bot_state["outreach.week"]`; approving the same job again
never spends a second slot. Outside prod the credit counters are simulated (never written),
so the estimate stays at the full monthly credits.

## Telegram

```
Outreach for Vistula Cloud: yes (priority A; 10 of 11 left this week).
Apply only for Example Corp: the last 2 slot(s) this week are kept for Apply high jobs. Free contacts (job posting, Contacts cache) are still used; no credits are spent.
  [Find contacts anyway]
```

`/outreach` shows this week's allowance, how much is used, the credits left and the rules.
