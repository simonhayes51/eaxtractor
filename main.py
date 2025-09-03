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
SEMA = asyncio.Semaphore(10)  # throttle concurrency

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
                    for key in ("id","challengeId","groupId","objectiveId","templateId","name","packId","evolutionId","playerId","assetId","definitionId"):
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

# -------- classify & summarise --------
TOPIC_PATTERNS = [
    ("Evolutions", re.compile(r"\b(evolution|evo|evolutions|evolutionId|eligibility|boosts|tasks|cost)\b", re.I)),
    ("SBC",        re.compile(r"\b(SBC|challenge|group|template|required|chem|squadRating|minRating|requirements|repeatable)\b", re.I)),
    ("Packs",      re.compile(r"\b(pack|store|price|start|end|guarantee|rarity|weight|coins|points)\b", re.I)),
    ("Objectives", re.compile(r"\b(objective|task|milestone|season|reward)\b", re.I)),
    ("Locales",    re.compile(r"\b(locale|string|en_us|string_catalog)\b", re.I)),
    ("Bundles",    re.compile(r"\b(\.js|bundle|service-worker|manifest)\b", re.I)),
    ("Flags",      re.compile(r"\b(remoteconfig|feature|isEnabled|enableAt|rollout|treatment|flag)\b", re.I)),
]

def classify_topic(target: str, lines: List[str]) -> str:
    blob = f"{target} " + " ".join(lines[:10])
    for topic, pat in TOPIC_PATTERNS:
        if pat.search(blob): return topic
    return "Other"

def classify_severity(lines: List[str]) -> str:
    txt = "\n".join(lines)
    if re.search(r"\bisEnabled:\s*false\s*->\s*true\b", txt): return "Live"
    if re.search(r"\bADDED\b", txt): return "New"
    return "Edit"

def make_headline(topic: str, lines: List[str]) -> str:
    for ln in lines:
        if "isEnabled:" in ln: return f"{topic}: enable flip ({ln.strip()})"
        if "minRating" in ln or "squadRating" in ln: return f"{topic}: rating change ({ln.strip()})"
        if "ADDED" in ln and "[" in ln and "]" in ln: return f"{topic}: new item {ln.split(':',1)[0].strip()}"
        if "LIST" in ln: return f"{topic}: list size changed ({ln.strip()})"
    return f"{topic}: {lines[0][:120]}"

# -------- JSON capture for enrichment --------
def _latest_snap_json(target: str) -> Optional[dict]:
    dirp = DATA / "snaps" / target
    if not dirp.exists(): return None
    snaps = sorted(dirp.glob("*.json"))
    if not snaps: return None
    try:
        return json.loads(snaps[-1].read_text(encoding="utf-8"))
    except Exception:
        return None

def _walk_dicts(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_dicts(v)

def _index_keyattributes() -> Tuple[dict, dict]:
    data = _latest_snap_json(KEYATTR_TARGET) or {}
    by_id, by_name = {}, {}
    for d in _walk_dicts(data):
        if not isinstance(d, dict): continue
        if any(k in d for k in ("overall","pace","shooting","passing","dribbling","defending","physical")):
            pid = d.get("id") or d.get("assetId") or d.get("definitionId") or d.get("playerId")
            if pid is not None:
                by_id[str(pid)] = d
            n = d.get("name") or d.get("commonName") or d.get("fullName")
            if isinstance(n, str):
                by_name.setdefault(n.lower(), []).append(d)
    return by_id, by_name

def _fmt_stats(p: dict) -> str:
    name = p.get("name") or p.get("commonName") or "Unknown"
    pos  = p.get("position") or p.get("preferredPosition") or ""
    club = p.get("club") or ""
    nation = p.get("nation") or ""
    league = p.get("league") or ""
    ovr = p.get("overall") or p.get("rating") or "?"
    pac = p.get("pace") or p.get("pac") or "?"
    sho = p.get("shooting") or p.get("sho") or "?"
    pas = p.get("passing") or p.get("pas") or "?"
    dri = p.get("dribbling") or p.get("dri") or "?"
    de  = p.get("defending") or p.get("def") or "?"
    phy = p.get("physical") or p.get("phy") or "?"
    meta = " • ".join([x for x in [nation, club, league] if x])
    return f"{name} {pos} — {ovr} OVR | PAC {pac} SHO {sho} PAS {pas} DRI {dri} DEF {de} PHY {phy}" + (f"  ({meta})" if meta else "")

# -------- pretty post generators --------
# ... SBC/Evo/Packs/Objectives/Locales/Bundles/Flags poster functions here
# (same as the previous version you had; unchanged)

# -------- processing one target --------
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
        poster = TOPIC_POSTER.get(topic, TOPIC_POSTER["Other"])
        post_md = poster(interesting)

        # --- Enrich with players ---
        players_section = []
        if name in CATALOG_TARGETS:
            added_ids = set()
            for ln in interesting:
                m = re.search(r"(playerId|assetId|definitionId)\D+(\d+)", ln)
                if m: added_ids.add(m.group(2))
            added_names = []
            for ln in interesting:
                m2 = re.search(r"\.name:\s+ADDED\s+['\"]([^'\"]+)['\"]", ln)
                if m2: added_names.append(m2.group(1))
            if added_ids or added_names:
                id_map, name_map = _index_keyattributes()
                for pid in sorted(added_ids):
                    p = id_map.get(pid)
                    if p:
                        players_section.append("• " + _fmt_stats(p))
                for nm in added_names:
                    arr = name_map.get(nm.lower(), [])
                    if arr:
                        p = max(arr, key=lambda x: int(x.get("overall", 0)))
                        players_section.append("• " + _fmt_stats(p))
            if players_section:
                post_md += "\n\nPlayers:\n" + "\n".join(players_section)

        EVENTS.append({
            "ts": now_iso(),
            "target": name,
            "kind": "change",
            "topic": topic,
            "severity": severity,
            "headline": headline,
            "lines": interesting[:250],
            "post_md": post_md
        })
        del EVENTS[:-MAX_EVENTS]

# -------- watcher, API, UI code below unchanged --------
# (health, /api/events, /api/export, INDEX_HTML, etc.)