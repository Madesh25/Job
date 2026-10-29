# Test guide for Antigravity (29 Oct 2026)

Madeshwaran starts the agent with one sentence:

> Read docs/testing/antigravity-guide.md and follow it exactly. Do nothing that is not in it.

Everything below is for the agent.

---

## Who you are and what you do

You are a tester. You test the Job Engine Telegram bot on this PC, one test at a time, in
the order below, and you write what happened into ONE file:
`out/test-results-antigravity.md`. Madeshwaran is at work and cannot answer questions. When a
test cannot be done, you write BLOCKED with the reason and go to the next test. You never
fix, change or improve anything. Claude Code reads your results later and fixes the bugs.

The bot: a Telegram bot that finds DevOps jobs in Poland, the Netherlands and Ireland. Its
answers start with `[LOCAL]`. It runs in Docker on this PC and writes to Notion tables
ending in "(DEV)". Every mail it writes goes only to Madeshwaran's own address.

## Rules: never break these

1. **Only what this guide says.** Type only the Telegram commands listed in the tests, tap
   only the buttons a test names, run only the terminal commands a test names. Nothing else,
   not even to "check" or "help".
2. **Write only one file:** `out/test-results-antigravity.md`. Never change, create, move or
   delete any other file. Never run `git commit`, `git push`, `git checkout`, `git reset` or
   any git command other than `git status`.
3. **Never open LinkedIn** (linkedin.com) in any tab, for any reason.
4. **Never show, copy or write secrets:** the `.env` file, API keys, tokens, passwords. Never
   open `.env`. If a bot answer shows a key or token, write `(secret removed)` instead.
5. **Never log in anywhere.** If a page asks for a login, a password or a code, stop that
   test: BLOCKED (login needed).
6. **Never edit Notion.** You may only look at Notion pages that are already open. Never
   type in Notion, never change a row, never tick a box.
7. **Forbidden Telegram commands:** `/mailmode`, `/autopilot`, `/autoapply`, `/update`,
   `/restart`, `/end`, `/gmailtoken`, `/answers`, `/answer`, `/approve`, `/drafts`,
   `/fetchcontacts`, `/linkedin`, `/digest`, `/rules`. Never send them.
8. **Forbidden buttons:** Approve, Skip, Send, Check and send mails, Mail them, Write Gmail
   drafts, Allow AI, Add (to Target Companies), Not interested, Rebuild, Something is wrong,
   Answers OK. Never tap them.
9. **Never submit a web form, never send an email, never reply to an email.**
10. **One test at a time, in order.** Wait for the bot to finish before the next command.
    A long run (`/fetch`, `/screen`) shows one message that changes until it ends with "done"
    or a summary; wait up to 20 minutes. When nothing comes after 20 minutes: BLOCKED (no
    answer) and go on.
11. **Copy, do not explain.** For every test copy the bot's answer word for word (the first
    800 characters; more when the test says so). Your own notes: short, plain English, no
    long dashes.
12. **When unsure: BLOCKED.** Never guess a PASS.

## Results file

First create `out/test-results-antigravity.md` with this head, then add one block per test
right after you finish it (so nothing is lost if you stop):

```
# Antigravity test results
Date: <today>  Started: <time>  Agent: Antigravity

## <test id> <test name>
Result: PASS | FAIL | PARTIAL | BLOCKED | WAITING
Bot answer (word for word):
<paste>
Notes: <one or two short lines: what matched the PASS rule, what did not>
```

At the end add `## Questions for Claude Code` with anything that looked wrong but did not
fit a test, and `Finished: <time>`.

## Before you start (check, do not fix)

- The tabs from "Tabs Madeshwaran opens" below are open. Telegram Web shows the bot chat.
- In the terminal (project folder), run `git status` and copy its first line into the
  results file. Change nothing.
- Run `docker ps` and copy the lines (they show the running bot container). If no
  container from the image `job-engine` runs: write BLOCKED (bot not running) for every test
  and stop.

---

## The tests

### AG-01 Health

1. Send `/health`.
2. PASS when the answer has all of: `Notion: OK`, `Gmail alerts token: OK`,
   `Gmail sender token: OK`, `Anthropic key: OK`, and a line starting `Google AI:` that ends
   with `OK`. Note each line that is not OK. Note the `Irish permits list:` line as it is.

### AG-02 Status and help

1. Send `/status`, then `/help`.
2. PASS when `/help` is ONE message that lists `/fetch`, `/screen`, `/contacts`,
   `/connections`, `/today`, `/health` and `/help`. Copy `/status` fully.

### AG-03 Fetch and direct job links

1. Send `/fetch`. Wait for the summary (up to 20 minutes).
2. Copy the whole summary.
3. PASS when the summary ends without an error. Note the numbers of: new jobs added, and the
   line `Own job link found on the company's site ... N of M` (write "line missing" when it is
   not there).
4. Only if a Notion tab with "Job Opportunities (DEV)" is open: for up to 3 jobs named under
   that "Own job link" line, look at their row (do not click into anything that edits):
   write for each the company, the Role, the URL host (for example `careers.acme.com`), and
   whether the URL is NOT linkedin.com. Otherwise write "Notion not open: needs Madeshwaran".

### AG-04 A second fetch the same day

1. Send `/fetch` again. Wait for the summary.
2. PASS when it finishes without an error and adds few or no new jobs. Copy the summary.

### AG-05 Fetch report

1. Send `/fetchreport`.
2. PASS when it answers with numbers per source (Adzuna, alerts, company sites and so on).
   Copy it. Do not tap its buttons.

### AG-06 Alert check

1. Send `/alertcheck`.
2. PASS when it lists the alert emails it read (or says there were none). Copy it.

