#!/usr/bin/env python3
"""CiteLab measurement runner — multi-channel.

Two measurement channels, selected with ``--channel``:

  * ``gemini``  (default) — query Gemini 2.5 Flash with Google Search grounding
    and record whether our target domain was among the cited source domains.
    The raw prompt is sent verbatim; Gemini decides on its own whether to search.

  * ``chatgpt`` — query ANONYMOUS ChatGPT (chatgpt.com/backend-anon) via the
    s5treak/reverse-chatgpt library (no account/key). Anonymous ChatGPT does not
    browse unless nudged, so this channel appends a "cite your sources" suffix to
    FORCE a web search, then parses the raw SSE stream for
    ``search_result_groups[].entries[].url`` (real source URLs, each tagged
    ``?utm_source=chatgpt.com``). Every record is honestly labelled with
    ``notes`` containing ``search_forced`` — it measures "WHEN ChatGPT searches
    this query, is our domain cited", analogous to Gemini's grounding tool.

One run record per prompt x run_index is written, validating against
runs/schema.json, and merged (idempotent per channel) into the per-date file
runs/<YYYY-MM-DD>.json.

Usage:
    # Gemini grounding channel (default):
    source ~/.config/citelab/secrets.env
    PYTHONPATH=~/.local/citelab-libs python3 experiments/runner.py [--limit K]

    # Anonymous ChatGPT channel (no key; run LOCALLY — datacenter IPs get blocked):
    PYTHONPATH=~/.local/citelab-libs python3 experiments/runner.py \
        --channel chatgpt [--limit K]

Env (all overridable):
    GEMINI_API_KEY        required for --channel gemini — never logged
    N                     repeats per prompt (default 3)
    TARGET_DOMAIN         our domain (default citelab-eks.pages.dev)
    MODEL                 gemini model id (default gemini-2.5-flash)
    CHATGPT_SEARCH_SUFFIX nudge appended to force search (default "Cite your sources.")
    CHATGPT_LIB_DIR       reverse-chatgpt checkout (default ~/.local/citelab-libs/reverse-chatgpt)
    CHATGPT_TIME_BUDGET   seconds before the chatgpt channel stops launching new prompts (default 900)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import yaml

# NOTE: the Gemini SDK (`google-genai`) and the reverse-chatgpt library are each
# imported lazily inside their own channel, so neither channel hard-requires the
# other's dependencies.

HERE = Path(__file__).resolve().parent
PROMPTS_FILE = HERE / "prompts.yaml"
RUNS_DIR = HERE / "runs"
SCHEMA_FILE = RUNS_DIR / "schema.json"

PLATFORM = "gemini"
QUERY_METHOD = "api"

# --- chatgpt channel constants ----------------------------------------------
CHATGPT_PLATFORM = "chatgpt"
CHATGPT_QUERY_METHOD = "reverse"
CHATGPT_MODEL = "reverse-chatgpt"
CHATGPT_LIB_DIR_DEFAULT = str(Path.home() / ".local/citelab-libs/reverse-chatgpt")
SEARCH_SUFFIX_DEFAULT = "Cite your sources."

# Bare-domain pattern the schema enforces for competitors (lowercase, no scheme/path).
DOMAIN_RE = re.compile(r"^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val else default


def domain_from_title(title: str | None) -> str | None:
    """Normalise a grounding chunk title to a bare lowercase domain.

    Gemini puts the real source DOMAIN in web.title (web.uri is a Google
    vertexaisearch redirect, not the real url). Titles are usually already a
    bare domain like "forbes.com", but we sanitise defensively: strip any
    scheme/path, lowercase, drop a leading "www.", and keep only the host token.
    Returns None if the result is not a valid bare domain.
    """
    if not title:
        return None
    t = title.strip().lower()
    # Drop scheme and anything after the first slash, space, or query char.
    t = re.sub(r"^[a-z]+://", "", t)
    t = re.split(r"[/\s?#]", t, maxsplit=1)[0]
    if t.startswith("www."):
        t = t[4:]
    return t if DOMAIN_RE.match(t) else None


def run_grounding(client, model: str, prompt: str):
    """Single grounded generate_content call. Returns the candidate or raises.

    The RAW prompt is sent verbatim — we must measure citation behavior for the
    real query, not a citation-nudged one. Gemini decides on its own whether to
    invoke Google Search; whether a search happened is read back from
    grounding_metadata (see extract_search_queries / extract_chunks).
    """
    from google.genai import types

    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        ),
    )
    return resp.candidates[0]


def extract_chunks(candidate):
    """Return the list of grounding chunks (or [] if none present)."""
    gm = getattr(candidate, "grounding_metadata", None)
    chunks = getattr(gm, "grounding_chunks", None) if gm else None
    return list(chunks) if chunks else []


def extract_search_queries(candidate):
    """Return the list of web search queries Gemini issued (or [] if none).

    Empty list + no chunks means Gemini answered from memory without searching,
    which must be distinguished from "searched but didn't cite us".
    """
    gm = getattr(candidate, "grounding_metadata", None)
    queries = getattr(gm, "web_search_queries", None) if gm else None
    return list(queries) if queries else []


def extract_excerpt(candidate, limit: int = 280):
    """First ~`limit` chars of the answer text (whitespace-collapsed), or None."""
    content = getattr(candidate, "content", None)
    parts = getattr(content, "parts", None) if content else None
    if not parts:
        return None
    for part in parts:
        text = getattr(part, "text", None)
        if text:
            return re.sub(r"\s+", " ", text).strip()[:limit] or None
    return None


def build_record(
    today: str,
    prompt_id: str,
    prompt: str,
    model: str,
    run_index: int,
    target_domain: str,
    chunks,
    note: str = "",
    response_excerpt: str | None = None,
):
    """Build one schema-valid run record from grounding chunks."""
    target = target_domain.lower()
    cited = False
    position = None
    source_url = None
    competitors: list[str] = []
    seen: set[str] = set()

    for idx, chunk in enumerate(chunks, start=1):
        web = getattr(chunk, "web", None)
        if web is None:
            continue
        title = getattr(web, "title", None)
        uri = getattr(web, "uri", None)
        # The real source domain is in web.title (web.uri is a Google redirect).
        # Detect our target ONLY via the normalised title domain — match exactly,
        # as a subdomain of target, or as a parent of target. No substring
        # fallback (that risks false positives).
        d = domain_from_title(title)
        is_target = bool(d) and (
            d == target
            or d.endswith("." + target)
            or target.endswith("." + d)
        )

        if is_target:
            if not cited:
                cited = True
                position = idx
                source_url = uri
            continue

        if d and d not in seen:
            seen.add(d)
            competitors.append(d)

    record = {
        "date": today,
        "prompt_id": prompt_id,
        "prompt": prompt,
        "platform": PLATFORM,
        "query_method": QUERY_METHOD,
        "model": model,
        "cited": cited,
        "citation_type": "link" if cited else "none",
        "position": position,
        "competitors": competitors,
        "source_url": source_url,
        "response_excerpt": response_excerpt,
        "run_index": run_index,
        "notes": note,
    }
    return record


# ---- chatgpt channel (anonymous, search-forced) -----------------------------

# Any absolute http(s) URL; used only as a regex fallback over the raw stream.
_URL_RE = re.compile(r"https?://[^\s\"'\\<>]+", re.IGNORECASE)


def domain_from_url(url: str | None) -> str | None:
    """Normalise a real source URL to a bare lowercase domain.

    Strips scheme, ``www.``, any path/query (so the ``?utm_source=chatgpt.com``
    tag ChatGPT appends is dropped) and any port. Returns the bare host if it
    matches the schema domain regex, else None.
    """
    if not url:
        return None
    u = url.strip()
    u = re.sub(r"^[a-z]+://", "", u, flags=re.IGNORECASE)
    # Host is everything up to the first /, ?, or #.
    u = re.split(r"[/?#]", u, maxsplit=1)[0]
    u = u.lower()
    if u.startswith("www."):
        u = u[4:]
    u = u.split(":", 1)[0]  # drop any :port
    return u if DOMAIN_RE.match(u) else None


def _collect_urls_under(node, out: list[str]) -> None:
    """Recursively collect every ``url`` string value under `node`."""
    if isinstance(node, dict):
        u = node.get("url")
        if isinstance(u, str) and u.startswith("http"):
            out.append(u)
        for v in node.values():
            _collect_urls_under(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_urls_under(v, out)
    elif isinstance(node, str):
        # Delta patches can deliver a bare URL string as the payload (e.g. when
        # the patch path is ".../entries/0/url" and "v" is the URL itself).
        if node.startswith("http"):
            out.append(node)


def extract_search_urls(events: list, raw_text: str) -> list[str]:
    """Pull the real cited source URLs out of the raw ChatGPT SSE stream.

    ChatGPT's anon backend delivers search results in/under a
    ``search_result_groups`` structure whose ``entries[].url`` are the real
    sources. Because the stream is a sequence of JSON-pointer patches, the key
    can appear either as a literal dict key (full snapshots) or only inside a
    patch's ``"p"`` path string (deltas), so we handle both, then fall back to a
    raw regex for URLs explicitly tagged ``utm_source=chatgpt.com``.
    Returns de-duplicated URLs in first-seen order.
    """
    urls: list[str] = []

    def search(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "search_result_groups":
                    _collect_urls_under(v, urls)
                elif k == "p" and isinstance(v, str) and "search_result_groups" in v:
                    # JSON-pointer patch: the payload rides in the sibling "v".
                    _collect_urls_under(node.get("v"), urls)
                else:
                    search(v)
        elif isinstance(node, list):
            for v in node:
                search(v)

    for ev in events:
        search(ev)

    seen: set[str] = set()
    ordered: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    if ordered:
        return ordered

    # Fallback: only URLs ChatGPT explicitly tags as search sources.
    for m in _URL_RE.finditer(raw_text or ""):
        u = m.group(0)
        if "utm_source=chatgpt.com" in u and u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


def _collect_answer_delta(data, out: list[str]) -> None:
    """Append answer-text deltas from one SSE event (mirrors decode_stream)."""
    if not isinstance(data, dict):
        return
    if "finish_details" in data or data.get("type") == "message_stream_complete":
        return
    v = data.get("v")
    if v is None:
        return
    if isinstance(v, list):
        for patch in v:
            if isinstance(patch, dict) and patch.get("o") == "append":
                val = patch.get("v")
                if isinstance(val, str):
                    out.append(val)
    elif isinstance(v, str):
        out.append(v)


def chatgpt_query(text: str):
    """Send one prompt to anonymous ChatGPT and return (answer_text, cited_urls).

    A FRESH session is bootstrapped per call (each anon conversation needs its
    own single-use sentinel proof-of-work token, exactly like the upstream
    FastAPI app). We reuse the library's session/payload/header builders but do
    the POST ourselves so we can read the RAW stream (its decode_stream() keeps
    only answer text and would drop the search results). Raises on transport or
    non-200 errors so the caller can record/retry.
    """
    from chat import ChatGPT  # imported here so the gemini channel needn't have it

    gpt = ChatGPT()  # __init__ bootstraps backend-anon + solves proof-of-work
    json_data = gpt.get_chat_payload(text)
    headers = gpt.get_headers()
    response = gpt.session.post(
        "https://chatgpt.com/backend-anon/conversation",
        headers=headers,
        json=json_data,
        stream=True,
        impersonate="chrome",
    )
    status = getattr(response, "status_code", None)
    if status is not None and status != 200:
        raise RuntimeError(f"backend-anon HTTP {status}")

    answer: list[str] = []
    events: list = []
    raw_chunks: list[str] = []
    buffer = ""
    for chunk in response.iter_content(chunk_size=1024):
        if not chunk:
            continue
        buffer += chunk.decode("utf-8", errors="replace")
        lines = buffer.split("\n")
        buffer = lines.pop()
        for line in lines:
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                continue
            raw_chunks.append(data_str)
            try:
                data = gpt.safe_parse(data_str)
            except Exception:  # noqa: BLE001 — malformed line, skip
                continue
            events.append(data)
            _collect_answer_delta(data, answer)

    answer_text = "".join(answer)
    cited_urls = extract_search_urls(events, "\n".join(raw_chunks))
    return answer_text, cited_urls


def build_chatgpt_record(
    today: str,
    prompt_id: str,
    prompt: str,
    run_index: int,
    target_domain: str,
    cited_urls,
    note: str,
    response_excerpt: str | None = None,
):
    """Build one schema-valid chatgpt record from real cited source URLs.

    Parallel to build_record() but for the chatgpt channel: platform=chatgpt,
    query_method=reverse, model=reverse-chatgpt. Detects the target by bare
    domain (exact / subdomain / parent), records its real URL + 1-based rank,
    and lists the other cited domains as deduped competitors.
    """
    target = target_domain.lower()
    cited = False
    position = None
    source_url = None
    competitors: list[str] = []
    seen: set[str] = set()

    for idx, url in enumerate(cited_urls, start=1):
        d = domain_from_url(url)
        if not d:
            continue
        is_target = (
            d == target or d.endswith("." + target) or target.endswith("." + d)
        )
        if is_target:
            if not cited:
                cited = True
                position = idx
                source_url = url
            continue
        if d not in seen:
            seen.add(d)
            competitors.append(d)

    return {
        "date": today,
        "prompt_id": prompt_id,
        "prompt": prompt,
        "platform": CHATGPT_PLATFORM,
        "query_method": CHATGPT_QUERY_METHOD,
        "model": CHATGPT_MODEL,
        "cited": cited,
        "citation_type": "link" if cited else "none",
        "position": position,
        "competitors": competitors,
        "source_url": source_url,
        "response_excerpt": response_excerpt,
        "run_index": run_index,
        "notes": note,
    }


# ---- validation -------------------------------------------------------------

def load_validator():
    """Return a callable(record) -> list[str] of error messages ([] if valid)."""
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    try:
        import jsonschema
        from jsonschema import Draft7Validator

        validator = Draft7Validator(schema)

        def validate(record):
            return [e.message for e in validator.iter_errors(record)]

        return validate
    except ImportError:
        # Minimal fallback: required fields + enum + key patterns.
        required = schema["required"]
        platforms = schema["properties"]["platform"]["enum"]
        methods = schema["properties"]["query_method"]["enum"]

        def validate(record):
            errs = []
            for field in required:
                if field not in record:
                    errs.append(f"missing required field: {field}")
            if record.get("platform") not in platforms:
                errs.append(f"platform not in {platforms}")
            if record.get("query_method") not in methods:
                errs.append(f"query_method not in {methods}")
            if not re.match(r"^p[0-9]{3,}$", str(record.get("prompt_id", ""))):
                errs.append("prompt_id pattern mismatch")
            for comp in record.get("competitors", []):
                if not DOMAIN_RE.match(comp):
                    errs.append(f"competitor not a bare domain: {comp}")
            return errs

        return validate


# ---- io ---------------------------------------------------------------------

def load_prompts(limit: int | None):
    data = yaml.safe_load(PROMPTS_FILE.read_text(encoding="utf-8"))
    if limit is not None:
        data = data[:limit]
    return data


def _record_key(r: dict):
    """Identity of a run record for idempotent per-channel merges."""
    return (
        r.get("date"),
        r.get("prompt_id"),
        r.get("platform"),
        r.get("query_method"),
        r.get("model"),
        r.get("run_index"),
    )


def write_runs(out_file: Path, new_records: list[dict]):
    existing = []
    if out_file.exists():
        try:
            existing = json.loads(out_file.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except json.JSONDecodeError:
            existing = []
    # Idempotent per channel: drop any existing record whose identity matches a
    # new one (same date/prompt/platform/query_method/model/run_index), so
    # re-running the same channel/day updates in place instead of duplicating.
    # Records from OTHER platforms/channels in the same date file are preserved.
    new_keys = {_record_key(r) for r in new_records}
    kept = [r for r in existing if _record_key(r) not in new_keys]
    merged = kept + new_records
    out_file.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return len(existing), len(merged)


# ---- gemini channel ---------------------------------------------------------

def run_gemini(args):
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(
            "ERROR: GEMINI_API_KEY not set. "
            "Run: source ~/.config/citelab/secrets.env",
            file=sys.stderr,
        )
        return 2

    n = int(_env("N", "3"))
    target_domain = _env("TARGET_DOMAIN", "citelab-eks.pages.dev")
    model = _env("MODEL", "gemini-2.5-flash")
    today = date.today().isoformat()

    prompts = load_prompts(args.limit)
    validate = load_validator()
    client = genai.Client(api_key=api_key)

    out_file = RUNS_DIR / f"{today}.json"

    print(
        f"CiteLab Gemini grounding runner | date={today} model={model} "
        f"target={target_domain} prompts={len(prompts)} N={n}"
    )

    records: list[dict] = []
    invalid: list[str] = []
    calls_ok = 0
    calls_failed = 0
    cited_runs = 0
    all_competitors: set[str] = set()

    for entry in prompts:
        prompt_id = entry["id"]
        prompt = entry["prompt"]
        for run_index in range(1, n + 1):
            note = ""
            chunks = []
            search_queries = []
            excerpt = None
            last_err = None
            # one call + one short retry
            for attempt in range(2):
                try:
                    candidate = run_grounding(client, model, prompt)
                    chunks = extract_chunks(candidate)
                    search_queries = extract_search_queries(candidate)
                    excerpt = extract_excerpt(candidate)
                    calls_ok += 1
                    last_err = None
                    break
                except Exception as exc:  # noqa: BLE001 — record, don't crash the panel
                    last_err = exc
                    if attempt == 0:
                        time.sleep(2.0)
            if last_err is not None:
                calls_failed += 1
                excerpt = None
                note = f"error: {type(last_err).__name__}: {last_err}"[:300]
                print(f"  [{prompt_id} run {run_index}] CALL FAILED: {note}")
            elif not search_queries and not chunks:
                # Call succeeded but Gemini answered from memory without searching.
                # cited=false here means "didn't search", not "searched, didn't
                # cite us" — flag it so the dataset stays honest.
                note = "no_search"

            record = build_record(
                today, prompt_id, prompt, model, run_index,
                target_domain, chunks, note=note, response_excerpt=excerpt,
            )
            errs = validate(record)
            if errs:
                invalid.append(f"{prompt_id} run {run_index}: {errs}")

            if record["cited"]:
                cited_runs += 1
            all_competitors.update(record["competitors"])
            records.append(record)

            flag = "CITED" if record["cited"] else "-"
            print(
                f"  [{prompt_id} run {run_index}] {flag} "
                f"competitors={len(record['competitors'])}"
            )
            time.sleep(1.0)  # be polite to the free tier

    before, after = write_runs(out_file, records)

    attempted = len(prompts) * n
    print("\n=== SUMMARY ===")
    print(f"prompts x N attempted : {len(prompts)} x {n} = {attempted}")
    print(f"calls ok / failed     : {calls_ok} / {calls_failed}")
    print(f"runs citing target    : {cited_runs} / {attempted}")
    print(f"distinct competitors  : {len(all_competitors)}")
    print(f"records written       : {len(records)} (file had {before}, now {after})")
    print(f"output file           : {out_file}")
    if invalid:
        print(f"SCHEMA-INVALID records: {len(invalid)}")
        for line in invalid:
            print(f"  ! {line}")
    else:
        print("schema validation     : all records valid")

    return 0


# ---- chatgpt channel --------------------------------------------------------

def run_chatgpt(args):
    # The reverse-chatgpt modules (chat, gpt_session, build, utils, tunsile) live
    # in their own checkout, not on PYTHONPATH; add it so `import chat` works.
    lib_dir = _env("CHATGPT_LIB_DIR", CHATGPT_LIB_DIR_DEFAULT)
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    try:
        import chat  # noqa: F401 — fail fast & clearly if the lib isn't installed
    except Exception as exc:  # noqa: BLE001
        print(
            f"ERROR: cannot import reverse-chatgpt from {lib_dir}: {exc}\n"
            "Clone https://github.com/s5treak/reverse-chatgpt there and install "
            "curl_cffi/beautifulsoup4/lxml into ~/.local/citelab-libs.",
            file=sys.stderr,
        )
        return 2

    n = int(_env("N", "3"))
    target_domain = _env("TARGET_DOMAIN", "citelab-eks.pages.dev")
    suffix = _env("CHATGPT_SEARCH_SUFFIX", SEARCH_SUFFIX_DEFAULT)
    time_budget = float(_env("CHATGPT_TIME_BUDGET", "900"))
    today = date.today().isoformat()

    prompts = load_prompts(args.limit)
    validate = load_validator()
    out_file = RUNS_DIR / f"{today}.json"

    print(
        f"CiteLab anonymous-ChatGPT runner (search-forced) | date={today} "
        f"model={CHATGPT_MODEL} target={target_domain} prompts={len(prompts)} "
        f"N={n} suffix={suffix!r}"
    )

    records: list[dict] = []
    invalid: list[str] = []
    calls_ok = 0
    calls_failed = 0
    no_search = 0
    cited_runs = 0
    all_competitors: set[str] = set()
    started = time.monotonic()
    aborted = False

    for entry in prompts:
        if aborted:
            break
        prompt_id = entry["id"]
        prompt = entry["prompt"]
        for run_index in range(1, n + 1):
            if time.monotonic() - started > time_budget:
                print(
                    f"  TIME BUDGET ({time_budget:.0f}s) exhausted — "
                    "stopping before launching more prompts."
                )
                aborted = True
                break

            text = f"{prompt} {suffix}"
            note = "search_forced"
            excerpt = None
            cited_urls: list[str] = []
            last_err = None

            # up to 2 retries (3 attempts total) with growing backoff.
            for attempt in range(3):
                try:
                    answer_text, cited_urls = chatgpt_query(text)
                    if answer_text:
                        excerpt = re.sub(r"\s+", " ", answer_text).strip()[:280] or None
                    calls_ok += 1
                    last_err = None
                    break
                except Exception as exc:  # noqa: BLE001 — record, don't crash the panel
                    last_err = exc
                    if attempt < 2:
                        time.sleep(3.0 * (attempt + 1))  # 3s, then 6s

            if last_err is not None:
                calls_failed += 1
                excerpt = None
                cited_urls = []
                note = f"error: {type(last_err).__name__}: {last_err}"[:300]
                print(f"  [{prompt_id} run {run_index}] CALL FAILED: {note}")
            elif not cited_urls:
                # Search was forced but the stream carried no search_result_groups.
                no_search += 1
                note = "search_forced;no_search"

            record = build_chatgpt_record(
                today, prompt_id, prompt, run_index,
                target_domain, cited_urls, note=note, response_excerpt=excerpt,
            )
            errs = validate(record)
            if errs:
                invalid.append(f"{prompt_id} run {run_index}: {errs}")

            if record["cited"]:
                cited_runs += 1
            all_competitors.update(record["competitors"])
            records.append(record)

            flag = "CITED" if record["cited"] else "-"
            print(
                f"  [{prompt_id} run {run_index}] {flag} "
                f"cited_domains={len(record['competitors']) + (1 if record['cited'] else 0)} "
                f"note={record['notes']}"
            )
            time.sleep(4.5)  # polite gap; backend-anon is IP-sensitive

    before, after = write_runs(out_file, records)

    attempted = len(records)
    print("\n=== SUMMARY ===")
    print(f"prompts (panel)       : {len(prompts)} (N={n})")
    print(f"runs recorded         : {attempted}")
    print(f"calls ok / failed     : {calls_ok} / {calls_failed}")
    print(f"runs forced w/o search: {no_search}")
    print(f"runs citing target    : {cited_runs} / {attempted}")
    print(f"distinct competitors  : {len(all_competitors)}")
    print(f"records written       : {len(records)} (file had {before}, now {after})")
    print(f"output file           : {out_file}")
    if invalid:
        print(f"SCHEMA-INVALID records: {len(invalid)}")
        for line in invalid:
            print(f"  ! {line}")
    else:
        print("schema validation     : all records valid")

    return 0


# ---- main -------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="CiteLab measurement runner")
    parser.add_argument(
        "--channel", choices=["gemini", "chatgpt"], default="gemini",
        help="measurement channel (default: gemini grounding API)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="run only the first K prompts (smoke test)",
    )
    args = parser.parse_args(argv)

    if args.channel == "chatgpt":
        return run_chatgpt(args)
    return run_gemini(args)


if __name__ == "__main__":
    raise SystemExit(main())
