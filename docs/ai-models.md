# Which AI does which job

Every AI job of the bot has its own Config row, and a row may name a Claude, a Google Gemini
or a DeepSeek model. Nothing changes until you change a row: every row that is not set uses
Haiku, as before.

No AI gets access to Gmail, Drive or Notion. The bot's own code reads and writes them and
sends the AI only the text it needs (a job description, a reply, your resume text).

## The jobs and their Config rows (your plan of 20 Oct)

| AI job | What it sends | Config row | Plan after go-live |
|---|---|---|---|
| Screening (and the half-price batch) | the job text only | `model.score` | `deepseek-v4-flash` |
| Mail detail pick (one detail per mail) | the job's details | `model.email` | `gemini-3.5-flash-lite` |
| Reply sorting (/today) | the recruiter's reply | `model.reply` | `gemini-3.5-flash-lite` |
| Cover letter | your resume and the job | `model.cover` | `gemini-3.5-flash-lite` |
| /prep interview questions | your resume and the job | `model.prep` | `gemini-3.5-flash-lite` |
| Resume build | your Golden Master and the job | `model.resume` | `gemini-3.5-flash-lite`, with a "Rebuild with Claude" button |
| /update strategy research | your target roles and countries, web search | `model.strategy` | `gemini-3.5-flash-lite` with Google Search |
| Contact search (docs/contacts.md) | the company, its website and the country, web search | `model.people` | `gemini-3.5-flash-lite` with Google Search |

Rules the code keeps whatever the Config says:
- **DeepSeek only ever screens.** It gets the job text and nothing about you or your mail. A
  DeepSeek model in any other row is replaced by Haiku. DeepSeek has no web search either.
- **Rebuild with Claude:** when Gemini wrote a resume, its preview has one more button. It
  builds the next version with Claude (Config `model.resume_rebuild`, Haiku when not set).
- **/update on Gemini:** Google gives every search result as a redirect link. The bot opens
  each link once to read where it points (only the redirect, never the page), so the source
  check still works. A tip Google says is supported by a result gets that result as its
  source. Gemini decides itself how many searches it makes (`strategy.max_searches` applies
  only on Claude). Gemini 3 search: 5,000 prompts a month free, then $14 per 1,000 searches.
- A row not set: Haiku. `model.cover`, `model.prep` and `model.resume` fall back to
  `model.tailor` first.

## Prices (US$ per million tokens, 20 Oct 2026)

| Model | In | Out | Note |
|---|---|---|---|
| `claude-haiku-4-5` | 1.00 | 5.00 | half price in the batch |
| `gemini-3.5-flash-lite` | 0.30 | 2.50 | about 2 to 3 times cheaper than Haiku |
| `gemini-3.1-flash-lite` | 0.25 | 1.50 | older, cheaper |
| `gemini-3.8-flash` | 0.75 until 31 Dec, then 1.50 | 3.75 until 31 Dec, then 7.50 | from January **more** than Haiku |
| `deepseek-v4-flash` | 0.14 | 0.28 | cache hits far cheaper; sources disagree, check deepseek.com |
| `deepseek-v4.1-flash` | 0.30 | 1.20 | |

Gemini bills its "thinking" as output tokens. Flash-Lite thinks very little by default;
3.8 Flash thinks more. You can lower it in the Config value:
`gemini-3.8-flash thinking:low` (levels: minimal, low, medium, high; 3.8 Flash has no
minimal). The cost lines and the monthly total in the digest use the 2027 price for 3.8
Flash, so they never show too little.

Google retired the 2.5 models in October 2026: use the names above.

## Safety net

- Any Gemini or DeepSeek failure (an error, a wrong or missing key, the service busy, broken
  JSON twice) is sent again to Haiku. The cost line then says "N calls failed,
  claude-haiku-4-5 answered". An outage never stops the bot. The cost line also names the
  non-Claude model used ("with gemini-3.5-flash-lite").
