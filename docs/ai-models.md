# Which AI does which job

Since 20 Oct every AI job of the bot has its own Config row, and a row may name a Claude
model or a Google Gemini model. Nothing changes until you change a row: every row that is
not set uses Haiku, as before.

## The jobs and their Config rows

| AI job | Config row | If the row is not set | Plan after go-live |
|---|---|---|---|
| Screening (and the half-price batch) | `model.score` | Haiku | `gemini-3.5-flash-lite`, if the comparison is good |
| Mail detail pick (one detail per mail) | `model.email` | Haiku | `gemini-3.5-flash-lite` |
| Cover letter | `model.cover` | `model.tailor`, then Haiku | Gemini: the better of `gemini-3.5-flash-lite` and `gemini-3.8-flash` in the comparison |
| /prep interview questions | `model.prep` | `model.tailor`, then Haiku | same as the cover letter |
| Resume build | `model.resume` | `model.tailor`, then Haiku | test Gemini; keep Haiku if Gemini is not as good |
| Reply sorting (/today) | `model.reply` | Haiku (not `model.score`) | stays Haiku |
| /update strategy research | `model.strategy` | Haiku | stays Haiku |

Why two jobs stay on Claude:
- **Reply sorting:** a wrong answer can hide a real interview invite as "other". It is a few
  cents a month, so the safest model stays.
- **/update:** it searches the web. Gemini's search gives hidden Google redirect links, so
  the check "is this tip's source a page the search really found" would fail every tip. A
  Gemini model in `model.strategy` is replaced by Haiku.

## Prices (US$ per million tokens, 20 Oct 2026)

| Model | In | Out | Note |
|---|---|---|---|
| `claude-haiku-4-5` | 1.00 | 5.00 | half price in the batch |
| `gemini-3.5-flash-lite` | 0.30 | 2.50 | about 2 to 3 times cheaper than Haiku |
| `gemini-3.1-flash-lite` | 0.25 | 1.50 | older, cheaper |
| `gemini-3.8-flash` | 0.75 until 31 Dec, then 1.50 | 3.75 until 31 Dec, then 7.50 | from January **more** than Haiku |

Gemini bills its "thinking" as output tokens. Flash-Lite thinks very little by default;
3.8 Flash thinks more. You can lower it in the Config value:
`gemini-3.8-flash thinking:low` (levels: minimal, low, medium, high; 3.8 Flash has no
minimal). The cost lines and the monthly total in the digest use the 2027 price for 3.8
Flash, so they never show too little.

Google retired the 2.5 models in October 2026: use the names above.

## Safety net

- Any Gemini failure (an error, a wrong or missing key, Google busy, broken JSON twice) is
  sent again to Haiku. The cost line then says "Gemini failed N times, claude-haiku-4-5
  answered". A Google outage never stops the bot.
- Local and dev always use Haiku (`llm.force_model`), whatever the Config says. Gemini runs
  only in prod.
- The half-price batch exists only on Claude. When `model.score` is Gemini, `/screen batch`
  and /autopilot screen at once instead; Flash-Lite at full price is still cheaper than the
  Haiku batch.
- Gemini calls go through `jobengine.http`, so `generativelanguage.googleapis.com` is on
  `safety.allowed_hosts`. Prompts are never logged.

## Setup (once, after go-live)

1. Google AI Studio (aistudio.google.com) -> Get API key, in a Google Cloud project **with
   billing on**. On the free tier Google may use your prompts (your resume and the job
   texts) to improve its models; on the paid tier it does not.
2. Put the key in Secret Manager as `gemini-api-key` in the prod project and give the prod
   runtime account access (docs/deploy.md, B2). For the comparison on your PC, put it in
   `.env` as `GEMINI_API_KEY` (never in a chat).
3. Redeploy: the deploy log shows "Mounting optional secret gemini-api-key".

## The 20-job comparison (before changing any row)

On your PC (local, DEV Notion), with `GEMINI_API_KEY` and `ANTHROPIC_API_KEY` in `.env`,
after `git pull` and `docker build -t job-engine .`, in PowerShell:

```
docker run --rm --env-file .env -e APP_ENV=local -v ${PWD}/out:/app/out job-engine python -m jobengine.llm_compare --model gemini-3.5-flash-lite
docker run --rm --env-file .env -e APP_ENV=local -v ${PWD}/out:/app/out job-engine python -m jobengine.llm_compare --model gemini-3.8-flash --tasks cover,prep,resume
```

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
- **Screening:** at least 90% of the answers agree, and Gemini has no more dropped quotes
  than Haiku (a dropped quote is a fact the AI made up). Then set `model.score` and
  `model.email` to `gemini-3.5-flash-lite`.
- **Cover letter and /prep:** read 5 to 10 of each side by side. Pick Gemini only if it is
  as good: no invented skill, the right years and notice period, no failure.
- **Resume:** open both PDFs of 5 jobs. Same check; any rule failure means Haiku stays.
- Then add the Config rows in prod Notion. Undo = clear the row (Haiku again).

## For developers

`llm.make_llm(s, config)` returns the `ModelRouter` every part of the bot uses; it routes
by model name prefix and falls back to `DEFAULT_MODEL`. `GeminiLLM` speaks the REST
`generateContent` API in JSON mode. Stages: `llm.STAGES`; a stage's parent for the Config
row: `llm.STAGE_PARENT`. Prices: `llm.PRICES_PER_MTOK`.