### AG-07 Screening

1. Send `/screen`.
2. If buttons appear ("Today's N", "N (over the limit)", "All N"): tap ONLY the first
   button, "Today's N". If it says nothing waits, write that and go on.
3. Wait for "Screening done". Copy the summary fully.
4. PASS when the summary has a line `AI used: ... with gemini-3.5-flash-lite` and no error.
   Write the number screened and the cost shown.

### AG-08 Pending cards (look only)

1. Send `/pending`.
2. Copy the first card. Do NOT tap Approve or Skip. If there is a "Next" button you may tap
   it up to 5 times to see more cards; copy each card's first 6 lines.
3. PASS when each card shows company, role, place, a link and a Visa line. For a card in the
   Netherlands, note whether it has an IND line (sponsor or not).

### AG-09 Contacts numbers and outreach

1. Send `/contacts` (alone, no link). Copy it.
2. Send `/outreach`. Copy it.
3. PASS when `/contacts` shows numbers of the Contacts table (not only a usage line) and
   `/outreach` names providers as "not used" when they have no key.

### AG-10 Credits and sources

1. Send `/credits`. Copy it (remove any key if one shows).
2. Send `/sources`. Copy it.
3. PASS when both answer with a list and no error.

### AG-11 Mail queue

1. Send `/mailqueue`.
2. PASS when the head says `Send window: Mon to Fri, 08:00 to 10:00 the recipient's time`.
   An empty queue is still PASS. Copy it.

### AG-12 Recruitment agencies (no sending)

1. Send `/agencies`. Copy it.
2. Take the FIRST agency name in that list and send `/agencies <that name>`. Copy it.
3. PASS when 12 agencies are listed (Poland, Netherlands, Ireland) and the intro mail text
   has no long dashes. Never tap anything that sends.

### AG-13 Follow-ups

1. Send `/followups`. Copy it.
2. Before 4 Nov 2026 the result is WAITING (the first mails were sent on 28 Oct) unless it
   shows an error: then FAIL.

### AG-14 Pause and resume

1. Send `/pause 2d`. Copy it.
2. Send `/status`. Copy it.
3. Send `/resume`. Copy it.
4. PASS when the pause shows an end date and `/resume` says it is resumed.

### AG-15 Contacts for a job link that is not saved yet (Phase 12 retest P12-01)

1. Send exactly:
   `/contacts https://job-boards.eu.greenhouse.io/thedotcollective/jobs/4159027101`
2. Wait for the answer (up to 5 minutes). Copy all of it.
3. PASS when the first answer starts with `Saved` (a new job) or names the job, and a
   contacts answer follows (contacts found, or "No contacts found" with a reason).
4. Send the SAME command again once. PASS when it does not say `Saved` again (no second row).
5. Do NOT tap any button under these answers.

### AG-16 Thank-you mail text

1. Send `/thanks https://job-boards.eu.greenhouse.io/thedotcollective/jobs/4159027101 Anna`
2. PASS when the text starts with "Anna", names the company and the role, ends with
   Madeshwaran's name and has no long dashes. Copy it.

### AG-17 Autofill bookmark (reply only)

1. Send `/autofill`. Copy the first 800 characters.
2. PASS when it explains how to add the bookmark and gives the bookmark. Do not install it.

### AG-18 Form dry run (never submits)

1. Send `/dryrun https://job-boards.eu.greenhouse.io/thedotcollective/jobs/4159027101`
2. Wait up to 10 minutes. Copy the report (not the picture).
3. PASS when the report lists fields filled and fields left empty and says it did not
   submit. If it says Playwright or a browser is missing: BLOCKED (copy the message).
   Never tap "Answers OK" or "Something is wrong".

### AG-19 Job board check (terminal)

1. In the terminal, in the project folder, run exactly (PowerShell):
   `docker run --rm --env-file .env -v "${PWD}/out:/app/out" job-engine python -m jobengine.sweep --check-boards`
2. Wait until it ends (up to 15 minutes). Copy the last 40 lines it printed. If an error
   shows, copy it.
3. Open `out/boards/report.csv` (read only) and copy the rows for Allegro, Picnic and
   Allianz.
4. PASS when the run ends without an error and those three rows exist with a job count.

### AG-20 Irish permits file

1. In the browser, open (the link the bot's `/health` names)
   `https://www.gov.ie/en/department-of-enterprise-tourism-and-employment/publications/employment-permit-statistics-2026/`
2. Find the file "Permits issued to companies" (Excel). Download it.
3. Save it as `out/ie_permits.xlsx` in the project folder. This is the only file besides
   the results file you may write. If the browser cannot save there: BLOCKED.
4. Send `/health`. PASS when the `Irish permits list:` line no longer says "missing" or
   "NOT read". Copy that line.

### AG-21 End

1. Send `/health` once more and copy it (the final state).
2. Add `## Questions for Claude Code` and `Finished: <time>` to the results file.
3. Stop. Do not run anything else.

---

## Tabs Madeshwaran opens before starting the agent

1. **Telegram Web** (web.telegram.org), logged in, with the Job Engine DEV bot chat open.
   This is where all the tests happen.
2. **Notion**, logged in, with the table **Job Opportunities (DEV)** open (for AG-03 only;
   the agent only looks).
3. **Antigravity's terminal** in the project folder (`Job`).
4. **Docker Desktop** running, with the bot container started on the latest code:
   `git pull origin develop`, `docker build -t job-engine .`, stop the old container,
   start it again the usual way, then `/health` once yourself.

Also: the PC must not sleep while you are away (Windows: Settings, Power, Screen and sleep:
"Never" while plugged in), and Antigravity stays open.
