import asyncio, aiohttp, json, hashlib, re, time, textwrap, io
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import yaml
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageDraw, ImageFont

APP = FastAPI()
APP.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# -------- storage --------
DATA = Path("./data")
(DATA / "snaps").mkdir(parents=True, exist_ok=True)

# -------- runtime state --------
EVENTS: List[Dict[str, Any]] = []     # rolling feed of events
MAX_EVENTS = 1000
LAST_TICK = {"ts": None}
CONFIG_ERR = {"message": None, "targets": 0}
WATCHER_TASK = None
SEMA = asyncio.Semaphore(10)

# ---- catalogs to enrich with player stats ----
CATALOG_TARGETS = {"sbc_catalog", "objectives_catalog", "evolution_catalog"}
KEYATTR_TARGET = "keyattributes_json"

# -------- helpers --------
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
                    for key in ("id","challengeId","groupId","objectiveId","templateId",
                                "name","packId","evolutionId","playerId","assetId","definitionId"):
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
        meta={"etag":r.headers.get("ETag"), "lm":r.headers.get("Last-Modified"),
              "ts":now_iso(), "status":r.status}
        meta_p.write_text(json.dumps(meta, indent=2))
        return body, meta

# -------- classify --------
TOPIC_PATTERNS = [
    ("Evolutions", re.compile(r"\b(evolution|evo|evolutionId|eligibility|boosts|tasks|cost)\b", re.I)),
    ("SBC",        re.compile(r"\b(SBC|challenge|group|template|required|chem|squadRating|minRating|requirements|repeatable)\b", re.I)),
    ("Packs",      re.compile(r"\b(pack|store|price|guarantee|rarity|coins|points)\b", re.I)),
    ("Objectives", re.compile(r"\b(objective|task|milestone|season|reward)\b", re.I)),
    ("Locales",    re.compile(r"\b(locale|string|en_us|string_catalog)\b", re.I)),
    ("Bundles",    re.compile(r"\b(\.js|bundle|service-worker|manifest)\b", re.I)),
    ("Flags",      re.compile(r"\b(remoteconfig|isEnabled|flag)\b", re.I)),
]

def classify_topic(target: str, lines: List[str]) -> str:
    blob = f"{target} " + " ".join(lines[:10])
    for topic, pat in TOPIC_PATTERNS:
        if pat.search(blob): return topic
    return "Other"

def classify_severity(lines: List[str]) -> str:
    txt = "\n".join(lines)
    if re.search(r"isEnabled:\s*false\s*->\s*true", txt): return "Live"
    if re.search(r"ADDED", txt): return "New"
    return "Edit"

def make_headline(topic: str, lines: List[str]) -> str:
    for ln in lines:
        if "isEnabled:" in ln: return f"{topic}: enable flip ({ln.strip()})"
        if "minRating" in ln: return f"{topic}: rating change ({ln.strip()})"
        if "ADDED" in ln: return f"{topic}: new item {ln.split(':',1)[0]}"
    return f"{topic}: {lines[0][:120]}"

# -------- JSON enrichment --------
def _latest_snap_json(target: str) -> Optional[dict]:
    dirp = DATA / "snaps" / target
    snaps = sorted(dirp.glob("*.json"))
    if not snaps: return None
    try: return json.loads(snaps[-1].read_text())
    except: return None

def _walk_dicts(node):
    if isinstance(node, dict):
        yield node
        for v in node.values(): yield from _walk_dicts(v)
    elif isinstance(node, list):
        for v in node: yield from _walk_dicts(v)

def _index_keyattributes() -> Tuple[dict, dict]:
    data = _latest_snap_json(KEYATTR_TARGET) or {}
    by_id, by_name = {}, {}
    for d in _walk_dicts(data):
        if not isinstance(d, dict): continue
        if any(k in d for k in ("overall","pace","shooting","passing","dribbling","defending","physical")):
            pid = d.get("id") or d.get("assetId") or d.get("definitionId") or d.get("playerId")
            if pid: by_id[str(pid)] = d
            n = d.get("name") or d.get("commonName") or d.get("fullName")
            if isinstance(n,str): by_name.setdefault(n.lower(), []).append(d)
    return by_id, by_name

def _fmt_stats(p: dict) -> str:
    name = p.get("name") or p.get("commonName") or "Unknown"
    pos = p.get("position") or p.get("preferredPosition") or ""
    ovr = p.get("overall") or p.get("rating") or "?"
    pac = p.get("pace") or "?"
    sho = p.get("shooting") or "?"
    pas = p.get("passing") or "?"
    dri = p.get("dribbling") or "?"
    de  = p.get("defending") or "?"
    phy = p.get("physical") or "?"
    return f"{name} {pos} — {ovr} OVR | PAC {pac} SHO {sho} PAS {pas} DRI {dri} DEF {de} PHY {phy}"

