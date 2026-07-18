"""
Video Maker Studio - server.py (multi-author)
A tiny local Flask app. Serves a dark/gold form, kicks off the pipeline in a
background thread, and lets the page poll /status/<job_id> for progress.

Run:  python3 server.py   then open  http://127.0.0.1:5060
"""

import os
import uuid
import threading

from flask import Flask, request, jsonify, send_from_directory, abort

import pipeline

app = Flask(__name__)

import json as _json
_OPTS = "".join(f'<option value="{k}">{v["display"]}</option>' for k, v in pipeline.AUTHORS.items())
_META = _json.dumps({k: {"hint": v["premise_hint"], "cta": v["cta_default"],
                         "anchor": v.get("anchor", "")} for k, v in pipeline.AUTHORS.items()})

OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
os.makedirs(OUTPUT_ROOT, exist_ok=True)

# in-memory job registry: job_id -> status dict
JOBS = {}


# --------------------------------------------------------------------------
# HTML page (served as a string, no templating engine)
# --------------------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Video Maker Studio</title>
<style>
  :root { --bg:#0d0d0d; --gold:#d4af37; --panel:#151515; --line:#2a2a2a; --text:#eae6dc; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family:Helvetica,Arial,sans-serif; line-height:1.5; }
  .wrap { max-width:820px; margin:0 auto; padding:32px 20px 80px; }
  h1 { color:var(--gold); font-weight:700; letter-spacing:.5px; margin:0 0 4px; }
  .sub { color:#9a9585; margin:0 0 28px; font-size:14px; }
  label { display:block; margin:16px 0 6px; color:var(--gold); font-size:13px;
          text-transform:uppercase; letter-spacing:.6px; }
  .hint { color:#8a8574; font-size:12px; text-transform:none; letter-spacing:0; }
  input[type=text], input[type=number], textarea, select {
    width:100%; background:var(--panel); border:1px solid var(--line);
    color:var(--text); padding:10px 12px; border-radius:6px; font-size:14px;
    font-family:inherit; }
  textarea { min-height:64px; resize:vertical; }
  input:focus, textarea:focus, select:focus { outline:none; border-color:var(--gold); }
  .row { display:flex; gap:16px; flex-wrap:wrap; }
  .row > div { flex:1; min-width:200px; }
  .check { display:flex; align-items:center; gap:10px; margin-top:20px; }
  .check input { width:18px; height:18px; accent-color:var(--gold); }
  .check label { margin:0; text-transform:none; letter-spacing:0; color:var(--text); }
  button { margin-top:28px; background:var(--gold); color:#0d0d0d; border:none;
           padding:14px 28px; font-size:15px; font-weight:700; border-radius:6px;
           cursor:pointer; letter-spacing:.5px; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px;
           padding:20px; margin-top:28px; }
  .bar { height:12px; background:#000; border:1px solid var(--line); border-radius:6px;
         overflow:hidden; margin:12px 0; }
  .fill { height:100%; width:0; background:var(--gold); transition:width .4s ease; }
  .stage { color:var(--gold); font-size:14px; }
  .stat { display:inline-block; background:#000; border:1px solid var(--line);
          border-radius:6px; padding:8px 12px; margin:6px 8px 0 0; font-size:13px; }
  .stat b { color:var(--gold); }
  a.dl { display:block; color:var(--gold); text-decoration:none; padding:8px 0;
         border-bottom:1px solid var(--line); }
  a.dl:hover { text-decoration:underline; }
  .warn { color:#e0a94a; font-size:13px; margin-top:8px; }
  .err { color:#e06a4a; }
  .hidden { display:none; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Video Maker Studio <a href="/videos" style="float:right;font-size:14px;color:var(--gold);text-decoration:none;border:1px solid var(--line);border-radius:6px;padding:8px 14px;">📁 Stored videos</a></h1>
  <p class="sub">Faceless video packages &mdash; pick the author, type the title, get script + mindmap.</p>

  <form id="form">
    <label>Video type</label>
    <select name="video_type" id="video_type">
      <option value="teaching">Teaching (keys + mindmap)</option>
      <option value="sleep">Sleep / Meditation (hypnotic journey)</option>
    </select>

    <label>Author <span class="hint">(each has their own locked script format)</span></label>
    <select name="author" id="author">__AUTHOR_OPTIONS__</select>

    <div id="custom_focus_wrap" style="display:none">
      <label>Custom topic or author <span class="hint">(anything - "spirituality in general", "Marcus Aurelius", "discipline", "Rumi"...)</span></label>
      <input type="text" name="custom_focus" id="custom_focus" placeholder="spirituality in general" autocomplete="off">
    </div>

    <label>Title <span class="hint">(required, read verbatim as the first line)</span></label>
    <input type="text" name="title" required placeholder="The One Sentence That Changes Everything">

    <div class="row">
      <div>
        <label>Target length <span class="hint">(chars; 850 &asymp; 1 min)</span></label>
        <input type="number" name="target_chars" value="40000">
      </div>
      <div id="numkeys_wrap">
        <label>Number of sections</label>
        <select name="num_keys"><option value="9">9</option><option value="6">6</option></select>
      </div>
      <div id="unit_wrap">
        <label>Call them</label>
        <select name="unit_word">
          <option value="keys">keys (classic)</option>
          <option value="decrees">decrees</option>
          <option value="signs">signs</option>
          <option value="laws">laws</option>
        </select>
      </div>
      <div id="ending_wrap">
        <label>Ending type</label>
        <select name="ending">
          <option value="product">product (course pitch)</option>
          <option value="funnel">funnel (second channel)</option>
          <option value="none">none (no product, no funnel)</option>
        </select>
      </div>
    </div>

    <label>Comment CTA <span class="hint">(the two words viewers type in the comments)</span></label>
    <select name="cta_choice" id="cta_choice"></select>
    <input type="text" name="cta" id="cta" placeholder="two words"
           style="display:none; margin-top:8px" autocomplete="off">

    <div id="premise_wrap">
    <label>Premise <span class="hint" id="premise_hint">(choose - no writing needed)</span></label>
    <select name="premise_choice" id="premise_choice"></select>
    <input type="text" name="scripture" id="scripture_input" placeholder="type your premise / verse / quote here"
           style="display:none; margin-top:8px" autocomplete="off">
    </div>

    <div id="affirmation_wrap">
    <label>Participation affirmation <span class="hint">(choose - some niches don't need one)</span></label>
    <select name="affirmation_choice" id="affirmation_choice"></select>
    </div>

    <label>Extra instructions <span class="hint">(optional)</span></label>
    <textarea name="extra" placeholder=""></textarea>

    <label>Anthropic API key <span class="hint">(whose balance pays for the script)</span></label>
    <select name="api_key_choice" id="api_key_choice">__KEY_OPTIONS__</select>
    <input type="text" name="custom_api_key" id="custom_api_key" placeholder="sk-ant-..."
           style="display:none; margin-top:8px" autocomplete="off">

    <label>Model override <span class="hint">(optional; blank = .env default)</span></label>
    <input type="text" name="model_override" placeholder="claude-sonnet-5">

    <div class="check">
      <input type="checkbox" name="do_images" id="do_images" checked>
      <label for="do_images">Generate images via KeyAI and embed them</label>
    </div>

    <div class="check" id="subscribe_wrap">
      <input type="checkbox" name="add_subscribe" id="add_subscribe">
      <label for="add_subscribe">Add a short subscribe reminder (after the affirmation, in the author's voice)</label>
    </div>

    <button type="submit" id="go">Generate</button>
  </form>

  <div class="panel hidden" id="progress">
    <div class="stage" id="stage">Starting...</div>
    <div class="bar"><div class="fill" id="fill"></div></div>
    <div id="pct" class="hint"></div>
  </div>

  <div class="panel hidden" id="results">
    <h3 style="color:var(--gold);margin-top:0">Package ready</h3>
    <div id="stats"></div>
    <div id="downloads" style="margin-top:16px"></div>
    <div id="warnings"></div>
  </div>
</div>

<script>
const form = document.getElementById('form');
const go = document.getElementById('go');
const progress = document.getElementById('progress');
const results = document.getElementById('results');
let poll = null;
const AUTHOR_META = __AUTHOR_META__;
const authorSel = document.getElementById('author');
const premiseSel = document.getElementById('premise_choice');
const scriptureInput = document.getElementById('scripture_input');
function syncPremiseInput(){
  scriptureInput.style.display = premiseSel.value === 'own' ? 'block' : 'none';
}
function syncAuthor(){
  const a = AUTHOR_META[authorSel.value];
  document.getElementById('premise_hint').textContent = '(' + a.hint + ')';
  document.getElementById('cta').placeholder = a.cta;
  const isCustom = authorSel.value === 'custom' || authorSel.value === 'auto';
  document.getElementById('custom_focus_wrap').style.display =
      authorSel.value === 'custom' ? 'block' : 'none';
  let opts = isCustom ? [
    ['auto',    'choose for me (best fit for the theme)'],
    ['biblical','Bible verse (model picks a real one)'],
    ['quote',   'real quote close to the theme (model picks)'],
    ['none',    'no premise'],
    ['own',     'my own premise (write below)'],
  ] : [
    ['auto', 'choose for me — ' + a.hint],
    ['own',  'my own premise (write below)'],
  ];
  premiseSel.innerHTML = opts.map(o => '<option value="'+o[0]+'">'+o[1]+'</option>').join('');
  const ctaSel = document.getElementById('cta_choice');
  ctaSel.innerHTML = [
    ['default', `author's default ("${a.cta}")`],
    ['own',     'my own two words (write below)'],
    ['none',    'no comment CTA'],
  ].map(o => '<option value="'+o[0]+'">'+o[1]+'</option>').join('');
  document.getElementById('cta').style.display = 'none';
  const affSel = document.getElementById('affirmation_choice');
  let affOpts = isCustom ? [
    ['adjusted', 'yes - adjusted to the theme'],
    ['spiritual','yes - spiritual'],
    ['none',     'no affirmation'],
  ] : [
    ['classic', "yes - the author's affirmation (ends '" + a.anchor + "')"],
    ['none',    'no affirmation'],
  ];
  affSel.innerHTML = affOpts.map(o => '<option value="'+o[0]+'">'+o[1]+'</option>').join('');
  syncPremiseInput();
}
premiseSel.addEventListener('change', syncPremiseInput);
const videoType = document.getElementById('video_type');
function syncVideoType(){
  const sleep = videoType.value === 'sleep';
  ['numkeys_wrap','unit_wrap','ending_wrap','premise_wrap','affirmation_wrap','subscribe_wrap']
    .forEach(id => { const el = document.getElementById(id);
                     if (el) el.style.display = sleep ? 'none' : ''; });
}
videoType.addEventListener('change', syncVideoType);
syncVideoType();

// remember every setting exactly as the user left it (survives back/reload)
const FIELDS = ['video_type','author','custom_focus','title','target_chars','num_keys','unit_word','ending',
                'cta_choice','cta','premise_choice','scripture','affirmation_choice',
                'extra','api_key_choice','model_override','do_images','add_subscribe'];
function saveForm(){
  const st = {};
  FIELDS.forEach(n => { const el = form[n]; if (!el) return;
    st[n] = el.type === 'checkbox' ? el.checked : el.value; });
  try { localStorage.setItem('vms_form', JSON.stringify(st)); } catch(e){}
}
function restoreForm(){
  let st = null;
  try { st = JSON.parse(localStorage.getItem('vms_form') || 'null'); } catch(e){}
  if (!st) return;
  if (st.author && AUTHOR_META[st.author]) { form.author.value = st.author; }
  syncAuthor();
  FIELDS.forEach(n => { const el = form[n]; if (!el || st[n] === undefined || n === 'author') return;
    if (el.type === 'checkbox') el.checked = !!st[n]; else el.value = st[n]; });
  syncPremiseInput();
  syncVideoType();
  document.getElementById('cta').style.display =
      form.cta_choice.value === 'own' ? 'block' : 'none';
  document.getElementById('custom_api_key').style.display =
      form.api_key_choice.value === 'custom' ? 'block' : 'none';
}
form.addEventListener('input', saveForm);
form.addEventListener('change', saveForm);
document.getElementById('cta_choice').addEventListener('change', function(){
  document.getElementById('cta').style.display = this.value === 'own' ? 'block' : 'none';
});
authorSel.addEventListener('change', syncAuthor); syncAuthor();
const keyChoice = document.getElementById('api_key_choice');
const customKey = document.getElementById('custom_api_key');
keyChoice.addEventListener('change', () => {
  customKey.style.display = keyChoice.value === 'custom' ? 'block' : 'none';
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  go.disabled = true;
  results.classList.add('hidden');
  progress.classList.remove('hidden');
  document.getElementById('stage').textContent = 'Starting...';
  document.getElementById('fill').style.width = '0%';

  const data = Object.fromEntries(new FormData(form).entries());
  data.do_images = form.do_images.checked;
  data.add_subscribe = form.add_subscribe.checked;

  const r = await fetch('/generate', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(data)
  });
  const j = await r.json();
  if (j.error) { fail(j.error); return; }
  const jobId = j.job_id;
  poll = setInterval(() => checkStatus(jobId), 1500);
});

function fail(msg){
  go.disabled = false;
  document.getElementById('stage').innerHTML = '<span class="err">'+msg+'</span>';
}

async function checkStatus(jobId){
  let r;
  try { r = await fetch('/status/'+jobId); }
  catch(e){ return; /* transient network blip - keep polling */ }
  if (r.status === 404){
    clearInterval(poll);
    fail('This job was lost (the server restarted mid-run, e.g. during an app update). ' +
         'Please click Generate again - the new run will complete normally.');
    return;
  }
  const j = await r.json();
  document.getElementById('stage').textContent = j.stage || '';
  document.getElementById('fill').style.width = (j.progress||0)+'%';
  document.getElementById('pct').textContent = (j.progress||0)+'%';

  if (j.status === 'done' || j.status === 'error'){
    clearInterval(poll);
    go.disabled = false;
    if (j.status === 'error'){ fail(j.stage); }
    if (j.result){ showResults(jobId, j); }
  }
}

function showResults(jobId, j){
  results.classList.remove('hidden');
  const s = j.result.stats || {};
  document.getElementById('stats').innerHTML =
    '<span class="stat">Author <b>'+(j.author_display||'?')+'</b></span>' +
    '<span class="stat">Spoken chars <b>'+s.spoken_chars+'</b></span>' +
    '<span class="stat">Approx minutes <b>'+s.minutes+'</b></span>' +
    '<span class="stat">Image cues <b>'+s.image_cues+'</b></span>' +
    '<span class="stat">Periods <b>'+s.periods+'</b> (should be 0)</span>';

  let html = '';
  (j.result.files||[]).forEach(f => {
    html += '<a class="dl" href="/download/'+jobId+'/'+encodeURIComponent(f)+'">&#8681; '+f+'</a>';
  });
  document.getElementById('downloads').innerHTML = html;

  let w = '';
  (j.warnings||[]).forEach(x => { w += '<div class="warn">&#9888; '+x+'</div>'; });
  document.getElementById('warnings').innerHTML = w;
}
restoreForm();
</script>
</body>
</html>"""


# --------------------------------------------------------------------------
# Password gate (active only when APP_PASSWORD is set, e.g. on Render).
# --------------------------------------------------------------------------
@app.before_request
def _password_gate():
    pw = os.environ.get("APP_PASSWORD", "").strip()
    if not pw:
        return  # no password configured (local use) -> open
    auth = request.authorization
    if auth and auth.password == pw:
        return
    from flask import Response
    return Response("Login required.", 401,
                    {"WWW-Authenticate": 'Basic realm="Video Maker Studio"'})


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------
@app.route("/")
def index():
    cfg = pipeline.load_env()
    named = sorted(k[len("ANTHROPIC_API_KEY_"):].lower()
                   for k in cfg if k.startswith("ANTHROPIC_API_KEY_") and cfg[k])
    key_opts = '<option value="kubko">kubko (built-in)</option>'
    key_opts += "".join(f'<option value="named:{n}">{n} (built-in)</option>' for n in named)
    key_opts += '<option value="custom">my own key (paste below)</option>'
    return (PAGE.replace("__AUTHOR_OPTIONS__", _OPTS)
                .replace("__AUTHOR_META__", _META)
                .replace("__KEY_OPTIONS__", key_opts))


@app.route("/generate", methods=["POST"])
def generate():
    d = request.get_json(force=True, silent=True) or {}
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title is required."}), 400

    def _int(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    author_key = (d.get("author") or "").strip().lower()
    if author_key not in pipeline.AUTHORS:
        return jsonify({"error": "No author selected - refresh the page and pick "
                                  "the author again (nothing was generated)."}), 400
    params = {
        "author": author_key,
        "title": title,
        "target_chars": _int(d.get("target_chars"), 40000),
        "num_keys": 9 if str(d.get("num_keys", "9")) == "9" else 6,
        "ending": d.get("ending", "product"),
        "cta": ((d.get("cta") or "").strip() or pipeline.AUTHORS[author_key]["cta_default"])
                if (d.get("cta_choice") or "default") == "own"
                else pipeline.AUTHORS[author_key]["cta_default"],
        "cta_none": (d.get("cta_choice") or "default") == "none",
        "scripture": (d.get("scripture") or "").strip()
                     if (d.get("premise_choice") or "auto") == "own" else "",
        "core": (d.get("core") or "").strip(),
        "extra": (d.get("extra") or "").strip(),
        "model_override": (d.get("model_override") or "").strip(),
        "do_images": bool(d.get("do_images", True)),
        "add_subscribe": bool(d.get("add_subscribe", False)),
        "custom_focus": (d.get("custom_focus") or "").strip(),
        "unit_word": d.get("unit_word") if d.get("unit_word") in
                     ("keys", "decrees", "signs", "laws") else "keys",
        "video_type": d.get("video_type") if d.get("video_type") in
                      ("teaching", "sleep") else "teaching",
        "premise_mode": d.get("premise_choice") if d.get("premise_choice") in
                        ("auto", "biblical", "quote", "none") else "auto",
        "affirmation_mode": d.get("affirmation_choice") if d.get("affirmation_choice") in
                        ("adjusted", "spiritual", "none", "classic") else "adjusted",
    }
    if author_key == "custom" and not params["custom_focus"]:
        return jsonify({"error": "Type the custom topic or author (e.g. 'spirituality "
                                  "in general') - nothing was generated."}), 400
    key_choice = d.get("api_key_choice") or "kubko"
    if key_choice == "custom":
        ck = (d.get("custom_api_key") or "").strip()
        if not ck.startswith("sk-ant-") or len(ck) < 30:
            return jsonify({"error": "That doesn't look like an Anthropic API key "
                                      "(it starts with sk-ant-...). Nothing was generated."}), 400
        params["custom_api_key"] = ck
    elif key_choice.startswith("named:"):
        nk = pipeline.load_env().get("ANTHROPIC_API_KEY_" + key_choice[6:].upper())
        if not nk:
            return jsonify({"error": f"Key '{key_choice[6:]}' is not configured on this "
                                      "server. Nothing was generated."}), 400
        params["custom_api_key"] = nk

    _cleanup_output()
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "status": "running", "progress": 0, "warnings": [],
        "author_display": pipeline.AUTHORS[author_key]["display"],
        "stage": f"Queued ({pipeline.AUTHORS[author_key]['display']})...",
    }

    t = threading.Thread(
        target=pipeline.run_pipeline,
        args=(job_id, params, JOBS[job_id], OUTPUT_ROOT),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/version")
def version():
    """Which build is running (Render sets RENDER_GIT_COMMIT on deploy)."""
    commit = os.environ.get("RENDER_GIT_COMMIT", "")
    if not commit:
        try:
            import subprocess
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            commit = "unknown"
    return jsonify({"commit": commit[:12]})


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


KEEP_DAYS = float(os.environ.get("KEEP_DAYS", "1"))   # stored videos live 24 hours


def _cleanup_output():
    """Delete stored video folders older than KEEP_DAYS days."""
    import shutil, time as _t
    cutoff = _t.time() - KEEP_DAYS * 86400
    try:
        for name in os.listdir(OUTPUT_ROOT):
            d = os.path.join(OUTPUT_ROOT, name)
            if os.path.isdir(d) and os.path.getmtime(d) < cutoff:
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


@app.route("/videos")
def videos():
    """Library of every stored generation, straight from disk - works even
    after a server restart (unlike the in-memory job status)."""
    import time as _t
    _cleanup_output()
    jobs = []
    try:
        for name in os.listdir(OUTPUT_ROOT):
            d = os.path.join(OUTPUT_ROOT, name)
            if not os.path.isdir(d) or name.startswith("test_"):
                continue
            script = next((f for f in sorted(os.listdir(d))
                           if f.endswith("_RECORDING.txt")), None)
            xmind = next((f for f in sorted(os.listdir(d))
                          if f.endswith(".xmind")), None)
            if not script and not xmind:
                continue
            title, author = "", ""
            try:
                with open(os.path.join(d, "_title.txt"), encoding="utf-8") as fh:
                    tl = fh.read().splitlines()
                    title = (tl[0] if tl else "").strip()
                    author = (tl[1] if len(tl) > 1 else "").strip()
            except Exception:
                pass
            if not title and (script or xmind):
                # older folders: prettify the file slug
                base = (script or xmind).rsplit("_RECORDING", 1)[0].rsplit("_MindMap", 1)[0]
                title = base.replace("_", " ").strip().title()
            jobs.append({
                "id": name, "title": title, "author": author,
                "age_h": (_t.time() - os.path.getmtime(d)) / 3600,
                "script": script, "xmind": xmind,
            })
    except Exception:
        pass
    # merge the permanent cloud archive (private GitHub repo), if configured
    try:
        cfg = pipeline.load_env()
        pipeline.archive_prune(cfg, KEEP_DAYS)
        local_ids = {j["id"] for j in jobs}
        entries, _sha = pipeline.archive_index(cfg)
        for e in entries:
            if e.get("id") in local_ids:
                continue
            script = next((f for f in e.get("files", []) if f.endswith("_RECORDING.txt")), None)
            xmind = next((f for f in e.get("files", []) if f.endswith(".xmind")), None)
            jobs.append({
                "id": e["id"], "title": (e.get("title") or "") + " ☁",
                "author": e.get("author", ""),
                "age_h": (_t.time() - e.get("ts", _t.time())) / 3600,
                "script": script, "xmind": xmind, "remote": True,
            })
    except Exception:
        pass
    jobs.sort(key=lambda j: j["age_h"])

    rows = []
    for j in jobs:
        age = f"{j['age_h']:.1f} h ago" if j["age_h"] < 48 else f"{j['age_h']/24:.1f} days ago"
        meta_line = " &middot; ".join(x for x in (j["author"], age) if x)
        base = "/download_remote" if j.get("remote") else "/download"
        links = ""
        if j["script"]:
            links += (f'<a class="dl" href="{base}/{j["id"]}/{j["script"]}">'
                      f'&#128220; Script <span style="color:#777;font-size:12px">({j["script"]})</span></a>')
        if j["xmind"]:
            links += (f'<a class="dl" href="{base}/{j["id"]}/{j["xmind"]}">'
                      f'&#128506; Mindmap <span style="color:#777;font-size:12px">({j["xmind"]})</span></a>')
        rows.append(
            f'<details class="panel" style="cursor:pointer">'
            f'<summary style="color:var(--gold);font-size:16px;font-weight:700">{j["title"]}'
            f'<span style="color:#999;font-size:12px;font-weight:400;margin-left:10px">{meta_line}</span>'
            f'</summary><div style="margin-top:10px">{links}</div></details>')

    body = "".join(rows) or '<div class="panel">No stored videos yet.</div>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Stored videos</title><style>{PAGE.split('<style>')[1].split('</style>')[0]}</style></head>
<body><div class="wrap"><h1>Stored videos
<a href="/" style="float:right;font-size:14px;color:var(--gold);text-decoration:none;border:1px solid var(--line);border-radius:6px;padding:8px 14px;">&#8592; Generator</a></h1>
<p style="color:#999;font-size:13px">Every generated script and mindmap is kept here for {KEEP_DAYS*24:g} hours, then deleted automatically.
On the free cloud server the storage is also cleared whenever the server restarts or updates - download anything important right away; the local app keeps the full {KEEP_DAYS*24:g} hours reliably.</p>
{body}</div></body></html>"""


@app.route("/download_remote/<job_id>/<path:filename>")
def download_remote(job_id, filename):
    if "/" in filename or ".." in filename or "/" in job_id or ".." in job_id:
        abort(400)
    cfg = pipeline.load_env()
    tok, repo = cfg.get("GITHUB_TOKEN"), cfg.get("STORAGE_REPO")
    if not tok or not repo:
        abort(404)
    import requests as _rq
    r = _rq.get(f"https://api.github.com/repos/{repo}/contents/jobs/{job_id}/{filename}",
                headers={"Authorization": f"Bearer {tok}",
                         "Accept": "application/vnd.github.raw+json"}, timeout=120)
    if r.status_code != 200:
        abort(404)
    from flask import Response
    return Response(r.content, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.route("/download/<job_id>/<path:filename>")
def download(job_id, filename):
    # guard against traversal
    if "/" in filename or ".." in filename:
        abort(400)
    folder = os.path.join(OUTPUT_ROOT, job_id)
    if not os.path.isdir(folder):
        abort(404)
    return send_from_directory(folder, filename, as_attachment=True)


if __name__ == "__main__":
    # Port 5000 = macOS AirPlay, 5080 free — both can be unreachable.
    # 5200 is clean. Override with the PORT env var if you like.
    port = int(os.environ.get("PORT", "5200"))
    print(f"Joseph Murphy Video Maker running at http://127.0.0.1:{port}")
    # host "127.0.0.1" -> always reach it via the IPv4 literal, not "localhost"
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
