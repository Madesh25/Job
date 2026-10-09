# Interviews: prep pack and thank-you mail (flow feature 15)

`src/jobengine/apply/interview.py`, Telegram commands `/prep` and `/thanks`.

## /prep <job link or page id>

One message to read before the interview:

- **They ask for and you have**: the tools the job description names that you have (the
  free skill matcher, the same list as "Skill match" on the /pending card). **Gaps to answer
  honestly**: the job's gaps from screening.
- **About the company**: the details stored from the job page (a detail that names one of
  your gaps is left out).
- **Likely questions**: one AI call (stage `tailor`) on the job description and your Golden
  Master resume gives up to 8 questions with an answer hint each. A hint may only use facts
  from the resume; a hint that names one of the job's gaps without saying you have not used
  it is replaced by a reminder to answer it honestly. When the call fails the pack says why
  and still has everything else.
- **Questions to ask them**: `interview.ask` in `config/base.yaml` (Notion Config
  `interview.ask`, one question per line, wins).
- **Your interview tips**: adopted strategy tips of the Interview category for the job's
  country.

The reply ping says "To prepare: /prep <job link>" when a reply may be about an interview.

## /thanks <job link or page id> [interviewer first name]

The thank-you mail to send the same day, from `interview.thanks` in `config/base.yaml`
(Notion Config `interview.thanks` wins), word for word, filled with the role, the company,
one detail from the job page (its "we" and "our" turned into "you" and "your"; a detail that
names a gap is never used) and Config `profile.name`. No AI, nothing sent: paste it into
your reply in the interview thread and edit it first.