# -------- post generators --------
TOPIC_POSTER = {
    "SBC": lambda lines: "SBC Update:\n"+"\n".join(lines),
    "Evolutions": lambda lines: "Evolution Update:\n"+"\n".join(lines),
    "Packs": lambda lines: "Pack Update:\n"+"\n".join(lines),
    "Objectives": lambda lines: "Objective Update:\n"+"\n".join(lines),
    "Locales": lambda lines: "Strings changed:\n"+"\n".join(lines),
    "Bundles": lambda lines: "Bundle changed:\n"+"\n".join(lines),
    "Flags": lambda lines: "Config changed:\n"+"\n".join(lines),
    "Other": lambda lines: "Update:\n"+"\n".join(lines),
}

# -------- processing --------
async def process_target(session, t):
    name, url, typ = t["name"], t["url"], t.get("type","text")
    tk = t.get("track_keys") or {}
    include, exclude = tk.get("include"), tk.get("exclude")

    raw, meta = await fetch(session, name, url, {"Accept":"*/*"})
    if not raw or meta.get("not_modified"): return

    ext = "json" if typ=="json" else "txt"
    dirp = DATA / "snaps" / name; dirp.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    snap = dirp / f"{ts}.{ext}"; snap.write_bytes(raw)

    def parse(b):
        if typ=="json":
            try: obj=json.loads(b.decode())
            except: return None
            return scrub_json(obj, include, exclude) if (include or exclude) else obj
        return {"lines": b.decode().splitlines()}

    snaps = sorted(dirp.glob(f"*.{ext}"))
    cur = parse(raw)
    prev = parse(snaps[-2].read_bytes()) if len(snaps)>=2 else None
    if prev is None:
        EVENTS.append({"ts":now_iso(),"target":name,"kind":"baseline","topic":"Other",
                       "severity":"Baseline","headline":f"📌 Baseline captured ({snap.name})","lines":[]})
        return

    changes = diff(prev, cur) if typ=="json" else list(set(cur.get("lines",[]))-set(prev.get("lines",[])))
    interesting=[c for c in changes if not re.search(r"(lastUpdated|build|timestamp)",c)]
    if interesting:
        topic = classify_topic(name, interesting)
        severity = classify_severity(interesting)
        headline = make_headline(topic, interesting)
        post_md = TOPIC_POSTER.get(topic, TOPIC_POSTER["Other"])(interesting)

        # optional: enrich with players
        if name in CATALOG_TARGETS:
            id_map, name_map = _index_keyattributes()
            players_section=[]
            for ln in interesting:
                m=re.search(r"(playerId|assetId|definitionId)\D+(\d+)",ln)
                if m:
                    p=id_map.get(m.group(2))
                    if p: players_section.append("• "+_fmt_stats(p))
            if players_section:
                post_md+="\n\nPlayers:\n"+"\n".join(players_section)

        EVENTS.append({"ts":now_iso(),"target":name,"kind":"change",
                       "topic":topic,"severity":severity,"headline":headline,
                       "lines":interesting[:200],"post_md":post_md})

# -------- watcher --------
def load_cfg():
    try:
        with open("endpoints.yaml","r") as f: cfg=yaml.safe_load(f) or {}
        CONFIG_ERR["targets"]=len(cfg.get("targets",[]))
        CONFIG_ERR["message"]=None if CONFIG_ERR["targets"] else "No targets"
        return cfg
    except Exception as e:
        CONFIG_ERR["message"]=str(e); CONFIG_ERR["targets"]=0
        return {"poll_interval_seconds":90,"targets":[]}

async def safe_process(sess,t):
    try:
        async with SEMA: await process_target(sess,t)
    except Exception as e:
        EVENTS.append({"ts":now_iso(),"target":t.get("name","?"),
                       "kind":"change","topic":"Other","severity":"Edit",
                       "headline":f"Error {e}","lines":[str(e)]})

async def watcher():
    cfg=load_cfg()
    interval=int(cfg.get("poll_interval_seconds",90))
    targets=cfg.get("targets",[])
    timeout=aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        while True:
            start=time.time()
            if targets:
                await asyncio.gather(*(safe_process(sess,t) for t in targets))
                LAST_TICK["ts"]=now_iso()
            await asyncio.sleep(max(0,interval-(time.time()-start)))

# -------- Web UI & API --------
INDEX_HTML="""<!doctype html><html><body><h1>FUT Change Watcher</h1>
<div id='events'>Loading…</div>
<script>
async function load(){
 let r=await fetch('/api/events');let j=await r.json();
 document.getElementById('events').innerHTML=j.events.map(e=>`<pre>${e.headline}</pre>`).join('')
}
load();setInterval(load,15000)
</script></body></html>"""

@APP.get("/",response_class=HTMLResponse)
def index(): return HTMLResponse(INDEX_HTML)

@APP.get("/api/events")
def api_events(): return JSONResponse({"events":EVENTS})

@APP.get("/api/health")
def health(): return JSONResponse({"last_check":LAST_TICK["ts"],"config_error":CONFIG_ERR})

@APP.on_event("startup")
async def _startup():
    asyncio.create_task(watcher())

if __name__=="__main__":
    import uvicorn; uvicorn.run("main:APP",host="0.0.0.0",port=8000)