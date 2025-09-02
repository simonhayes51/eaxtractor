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
    if re.search(r"\bisEnabled:\s*false\s*->\s*true\b", txt): return "Live"  # went live
    if re.search(r"\bADDED\b", txt): return "New"
    return "Edit"

def make_headline(topic: str, lines: List[str]) -> str:
    for ln in lines:
        if "isEnabled:" in ln: return f"{topic}: enable flip ({ln.strip()})"
        if "minRating" in ln or "squadRating" in ln: return f"{topic}: rating change ({ln.strip()})"
        if "ADDED" in ln and "[" in ln and "]" in ln: return f"{topic}: new item {ln.split(':',1)[0].strip()}"
        if "LIST" in ln: return f"{topic}: list size changed ({ln.strip()})"
    return f"{topic}: {lines[0][:120]}"

# -------- pretty post generators --------
def _val_right(ln: str) -> str:
    if "->" in ln: return ln.split("->")[-1].strip().strip("'\"")
    if ":" in ln:  return ln.split(":")[-1].strip().strip("'\"")
    return ""

def _collect_window(lines: List[str]) -> Optional[str]:
    s, e = None, None
    for ln in lines:
        low = ln.lower()
        if ("availability.start" in low or re.search(r"(^|\.)(start)\b", low)) and "T" in ln:
            s = _val_right(ln)
        if ("availability.end" in low or re.search(r"(^|\.)(end)\b", low)) and "T" in ln:
            e = _val_right(ln)
    if s or e:
        return f"{s or '?'} → {e or '?'}"
    return None

def post_for_sbc(lines: List[str]) -> str:
    title = None; reward = None; repeatable = None
    req = []
    for ln in lines:
        low = ln.lower()
        if ".name:" in low and not title:
            title = _val_right(ln)
        if "reward" in low and ":" in ln and not reward:
            reward = _val_right(ln)
        if "repeatable" in low and repeatable is None:
            repeatable = _val_right(ln)
        # requirement-ish signals
        if any(k in low for k in ["requirements","minrating","squadrating","chem","sameleague","samenation","rareplayers","gold","playersfrom","teamchemistry"]):
            val = _val_right(ln)
            # crude humanization
            pretty = None
            if "minrating" in low or "squadrating" in low: pretty = f"• Min Squad Rating {val}"
            elif "teamchemistry" in low or "chem" in low:   pretty = f"• Min Team Chemistry {val}"
            elif "sameleague" in low and "min" in low:      pretty = f"• Min {val} from same League"
            elif "sameleague" in low and "max" in low:      pretty = f"• Max {val} from same League"
            elif "samenation" in low and "min" in low:      pretty = f"• Min {val} from same Nation"
            elif "samenation" in low and "max" in low:      pretty = f"• Max {val} from same Nation"
            elif "playersfrom" in low and "league" in low:  pretty = f"• League players: {val}"
            elif "playersfrom" in low and "nation" in low:  pretty = f"• Nationalities: {val}"
            elif "rareplayers" in low and "min" in low:     pretty = f"• Min {val} Rare players"
            elif "gold" in low and ("min" in low or "count" in low): pretty = f"• Min {val} Gold players"
            if pretty and pretty not in req: req.append(pretty)
    window = _collect_window(lines)
    md = []
    md.append(f"SBC: {title or 'New SBC'}")
    if reward: md.append(f"Reward: {reward}")
    if window: md.append(f"Window: {window}")
    if repeatable is not None: md.append(f"Repeatable: {repeatable}")
    if req:
        md.append("Requirements:")
        md += req
    return "\n".join(md)

