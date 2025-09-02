import asyncio, aiohttp, json, hashlib, re, time, os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List
import yaml

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

APP = FastAPI()
APP.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

DATA = Path("./data"); (DATA / "snaps").mkdir(parents=True, exist_ok=True)
EVENTS: List[Dict[str, Any]] = []  # rolling in-memory feed
MAX_EVENTS = 1000

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def jdump(x): return json.dumps(x, sort_keys=True, separators=(",",":"))
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
                    for key in ("id","challengeId","groupId","objectiveId","templateId","name"):
                        if key in it: res[(key,it[key])] = sha(jdump(it).encode())[:8]; break
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
        lines=[ln for ln in txt.splitlines() if re.search(r"(SBC|Objective|Promo|Challenge|Season|Pack)", ln, re.I)]
        return {"lines":lines}

    snaps = sorted(dirp.glob(f"*.{ext}"))
    cur = parse(raw)
    prev = parse(snaps[-2].read_bytes()) if len(snaps)>=2 else None
    if prev is None:
        EVENTS.append({"ts": now_iso(), "target": name, "kind":"baseline", "lines":[f"First snapshot: {snap.name}"]})
        del EVENTS[:-MAX_EVENTS]; return

    changes = diff(prev, cur) if typ=="json" else list(sorted(set(cur.get("lines",[]))-set(prev.get("lines",[]))))
    interesting=[c for c in changes if not re.search(r"(lastUpdated|generatedAt|build|timestamp)", c, re.I)]
    if interesting:
        EVENTS.append({"ts": now_iso(), "target": name, "kind":"change", "lines": interesting[:200]})
        del EVENTS[:-MAX_EVENTS]

async def watcher():
    # load config
    cfg = yaml.safe_load(open("endpoints.yaml","r",encoding="utf-8"))
    interval = int(cfg.get("poll_interval_seconds", 90))
    targets = cfg.get("targets", [])
    timeout = aiohttp.ClientTimeout(total=25)
    headers={"User-Agent":"FUT-PrivateWatcher/1.0 (+gentle polling)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        while True:
            start=time.time()
            await asyncio.gather(*(process_target(sess, t) for t in targets))
            await asyncio.sleep(max(0, interval - (time.time()-start)))

@APP.on_event("startup")
async def _startup():
    asyncio.create_task(watcher())

# ---------- Web UI ----------
INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>FUT Change Watcher</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/water.css@2/out/water.css">
<style>
body{max-width:1100px}
.event{padding:12px;border:1px solid #e3e3e3;border-radius:12px;margin:12px 0}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;margin-right:8px}
.badge.baseline{background:#eef}
.badge.change{background:#efe}
.lines{font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; white-space:pre-wrap}
small{color:#666}
</style>
</head>
<body>
  <h1>FUT Change Watcher</h1>
  <p>Live diffs from public FUT content endpoints you captured.<br><small>Page refreshes every 15s. Only semantic changes are shown.</small></p>
  <div id="events">Loading…</div>
<script>
async function load(){
  const r = await fetch('/api/events');
  const js = await r.json();
  const wrap = document.getElementById('events');
  wrap.innerHTML='';
  js.events.slice().reverse().forEach(ev=>{
    const d = document.createElement('div');
    d.className='event';
    const badge = `<span class="badge ${ev.kind}">${ev.kind}</span>`;
    d.innerHTML = `<div>${badge}<b>${ev.target}</b> <small>${ev.ts}</small></div>
                   <div class="lines">${ev.lines.map(l=>l.replace(/&/g,'&amp;').replace(/</g,'&lt;')).join('\\n')}</div>`;
    wrap.appendChild(d);
  });
}
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
