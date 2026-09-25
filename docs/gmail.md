# Gmail drafts (Module 06)

When contacts are ready for a job, the bot writes one cold mail per contact into Gmail
**Drafts**: the approved Cold Mail Templates word for word, the Config signature, and the
approved resume PDF attached. It never sends anything. You open Gmail > Drafts, read, edit if
needed, and send yourself.

## Rules

- **Draft only.** `src/jobengine/gmail_client.py` is the only module that writes to Gmail. It
  may call `users.drafts.create`, `drafts.get`, `drafts.list`, `messages.get`, `threads.get`,
  `labels.list` and `messages.modify` (labels, Module 07). Sending, removing, trashing, editing
  a draft and creating labels are not in the code; `tests/test_repo_rules.py` checks the file.
- **Templates word for word.** Templates are read from the Notion page Cold Mail Templates at
  run time. A section without `APPROVED` in its heading is never used. Only the placeholders
  change. No LLM writes mail text; one LLM call (stage `email`) only returns the index of the
  best stored specific detail when there are several.
- **Every recipient goes through `safety.route_recipients`.** In local and dev every draft is
  addressed to madeshwaranm02@gmail.com, cc is dropped and the subject starts with
  `[DEV] to <real address> | ` (`[LOCAL]` locally). Prod has the real recipient and the plain
  template subject.
- **DRY_RUN** (the default): no draft is created and nothing is written. The bot logs
  `DRY RUN: would create draft to <addr>` and shows the first rendered mail in Telegram.
- **Never mailed:** contacts with Status `Bounced` or `Do not contact`, Type `Other` (except a
  generic mailbox when Config `mail.generic_greeting_name` exists), and anyone drafted in the
  last Config `mail.same_person_cooldown_days` (30) days for another role.
- **No em or en dash** in the subject, body or signature. The final MIME text is checked; a hit
  aborts that draft.
- **Attachment:** the approved revision's PDF from Drive (Resume Log `File`), or from `out/`
  when it was approved in DRY_RUN. At most Config `mail.max_attachment_kb` (250) KB.

## Templates and placeholders

| Contact Type | Template |
|---|---|
| Hiring | Hiring manager (the Recruiter one when the job has no specific detail) |
| Recruiter/TA | Recruiter / Talent Acquisition |
| Peer engineer | Peer engineer (the swap rule is not applied) |

| Placeholder | Source |
|---|---|
| `{first_name}` | first word of the contact's Name (or `mail.generic_greeting_name`) |
| `{company}` | Job `Company` |
| `{role}` | Job `Role` without gender markers such as `(m/f/d)` |
| `{city}` | Job `City`; `Remote` becomes the country |
| `{country}` | Job `Country` |
| `{permit_word}` | Config `mail.permit_word.poland`, `.netherlands`, `.ireland` |
| `{specific_detail}` | one of the screening `Specific details`, exactly as stored |

An unfilled placeholder aborts that draft. Polish-language mails are not generated.

## The draft

- Body: the filled template, one `<p>` per paragraph, then the signature from Config
  `mail.signature` with `{profile.*}` filled from Config. The phone is plain text. Only
  `profile.linkedin_url` and `profile.website_url` may be linked; any other link aborts.
- `multipart/mixed`: `text/plain` and `text/html` alternatives plus the PDF. `To` and
  `Subject` only; Gmail sets `From`. No Bcc, no tracking.
- After each draft, Contacts gets `Gmail draft ID`, `Gmail thread ID`, `Status` Drafted
  (only from Unverified or Verified) and a Notes line `drafted for <Role> <date>`. The job gets
  `Last activity date`; its Status stays `Resume built` until you tap I applied.
- Running again is safe: contacts that already have a draft for the job are skipped.

## Telegram

After the contacts summary the bot sends:

```
Drafts for Vistula Cloud, DevOps Engineer
Hiring: Piotr Example (hiring template)
Recruiter/TA: Ola Example (recruiter template)
Peer engineer: Anna Example, Jan Example (peer template)
4 drafts created in Gmail (madeshwaranm02 in DEV). Review and send them from Gmail > Drafts.
Skipped: none
Reminder: soft cap is 15 mails a day.
```

`/drafts <job URL or page id>` retries a job. When more than `mail.daily_send_cap` drafts are
waiting (Contacts with Status Drafted) the summary says so.

## Tokens (gmail.modify)

Drafts need `gmail.modify`; the old OAuth Playground tokens only have `gmail.readonly`,
`gmail.send` and `drive.file`. Before any Gmail write the client checks the token's scopes
and stops with "Sender token lacks gmail.modify. Regenerate it with python -m
jobengine.gmail_auth." when the scope is missing.

1. In Google Cloud Console, publish the OAuth consent screen to **Production** first. Refresh
   tokens made while it is in Testing expire after 7 days.
2. Download the OAuth client JSON (Desktop app) to a folder outside the repo.
3. On your machine, once per account:

   ```bash
   pip install -e ".[auth]"
   APP_ENV=local python -m jobengine.gmail_auth --client-secret ~/oauth-client.json --account m02
   APP_ENV=local python -m jobengine.gmail_auth --client-secret ~/oauth-client.json --account main
   ```

   It asks for exactly `gmail.modify` and `drive.file`, prints the account you authorised and
   writes `.secrets/gmail-<account>-token.json` (ignored by git). It refuses to run when
   `APP_ENV` is not `local`.
4. Local: put the JSON (one line) into `.env` as `GMAIL_SENDER_TOKEN_JSON` (the m02 token). The
   alerts token (`GMAIL_ALERTS_TOKEN_JSON`) only needs `gmail.readonly`; locally it may be the
   same JSON. Dev and prod: store it in Secret Manager (Module 09); prod uses the main
   account.

## Notion prerequisites

Config rows: `mail.permit_word.poland` = `a Polish work permit`,
`mail.permit_word.netherlands` = `a Dutch work permit (highly skilled migrant)`,
`mail.permit_word.ireland` = `an Irish employment permit`, `mail.max_attachment_kb` = 250,
`mail.same_person_cooldown_days` = 30. `mail.generic_greeting_name` only if you want generic
mailboxes mailed. `mail.signature`, `mail.attach_resume` and `mail.daily_send_cap` already
exist.

## Run it

```bash
python -m jobengine.mail --fake --job fixture-clean-pl              # fixtures, no network
python -m jobengine.mail --fake --job fixture-clean-pl --no-write   # print every mail
python -m jobengine.mail --job <page id> --no-write                 # real data, nothing created
```