- Local and dev always use Haiku (`llm.force_model`), whatever the Config says. Gemini and
  DeepSeek run only in prod (and in the comparison command).
- The half-price batch exists only on Claude. When `model.score` is Gemini or DeepSeek,
  `/screen batch` and /autopilot screen at once instead; both cost less at full price than
  the Haiku batch.
- Gemini calls go through `jobengine.http`, so `generativelanguage.googleapis.com` is on
  `safety.allowed_hosts`. Prompts are never logged.

## Setup (once, after go-live)

1. Google AI Studio (aistudio.google.com) -> Get API key, in a Google Cloud project **with
   billing on**. On the free tier Google may use your prompts (your resume and the job
   texts) to improve its models; on the paid tier it does not.
2. Put the key in Secret Manager as `gemini-api-key` in the prod project and give the prod
   runtime account access (docs/deploy.md, B2). For the comparison on your PC, put it in
   `.env` as `GEMINI_API_KEY` (never in a chat).
3. DeepSeek: platform.deepseek.com -> API keys, add a little credit. Secret
   `deepseek-api-key` in prod, `DEEPSEEK_API_KEY` in `.env` for the comparison.
4. Redeploy: the deploy log shows "Mounting optional secret gemini-api-key" (and
   deepseek-api-key).

## The 20-job comparison (before changing any row)

On your PC (local, DEV Notion), with `GEMINI_API_KEY` and `ANTHROPIC_API_KEY` in `.env`,
after `git pull` and `docker build -t job-engine .`, in PowerShell:

```
docker run --rm --env-file .env -e APP_ENV=local -v ${PWD}/out:/app/out job-engine python -m jobengine.llm_compare --model gemini-3.5-flash-lite
docker run --rm --env-file .env -e APP_ENV=local -v ${PWD}/out:/app/out job-engine python -m jobengine.llm_compare --model deepseek-v4-flash --tasks score
```

DeepSeek may only run the score task in the comparison too (it never gets your resume).

- It takes the 20 newest screened jobs with a full description (or `--job <id>`, repeatable;
  `--jobs N` for another number) and runs screening, cover letter, /prep and a resume build
  with Haiku (`--base`) and with the model you name.
- It writes nothing to Notion, Gmail or Drive. The models are called by name, so it runs
  locally too. Both resume PDFs of each job go to `out/compare/`.
- The report is `out/compare/compare-<date>.html`: a summary (done, failed, seconds,
  dropped quotes, how many screening answers agree, the cost of each model), then each job
  with both answers side by side.
- Cost: about $0.50 for 20 jobs and all four tasks with Haiku, less with Flash-Lite.

How to decide:
- **Screening:** at least 90% of the answers agree, and DeepSeek (or Gemini) has no more
  dropped quotes than Haiku (a dropped quote is a fact the AI made up). Then set
  `model.score` to it.
- **Cover letter and /prep:** read 5 to 10 of each side by side. Pick Gemini only if it is
  as good: no invented skill, the right years and notice period, no failure.
- **Resume:** open both PDFs of 5 jobs. If Gemini is close, set `model.resume` to it and use
  "Rebuild with Claude" when a resume is not good enough; if it is clearly worse, keep Haiku.
- **Mail pick, replies, /update:** set them to `gemini-3.5-flash-lite`; watch the first week
  of /today (reply sorting) and the first /update.
- Then add the Config rows in prod Notion. Undo = clear the row (Haiku again).

## For developers

`llm.make_llm(s, config)` returns the `ModelRouter` every part of the bot uses; it routes
by model name prefix and falls back to `DEFAULT_MODEL`. `GeminiLLM` speaks the REST
`generateContent` API (JSON mode; Google Search for /update), `DeepSeekLLM` the OpenAI-style
chat API (JSON mode, `llm.DEEPSEEK_STAGES` only). Stages: `llm.STAGES`; a stage's parent for the Config
row: `llm.STAGE_PARENT`. Prices: `llm.PRICES_PER_MTOK`.
