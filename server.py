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
_META = _json.dumps({k: {"hint": v["premise_hint"], "cta": v["cta_default"]} for k, v in pipeline.AUTHORS.items()})

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
  <h1>Video Maker Studio</h1>
  <p class="sub">Faceless video packages &mdash; pick the author, type the title, get script + mindmap.</p>

  <form id="form">
    <label>Author <span class="hint">(each has their own locked script format)</span></label>
    <select name="author" id="author">__AUTHOR_OPTIONS__</select>

    <label>Title <span class="hint">(required, read verbatim as the first line)</span></label>
    <input type="text" name="title" required placeholder="The One Sentence That Changes Everything">

    <div class="row">
      <div>
        <label>Target length <span class="hint">(chars; 850 &asymp; 1 min)</span></label>
        <input type="number" name="target_chars" value="40000">
      </div>
      <div>
        <label>Number of keys</label>
        <select name="num_keys"><option value="9">9</option><option value="6">6</option></select>
      </div>
      <div>
        <label>Ending type</label>
        <select name="ending">
          <option value="product">product (course pitch)</option>
          <option value="funnel">funnel (second channel)</option>
          <option value="none">none (no product, no funnel)</option>
        </select>
      </div>
    </div>

    <label>Comment CTA <span class="hint">(two words; blank = author's default)</span></label>
    <input type="text" name="cta" id="cta" placeholder="">

    <label>Premise <span class="hint" id="premise_hint">(optional; blank = model picks)</span></label>
    <input type="text" name="scripture" placeholder="">

    <label>Core sentence / phrase the video teaches <span class="hint">(optional)</span></label>
    <input type="text" name="core" placeholder="">

    <label>Extra instructions <span class="hint">(optional)</span></label>
    <textarea name="extra" placeholder=""></textarea>

    <label>Model override <span class="hint">(optional; blank = .env default)</span></label>
    <input type="text" name="model_override" placeholder="claude-sonnet-5">

    <div class="check">
      <input type="checkbox" name="do_images" id="do_images" checked>
      <label for="do_images">Generate images via KeyAI and embed them</label>
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
function syncAuthor(){
  const a = AUTHOR_META[authorSel.value];
  document.getElementById('premise_hint').textContent = '(optional; blank = model picks — ' + a.hint + ')';
  document.getElementById('cta').placeholder = a.cta;
}
authorSel.addEventListener('change', syncAuthor); syncAuthor();

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  go.disabled = true;
  results.classList.add('hidden');
  progress.classList.remove('hidden');
  document.getElementById('stage').textContent = 'Starting...';
  document.getElementById('fill').style.width = '0%';

  const data = Object.fromEntries(new FormData(form).entries());
  data.do_images = form.do_images.checked;

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
    return PAGE.replace("__AUTHOR_OPTIONS__", _OPTS).replace("__AUTHOR_META__", _META)


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

    author_key = d.get("author") or "shinn"
    if author_key not in pipeline.AUTHORS:
        return jsonify({"error": "unknown author"}), 400
    params = {
        "author": author_key,
        "title": title,
        "target_chars": _int(d.get("target_chars"), 40000),
        "num_keys": 9 if str(d.get("num_keys", "9")) == "9" else 6,
        "ending": d.get("ending", "product"),
        "cta": (d.get("cta") or pipeline.AUTHORS[author_key]["cta_default"]).strip(),
        "scripture": (d.get("scripture") or "").strip(),
        "core": (d.get("core") or "").strip(),
        "extra": (d.get("extra") or "").strip(),
        "model_override": (d.get("model_override") or "").strip(),
        "do_images": bool(d.get("do_images", True)),
    }

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "status": "running", "stage": "Queued...", "progress": 0, "warnings": [],
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
