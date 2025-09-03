import asyncio, aiohttp, json, hashlib, re, time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List
import yaml

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

APP = FastAPI()
APP.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

DATA = Path("./data")
(DATA / "snaps").mkdir(parents=True, exist_ok=True)

EVENTS: List[Dict[str, Any]] = []   # rolling in-memory feed
MAX_EVENTS = 1000
LAST_TICK = {"ts": None}

# ---------------- utils ----------------
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def jdump(x): return json.dumps(x, sort_keys=True, separators=(",", ":"))
def sha(b: bytes): return hashlib.sha256(b).hexdigest()

def scrub_json(obj, include=None, exclude=None):
    def keep(path):
        inc_ok = True if not include else any(s in path for s in include)
        exc_ok = True if not exclude else not any(s in path for s in exclude)
        return inc_ok and exc_ok
    def walk(node, path):
        if isinstance(node, dict):
            out={}
            for k,v in node.items():
                p = f"{path}.{k}" if path else k
                if keep(p): out[k]=walk(v,p)
            return out
        if isinstance(node, list):
            return [walk(v, f"{path}[]") for v in node]
        return node
    return walk(obj, "")

def diff(a, b, path=""):
    out=[]
    if type(a)!=type(b):
        out.append(f"{path}: TYPE {type(a).__name__} -> {type(b).__name__}"); return out
    if isinstance(a, dict):
        ks=set(a)|set(b)
        for k in sorted(ks):
            p=f"{path}.{k}" if path else k
            if k not in a: out.append(f"{p}: ADDED {repr(b[k])}")
            elif k not in b: out.append(f"{p}: REMOVED")
            else: out.extend(diff(a[k], b[k], p))
        return out
    if isinstance(a, list):
        if len(a)!=len(b): out.append(f"{path}: LIST {len(a)} -> {len(b)}")
        def keyed(xs):
            res={}
            for it in xs:
                if isinstance(it,dict):
                    for key in ("id","challengeId","groupId","objectiveId","templateId","name","packId","evolutionId"):
                        if key in it:
                            res[(key,it[key])] = sha(jdump(it).encode())[:8]; break
            return res
        ka,kb = keyed(a), keyed(b)
        for k in sorted(set(ka)|set(kb)):
            if k not in ka: out.append(f"{path}[{k}]: ADDED")
            elif k not in kb: out.append(f"{path}[{k}]: REMOVED")
        return out
    if a!=b: out.append(f"{path}: {repr(a)} -> {repr(b)}")
    return out

async def fetch(session: aiohttp.ClientSession, name: str, url: str, cond_headers: Dict[str,str]):
    meta_p = DATA / f"{name}.meta.json"
    headers = {}
    if meta_p.exists():
        m=json.loads(meta_p.read_text())
        if m.get("etag"): headers["If-None-Match"]=m["etag"]
        if m.get("lm"): headers["If-Modified-Since"]=m["lm"]
    headers.update(cond_headers or {})
    async with session.get(url, headers=headers) as r:
        if r.status==304: return None, {"not_modified":True}
        body=await r.read()
        meta={"etag":r.headers.get("ETag"), "lm":r.headers.get("Last-Modified"), "ts":now_iso(), "status":r.status}
        meta_p.write_text(json.dumps(meta, indent=2))
        return body, meta

# -------- classify & summarise events --------
TOPIC_PATTERNS = [
    ("Evolutions", re.compile(r"\b(evolution|evo|evolutions|evolutionId|eligibility|boosts|tasks)\b", re.I)),
    ("SBC",        re.compile(r"\b(SBC|challenge|group|template|required|chem|squadRating|minRating|requirements)\b", re.I)),
    ("Packs",      re.compile(r"\b(pack|store|price|start|end|guarantee|rarity|weight)\b", re.I)),
    ("Objectives", re.compile(r"\b(objective|task|milestone|season|reward)\b", re.I)),
    ("Locales",    re.compile(r"\b(locale|string|en_us|string_catalog)\b", re.I)),
    ("Bundles",    re.compile(r"\b(\.js|bundle|service-worker|manifest)\b", re.I)),
    ("Flags",      re.compile(r"\b(remoteconfig|feature|isEnabled|enableAt|rollout|treatment)\b", re.I)),
]

def classify_topic(target: str, lines: List[str]) -> str:
    blob = f"{target} " + " ".join(lines[:10])
    for topic, pat in TOPIC_PATTERNS:
        if pat.search(blob): return topic
    return "Other"

def classify_severity(lines: List[str]) -> str:
    txt = "\n".join(lines)
    if re.search(r"\bisEnabled:\s*false\s*->\s*true\b", txt): return "Live"     # went live
    if re.search(r"\bADDED\b", txt): return "New"
    return "Edit"

