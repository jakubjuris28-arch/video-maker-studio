"""
Video Maker Studio - pipeline.py (multi-author)
All the heavy lifting: env loading, Anthropic calls, image generation,
xmind building, and the end-to-end job runner.

Nothing in here talks to Flask directly. server.py hands us a `job` dict
(a shared mutable status object) and we update it as we go.
"""

import os
import re
import json
import time
import base64
import secrets
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from authors import AUTHORS


# --------------------------------------------------------------------------
# .env loading (tiny parser, no python-dotenv dependency)
# --------------------------------------------------------------------------
def load_env(path=None):
    """Read a .env file into a dict. Supports # comments, optional `export`,
    and single/double quoted values. Also folds in os.environ so real
    environment variables win if set."""
    cfg = {}
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                cfg[key] = val
    # real environment overrides file
    for k, v in os.environ.items():
        if k in ("MODEL", "ANTHROPIC_API_KEY", "KEYAI_API_KEY", "KEYAI_ENDPOINT",
                 "KEYAI_MODEL", "KEYAI_SIZE", "KEYAI_ASPECT", "KEYAI_OUTPUT_FORMAT",
                 "GITHUB_TOKEN", "STORAGE_REPO") \
                or k.startswith("PRODUCT_NAME") or k.startswith("FUNNEL_CHANNEL") \
                or k.startswith("ANTHROPIC_API_KEY_"):
            cfg[k] = v
    return cfg


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def slugify(title, cap=40):
    s = re.sub(r"[^a-zA-Z0-9]+", "_", title.lower()).strip("_")
    return (s[:cap]).strip("_") or "video"


def nid():
    """26-char hex id, as XMind uses for topic ids."""
    return secrets.token_hex(13)


def ordinal(n):
    words = ["zeroth", "first", "second", "third", "fourth", "fifth", "sixth",
             "seventh", "eighth", "ninth", "tenth"]
    return words[n] if n < len(words) else f"{n}th"


def parse_json_loose(text):
    """Parse JSON that may be wrapped in ```json fences, have leading prose,
    or contain trailing commas (which the model sometimes emits)."""
    if not text:
        raise ValueError("empty meta response")
    t = text.strip()
    # strip code fences
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    # narrow to the outermost {...} block
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        t = t[start:end + 1]
    # try as-is, then with trailing commas removed (strict=False tolerates
    # literal control chars like raw newlines inside string values)
    no_trailing = re.sub(r",(\s*[}\]])", r"\1", t)
    for candidate in (t, no_trailing):
        try:
            return json.loads(candidate, strict=False)
        except Exception:
            continue
    return json.loads(no_trailing, strict=False)  # raise a clear error if still bad


# --------------------------------------------------------------------------
# Anthropic API
# --------------------------------------------------------------------------
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def call_anthropic(cfg, model, system, user_content, max_tokens, strict=False):
    key = cfg.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY missing from .env")
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": int(max_tokens),
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
    }
    # transient errors (overload, rate limit, gateway) shouldn't kill the whole
    # job - retry with backoff before giving up
    last_err = None
    for attempt in range(4):
        if attempt:
            time.sleep(min(5 * 2 ** attempt, 60))
        try:
            resp = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=600)
        except requests.RequestException as e:
            last_err = RuntimeError(f"Anthropic API connection error: {e}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504, 529):
            last_err = RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:500]}")
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:500]}")
        break
    else:
        raise last_err
    data = resp.json()
    parts = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    text = "".join(parts).strip()
    if strict and data.get("stop_reason") == "max_tokens":
        # signal truncation so callers can retry with more room instead of
        # trying to parse a cut-off response
        raise RuntimeError(f"response truncated at max_tokens={max_tokens}; "
                           f"got {len(text)} chars")
    return text


# --------------------------------------------------------------------------
# The locked script system prompt
# --------------------------------------------------------------------------
def build_script_system(p):
    """Dispatch to the selected author's locked script format."""
    s = AUTHORS[p["author"]]["build_system"](p)
    n = p["num_keys"]
    s += ("\n\nDECREES: every key CLOSES with one short first-person decree in the "
          "author's own register (a spoken decree for the manifestation authors, a "
          "quiet resolution line for Carl Jung) - introduce it naturally ('so speak "
          "this decree with me...' or the author's equivalent), speak it once and "
          "repeat it once, keep it under 15 words, and make it DISTINCT for every key.")
    unit = p.get("unit_word") or "keys"
    if unit != "keys":
        u1 = unit.rstrip("s")
        s += (f"\n\nUNIT NAME: in this video the numbered sections are called "
              f"'{unit}', not 'keys' - every spoken 'the first key is' becomes "
              f"'the first {u1} is' and so on, and the recap language matches. The "
              f"[IMAGE: MASTER KEY N ...] cue lines stay EXACTLY as specified (they "
              f"are never spoken).")
    s += (f"\n\nFINAL SELF-CHECK before you finish - all three must hold or the "
          f"script is rejected:\n"
          f"1. EXACTLY {n} keys, each opening with its ordinal ('the first key is...' "
          f"through the {n}th), never fewer, never merged.\n"
          f"2. EXACTLY {n} cue lines of the form [IMAGE: MASTER KEY N ...], numbered "
          f"1 to {n}, one immediately before each key.\n"
          f"3. The recap begins with the exact words 'here is the whole picture', and "
          f"the spoken text contains zero periods.")
    if p.get("cta_none"):
        s = re.sub(r"(?m)^5\. A SHORT comment call to action.*$",
                   "5. No comment call to action in this video - never ask viewers "
                   "to comment, type words, or engage.", s)
    if p.get("affirmation_mode") == "none" and p["author"] not in ("custom", "auto"):
        # remove the affirmation from a locked format (custom handles it natively)
        s = re.sub(r"(?m)^4\. PARTICIPATION (AFFIRMATION|RESOLUTION).*$",
                   "4. NO participation affirmation in this video - do not include "
                   "one anywhere, and never say 'say it with me'.", s)
        s = re.sub(r"(?m)^9\. The final (affirmation|resolution) once more.*$",
                   "9. A short closing line tying back to the title.", s)
    return s


def build_expand_user(p, script, current_chars):
    target = p["target_chars"]
    return (
        f"This draft is only {current_chars} characters of spoken text, but it MUST be at "
        f"least {target} characters (about {round(target/850)} minutes). Rewrite it LONGER "
        f"to reach that target.\n\n"
        f"HOW to lengthen: deepen every key with more teaching, more concrete relatable "
        f"everyday examples, and more restatement of " + p.get("author_display","the author") + "'s principles. "
        f"The later keys especially should be full, deep teachings - there is no upper limit on them.\n\n"
        f"KEEP EVERYTHING ELSE IDENTICAL: the same title as the first spoken line, the same "
        f"order and structure, the same [IMAGE: MASTER KEY N ...] cue before each key, the "
        f"[long_pause] markers, commas only and no periods, the same affirmation, and the "
        f"same ending block appearing only once at the very end. Do NOT add extra keys and do "
        f"NOT summarize.\n\nReturn ONLY the complete expanded script.\n\n"
        f"CURRENT DRAFT:\n{script}"
    )


# --------------------------------------------------------------------------
# permanent archive in a private GitHub repo (survives free-tier disk wipes)
# --------------------------------------------------------------------------
def _gh(cfg, method, path, **kw):
    tok = cfg.get("GITHUB_TOKEN")
    repo = cfg.get("STORAGE_REPO")
    if not tok or not repo:
        return None
    url = f"https://api.github.com/repos/{repo}/{path}"
    last = None
    for attempt in range(3):   # transient GitHub hiccups shouldn't lose a video
        if attempt:
            time.sleep(3 * attempt)
        try:
            last = requests.request(method, url, timeout=60,
                                    headers={"Authorization": f"Bearer {tok}",
                                             "Accept": "application/vnd.github+json"}, **kw)
        except requests.RequestException:
            continue
        if last.status_code < 500:
            return last
    return last


def archive_index(cfg):
    """Read the archive index; returns (entries, sha) - ([], None) if empty/off."""
    r = _gh(cfg, "GET", "contents/index.json")
    if r is None or r.status_code != 200:
        return [], None
    d = r.json()
    try:
        entries = json.loads(base64.b64decode(d["content"]).decode("utf-8"))
    except Exception:
        entries = []
    return entries, d.get("sha")


def _archive_put(cfg, path, data_bytes, message, sha=None):
    body = {"message": message,
            "content": base64.b64encode(data_bytes).decode("ascii")}
    if sha:
        body["sha"] = sha
    r = _gh(cfg, "PUT", f"contents/{path}", json=body)
    return r is not None and r.status_code in (200, 201)


def archive_job(cfg, job_id, title, author_display, file_paths, warn):
    """Upload the finished video's files + register it in index.json."""
    if not cfg.get("GITHUB_TOKEN") or not cfg.get("STORAGE_REPO"):
        return
    try:
        names = []
        for fp in file_paths:
            name = os.path.basename(fp)
            with open(fp, "rb") as fh:
                if not _archive_put(cfg, f"jobs/{job_id}/{name}", fh.read(),
                                    f"archive {job_id}/{name}"):
                    warn(f"Cloud archive: upload of {name} failed.")
                    return
            names.append(name)
        entries, sha = archive_index(cfg)
        entries.append({"id": job_id, "title": title, "author": author_display,
                        "ts": time.time(), "files": names})
        _archive_put(cfg, "index.json",
                     json.dumps(entries, ensure_ascii=False, indent=0).encode("utf-8"),
                     f"index + {job_id}", sha)
    except Exception as e:
        warn(f"Cloud archive failed: {e}")


