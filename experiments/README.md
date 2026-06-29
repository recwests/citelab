# CiteLab — Measurement Harness (skeleton)

CiteLab is a GEO testbed: it measures whether AI answer engines (ChatGPT,
Perplexity, Google AI Overviews, Gemini, Bing/Copilot) **cite this site** for
the questions our content targets.

This directory is **data + schema + docs only**. It lives outside `src/`, has no
`.ts`/`.astro` files, and is **not part of the Astro build**. There is no
measurement code here yet — that is a later sprint.

## Measurement-first principle

Decisions about content and structure are made **only against measured citation
data**, never against assumptions. We measure the current state _before_
changing anything, then measure again after, and attribute change to evidence.

## What's here

```
experiments/
  prompts.yaml              # the target query panel (experiment inputs)
  runs/
    schema.json             # JSON Schema (draft-07) for one run record
    YYYY-MM-DD.json         # one file per measurement date (array of records)
    2026-06-29.example.json # EXAMPLE record — not real data
  README.md
```

## How a run is recorded

- **One file per date**, named `runs/YYYY-MM-DD.json`.
- Each file contains a **JSON array of run records**.
- Each record validates against `runs/schema.json` (one prompt × one platform ×
  one repeat). Required fields: `date`, `prompt_id`, `prompt`, `platform`,
  `query_method`, `cited`, `run_index`.
- `prompt_id` references an `id` in `prompts.yaml`; `prompt` copies the text used
  at run time so records stay interpretable even if the panel evolves.

### Attribution fields (so a citation is interpretable, not just a yes/no)

- `citation_type` — `link` (clickable source, strongest) / `quote` (our text
  quoted) / `mention` (brand named, no link) / `none`. A bare `cited:true` hides
  this difference, which is exactly what GEO analysis lives on.
- `model` — the specific model/version behind the platform at run time
  (e.g. `sonar`, `gemini-2.0-flash`). Lets us tell a model update apart from a
  content change.
- `query_method` — `api` vs `manual`; the same platform can answer differently
  via API vs UI, so it is recorded explicitly (and is required).
- `response_excerpt` — short verbatim snippet around the citation; AI answers are
  not replayable, so this is the only durable evidence (esp. for manual runs).

## N≥3 rule (non-determinism)

AI answers are non-deterministic: the same prompt can cite different sources on
different runs. Every prompt × platform combination is run **at least 3 times**
(`run_index` 1..N) so citation rate is measured as a frequency, not a single
coin flip.

## Baseline before content

Establish a **baseline** citation rate across the panel _before_ publishing or
changing content. Without a baseline there is nothing to attribute later gains
(or losses) to.

## Platforms: auto-measurable vs manual

Recorded in `platform`:

| Platform       | How (later sprint)                                   |
| -------------- | ---------------------------------------------------- |
| `gemini`       | Auto — Gemini grounding API (free tier)              |
| `perplexity`   | Auto — Perplexity Sonar API (cheap, returns sources) |
| `google_aio`   | Manual — Google AI Overviews, observed by hand       |
| `chatgpt`      | Manual — observed by hand                            |
| `bing_copilot` | Manual — observed by hand                            |

## Hard rule: no UI scraping

Do **NOT** scrape the ChatGPT or Perplexity web UIs — it violates their Terms of
Service. Manual platforms are recorded by hand; automated platforms use their
official APIs only.
