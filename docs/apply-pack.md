# Apply pack (PR 11)

When a resume is approved (the **Approve resume** tap, or `/autopilot`), the bot sends one
more message with ready answers for the job's portal form, and saves the same text on the
job's Notion page under `Apply pack (<date>)`. `/applypack <job URL or page id>` sends it again.
No AI: every answer is yours.

## What it holds

- **Work permit:** "Authorized to work without sponsorship? No", "Need visa sponsorship? Yes",
  and the country's line (Poland: type A work permit; Netherlands: Highly Skilled Migrant with
  a recognised IND sponsor; Ireland: General Employment Permit, no labour market test because
  DevOps is on the Critical Skills Occupations List).
- **Salary:** "Open, any range at or above the visa minimum", the visa minimum for that country
  (2026: Netherlands EUR 4,357 a month gross under 30; Ireland EUR 36,605 a year; Poland none),
  the salary the posting states, and "Current salary: Prefer not to say".
- **Notice and start:** 90 days, can be shortened to 30 to 40 days; start about 3 months after
  the offer, including the permit.
- **About you:** experience, education, English (C1, only where a level is required), other
  languages, relocation, interview availability, gender and other diversity questions, and
  the LinkedIn, GitHub and website links from Config `profile.*`.
- **Why this company:** "The role's focus on {detail} is close to my day-to-day work at
  Xerago.", with the job's own stored specific detail. Without one, it asks you to write it.
- **Resume for this job:** the headline and location line the resume used.

## Where the answers live

`config/base.yaml` under `apply_pack`. A Notion Config key `apply.<name>` wins over each one
(for example `apply.notice_period`), so you can change an answer without a code change. A
missing answer shows as `(not set: add Config apply.<name>)`, never a guess.

The visa minimums change every year (IND in January, DETE in March): update
`apply_pack.countries` then. The Irish Critical Skills Employment Permit needs EUR 68,911
without an ICT degree, so the pack uses the General Employment Permit line.

## The resume header per job

`resume/header.py`, applied before the resume is rendered (the skills and bullets and their
integrity gate are unchanged):

- **Headline:** its first part (before `|`) becomes the job's title when the role names one of
  your approved titles (`resume.headline_titles`: DevOps Engineer, Site Reliability Engineer,
  Platform Engineer, Cloud Engineer, Infrastructure Engineer, Kubernetes Engineer; "SRE"
  counts as Site Reliability Engineer). The approved title is used, never the posting's words,
  so "Senior Site Reliability Engineer (m/f/d)" gives "Site Reliability Engineer". Otherwise
  the headline stays.
- **Location line** (`p.reloc`): `resume.relocation_template`, by default
  `Chennai, India (relocating to {place})` with the job's city and country.

Config keys `resume.headline_titles` and `resume.relocation_template` win when set.