def post_for_evo(lines: List[str]) -> str:
    name=None; costc=None; costp=None
    elig=[]; boosts=[]; tasks=[]
    for ln in lines:
        low=ln.lower()
        if ".name:" in low and not name: name=_val_right(ln)
        if "cost.coins" in low: costc=_val_right(ln)
        if "cost.points" in low: costp=_val_right(ln)
        if any(k in low for k in ["eligibility.","eligibility"]):
            # try to compress common patterns
            if "overallmax" in low: elig.append(f"• OVR ≤ {_val_right(ln)}")
            elif "overallmin" in low: elig.append(f"• OVR ≥ {_val_right(ln)}")
            elif "position" in low: elig.append(f"• Position { _val_right(ln) }")
            elif "pacemin" in low: elig.append(f"• Pace ≥ {_val_right(ln)}")
            elif "leagues.anyof" in low: elig.append(f"• Leagues: {_val_right(ln)}")
            elif "nations.anyof" in low: elig.append(f"• Nations: {_val_right(ln)}")
        if "boosts." in low:
            if "overall" in low: boosts.append(f"• Overall {_val_right(ln)}")
            else: boosts.append(f"• {ln.split('.',1)[1].split(':',1)[0].title()} {_val_right(ln)}")
        if ".tasks" in low or "objective" in low:
            if ":" in ln: tasks.append(f"• {_val_right(ln)}")
    window=_collect_window(lines)
    md=[]
    md.append(f"Evolution: {name or 'New Evolution'}")
    if costc or costp: md.append(f"Cost: {costc or 0} coins / {costp or 0} points")
    if window: md.append(f"Window: {window}")
    if elig: md.append("Eligibility:"); md += elig
    if boosts: md.append("Boosts:"); md += boosts
    if tasks: md.append("Tasks:"); md += tasks
    return "\n".join(md)

def post_for_packs(lines: List[str]) -> str:
    name=None; price=None; window=_collect_window(lines)
    for ln in lines:
        low=ln.lower()
        if ".name:" in low and not name: name=_val_right(ln)
        if "price" in low and price is None: price=_val_right(ln)
    md=[f"Pack: {name or 'New Pack'}"]
    if price: md.append(f"Price: {price}")
    if window: md.append(f"Window: {window}")
    md.append("Status: change detected in Store pack config")
    return "\n".join(md)

def post_for_objectives(lines: List[str]) -> str:
    name=None; rewards=[]; tasks=[]
    for ln in lines:
        low=ln.lower()
        if ".name:" in low and not name: name=_val_right(ln)
        if "rewards" in low and ":" in ln: rewards.append(f"• {_val_right(ln)}")
        if "task" in low or "objective" in low:
            if ":" in ln: tasks.append(f"• {_val_right(ln)}")
    window=_collect_window(lines)
    md=[f"Objectives: {name or 'New Objective'}"]
    if window: md.append(f"Window: {window}")
    if rewards: md.append("Rewards:"); md+=rewards
    if tasks: md.append("Tasks:"); md+=tasks
    return "\n".join(md)

def post_for_locales(lines: List[str]) -> str:
    # Summarise that new strings landed (names often leak early)
    samples = [ln for ln in lines if "name:" in ln or "SBC" in ln or "PROMO" in ln][:6]
    md=["Strings update detected (promo/SBC names often appear early)."]
    md += [f"• {s}" for s in samples] if samples else []
    return "\n".join(md)

def post_for_bundles(lines: List[str]) -> str:
    samples = lines[:6]
    md=["Bundle updated (JS/manifest/service worker). Look for new labels:"]
    md += [f"• {s}" for s in samples]
    return "\n".join(md)

def post_for_flags(lines: List[str]) -> str:
    md=["Feature flag/config change:"]
    md += [f"• {s}" for s in lines[:10]]
    return "\n".join(md)

TOPIC_POSTER = {
    "SBC": post_for_sbc,
    "Evolutions": post_for_evo,
    "Packs": post_for_packs,
    "Objectives": post_for_objectives,
    "Locales": post_for_locales,
    "Bundles": post_for_bundles,
    "Flags": post_for_flags,
    "Other": lambda lines: "Update detected:\n" + "\n".join(f"• {l}" for l in lines[:12]),
}