def make_headline(topic: str, lines: List[str]) -> str:
    for ln in lines:
        if "isEnabled:" in ln: return f"{topic}: enable flip ({ln.strip()})"
        if "minRating" in ln or "squadRating" in ln: return f"{topic}: rating change ({ln.strip()})"
        if "ADDED" in ln and "[" in ln and "]" in ln: return f"{topic}: new item {ln.split(':',1)[0].strip()}"
        if "LIST" in ln: return f"{topic}: list size changed ({ln.strip()})"
    return f"{topic}: {lines[0][:120]}"

# -------- core processing --------
async def process_target(session, t):
    name, url, typ = t["name"], t["url"], t.get("type","text")
    tk = t.get("track_keys") or {}
    include, exclude = tk.get("include"), tk.get("exclude")

    raw, meta = await fetch(session, name, url, {"Accept":"*/*","Accept-Encoding":"gzip, deflate, br"})
    if not raw or meta.get("not_modified"): return

    # snapshot
    ext = "json" if typ=="json" else "txt"
    dirp = DATA / "snaps" / name; dirp.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    snap = dirp / f"{ts}.{ext}"; snap.write_bytes(raw)

    def parse(b):
        if typ=="json":
            try: obj=json.loads(b.decode("utf-8","ignore"))
            except: return None
            return scrub_json(obj, include, exclude) if (include or exclude) else obj
        txt=b.decode("utf-8","ignore")
        lines=[ln for ln in txt.splitlines() if re.search(r"(SBC|Objective|Promo|Challenge|Season|Pack|isEnabled|manifest|service-worker|Evolution)", ln, re.I)]
        return {"lines":lines}

    snaps = sorted(dirp.glob(f"*.{ext}"))
    cur = parse(raw)
    prev = parse(snaps[-2].read_bytes()) if len(snaps)>=2 else None

    if prev is None:
        EVENTS.append({
            "ts": now_iso(), "target": name, "kind":"baseline",
            "topic": classify_topic(name, ["baseline"]),
            "severity": "Baseline",
            "headline": f"📌 Baseline captured ({snap.name})",
            "lines":[]
        })
        del EVENTS[:-MAX_EVENTS]; return

    changes = diff(prev, cur) if typ=="json" else list(sorted(set(cur.get("lines",[]))-set(prev.get("lines",[]))))
    interesting=[c for c in changes if not re.search(r"(lastUpdated|generatedAt|build|timestamp|version)", c, re.I)]

    if interesting:
        topic = classify_topic(name, interesting)
        severity = classify_severity(interesting)
        headline = make_headline(topic, interesting)
        EVENTS.append({
            "ts": now_iso(), "target": name, "kind":"change",
            "topic": topic, "severity": severity, "headline": headline,
            "lines": interesting[:250]
        })
        del EVENTS[:-MAX_EVENTS]