def archive_prune(cfg, keep_days):
    """Drop index entries older than keep_days and delete their files."""
    try:
        entries, sha = archive_index(cfg)
        if not entries:
            return
        cutoff = time.time() - keep_days * 86400
        keep, drop = [], []
        for e in entries:
            (keep if e.get("ts", 0) >= cutoff else drop).append(e)
        if not drop:
            return
        for e in drop:
            for name in e.get("files", []):
                r = _gh(cfg, "GET", f"contents/jobs/{e['id']}/{name}")
                if r is not None and r.status_code == 200:
                    _gh(cfg, "DELETE", f"contents/jobs/{e['id']}/{name}",
                        json={"message": f"prune {e['id']}", "sha": r.json()["sha"]})
        _archive_put(cfg, "index.json",
                     json.dumps(keep, ensure_ascii=False, indent=0).encode("utf-8"),
                     "prune old entries", sha)
    except Exception:
        pass


# --------------------------------------------------------------------------
# per-author generation history -> anti-repetition context for the next video
# --------------------------------------------------------------------------
HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "output", "_history.json")


def _load_history():
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def remember_run(author, title, meta):
    """Store the distinguishing parts of a finished video so the next
    generation can be told not to repeat them."""
    if not meta:
        return
    try:
        entry = {
            "title": title,
            "premise": " ".join(str(meta.get("premise", "")).split())[:160],
            "affirmation": " ".join(str(meta.get("affirmation", "")).split())[:100],
            "keys": [str(k.get("title", ""))[:90] for k in (meta.get("keys") or [])],
        }
        hist = _load_history()
        hist.setdefault(author, [])
        hist[author] = (hist[author] + [entry])[-12:]   # keep the last 12 runs
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
            json.dump(hist, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass   # history is best-effort; never break a job over it


def variety_context(author):
    """A DO-NOT-REPEAT block built from this author's recent videos."""
    entries = _load_history().get(author, [])
    if not entries:
        return ""
    recent = entries[-8:]
    premises = [e["premise"] for e in recent if e.get("premise")]
    keys = [k for e in recent for k in e.get("keys", []) if k]
    affs = [e["affirmation"] for e in recent if e.get("affirmation")]
    block = ["", "VARIETY (critical - this channel publishes daily and repeat "
                 "viewers notice): previous videos already used the material "
                 "below. Do NOT reuse it. Choose a DIFFERENT premise quote, "
                 "different key angles, different stories and examples, and a "
                 "differently-worded affirmation (same required ending)."]
    if premises:
        block.append("Recently used premises: " + " | ".join(premises[-6:]))
    if keys:
        block.append("Recently used key headlines: " + " | ".join(keys[-30:]))
    if affs:
        block.append("Recently used affirmation openings: " + " | ".join(affs[-4:]))
    return "\n".join(block)[:2600]


def build_script_user(p):
    lines = [f"TITLE: {p['title']}"]
    lines.append(f"Target length: about {p['target_chars']} characters "
                 f"(~{round(p['target_chars']/850)} minutes at 850 chars/min).")
    lines.append(f"Number of keys: {p['num_keys']}.")
    lines.append(f"Comment CTA (two words): {p['cta']}.")
    if p.get("scripture"):
        lines.append(f"Premise to use: {p['scripture']}.")
    else:
        hint = AUTHORS[p["author"]]["premise_hint"]
        lines.append(f"Premise: none given, pick a fresh, fitting one yourself ({hint}).")
    if p.get("core"):
        lines.append(f"Core sentence/phrase this video teaches: {p['core']}.")
    if p.get("extra"):
        lines.append(f"Extra instructions: {p['extra']}.")
    if p.get("add_subscribe"):
        lines.append("SUBSCRIBE REMINDER: right after the participation affirmation "
                     "block (before the first key), add ONE warm sentence in the "
                     "author's voice gently inviting the listener to subscribe so "
                     "tomorrow's teaching reaches them - a single sentence, commas "
                     "only, no hype, then continue the locked format.")
    vc = variety_context(p.get("history_key") or p["author"])
    if vc:
        lines.append(vc)
    lines.append("\nWrite the full script now, following the locked format exactly.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The meta step (second Anthropic call) -> strict JSON
# --------------------------------------------------------------------------
def _check_meta_lengths(m):
    """Reject meta output that overshoots the locked V37/V38/V39 text budget
    (key body: 4 short sentences <=280 chars; stepping back <=500 chars) so the
    retry loop regenerates it instead of producing an overcrowded mindmap."""
    for i, k in enumerate(m.get("keys") or [], 1):
        body = k.get("body") or []
        if isinstance(body, str):
            body = [body]
        if len(body) > 4:
            raise ValueError(f"key {i} body has {len(body)} sentences, max 4")
        joined = " ".join(str(s) for s in body)
        if len(joined) > 400:
            raise ValueError(f"key {i} body is {len(joined)} chars, budget ~280 (hard cap 400)")
    sb = m.get("stepping_back") or []
    if isinstance(sb, str):
        sb = [sb]
    sb_joined = " ".join(str(s) for s in sb)
    if len(sb) > 7 or len(sb_joined) > 650:
        raise ValueError(f"stepping_back is {len(sb)} lines / {len(sb_joined)} chars, budget 4-6 lines / ~500 chars")


def compress_meta(cfg, model, meta):
    """Shrink over-budget mindmap text (key bodies / stepping back) without
    regenerating or changing anything else."""
    system = (
        "You compress text fields inside a JSON object WITHOUT changing its "
        "structure, meaning, or any other field. Required edits:\n"
        "- every keys[i].body: keep EXACTLY 4 strings - the same 4 ideas in the "
        "same order and near the same wording, each tightened to ~8-14 words, the "
        "whole body under 240 characters total.\n"
        "- stepping_back: 4-6 short lines, under 500 characters total (fold keys "
        "together; semicolons welcome).\n"
        "Every other field stays byte-identical. Inside string values use only "
        "single quotes, never double quotes. Return ONLY the full corrected JSON object."
    )
    raw = call_anthropic(cfg, model, system,
                         json.dumps(meta, ensure_ascii=False), 16000, strict=True)
    fixed = parse_json_loose(raw)
    if len(fixed.get("keys") or []) != len(meta.get("keys") or []):
        raise ValueError("compress pass changed the key count")
    return fixed


META_SYSTEM = """You extract a structured mindmap + image-prompt package from a finished video script.
Return ONLY valid JSON. No markdown fences, no prose, no trailing commentary.

The JSON object must have exactly these fields:
{
  "premise": "a short multi-line block: the quoted verse read the way Joseph Murphy read it (as a law of mind), then a line '- <Book chapter:verse>', then 2-4 short connecting lines that land emotionally (use real line breaks)",
  "affirmation": "the exact three-part first-person present-tense assumption on its own lines, ending in 'my subconscious brings it to pass', then a blank line and 1-2 short grounding lines",
  "affirmation_title": "a 2-4 word ALL-CAPS signature phrase for this video's affirmation branch (e.g. 'IMPRESS AND EXPRESS')",
  "cta": "the two-word comment phrase",
  "keys": [
    {
      "title": "the key number + a short headline that matches the script's key exactly, e.g. '1, Why one sentence is enough'",
      "body": ["sentence 1 - sums up the FIRST quarter of this key", "sentence 2 - sums up the SECOND quarter", "sentence 3 - sums up the THIRD quarter", "sentence 4 - sums up the LAST quarter. EXACTLY 4 sentences, never more, that walk through the key IN ORDER, each covering its consecutive ~quarter of the key's text so together they trace the whole key start to finish; each sentence SHORT - roughly 8-14 words, the whole body STRICTLY under 240 characters total (count them - this is the hard limit that matches the locked mindmap style) - using the script's OWN words closely (near-verbatim key phrases), not vague summaries. Compress hard: one tight clause per quarter, like 'In a crisis, a panicked mind can't manage complex rituals'"],
      "image_slot": "the visual CONCEPT from this key's [IMAGE: MASTER KEY N ...] cue in the script (what the picture should show/explain)",
      "gemini_prompt": "a full image prompt that renders that concept, in the style below"
    }
  ],
  "stepping_back": ["4-6 SHORT recap lines for the whole video, total under 500 characters. Do NOT write one full sentence per key - compress: each line is a tight clause, and one line may fold two keys together (semicolons welcome). It reads as one flowing 'here is the whole picture' paragraph, like: 'one anchor is enough in a crisis; your supply was never money but God'"],
  "closing": ["ONLY the final closing lines that tie back to the title; do NOT include the affirmation or any product/channel pitch - those are added separately"],
  "product_pitch": "(only if the video ends with a product pitch) reformat the script's closing pitch as: 1-2 short lead-in lines about why the product is needed, then a blank line, then a line '★ <PRODUCT NAME IN CAPS> ★', then 2-3 '→ ...' bullet lines including any price / refund / 'link in description', with real line breaks. Empty string otherwise."
}

ALIGNMENT (critical): everything must match the actual script.
- The keys correspond ONE-TO-ONE and IN ORDER to the script's keys (each "the [ordinal] key is..." section). 'title' and 'body' summarize that exact key.
- Take premise, affirmation, cta, stepping_back and closing from what the script actually says.
- Base each key's image_slot and gemini_prompt on THAT key's own "[IMAGE: MASTER KEY N ...]" cue in the script — the picture must illustrate that specific idea.

Each gemini_prompt renders the key's image cue as a cinematic teaching illustration that EXPLAINS the idea (a clear visual metaphor, a simple mechanism, a before-and-after contrast, or a labelled concept) — NEVER a random unrelated object, and NEVER flooded with text. Follow this exact style:
'A cinematic spiritual teaching illustration, 16:9, deep black background with a soft warm vignette, high production quality. Everything rendered in rich warm gold with a gentle glow, elegant and uncluttered. Title at top in gold serif capitals: "TITLE" (a 3 to 6 word teaching phrase capturing the key, like "THE DOOR STANDS OPEN" — not just one or two words). Central visual: <a clear visual metaphor that EXPLAINS the concept — show the transformation, contrast, or mechanism, not just a static object>. Where the idea is a contrast, render the old/negative side dim, muted and shadowed, and the true/positive side bright, warm and glowing gold, and label each side with one small gold word. Use at most 2 to 3 small gold label words inside the scene, and only where they make the idea clearer. A single gold caption line at the bottom — a fuller teaching sentence of about 6 to 12 words that states the lesson, like "at night the doubting guard sleeps - whatever you carry in takes root": "<caption>". Warm gold on black, cinematic and meaningful, lots of dark space, calm and never crowded with text.'

'keys' must contain EXACTLY the requested number of keys, in order.
CRITICAL JSON RULE: inside every string value, use ONLY single quotes ' ' for any quoted words - NEVER double quotes - so the JSON stays valid and parseable.
Output raw JSON only."""


def build_meta_user(p, script):
    return (f"The video has EXACTLY {p['num_keys']} keys. The comment CTA is "
            f"\"{p['cta']}\".\n\nHere is the finished script:\n\n{script}\n\n"
            f"Return the JSON package now, with exactly {p['num_keys']} keys.")


# --------------------------------------------------------------------------
# KeyAI / kie.ai image adapter
# --------------------------------------------------------------------------
#
#  >>> THIS IS THE ONE PLACE TO ADJUST THE IMAGE API. <<<
#
#  Wired for kie.ai's async "jobs" API (nano-banana / Gemini image models):
#    1. POST  https://api.kie.ai/api/v1/jobs/createTask
#         Authorization: Bearer <KEYAI_API_KEY>
#         body: {"model": "google/nano-banana",
#                "input": {"prompt", "output_format", "aspect_ratio"}}
#       -> returns data.taskId
#    2. GET   https://api.kie.ai/api/v1/jobs/recordInfo?taskId=<id>   (poll)
#       -> data.state in {waiting, queuing, generating, success, fail}
#       -> on success: data.resultJson is a JSON *string* -> {"resultUrls": [url]}
#    3. download the first resultUrl -> raw image bytes
#
#  .env knobs:
#    KEYAI_API_KEY      your kie.ai key (required)
#    KEYAI_ENDPOINT     default https://api.kie.ai/api/v1/jobs/createTask
#    KEYAI_MODEL        default google/nano-banana  (try google/nano-banana-pro)
#    KEYAI_ASPECT       default 16:9
#    KEYAI_OUTPUT_FORMAT default png
#
#  To swap to a different provider later, replace the body/parse logic here;
#  the rest of the pipeline just wants raw bytes back.
# --------------------------------------------------------------------------
def generate_image(prompt, cfg):
    """Return raw image bytes for a prompt using kie.ai's nano-banana jobs API."""
    key = cfg.get("KEYAI_API_KEY")
    if not key:
        raise RuntimeError("KEYAI_API_KEY missing from .env")

    create_url = cfg.get("KEYAI_ENDPOINT", "https://api.kie.ai/api/v1/jobs/createTask")
    # recordInfo lives next to createTask on the same base path
    status_url = create_url.replace("createTask", "recordInfo")
    model = cfg.get("KEYAI_MODEL", "google/nano-banana")
    aspect = cfg.get("KEYAI_ASPECT", "16:9")
    out_fmt = cfg.get("KEYAI_OUTPUT_FORMAT", "png")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    # ---- 1. create task ----
    body = {
        "model": model,
        "input": {
            "prompt": prompt[:5000],
            "output_format": out_fmt,
            "aspect_ratio": aspect,
        },
    }
    resp = requests.post(create_url, headers=headers, json=body, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"kie.ai createTask {resp.status_code}: {resp.text[:400]}")
    cdata = resp.json()
    if cdata.get("code") not in (200, None):
        raise RuntimeError(f"kie.ai createTask error: {cdata.get('msg')} ({cdata})")
    task_id = (cdata.get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"kie.ai createTask returned no taskId: {cdata}")

    # ---- 2. poll recordInfo until success/fail (up to ~5 min) ----
    deadline = 100  # polls
    interval = 3    # seconds
    for _ in range(deadline):
        time.sleep(interval)
        r = requests.get(status_url, headers=headers,
                         params={"taskId": task_id}, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"kie.ai recordInfo {r.status_code}: {r.text[:300]}")
        d = (r.json() or {}).get("data") or {}
        state = d.get("state")
        if state == "success":
            result_json = d.get("resultJson")
            if isinstance(result_json, str):
                result_json = json.loads(result_json or "{}")
            urls = (result_json or {}).get("resultUrls") or []
            if not urls:
                raise RuntimeError(f"kie.ai success but no resultUrls: {d}")
            img = requests.get(urls[0], timeout=300)
            img.raise_for_status()
            return img.content
        if state == "fail":
            raise RuntimeError(f"kie.ai generation failed: {d.get('failMsg') or d}")
        # else waiting / queuing / generating -> keep polling

    raise RuntimeError(f"kie.ai task {task_id} timed out (still {state}).")


# --------------------------------------------------------------------------
# XMind builder
# --------------------------------------------------------------------------
# The exact dark theme lifted from the reference .xmind (black bg, grey topics,
# Helvetica 18pt, central=rect, main/sub=underline, elbow connectors). This is
# the LOCKED look — do not change without a new reference file.
XMIND_THEME = {
    "map": {"id": "8a27b25b-d1f5-4242-8812-d1eb47fbbc6b", "properties": {"svg:fill": "#080808ff", "color-list": "#eeeeeeff #080808ff"}},
    "centralTopic": {"id": "69dca817-d6f4-4e10-9c83-3e306b85931a", "properties": {"fo:font-family": "Helvetica", "fo:font-size": "18pt", "fo:color": "inherited", "fo:text-align": "center", "svg:fill": "#555555ff", "fill-pattern": "none", "line-color": "#eeeeeeff", "border-line-color": "inherited", "border-line-width": "1pt", "shape-class": "org.xmind.topicShape.rect", "line-class": "org.xmind.branchConnection.roundedElbow"}},
    "mainTopic": {"id": "ce5cd480-71e3-4991-8447-eba1bde0a5a4", "properties": {"fo:font-family": "Helvetica", "fo:font-size": "18pt", "fo:color": "inherited", "fo:text-align": "left", "svg:fill": "#3c3c3c", "fill-pattern": "none", "line-color": "inherited", "border-line-color": "inherited", "shape-class": "org.xmind.topicShape.underline", "line-class": "org.xmind.branchConnection.elbow"}},
    "subTopic": {"id": "38c686ba-0c10-4b71-b691-aee48af8f983", "properties": {"fo:font-family": "Helvetica", "fo:font-size": "18", "fo:color": "inherited", "fo:text-align": "left", "svg:fill": "#3c3c3c", "fill-pattern": "none", "line-color": "inherited", "border-line-color": "inherited", "shape-class": "org.xmind.topicShape.underline"}},
    "floatingTopic": {"id": "f68087b8-3e96-447b-94d2-6b764d40f1c5", "properties": {"fo:color": "inherited", "svg:fill": "#3c3c3c", "line-color": "inherited", "border-line-color": "#eeeeeeff"}},
    "summaryTopic": {"id": "1759a632-2960-4eb7-9653-3fe373e6ef0b", "properties": {"fo:color": "inherited", "svg:fill": "#6f6f6fff", "line-color": "inherited", "border-line-color": "#6f6f6fff"}},
    "calloutTopic": {"id": "d7711de5-f709-42c2-bab4-3276a22b29e0", "properties": {"fo:color": "inherited", "svg:fill": "#6f6f6fff", "line-color": "inherited", "border-line-color": "#6f6f6fff"}},
    "importantTopic": {"id": "1bb65a22-eb4a-4202-9235-1e229bb2a1d5", "properties": {"svg:fill": "#a61d39", "border-line-color": "#eeeeeeff"}},
    "minorTopic": {"id": "1b5c8093-b176-4df5-8395-2a7c24cedb86", "properties": {"svg:fill": "#b16c00", "border-line-color": "#b16c00"}},
    "boundary": {"id": "d283b7cb-dc1e-4fe0-a516-a513c97133f0", "properties": {"fo:color": "inherited", "svg:fill": "#6f6f6fff", "line-color": "#6f6f6fff"}},
    "zone": {"id": "e3d10791-235f-470c-a704-cba67fe63093", "properties": {"fo:color": "inherited", "svg:fill": "#6f6f6f33", "border-line-color": "#6f6f6fff"}},
    "summary": {"id": "bdc32616-1a40-45b7-b996-02cd2be59e69", "properties": {"line-color": "#6f6f6fff"}},
    "relationship": {"id": "8f324a3f-0381-49da-a68f-239d519aa1be", "properties": {"fo:color": "inherited", "line-color": "#6f6f6fff"}},
    "colorThemeId": "723815a9-c06c-412b-a7bd-9be493e18d96",
}

# 1x1 black PNG, used as the .xmind thumbnail when no image is available.
_BLACK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def _topic(title, children=None, image=None, custom_width=None,
           folded=False, attributed=None):
    """One topic node in the reference's shape."""
    t = {"id": nid(), "title": title}
    if attributed:
        t["attributedTitle"] = attributed
    if image:
        t["image"] = image
    if custom_width is not None:
        t["customWidth"] = custom_width
    if folded:
        t["branch"] = "folded"
    if children:
        t["children"] = {"attached": children}
    return t


def _img(sha):
    """Image sub-object exactly as the reference embeds it (16:9, sat below text)."""
    return {"src": f"xap:resources/{sha}.png",
            "width": 401.33333333333337, "height": 224, "align": "bottom"}


def _lines(x, fallback=""):
    """Join a list of lines with newlines; pass strings through."""
    if isinstance(x, list):
        return "\n".join(str(i) for i in x)
    return str(x) if x else fallback


def _caps_hook(title):
    """Turn the title into a CAPS hook line + normal subtitle line, like the
    reference title-branch (e.g. 'IF YOU WAKE UP AT 3AM\\nGod Is Ready...')."""
    t = title.replace("\n", " ").strip()
    for sep in [" - ", " — ", ": ", ", "]:
        if sep in t:
            a, b = t.split(sep, 1)
            return a.strip().upper() + "\n" + b.strip()
    return t.upper()


def build_xmind(path, title, num_keys, ending, meta, images,
                product_name="The Method",
                funnel_channel="Divine Manifestation",
                signature="- Unknown Author",
                product_fallback="★ {PRODUCT} ★\n→ link in description.",
                anchor="my word is my wand",
                premise_on=True, affirmation_on=True, unit_word="Keys"):
    """Write a valid .xmind zip in the LOCKED reference style.

    images: dict {index -> png_bytes} for keys that got an image.
    meta:   parsed meta dict (may be None -> skeleton with placeholders).
    """
    import hashlib
    num_word = "NINE" if num_keys == 9 else "SIX"
    unit_title = (unit_word or "Keys").capitalize()
    meta = meta or {}

    # ---- meta pieces ----
    premise = _lines(meta.get("premise"), "[premise unavailable]")
    affirmation = _lines(meta.get("affirmation"),
                         f"[affirmation unavailable, ends '{anchor}']")
    aff_title = (meta.get("affirmation_title") or "THE AFFIRMATION").upper()
    cta = meta.get("cta", "")
    keys = meta.get("keys", []) or []
    stepping = _lines(meta.get("stepping_back"), "here is the whole picture")
    closing = _lines(meta.get("closing"), "closing")

    # ---- content-addressed image resources (sha256, like the reference) ----
    resources = {}          # "resources/<sha>.png" -> bytes
    sha_by_index = {}       # key index -> sha
    for i, png in images.items():
        sha = hashlib.sha256(png).hexdigest()
        resources[f"resources/{sha}.png"] = png
        sha_by_index[i] = sha

    # ---- key nodes: each key -> one folded child holding 4 lines + image ----
    key_nodes = []
    for i in range(num_keys):
        k = keys[i] if i < len(keys) else {}
        ktitle = k.get("title", f"{i+1}, Key {i+1}")
        body_text = _lines(k.get("body", []))
        _decree = str(k.get("decree", "") or "").strip().strip("'\"")
        if _decree:
            body_text += f"\n\n'{_decree}'"
        if i in sha_by_index:
            child = _topic(body_text, image=_img(sha_by_index[i]),
                          custom_width=490, folded=True)
        else:
            slot = k.get("image_slot", "")
            txt = body_text + (f"\n\n[ image slot: {slot} ]" if slot else "")
            child = _topic(txt, custom_width=490, folded=True)
        key_nodes.append(_topic(ktitle, children=[child]))

    # product: "The Daily Covenant" node as the last item inside the keys list.
    # Prefer the script-aligned pitch from meta; fall back to a template.
    if ending == "product":
        cov = _lines(meta.get("product_pitch")) or \
            product_fallback.replace("{PRODUCT}", product_name.upper())
        key_nodes.append(_topic("The Daily Covenant",
                                children=[_topic(cov, custom_width=490)]))

    keys_group = _topic(f"The {num_word} {unit_title}:", children=key_nodes)

    # ---- funnel: TWO second-channel nodes, matching the reference ----
    # (a) an early top-level mention before the title branch, and
    # (b) a late mention inside the title branch, between Stepping Back and Closing.
    funnel_early = _topic(funnel_channel, folded=True, children=[_topic(
        "Early mention (catches everyone before drop-off):\n\n"
        f"→ 'if this teaching speaks to you, I have a second channel called "
        f"{funnel_channel}, go subscribe, the link is in the description'",
        custom_width=490, folded=True)])
    funnel_late = _topic(funnel_channel, children=[_topic(
        "Second mention (catches the committed viewers):\n\n"
        f"→ 'if you want to go deeper, subscribe to {funnel_channel}, "
        f"the link is in the description'",
        custom_width=490, folded=True)])

    # ---- closing node: closing words, then a product/funnel line, then the
    # final affirmation flattened onto one line and wrapped in single quotes,
    # each part separated by a blank line (matching the reference) ----
    final_aff = affirmation.split("\n\n", 1)[0].replace("\n", " ").strip()
    closing_parts = [closing]
    if ending == "product":
        closing_parts.append(f"{product_name} builds this day by day - link below.")
    elif ending == "funnel":
        closing_parts.append(f"Go deeper on the second channel, {funnel_channel} - link below.")
    if affirmation_on:
        closing_parts.append(f"'{final_aff}'")
    closing_full = "\n\n".join(p for p in closing_parts if p)

    # ---- video-title branch: keys -> Stepping Back -> (funnel late) -> Closing ----
    title_children = [
        keys_group,
        _topic("Stepping Back", children=[_topic(stepping, custom_width=490, folded=True)]),
    ]
    if ending == "funnel":
        title_children.append(funnel_late)
    title_children.append(
        _topic("Closing", children=[_topic(closing_full, custom_width=490, folded=True)]))
    title_branch = _topic(_caps_hook(title), children=title_children)

    # ---- affirmation branch (CTA folded in via attributedTitle) ----
    # wrap the assumption itself in quotes (grounding lines after a blank line stay plain)
    _ap = affirmation.split("\n\n", 1)
    aff_body = '"' + _ap[0].strip() + '"'
    if len(_ap) > 1 and _ap[1].strip():
        aff_body += "\n\n" + _ap[1].strip()
    aff_attr = [{"text": aff_body + "\n\n"}]
    aff_full = aff_body
    if cta:
        aff_full = aff_body + f"\n\nType two words:\n\"{cta}\""
        aff_attr.append({"text": f"Type two words:\n\"{cta}\""})
    aff_branch = _topic("Participation Affirmation", children=[
        _topic("Say it with me:", custom_width=340, folded=True),
        _topic(aff_full, custom_width=450, folded=True, attributed=aff_attr),
    ])

    premise_branch = _topic("Premise",
                            children=[_topic(premise, custom_width=490, folded=True)])

    branches = []
    if premise_on:
        branches.append(premise_branch)
    if affirmation_on:
        branches.append(aff_branch)
    elif cta:
        # no affirmation block, but the comment CTA still gets its node
        branches.append(_topic("Comment CTA", children=[
            _topic(f'Type two words:\n"{cta}"', custom_width=340, folded=True)]))
    if ending == "funnel":
        branches.append(funnel_early)   # early top-level mention
    branches.append(title_branch)

    root = {
        "id": nid(), "class": "topic", "title": (title + "\n" + signature).strip(),
        "structureClass": "org.xmind.ui.logic.right",
        "children": {"attached": branches},
    }
    sheet = {
        "id": nid(), "class": "sheet", "title": "Map 1",
        "rootTopic": root, "theme": XMIND_THEME,
        "topicOverlapping": "overlap", "zones": [],
    }
    content = [sheet]

    metadata = {
        "dataStructureVersion": "3",
        "creator": {"name": "Shinn Video Maker", "version": "1.0"},
        "layoutEngineVersion": "5",
    }

    # thumbnail: first real image if we have one, else a black placeholder
    thumb = next(iter(images.values()), _BLACK_PNG)

    file_entries = {"content.json": {}, "metadata.json": {},
                    "Thumbnails/thumbnail.png": {}}
    for zip_res in resources:
        file_entries[zip_res] = {}
    manifest = {"file-entries": file_entries}

    # ---- write zip ----
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("content.json", json.dumps(content, ensure_ascii=False))
        z.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False))
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        z.writestr("Thumbnails/thumbnail.png", thumb)
        for zip_res, b in resources.items():
            z.writestr(zip_res, b)


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------
def compute_stats(script):
    spoken = re.sub(r"\[[^\]]*\]", "", script)  # strip [IMAGE:...] and [long_pause]
    spoken_chars = len(spoken.strip())
    image_cues = len(re.findall(r"\[IMAGE:", script))
    periods = script.count(".")
    minutes = round(spoken_chars / 850, 1)
    return {
        "spoken_chars": spoken_chars,
        "minutes": minutes,
        "image_cues": image_cues,
        "periods": periods,
    }


