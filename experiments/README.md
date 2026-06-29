# CiteLab — Measurement Harness (skeleton)

CiteLab is a GEO testbed: it measures whether AI answer engines (ChatGPT,
Perplexity, Google AI Overviews, Gemini, Bing/Copilot) **cite this site** for
the questions our content targets.

This directory holds the **measurement data + schema + docs**, plus the
**measurement runner** (`runner.py`) — two auto channels selected with
`--channel`: `gemini` (Gemini grounding API, default) and `chatgpt` (anonymous
ChatGPT, search-forced). It lives outside `src/`, has no `.ts`/`.astro` files,
and is **not part of the Astro build**.

## Running the runner (Gemini grounding channel)

`runner.py` is the **only auto channel implemented so far**: Gemini 2.5 Flash +
Google Search grounding (official API, free tier). Perplexity (Sonar API) is the
next auto channel; ChatGPT, Google AI Overviews and Bing/Copilot stay manual
(see the platforms table below).

```bash
# the key lives in ~/.config/citelab/secrets.env as GEMINI_API_KEY=...
# `set -a` exports it to the python child; secrets.env has no `export` lines.
set -a; source ~/.config/citelab/secrets.env; set +a
PYTHONPATH=~/.local/citelab-libs python3 experiments/runner.py --limit 3   # smoke test
PYTHONPATH=~/.local/citelab-libs python3 experiments/runner.py             # full panel (N=3)
```

Config via env (all overridable): `N` (repeats/prompt, default 3),
`TARGET_DOMAIN` (default `citelab-eks.pages.dev`), `MODEL` (default
`gemini-2.5-flash`). `--limit K` runs only the first K prompts.

Each prompt × `run_index` produces one record (validated against
`runs/schema.json`) appended to `runs/<YYYY-MM-DD>.json`. The runner reads
`GEMINI_API_KEY` from `os.environ` only and never prints it. Deps:
`requirements.txt` (`google-genai`, `pyyaml`, optional `jsonschema`).

**Detecting our citation.** Gemini returns grounding chunks where `web.uri` is a
`vertexaisearch…/grounding-api-redirect/…` Google redirect — **not** the real
url. The real source **domain is in `web.title`** (e.g. `forbes.com`), so the
runner detects citation **only** via the sanitised `web.title` (bare lowercase
domain): the normalised title domain must equal `TARGET_DOMAIN`, be a subdomain
of it, or be a parent of it. There is no substring fallback (it risks false
positives).

The **raw prompt is sent verbatim** — we do not append any "cite your sources"
nudge, so we measure citation behavior for the real query. Gemini may answer
from memory without searching; when a call succeeds but issues no
`web_search_queries` and returns no chunks, the record's `notes` is set to
`no_search` (so `cited:false` from "didn't search" is distinguishable from
"searched but didn't cite us").

## Running the runner (anonymous ChatGPT channel, search-forced)

The `chatgpt` channel queries **anonymous ChatGPT** (`chatgpt.com/backend-anon`)
through the [`s5treak/reverse-chatgpt`](https://github.com/s5treak/reverse-chatgpt)
library. **No account, no API key** — the library bootstraps an anonymous
session and solves ChatGPT's proof-of-work challenge itself.

```bash
# one-time install (no key needed):
git clone https://github.com/s5treak/reverse-chatgpt ~/.local/citelab-libs/reverse-chatgpt
pip install --target=~/.local/citelab-libs --break-system-packages curl_cffi beautifulsoup4 lxml

PYTHONPATH=~/.local/citelab-libs python3 experiments/runner.py --channel chatgpt --limit 2  # smoke
PYTHONPATH=~/.local/citelab-libs python3 experiments/runner.py --channel chatgpt            # full panel
```

Config via env: `N`, `TARGET_DOMAIN` (shared with the gemini channel),
`CHATGPT_SEARCH_SUFFIX` (default `"Cite your sources."`), `CHATGPT_LIB_DIR`
(default `~/.local/citelab-libs/reverse-chatgpt`), `CHATGPT_TIME_BUDGET`
(seconds before the channel stops launching new prompts, default 900).

**Run LOCALLY.** The anonymous backend is IP-sensitive — datacenter/cloud IPs
get Cloudflare-blocked. From a residential connection it works without an
account. The channel uses up to 2 retries with growing backoff (3s, 6s) per
prompt, a fresh session per call (each anon conversation needs its own
single-use proof-of-work token), and a polite ~4.5s gap between prompts. On
total failure a record is still written with the error in `notes` and
`cited:false`.

**Forced-search semantics.** Anonymous ChatGPT does **not** browse unless
nudged, so this channel appends `CHATGPT_SEARCH_SUFFIX` to every prompt to
**force a web search**, and labels every record honestly: `notes` contains
`search_forced`. This measures *"when ChatGPT searches this query, is our domain
cited"* — analogous to enabling Gemini's grounding tool, **not** a measure of
ChatGPT's spontaneous browsing. Records use `query_method="reverse"`,
`platform="chatgpt"`, `model="reverse-chatgpt"`.

**Detecting our citation.** The library's `decode_stream()` keeps only answer
text, so the runner does its own POST and parses the **raw** SSE stream for
`search_result_groups[].entries[].url` — the real source URLs (each tagged
`?utm_source=chatgpt.com`). Each URL is reduced to a bare lowercase domain via
`domain_from_url()` (scheme/`www.`/path/query/port stripped); our target is
detected by exact / subdomain / parent match, the rest become `competitors`. If
the forced call succeeds but the stream carries **no** `search_result_groups`,
`notes` is `search_forced;no_search`.

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
| `chatgpt`      | Auto — anonymous backend-anon, search-forced (`query_method=reverse`); also recordable manually |
| `perplexity`   | Auto — Perplexity Sonar API (cheap, returns sources) |
| `google_aio`   | Manual — Google AI Overviews, observed by hand       |
| `bing_copilot` | Manual — observed by hand                            |

## Hard rule: no UI scraping

Do **NOT** scrape the ChatGPT or Perplexity rendered web UIs — it violates their
Terms of Service. Manual platforms are recorded by hand. The `chatgpt` channel
is **not** UI scraping: it calls the anonymous `backend-anon` JSON endpoint (no
account, no login, no rendered-page scraping) via a reverse-engineered client,
strictly for low-volume research measurement, run locally with polite pacing. It
is labelled `query_method=reverse` to keep it distinct from official `api`
channels.