async def watcher():
    cfg = yaml.safe_load(open("endpoints.yaml","r",encoding="utf-8"))
    interval = int(cfg.get("poll_interval_seconds", 90))
    targets = cfg.get("targets", [])
    timeout = aiohttp.ClientTimeout(total=25)
    headers={"User-Agent":"FUT-PrivateWatcher/1.1 (+gentle polling)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        while True:
            start=time.time()
            await asyncio.gather(*(process_target(sess, t) for t in targets))
            LAST_TICK["ts"] = now_iso()
            await asyncio.sleep(max(0, interval - (time.time()-start)))

@APP.on_event("startup")
async def _startup():
    asyncio.create_task(watcher())

# ---------------- Web UI ----------------
INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>FUT Change Watcher</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/water.css@2/out/water.css">
<style>
:root{
  --bg:#0b0f14; --card:#121820; --muted:#8a97a6; --lime:#93ff66; --red:#ff6b6b; --amber:#ffd166; --cyan:#66e0ff; --vio:#b794f4;
}
body{max-width:1100px;background:var(--bg);color:#e7edf3}
.header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;margin-right:8px;color:#000}
.badge.Baseline{background:#cbd5e1}
.badge.New{background:var(--cyan)}
.badge.Live{background:var(--lime)}
.badge.Edit{background:var(--amber)}
.topic{background:#223046;color:#b7ccff}
.event{padding:14px;border:1px solid #1f2a38;border-radius:14px;margin:12px 0;background:var(--card)}
.event.change.fresh{animation: glow 2s ease-out}
@keyframes glow{0%{box-shadow:0 0 0 rgba(147,255,102,.0)} 30%{box-shadow:0 0 24px rgba(147,255,102,.35)} 100%{box-shadow:0 0 0 rgba(147,255,102,.0)}}
.lines{font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; white-space:pre-wrap}
small{color:var(--muted)}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
select,input[type="search"]{background:#0e141c;border:1px solid #273345;color:#e7edf3}
summary{cursor:pointer}
.mini{font-size:12px;color:var(--muted)}
.headline{font-weight:700;margin:6px 0}
.icon{opacity:.9;margin-right:6px}

/* diff tokens */
.highlight-added{background:rgba(102,224,255,.18);padding:0 4px;border-radius:4px}
.highlight-removed{background:rgba(255,107,107,.18);padding:0 4px;border-radius:4px}
.highlight-arrow{background:rgba(255,209,102,.18);padding:0 4px;border-radius:4px}
</style>
</head>
<body>
  <div class="header">
    <h1 style="margin:0">FUT Change Watcher</h1>
    <div><small>Last check: <span id="last">—</span></small></div>
  </div>

  <div class="controls">
    <select id="kind">
      <option value="">All events</option>
      <option value="change">Changes only</option>
      <option value="baseline">Baselines only</option>
    </select>
    <select id="topic">
      <option value="">All topics</option>
      <option>Evolutions</option><option>SBC</option><option>Packs</option>
      <option>Objectives</option><option>Locales</option><option>Bundles</option><option>Flags</option><option>Other</option>
    </select>
    <select id="severity">
      <option value="">Any severity</option>
      <option>New</option><option>Live</option><option>Edit</option><option>Baseline</option>
    </select>
    <input id="q" type="search" placeholder="Search text…" />
    <button id="clear">Clear filters</button>
  </div>

  <div id="events">Loading…</div>

<script>
const ICONS = {
  "SBC":"🧩", "Packs":"🎁", "Objectives":"🎯", "Locales":"📝",
  "Bundles":"📦", "Flags":"🚩", "Evolutions":"🔄", "Other":"✨"
};

function decorate(line){
  line = line.replace(/\bADDED\b/g, '<span class="highlight-added">ADDED</span>');
  line = line.replace(/\bREMOVED\b/g, '<span class="highlight-removed">REMOVED</span>');
  line = line.replace(/: ([^\n]*?) -> ([^\n]*)/g, ': <span class="highlight-arrow">$1 -> $2</span>');
  return line.replace(/&/g,'&amp;').replace(/</g,'&lt;');
}

let lastSeen = 0;
async function load(){
  const [evRes, hRes] = await Promise.all([fetch('/api/events'), fetch('/api/health')]);
  const js = await evRes.json();
  const health = await hRes.json();
  document.getElementById('last').textContent = health.last_check || '—';
  const wrap = document.getElementById('events');
  const kind = document.getElementById('kind').value;
  const topic = document.getElementById('topic').value;
  const severity = document.getElementById('severity').value;
  const q = (document.getElementById('q').value || '').toLowerCase();

  wrap.innerHTML='';
  const events = js.events.slice();
  let newestTs = lastSeen;

  events.reverse().forEach(ev=>{
    if (kind && ev.kind !== kind) return;
    if (topic && ev.topic !== topic) return;
    if (severity && ev.severity !== severity) return;
    if (q){
      const text = (ev.headline + ' ' + (ev.lines||[]).join(' ')).toLowerCase();
      if (!text.includes(q)) return;
    }
    const fresh = ev.kind === 'change' && (!lastSeen || Date.parse(ev.ts.replace(' UTC','Z')) > lastSeen);

    // Compact baseline
    if (ev.kind === 'baseline'){
      const mini = document.createElement('div');
      mini.className='mini';
      mini.innerHTML = `📌 <span class="badge Baseline">Baseline</span> <span class="badge topic">${ev.topic||'Other'}</span> <b>${ev.target}</b> <small>${ev.ts}</small>`;
      wrap.appendChild(mini);
      return;
    }

    const d = document.createElement('div');
    d.className='event ' + ev.kind + (fresh ? ' fresh' : '');
    const icon = ICONS[ev.topic||'Other'] || '✨';
    d.innerHTML = `
      <div>
        <span class="badge ${ev.severity}">${ev.severity}</span>
        <span class="badge topic">${ev.topic || 'Other'}</span>
        <b class="icon">${icon}</b><b>${ev.target}</b> <small>${ev.ts}</small>
      </div>
      <div class="headline">${ev.headline || ''}</div>
      <details open>
        <summary>Show details</summary>
        <div class="lines">${(ev.lines||[]).map(l=>decorate(l)).join('\\n')}</div>
      </details>
    `;
    wrap.appendChild(d);
    const tsNum = Date.parse(ev.ts.replace(' UTC','Z'));
    if (tsNum > newestTs) newestTs = tsNum;
  });
  if (newestTs) lastSeen = newestTs;
}

document.getElementById('clear').onclick = () => {
  document.getElementById('kind').value='';
  document.getElementById('topic').value='';
  document.getElementById('severity').value='';
  document.getElementById('q').value='';
  load();
};

load(); setInterval(load, 15000);
</script>
</body>
</html>"""

@APP.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INDEX_HTML)

@APP.get("/api/events")
def api_events():
    return JSONResponse({"events": EVENTS})

@APP.get("/api/health")
def health():
    return JSONResponse({"last_check": LAST_TICK["ts"], "events_cached": len(EVENTS)})