def key_budgets(target_chars, num_keys):
    """Keys 1-3 have locked sizes (2, 3, 4 minutes at ~850 chars/min).
    Keys 4+ have no per-key rule - they share the rest of the target evenly,
    purely as an expansion floor so the TOTAL lands on target_chars."""
    fixed = [1700, 2550, 3400][:min(3, num_keys)]
    rest = num_keys - len(fixed)
    if rest <= 0:
        return fixed[:num_keys]
    overhead = 3500  # premise + affirmation + recap + ending block
    pool = max(target_chars - overhead - sum(fixed), rest * 800)
    return fixed + [int(pool / rest)] * rest


KEY_CUE_RE = re.compile(r"\[IMAGE:\s*MASTER\s+KEY\s+(\d+)", re.I)
RECAP_RE = re.compile(r"here(?:\u2019s|'s| is) the whole picture", re.I)


def spoken_len(text):
    return len(re.sub(r"\[[^\]]*\]", "", text).strip())


def split_key_sections(script, num_keys):
    """Split the script into (head, [key sections], tail). Each key section
    starts at its [IMAGE: MASTER KEY N ...] cue and runs to the next cue;
    the last one ends where the recap paragraph begins. Returns None if the
    script doesn't have exactly num_keys cues + a recap (caller falls back)."""
    cues = list(KEY_CUE_RE.finditer(script))
    if len(cues) != num_keys:
        return None
    m = RECAP_RE.search(script, cues[-1].end())
    if not m:
        return None
    tail_start = script.rfind("\n", cues[-1].end(), m.start())
    tail_start = m.start() if tail_start == -1 else tail_start + 1
    head = script[:cues[0].start()]
    sections = []
    for i, c in enumerate(cues):
        end = cues[i + 1].start() if i + 1 < len(cues) else tail_start
        sections.append(script[c.start():end])
    return head, sections, script[tail_start:]