# -------- PNG rendering --------
def render_text_png(md: str, title: Optional[str]=None) -> bytes:
    # simple dark card with wrapped text
    W = 1200
    P = 32
    BG = (13,17,23)     # dark
    CARD = (18,24,33)
    FG = (231,237,243)
    ACC = (147,255,102)

    # fonts
    try:
        # DejaVu is bundled in many python images; fallback to default if missing
        title_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 48)
        body_font = ImageFont.truetype("DejaVuSans.ttf", 30)
        small_font = ImageFont.truetype("DejaVuSans.ttf", 24)
    except:
        title_font = ImageFont.load_default()
        body_font = ImageFont.load_default()
        small_font = ImageFont.load_default()

    # wrap text
    wrapper = textwrap.TextWrapper(width=70, replace_whitespace=False)
    lines = []
    for raw in md.splitlines():
        if not raw.strip():
            lines.append("")
            continue
        if raw.startswith("• "):
            wrapped = textwrap.wrap(raw[2:], width=68) or [""]
            lines.append("• " + wrapped[0])
            for w in wrapped[1:]:
                lines.append("  " + w)
        else:
            lines += wrapper.wrap(raw) or [""]

    # compute height
    body_h = sum(body_font.getbbox(l or "A")[3] - body_font.getbbox(l or "A")[1] + 8 for l in lines)
    title_text = title or "FUT Change Watcher Export"
    title_h = title_font.getbbox(title_text)[3] - title_font.getbbox(title_text)[1]
    footer = f"Generated {now_iso()}"
    foot_h = small_font.getbbox(footer)[3] - small_font.getbbox(footer)[1]

    H = P*3 + title_h + body_h + foot_h
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # card bg
    draw.rounded_rectangle((P//2, P//2, W-P//2, H-P//2), radius=24, fill=CARD)

    # title
    draw.text((P+8, P+8), title_text, font=title_font, fill=ACC)

    # body
    y = P + title_h + 24
    for l in lines:
        draw.text((P+12, y), l, font=body_font, fill=FG)
        y += (body_font.getbbox(l or "A")[3] - body_font.getbbox(l or "A")[1] + 8)

    # footer
    draw.text((P+12, H-P-8), footer, font=small_font, fill=(138,151,166))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

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
        # auto-generate post
        poster = TOPIC_POSTER.get(topic, TOPIC_POSTER["Other"])
        post_md = poster(interesting)

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

# -------- fault-tolerant loop & config loading --------
def load_cfg():
    try:
        with open("endpoints.yaml","r",encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        targets = cfg.get("targets") or []
        CONFIG_ERR["targets"] = len(targets)
        CONFIG_ERR["message"] = None if targets else "No targets found in endpoints.yaml"
        return cfg
    except Exception as e:
        CONFIG_ERR["message"] = f"{type(e).__name__}: {e}"
        CONFIG_ERR["targets"] = 0
        return {"poll_interval_seconds": 90, "targets": []}

async def safe_process(session, t):
    try:
        async with SEMA:
            await process_target(session, t)
    except Exception as e:
        EVENTS.append({
            "ts": now_iso(),
            "target": t.get("name","unknown"),
            "kind": "change",
            "topic": "Flags",
            "severity": "Edit",
            "headline": f"Fetcher error: {type(e).__name__}",
            "lines": [str(e)]
        })
        del EVENTS[:-MAX_EVENTS]

async def watcher():
    cfg = load_cfg()
    interval = int(cfg.get("poll_interval_seconds", 90))
    targets = cfg.get("targets", [])
    print(f"[watcher] loaded {len(targets)} targets, interval={interval}s")

    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
    headers={"User-Agent":"FUT-PrivateWatcher/1.3", "Accept":"*/*"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        while True:
            start = time.time()
            try:
                if not targets:
                    await asyncio.sleep(60)
                    cfg = load_cfg()
                    interval = int(cfg.get("poll_interval_seconds", 90))
                    targets = cfg.get("targets", [])
                    print(f"[watcher] reloaded: {len(targets)} targets")
                else:
                    await asyncio.gather(*(safe_process(sess, t) for t in targets), return_exceptions=True)
                    LAST_TICK["ts"] = now_iso()
            except Exception as e:
                print(f"[watcher] ERROR: {type(e).__name__}: {e}")
            await asyncio.sleep(max(0, interval - (time.time()-start)))

# --------- Web UI & API ---------
INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>FUT Change Watcher</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/water.css@2/out/water.css">
<style>
:root{ --bg:#0b0f14; --card:#121820; --muted:#8a97a6; --lime:#93ff66; --amber:#ffd166; --cyan:#66e0ff; }
body{max-width:1100px;background:var(--bg);color:#e7edf3}
.header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;margin-right:8px;color:#000}
.badge.Baseline{background:#cbd5e1}
.badge.New{background:var(--cyan)}
.badge.Live{background:var(--lime)}
.badge.Edit{background:var(--amber)}
.topic{background:#223046;color:#b7ccff}
.event{padding:14px;border:1px solid #1f2a38;border-radius:14px;margin:12px 0;background:var(--card)}
.lines{font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; white-space:pre-wrap}
small{color:var(--muted)}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
select,input[type="search"]{background:#0e141c;border:1px solid #273345;color:#e7edf3}
summary{cursor:pointer}
.mini{font-size:12px;color:var(--muted)}
.headline{font-weight:700;margin:6px 0}
.icon{opacity:.9;margin-right:6px}
</style>
</head>
<body>
  <div class="header">
    <h1 style="margin:0">FUT Change Watcher</h1>
    <div><small>Last check: <span id="last">—</span> <span id="cfg" style="color:#f88"></span></small></div>
  </div>

  <div class="controls">
    <select id="kind"><option value="">All events</option><option value="change">Changes only</option><option value="baseline">Baselines only</option></select>
    <select id="topic"><option value="">All topics</option><option>Evolutions</option><option>SBC</option><option>Packs</option><option>Objectives</option><option>Locales</option><option>Bundles</option><option>Flags</option><option>Other</option></select>
    <select id="severity"><option value="">Any severity</option><option>New</option><option>Live</option><option>Edit</option><option>Baseline</option></select>
    <input id="q" type="search" placeholder="Search text…"/>
    <button id="clear">Clear filters</button>
    <button id="copy">Copy latest post</button>
    <button id="png">Download latest post (PNG)</button>
  </div>

  <div id="events">Loading…</div>

<script>
const ICONS = { "SBC":"🧩", "Packs":"🎁", "Objectives":"🎯", "Locales":"📝", "Bundles":"📦", "Flags":"🚩", "Evolutions":"🔄", "Other":"✨" };

function el(tag, html){ const d=document.createElement(tag); d.innerHTML=html; return d; }

async function load(){
  const [evRes, hRes] = await Promise.all([fetch('/api/events'), fetch('/api/health')]);
  const js = await evRes.json();
  const health = await hRes.json();
  document.getElementById('last').textContent = health.last_check || '—';
  document.getElementById('cfg').textContent = (health.config_error && health.config_error.message) ? ` • ${health.config_error.message}` : '';
  const wrap = document.getElementById('events');
  const kind = document.getElementById('kind').value;
  const topic = document.getElementById('topic').value;
  const severity = document.getElementById('severity').value;
  const q = (document.getElementById('q').value || '').toLowerCase();

  wrap.innerHTML='';
  const events = js.events.slice();
  events.reverse().forEach(ev=>{
    if (kind && ev.kind !== kind) return;
    if (topic && ev.topic !== topic) return;
    if (severity && ev.severity !== severity) return;
    if (q){
      const text = (ev.headline + ' ' + (ev.lines||[]).join(' ')).toLowerCase();
      if (!text.includes(q)) return;
    }
    if (ev.kind === 'baseline'){
      wrap.appendChild(el('div', `📌 <span class="badge Baseline">Baseline</span> <span class="badge topic">${ev.topic||'Other'}</span> <b>${ev.target}</b> <small>${ev.ts}</small>`));
      return;
    }
    const icon = ICONS[ev.topic||'Other'] || '✨';
    const card = `
      <div class="event">
        <div>
          <span class="badge ${ev.severity}">${ev.severity}</span>
          <span class="badge topic">${ev.topic || 'Other'}</span>
          <b class="icon">${icon}</b><b>${ev.target}</b> <small>${ev.ts}</small>
        </div>
        <div class="headline">${ev.headline || ''}</div>
        ${ev.post_md ? `<pre class="lines">${ev.post_md}</pre>` : ''}
        <details><summary>Show raw diff</summary><div class="lines">${(ev.lines||[]).join('\\n')}</div></details>
      </div>`;
    wrap.appendChild(el('div', card));
  });
}

document.getElementById('clear').onclick = () => {
  document.getElementById('kind').value='';
  document.getElementById('topic').value='';
  document.getElementById('severity').value='';
  document.getElementById('q').value='';
  load();
};

document.getElementById('copy').onclick = async () => {
  const topic = document.getElementById('topic').value || 'SBC';
  const r = await fetch('/api/export?format=markdown&topic='+encodeURIComponent(topic));
  const j = await r.json();
  if(!j.ok){ alert(j.error || 'No post yet'); return; }
  await navigator.clipboard.writeText(j.body);
  alert(`Copied latest ${topic} post!`);
};

document.getElementById('png').onclick = async () => {
  const topic = document.getElementById('topic').value || 'SBC';
  const r = await fetch('/api/export?format=png&topic='+encodeURIComponent(topic));
  if(r.status!==200){ const j=await r.json().catch(()=>({error:'No post'})); alert(j.error||'No post yet'); return; }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href=url; a.download=`fut_${topic.toLowerCase()}_post.png`; a.click();
  URL.revokeObjectURL(url);
};

load(); setInterval(load, 15000);
</script>
</body>
</html>"""

@APP.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INDEX_HTML)

@APP.get("/favicon.ico")
def favicon():
    return Response(status_code=204)

@APP.get("/api/events")
def api_events():
    return JSONResponse({"events": EVENTS})

@APP.get("/api/health")
def health():
    return JSONResponse({"last_check": LAST_TICK["ts"], "events_cached": len(EVENTS), "config_error": CONFIG_ERR})

@APP.get("/api/config")
def api_config():
    try:
        with open("endpoints.yaml","r",encoding="utf-8") as f:
            raw = f.read()
        return JSONResponse({"ok": True, "targets": CONFIG_ERR["targets"], "yaml": raw[:4000]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})

@APP.get("/api/export")
def export_latest(
    format: str = Query("markdown", pattern="^(markdown|text|png)$"),
    topic: str = Query("SBC")
):
    # Find newest change for topic with a post
    for ev in reversed(EVENTS):
        if ev.get("kind")=="change" and ev.get("topic")==topic and ev.get("post_md"):
            if format=="markdown":
                return JSONResponse({"ok": True, "format":"markdown", "body": ev["post_md"]})
            if format=="text":
                body = ev["post_md"].replace("• ", "- ")
                return JSONResponse({"ok": True, "format":"text", "body": body})
            # PNG
            png = render_text_png(ev["post_md"], title=f"{topic} Update")
            return Response(content=png, media_type="image/png")
    return JSONResponse({"ok": False, "error": f"No {topic} post available yet."}, status_code=404)

# --- auto-start watcher on boot ---
@APP.on_event("startup")
async def _startup():
    global WATCHER_TASK
    print("[startup] starting watcher…")
    WATCHER_TASK = asyncio.create_task(watcher())