def build_expand_key_user(p, script, key_num, section, budget, exact=False):
    if exact:
        size_rule = (f"it has a LOCKED length of about {budget} characters "
                     f"(~{round(budget / 850, 1)} minutes) - reach it, but do not run far past it")
    else:
        size_rule = (f"it must be at least {budget} characters "
                     f"(~{round(budget / 850, 1)} minutes of speaking), longer is fine")
    return (
        f"Below is the full finished script. Key {key_num} is only "
        f"{spoken_len(section)} characters of spoken text, but {size_rule}.\n\n"
        f"Rewrite ONLY key {key_num}, making it LONGER and deeper: more teaching, a "
        f"concrete relatable everyday story, the same idea restated from new angles - "
        f"never filler, never a new topic, and do NOT drift into the other keys' points.\n\n"
        f"KEEP THE LOCKED FORMAT: start your output with this key's "
        f"[IMAGE: MASTER KEY {key_num} ...] cue line exactly as it is, keep the same key "
        f"heading wording, commas only and no periods, keep [long_pause] markers, and end "
        f"where the key ends (do NOT include the next key, the recap, or the ending).\n\n"
        f"Return ONLY the rewritten key {key_num} section, nothing else.\n\n"
        f"FULL SCRIPT:\n{script}"
    )


def build_trim_key_user(p, script, key_num, section, budget):
    return (
        f"Below is the full finished script. Key {key_num} is {spoken_len(section)} characters of "
        f"spoken text, which makes the whole script longer than requested - condense this key "
        f"to about {budget} characters.\n\n"
        f"HOW to shorten: cut repetition and filler while KEEPING every distinct teaching beat, "
        f"story and example (compressed, not deleted) - the key must still feel complete.\n\n"
        f"KEEP THE LOCKED FORMAT: start with this key's [IMAGE: MASTER KEY {key_num} ...] cue "
        f"line exactly as it is, keep the same key heading wording, commas only and no periods, "
        f"keep [long_pause] markers, and end where the key ends.\n\n"
        f"Return ONLY the rewritten key {key_num} section, nothing else.\n\n"
        f"FULL SCRIPT:\n{script}"
    )


FALLBACK_IMAGE_STYLE = (
    'A cinematic spiritual teaching illustration, 16:9, deep black background with a '
    'soft warm vignette, high production quality. Everything rendered in rich warm '
    'gold with a gentle glow, elegant and uncluttered. Central visual: {concept}, '
    'rendered as a clear meaningful visual metaphor in gleaming gold. '
    'A single short gold caption line at the bottom summarizing the idea. '
    'Warm gold on black, cinematic and meaningful, lots of dark space, calm and '
    'never crowded with text. Do NOT write the words "master key" anywhere in the image.'
)


def extract_image_prompts_from_script(script):
    """Fallback if the meta step fails: pull each [IMAGE: ...] cue, strip the
    'MASTER KEY N - ...' label, and wrap the concept in the full locked style
    so fallback images still match the channel look."""
    out = []
    for m in re.findall(r"\[IMAGE:\s*(.*?)\]", script):
        concept = m.strip()
        # strip "MASTER KEY N - gold-on-black diagram:" style prefixes
        concept = re.sub(r"(?i)^master\s+key\s+\d+\s*[-–:]\s*", "", concept)
        concept = re.sub(r"(?i)^gold-on-black\s+diagram\s*:\s*", "", concept)
        out.append(FALLBACK_IMAGE_STYLE.format(concept=concept.strip()))
    return out


# --------------------------------------------------------------------------
# The end-to-end runner (called in a background thread by server.py)
# --------------------------------------------------------------------------
SCENE_CUE_RE = re.compile(r"\[SCENE\s+(\d+)", re.I)

SLEEP_IMAGE_STYLE = (
    "A dreamlike spiritual night illustration, 16:9, deep midnight blue-black "
    "background, soft luminous warm gold light, ethereal and calm, high production "
    "quality. Central visual: {concept}, rendered as a gentle glowing dream image. "
    "NO text anywhere in the image, no words, no letters - pure imagery only. "
    "Misty, weightless, sacred, restful.")


def build_xmind_sleep(path, title, meta, images, signature=""):
    """Sleep-mode mindmap: central title -> Tonight (hook) -> CAPS title branch
    holding the numbered movements (essence + declaration + image) -> Closing."""
    import hashlib
    meta = meta or {}
    hook = _lines(meta.get("hook"), "[hook unavailable]")
    closing = _lines(meta.get("closing"), "closing")
    phrase = str(meta.get("comment_phrase", "") or "").strip().strip("'\"")
    movements = meta.get("movements", []) or []

    resources = {}
    sha_by_index = {}
    for i, png in images.items():
        sha = hashlib.sha256(png).hexdigest()
        resources[f"resources/{sha}.png"] = png
        sha_by_index[i] = sha

    move_nodes = []
    for i, m in enumerate(movements):
        mtitle = f"{i+1}, {str(m.get('title') or f'Movement {i+1}').strip()}"
        body = _lines(m.get("essence"), "")
        decl = str(m.get("declaration", "") or "").strip().strip("'\"")
        if decl:
            body = (body + "\n\n" if body else "") + f"'{decl}'"
        if i in sha_by_index:
            child = _topic(body or "...", image=_img(sha_by_index[i]),
                           custom_width=490, folded=True)
        else:
            child = _topic(body or "...", custom_width=490, folded=True)
        move_nodes.append(_topic(mtitle, children=[child]))

    closing_text = closing
    if phrase:
        closing_text += f"\n\nComment: '{phrase}'"
    title_branch = _topic(_caps_hook(title), children=[
        _topic(f"The Journey ({len(move_nodes)} movements):", children=move_nodes),
        _topic("Closing", children=[_topic(closing_text, custom_width=490, folded=True)]),
    ])
    branches = [
        _topic("Tonight", children=[_topic(hook, custom_width=490, folded=True)]),
        title_branch,
    ]

    root = {
        "id": nid(), "class": "topic", "title": (title + "\n" + signature).strip(),
        "structureClass": "org.xmind.ui.logic.right",
        "children": {"attached": branches},
    }
    sheet = {
        "id": nid(), "class": "sheet", "title": "Map 1",
        "rootTopic": root, "theme": XMIND_THEME,
        "topicOverlapping": "overlap", "zones": [],
    }
    metadata = {
        "dataStructureVersion": "3",
        "creator": {"name": "Shinn Video Maker", "version": "1.0"},
        "layoutEngineVersion": "5",
    }
    thumb = next(iter(images.values()), _BLACK_PNG)
    file_entries = {"content.json": {}, "metadata.json": {},
                    "Thumbnails/thumbnail.png": {}}
    for zip_res in resources:
        file_entries[zip_res] = {}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("content.json", json.dumps([sheet], ensure_ascii=False))
        z.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False))
        z.writestr("manifest.json", json.dumps({"file-entries": file_entries}, ensure_ascii=False))
        z.writestr("Thumbnails/thumbnail.png", thumb)
        for zip_res, b in resources.items():
            z.writestr(zip_res, b)


def run_pipeline_sleep(job_id, p, job, output_root):
    """Sleep/Meditation mode: hypnotic journey script + movement mindmap."""
    from authors import build_sleep_system, META_SLEEP
    try:
        job["warnings"] = []
        cfg = load_env()
        if p.get("custom_api_key"):
            cfg["ANTHROPIC_API_KEY"] = p["custom_api_key"]
        author = AUTHORS[p.get("author") or "shinn"]
        p["author"] = p.get("author") or "shinn"
        p["author_display"] = author["display"]
        signature = author["signature"]
        if p["author"] == "custom":
            focus = (p.get("custom_focus") or "Spiritual Teaching").strip()
            p["author_display"] = focus.title()
            signature = "- " + focus.title()
        elif p["author"] == "auto":
            p["author_display"] = "Auto (from title)"
            signature = ""
        hist_key = "sleep_" + (p.get("custom_focus") and
                               ("custom_" + slugify(p["custom_focus"], 24)) or p["author"])
        model = p.get("model_override") or cfg.get("MODEL", "claude-sonnet-5")
        do_images = p.get("do_images", True)
        title = p["title"]
        slug = slugify(title)
        target = p["target_chars"]
        p["n_scenes"] = max(8, min(20, target // 2800))

        out_dir = os.path.join(output_root, job_id)
        os.makedirs(out_dir, exist_ok=True)
        try:
            with open(os.path.join(out_dir, "_title.txt"), "w", encoding="utf-8") as fh:
                fh.write(title + "\n" + p["author_display"] + " (sleep)")
        except Exception:
            pass

        # ---------- 1. script ----------
        job.update(stage=f"Writing the sleep journey ({p['author_display']})...", progress=8)
        system = build_sleep_system(p)
        user_msg = (f"TITLE: {title}\n"
                    f"Target length: about {target} characters (~{round(target/850)} minutes "
                    f"of unique spoken content).\n"
                    f"Number of movements: exactly {p['n_scenes']}.\n")
        if p.get("extra"):
            user_msg += f"Extra instructions: {p['extra']}.\n"
        vc = variety_context(hist_key)
        if vc:
            user_msg += vc + "\n"
        user_msg += "\nWrite the full sleep journey script now, following the locked format exactly."
        max_tokens = min(int(target / 2.6), 32000)
        script = call_anthropic(cfg, model, system, user_msg, max_tokens)

        # scene-count guard (one repair attempt)
        cues = len(SCENE_CUE_RE.findall(script))
        if abs(cues - p["n_scenes"]) > 1:
            job.update(stage=f"Repairing scenes ({cues}/{p['n_scenes']})...", progress=14)
            try:
                fixed = call_anthropic(
                    cfg, model, system,
                    (f"This script has {cues} [SCENE N ...] cue lines but must have exactly "
                     f"{p['n_scenes']}, numbered 1 to {p['n_scenes']} in order, one before each "
                     f"movement. Merge or split movements at natural points to fix this, keep "
                     f"everything else identical, and return ONLY the corrected script.\n\n"
                     f"SCRIPT:\n{script}"),
                    min(int(len(script) / 2.6) + 3000, 32000))
                if abs(len(SCENE_CUE_RE.findall(fixed)) - p["n_scenes"]) <= 1 \
                        and spoken_len(fixed) > 0.8 * spoken_len(script):
                    script = fixed
            except Exception as e:
                job["warnings"].append(f"Scene repair failed: {e}")
        stats = compute_stats(script)

        # length: expand up to 2 passes, then one condense if far over
        tries = 0
        while stats["spoken_chars"] < 0.92 * target and tries < 2:
            tries += 1
            job.update(stage=f"Journey is {stats['spoken_chars']}/{target} chars, "
                             f"deepening (pass {tries})...", progress=14 + tries * 6)
            try:
                longer = call_anthropic(
                    cfg, model, system,
                    (f"This journey is only {stats['spoken_chars']} characters of spoken text "
                     f"but must reach about {target}. Rewrite it LONGER by deepening every "
                     f"movement experientially - slower, richer imagery, more space - never "
                     f"by adding teaching. Keep the same [SCENE N ...] cues, the same "
                     f"declarations, the same order and endings. Return ONLY the complete "
                     f"expanded script.\n\nCURRENT DRAFT:\n{script}"),
                    max_tokens)
            except Exception as e:
                job["warnings"].append(f"Deepen pass {tries} failed: {e}")
                break
            ns = compute_stats(longer)
            if ns["spoken_chars"] > stats["spoken_chars"] + 300 \
                    and abs(len(SCENE_CUE_RE.findall(longer)) - p["n_scenes"]) <= 1:
                script, stats = longer, ns
            else:
                break
        if stats["spoken_chars"] > 1.15 * target:
            job.update(stage="Journey over target - condensing...", progress=28)
            try:
                shorter = call_anthropic(
                    cfg, model, system,
                    (f"This journey is {stats['spoken_chars']} characters but must be about "
                     f"{target} (within five percent). Condense it by removing repetition, "
                     f"never removing whole movements or declarations. Keep all [SCENE N ...] "
                     f"cues. Return ONLY the condensed script.\n\nSCRIPT:\n{script}"),
                    max_tokens)
                cs = compute_stats(shorter)
                if 0.85 * target <= cs["spoken_chars"] < stats["spoken_chars"]:
                    script, stats = shorter, cs
            except Exception as e:
                job["warnings"].append(f"Condense failed: {e}")

        script_path = os.path.join(out_dir, f"{slug}_RECORDING.txt")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(script)

        # ---------- 2. meta ----------
        job.update(stage="Extracting movements + image prompts...", progress=35)
        meta = None
        meta_raw = None
        for attempt in range(3):
            try:
                meta_raw = call_anthropic(
                    cfg, model, META_SLEEP,
                    (f"The script has {p['n_scenes']} [SCENE N ...] movements.\n\n"
                     f"Here is the finished script:\n\n{script}\n\n"
                     f"Return the JSON package now."),
                    16000, strict=True)
                cand = parse_json_loose(meta_raw)
                if len(cand.get("movements") or []) < max(6, p["n_scenes"] - 2):
                    raise ValueError("too few movements in meta")
                meta = cand
                break
            except Exception:
                continue
        if meta is None and meta_raw:
            try:
                fixed = call_anthropic(
                    cfg, model,
                    "You repair broken JSON. Escape inner double quotes, remove trailing "
                    "commas, return ONLY the corrected JSON object.",
                    meta_raw, 16000)
                meta = parse_json_loose(fixed)
                job["warnings"].append("Sleep meta needed a repair pass.")
            except Exception as e:
                job["warnings"].append(f"Sleep meta failed: {e}")

        # ---------- 3. images ----------
        images = {}
        movements = (meta or {}).get("movements", []) or []
        if do_images and movements:
            def _one_image(idx, pr):
                png = generate_image(pr, cfg)
                with open(os.path.join(out_dir, f"{slug}_scene{idx+1}.png"), "wb") as fh:
                    fh.write(png)
                return png
            futures = {}
            with ThreadPoolExecutor(max_workers=5) as ex:
                for i, m in enumerate(movements):
                    pr = (m.get("gemini_prompt") or "").strip() or \
                         SLEEP_IMAGE_STYLE.format(concept=(m.get("image_slot") or m.get("title") or "a calm night sky"))
                    futures[ex.submit(_one_image, i, pr)] = i
                job.update(stage=f"Generating {len(futures)} scene images (in parallel)...", progress=45)
                done = 0
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        images[i] = fut.result()
                    except Exception as e:
                        job["warnings"].append(f"Scene {i+1} image failed: {e}")
                    done += 1
                    job.update(stage=f"Scene images ({done}/{len(futures)})...",
                               progress=45 + int(35 * done / max(len(futures), 1)))
        elif not do_images:
            job["warnings"].append("Image generation skipped (checkbox off).")

        # ---------- 4. xmind + finish ----------
        job.update(stage="Building the sleep mindmap...", progress=88)
        xmind_path = os.path.join(out_dir, f"{slug}_MindMap.xmind")
        build_xmind_sleep(xmind_path, title, meta, images, signature=signature)

        if meta:
            remember_run(hist_key, title, {
                "premise": str(meta.get("hook", ""))[:160],
                "affirmation": str(meta.get("comment_phrase", ""))[:100],
                "keys": [{"title": str(m.get("title", ""))[:90]} for m in movements],
            })
        job.update(stage="Archiving to permanent storage...", progress=97)
        archive_job(cfg, job_id, title, p["author_display"] + " (sleep)",
                    [script_path, xmind_path], job["warnings"].append)
        job["result"] = {
            "files": [os.path.basename(script_path), os.path.basename(xmind_path)],
            "stats": stats, "slug": slug,
        }
        job.update(stage="Done", progress=100, status="done")
    except Exception as e:
        job["warnings"] = job.get("warnings", [])
        job.update(stage=f"Error: {e}", progress=100, status="error", error=str(e))


def run_pipeline(job_id, p, job, output_root):
    """Mutates `job` in place: job['stage'], job['progress'], job['status'],
    job['warnings'], job['result']."""
    if p.get("video_type") == "sleep":
        return run_pipeline_sleep(job_id, p, job, output_root)
    try:
        job["warnings"] = []
        cfg = load_env()
        if p.get("custom_api_key"):
            # the visitor's own key pays for this job; used in memory only,
            # never written to disk or logs
            cfg["ANTHROPIC_API_KEY"] = p["custom_api_key"]
        author = AUTHORS[p.get("author") or "shinn"]
        p["author"] = p.get("author") or "shinn"
        p["author_display"] = author["display"]
        signature = author["signature"]
        if p["author"] == "custom":
            focus = (p.get("custom_focus") or "Spiritual Teaching").strip()
            p["author_display"] = focus.title()
            signature = "- " + focus.title()
            # each custom topic gets its own do-not-repeat memory
            p["history_key"] = "custom_" + slugify(focus, 24)
        elif p["author"] == "auto":
            p["author_display"] = "Auto (from title)"
            signature = ""   # central node shows the title alone
        model = p.get("model_override") or cfg.get("MODEL", "claude-sonnet-5")
        do_images = p.get("do_images", True)
        title = p["title"]
        slug = slugify(title)

        # channel-specific names (editable in .env, no code change needed)
        akey = p["author"].upper()
        p["product_name"] = cfg.get(f"PRODUCT_NAME_{akey}") or cfg.get("PRODUCT_NAME") or author["product_default"]
        p["funnel_channel"] = cfg.get(f"FUNNEL_CHANNEL_{akey}") or cfg.get("FUNNEL_CHANNEL") or author["funnel_default"]

        out_dir = os.path.join(output_root, job_id)
        os.makedirs(out_dir, exist_ok=True)
        # the human title names this video in the Stored videos library
        # (underscore prefix keeps it out of the download listings)
        try:
            with open(os.path.join(out_dir, "_title.txt"), "w", encoding="utf-8") as fh:
                fh.write(title + "\n" + p["author_display"])
        except Exception:
            pass

        # ---------- 1. script ----------
        job.update(stage=f"Writing the script with the Anthropic API ({author['display']})...", progress=8)
        system = build_script_system(p)
        target = p["target_chars"]
        # generous token headroom (~4 chars/token) so length is never capped
        max_tokens = min(int(target / 2.6), 32000)
        script = call_anthropic(cfg, model, system, build_script_user(p), max_tokens)

        # format guard: the script must have exactly num_keys keys (each with
        # its cue line) and the locked recap opener. Repair can SPLIT oversized
        # keys into real new ones; if repair fails, regenerate once.
        def _format_ok(sc):
            return (len(KEY_CUE_RE.findall(sc)) == p["num_keys"]
                    and bool(RECAP_RE.search(sc)))

        for gen_try in range(2):
            if _format_ok(script):
                break
            for rep_try in range(2):
                cues = len(KEY_CUE_RE.findall(script))
                job.update(stage=f"Repairing format ({cues}/{p['num_keys']} keys, "
                                 f"attempt {rep_try + 1})...", progress=12)
                try:
                    fixed = call_anthropic(
                        cfg, model, system,
                        (f"This script violates the locked format: it has {cues} "
                         f"[IMAGE: MASTER KEY N ...] cue lines / key sections but must have "
                         f"EXACTLY {p['num_keys']} keys. Fix it while keeping all the "
                         f"teaching content:\n"
                         f"- If there are fewer than {p['num_keys']} keys, SPLIT the longest "
                         f"keys into two real keys each at a natural point, giving every key "
                         f"its own ordinal opener ('the third key is...') and its own "
                         f"[IMAGE: MASTER KEY N ...] cue line, renumbering so the ordinals and "
                         f"cue numbers run 1 to {p['num_keys']} in order.\n"
                         f"- If a cue line is merely missing, insert it immediately before its key "
                         f"(write a fitting gold-on-black visual).\n"
                         f"- The recap must begin with the exact words 'here is the whole picture'.\n"
                         f"- Commas only, no periods, and change nothing else.\n"
                         f"Return ONLY the corrected script.\n\nSCRIPT:\n{script}"),
                        min(int(len(script) / 2.6) + 3000, 32000))
                    if (_format_ok(fixed)
                            and spoken_len(fixed) > 0.8 * spoken_len(script)):
                        script = fixed
                        break
                except Exception as e:
                    job["warnings"].append(f"Format repair failed: {e}")
                    break
            if _format_ok(script):
                break
            if gen_try == 0:
                job.update(stage="Format still wrong - regenerating the script...",
                           progress=10)
                try:
                    script = call_anthropic(
                        cfg, model, system,
                        build_script_user(p) +
                        f"\n\nCRITICAL: the previous attempt produced the wrong number of "
                        f"keys. This script MUST contain exactly {p['num_keys']} keys, each "
                        f"with its own [IMAGE: MASTER KEY N ...] cue line, numbered 1 to "
                        f"{p['num_keys']}.",
                        max_tokens)
                except Exception as e:
                    job["warnings"].append(f"Regeneration failed: {e}")
                    break
        if not _format_ok(script):
            job["warnings"].append(
                f"Format still imperfect after repair + regeneration "
                f"({len(KEY_CUE_RE.findall(script))}/{p['num_keys']} keys).")
        stats = compute_stats(script)

        # per-key expansion: one-pass generation stalls well under long targets.
        # Keys 1-3 are brought to their locked 2/3/4-minute sizes; keys 4+ have
        # no rule of their own, so only the shortest are grown - just enough for
        # the TOTAL to land on target. The rewrites are independent (each call
        # replaces only its own key with the identical prompt as before), so
        # they run IN PARALLEL for speed.
        if stats["spoken_chars"] < 0.92 * target:
            parts = split_key_sections(script, p["num_keys"])
            if parts:
                head, sections, tail = parts
                budgets = key_budgets(target, p["num_keys"])
                n_fixed = min(3, p["num_keys"])  # keys with a locked minute size

                def _expand_one(i, base_script):
                    """Rewrite key i+1 alone; returns (i, new_section | None)."""
                    try:
                        bigger = call_anthropic(
                            cfg, model, system,
                            build_expand_key_user(p, base_script, i + 1, sections[i],
                                                  budgets[i], exact=i < n_fixed),
                            min(int(budgets[i] / 2.2) + 1000, 9000)).strip()
                    except Exception as e:
                        job["warnings"].append(f"Key {i+1} expand failed: {e}")
                        return i, None
                    # accept only a valid, longer replacement for THIS key
                    mm = KEY_CUE_RE.search(bigger)
                    if (mm and int(mm.group(1)) == i + 1
                            and len(KEY_CUE_RE.findall(bigger)) == 1
                            and spoken_len(bigger) > spoken_len(sections[i]) + 200):
                        return i, bigger.rstrip() + "\n\n"
                    return i, None

                for round_no in range(2):
                    # keys 1-3: always fill their locked sizes
                    todo = [i for i in range(n_fixed)
                            if spoken_len(sections[i]) < 0.9 * budgets[i]]
                    # keys 4+: shortest first, only enough to reach the total
                    base = head + "".join(sections) + tail
                    shortfall = (target - compute_stats(base)["spoken_chars"]
                                 - sum(budgets[i] - spoken_len(sections[i]) for i in todo))
                    for i in sorted(range(n_fixed, p["num_keys"]),
                                    key=lambda k: spoken_len(sections[k])):
                        if shortfall <= 0:
                            break
                        if spoken_len(sections[i]) < 0.9 * budgets[i]:
                            todo.append(i)
                            shortfall -= budgets[i] - spoken_len(sections[i])
                    if not todo:
                        break
                    job.update(
                        stage=(f"Expanding keys {', '.join(str(i+1) for i in sorted(todo))} "
                               f"in parallel (round {round_no + 1})..."),
                        progress=12 + 14 * round_no)
                    with ThreadPoolExecutor(max_workers=4) as ex:
                        for i, new_sec in ex.map(lambda k: _expand_one(k, base), todo):
                            if new_sec:
                                sections[i] = new_sec
                script = head + "".join(sections) + tail
                stats = compute_stats(script)
            else:
                job["warnings"].append(
                    "Couldn't split the script into keys for per-key expansion; "
                    "falling back to whole-script expansion.")

        # legacy whole-script expansion as a final nudge if still short
        tries = 0
        while stats["spoken_chars"] < 0.92 * target and tries < 2:
            tries += 1
            job.update(
                stage=(f"Script is {stats['spoken_chars']}/{target} chars, "
                       f"expanding (pass {tries})..."),
                progress=8 + tries * 6,
            )
            try:
                longer = call_anthropic(
                    cfg, model, system,
                    build_expand_user(p, script, stats["spoken_chars"]),
                    max_tokens,
                )
            except Exception as e:
                job["warnings"].append(f"Expand pass {tries} failed: {e}")
                break
            new_stats = compute_stats(longer)
            # keep it only if it actually grew meaningfully
            if new_stats["spoken_chars"] > stats["spoken_chars"] + 300:
                script, stats = longer, new_stats
            else:
                break

        # symmetric trim: if the model OVERSHOT the requested length, condense
        # every over-budget key IN PARALLEL, up to two rounds, until the total
        # lands close to the target; whole-script rewrite as a last resort
        if stats["spoken_chars"] > 1.12 * target:
            parts = split_key_sections(script, p["num_keys"])
            if parts:
                head, sections, tail = parts
                budgets = key_budgets(target, p["num_keys"])

                def _trim_one(i, base_script):
                    try:
                        smaller = call_anthropic(
                            cfg, model, system,
                            build_trim_key_user(p, base_script, i + 1, sections[i], budgets[i]),
                            min(int(budgets[i] / 2.2) + 1000, 9000)).strip()
                    except Exception as e:
                        job["warnings"].append(f"Key {i+1} condense failed: {e}")
                        return i, None
                    mm = KEY_CUE_RE.search(smaller)
                    if (mm and int(mm.group(1)) == i + 1
                            and len(KEY_CUE_RE.findall(smaller)) == 1
                            and spoken_len(smaller) < spoken_len(sections[i]) - 200):
                        return i, smaller.rstrip() + "\n\n"
                    return i, None

                for round_no in range(2):
                    base = head + "".join(sections) + tail
                    total_now = compute_stats(base)["spoken_chars"]
                    if total_now <= 1.08 * target:
                        break
                    todo = sorted(
                        (i for i in range(p["num_keys"])
                         if spoken_len(sections[i]) > budgets[i] + 300),
                        key=lambda i: spoken_len(sections[i]) - budgets[i],
                        reverse=True)
                    if not todo:
                        break
                    job.update(stage=(f"Script is {total_now}/{target} chars - condensing "
                                      f"keys {', '.join(str(i+1) for i in sorted(todo))} "
                                      f"(round {round_no + 1})..."), progress=30)
                    with ThreadPoolExecutor(max_workers=4) as ex:
                        for i, new_sec in ex.map(lambda k: _trim_one(k, base), todo):
                            if new_sec:
                                sections[i] = new_sec
                script = head + "".join(sections) + tail
                stats = compute_stats(script)

        # last resort for a still-oversized script (or when splitting failed):
        # one whole-script condense pass
        if stats["spoken_chars"] > 1.12 * target:
            job.update(stage=f"Still {stats['spoken_chars']}/{target} chars - full condense pass...",
                       progress=32)
            try:
                condensed = call_anthropic(
                    cfg, model, system,
                    (f"This script is {stats['spoken_chars']} characters of spoken text but it "
                     f"must be about {target} (within five percent). Rewrite it SHORTER to that "
                     f"length: cut repetition and filler, never a whole teaching point.\n\n"
                     f"KEEP EVERYTHING STRUCTURAL IDENTICAL: the title as the first spoken line, "
                     f"every section in the same order, all {p['num_keys']} keys each with its "
                     f"[IMAGE: MASTER KEY N ...] cue line, the [long_pause] markers, commas only "
                     f"and no periods, the same affirmation and ending block.\n\n"
                     f"Return ONLY the complete condensed script.\n\nCURRENT SCRIPT:\n{script}"),
                    min(int(target / 2.6), 32000))
                cstats = compute_stats(condensed)
                if (0.85 * target <= cstats["spoken_chars"] < stats["spoken_chars"]
                        and cstats["image_cues"] == p["num_keys"]):
                    script, stats = condensed, cstats
                else:
                    job["warnings"].append(
                        f"Full condense pass rejected (came back {cstats['spoken_chars']} chars, "
                        f"{cstats['image_cues']} cues); keeping the longer script.")
            except Exception as e:
                job["warnings"].append(f"Full condense pass failed: {e}")

        if stats["spoken_chars"] > 1.15 * target:
            job["warnings"].append(
                f"Script is {stats['spoken_chars']} chars vs target {target} - still over "
                f"after condensing. The extra length is teaching content, not filler.")

        if stats["spoken_chars"] < 0.85 * target:
            job["warnings"].append(
                f"Script reached {stats['spoken_chars']} chars vs target {target} "
                f"(~{stats['minutes']} min). The model would not go longer; "
                f"try raising the target or adding an Extra instruction to expand the keys.")

        job["script"] = script
        script_path = os.path.join(out_dir, f"{slug}_RECORDING.txt")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(script)

        # ---------- 2. meta / image prompts ----------
        job.update(stage="Extracting image prompts + mindmap data...", progress=30)
        meta = None
        last_err = None
        meta_raw = None
        for attempt in range(3):   # the model occasionally emits invalid JSON; retry
            try:
                meta_raw = call_anthropic(cfg, model, author["meta_system"], build_meta_user(p, script), 16000, strict=True)
                candidate = parse_json_loose(meta_raw)
                if len(candidate.get("keys") or []) != p["num_keys"]:
                    raise ValueError(f"meta returned {len(candidate.get('keys') or [])} keys, expected {p['num_keys']}")
                meta = candidate
                break
            except Exception as e:
                last_err = e
        if meta is None and meta_raw:
            # final resort: ask the model to REPAIR the broken JSON it produced
            try:
                fixed = call_anthropic(
                    cfg, model,
                    "You repair broken JSON. Escape all inner double quotes, remove "
                    "trailing commas, and return ONLY the corrected, valid JSON object. "
                    "No commentary, no fences.",
                    meta_raw, 16000)
                candidate = parse_json_loose(fixed)
                if len(candidate.get("keys") or []) == p["num_keys"]:
                    meta = candidate
                    job["warnings"].append("Meta JSON needed a repair pass (fixed automatically).")
                else:
                    job["warnings"].append("Repair pass produced incomplete keys; using best available data.")
                    meta = candidate  # partial is still better than skeleton
            except Exception as e:
                last_err = e
        if meta is None:
            job["warnings"].append(f"Meta JSON step failed after retries + repair, script kept: {last_err}")

        # length budget: if the mindmap text overshoots the locked V37-V39 style
        # (key bodies / stepping back too long), COMPRESS it instead of
        # regenerating - this also covers the repair path, which used to skip
        # the length check entirely
        if meta is not None:
            try:
                _check_meta_lengths(meta)
            except ValueError:
                job.update(stage="Mindmap text over the locked budget, compressing...", progress=45)
                try:
                    meta = compress_meta(cfg, model, meta)
                    _check_meta_lengths(meta)
                    # routine self-correction - not worth a user-facing warning
                except Exception as e2:
                    job["warnings"].append(
                        f"Length compression incomplete ({e2}); using best available text.")

        if p.get("cta_none") and meta:
            meta["cta"] = ""
        remember_run(p.get("history_key") or p["author"], title, meta)

        # figure out the prompts we'll feed to KeyAI
        prompts = []
        if meta and isinstance(meta.get("keys"), list):
            for k in meta["keys"]:
                prompts.append(k.get("gemini_prompt", ""))
        if not prompts:
            prompts = extract_image_prompts_from_script(script)
        prompts = prompts[: p["num_keys"]]

        # write the gemini prompts file
        gp_path = os.path.join(out_dir, f"{slug}_GEMINI_PROMPTS.txt")
        with open(gp_path, "w", encoding="utf-8") as fh:
            for i, pr in enumerate(prompts):
                ktitle = ""
                if meta and i < len(meta.get("keys", [])):
                    ktitle = meta["keys"][i].get("title", "")
                fh.write(f"MK{i+1} - {ktitle}\n{pr}\n\n")

        # ---------- 3. images ----------
        images = {}  # index -> png_bytes
        if do_images:
            # all images render at the same time - KeyAI jobs are independent,
            # so 9 sequential 30-60s waits collapse into roughly one
            def _one_image(idx, pr):
                png = generate_image(pr, cfg)
                # always save a standalone PNG too (safety net)
                png_path = os.path.join(out_dir, f"{slug}_mk{idx+1}.png")
                with open(png_path, "wb") as fh:
                    fh.write(png)
                return png

            futures = {}
            with ThreadPoolExecutor(max_workers=5) as ex:
                for i, pr in enumerate(prompts):
                    if not pr:
                        job["warnings"].append(f"Key {i+1}: no prompt, slot left empty.")
                        continue
                    futures[ex.submit(_one_image, i, pr)] = i
                job.update(stage=f"Generating {len(futures)} images via KeyAI (in parallel)...",
                           progress=32)
                done = 0
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        images[i] = fut.result()
                    except Exception as e:
                        job["warnings"].append(f"Key {i+1} image failed, slot left empty: {e}")
                    done += 1
                    job.update(stage=f"Generating images via KeyAI ({done}/{len(futures)} done)...",
                               progress=30 + int(45 * done / max(len(futures), 1)))
        else:
            job["warnings"].append("Image generation skipped (checkbox off). Empty slots in mindmap.")

        # ---------- 4. xmind ----------
        job.update(stage="Building the .xmind mindmap...", progress=85)
        xmind_path = os.path.join(out_dir, f"{slug}_MindMap.xmind")
        build_xmind(xmind_path, title, p["num_keys"], p["ending"], meta, images,
                    product_name=p["product_name"], funnel_channel=p["funnel_channel"],
                    signature=signature,
                    premise_on=p.get("premise_mode", "auto") != "none",
                    unit_word=p.get("unit_word", "keys"),
                    affirmation_on=p.get("affirmation_mode", "adjusted") != "none",
                    product_fallback=author["product_pitch_fallback"],
                    anchor=author.get("anchor", ""))

        # ---------- 5. finish ----------
        # show only the script and the mindmap on the page; the prompts file
        # and standalone PNGs are still saved in the output folder as a backup
        files = [os.path.basename(f) for f in [script_path, xmind_path]]

        job["result"] = {
            "files": files,
            "stats": stats,
            "slug": slug,
        }
        job.update(stage="Archiving to permanent storage...", progress=97)
        archive_job(cfg, job_id, title, p["author_display"],
                    [script_path, xmind_path], job["warnings"].append)
        job.update(stage="Done", progress=100, status="done")

    except Exception as e:
        job["warnings"] = job.get("warnings", [])
        job.update(stage=f"Error: {e}", progress=100, status="error", error=str(e))
