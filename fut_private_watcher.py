# fut_private_watcher.py
import asyncio, aiohttp, json, hashlib, time, re
from pathlib import Path
from datetime import datetime, timezone
import yaml

DATA = Path("./private_data")
SNAPS = DATA / "snaps"
META = DATA / "meta"
for p in (SNAPS, META): p.mkdir(parents=True, exist_ok=True)

def now(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
def jdump(x): return json.dumps(x, sort_keys=True, separators=(",",":"))
def hsh(b: bytes): return hashlib.sha256(b).hexdigest()

def scrub_json(obj, include=None, exclude=None):
    def keep(path):
        inc_ok = True if not include else any(s in path for s in include)
        exc_ok = True if not exclude else not any(s in path for s in exclude)
        return inc_ok and exc_ok
    def walk(node, path):
        if isinstance(node, dict):
            out = {}
            for k,v in node.items():
                p = f"{path}.{k}" if path else k
                if keep(p): out[k] = walk(v, p)
            return out
        if isinstance(node, list):
            return [walk(v, f"{path}[]") for v in node]
        return node
    return walk(obj, "")

def diff(a, b, path=""):
    out = []
    if type(a) != type(b):
        out.append(f"{path}: TYPE {type(a).__name__} -> {type(b).__name__}"); return out
    if isinstance(a, dict):
        ks = set(a)|set(b)
        for k in sorted(ks):
            p = f"{path}.{k}" if path else k
            if k not in a: out.append(f"{p}: ADDED {repr(b[k])}")
            elif k not in b: out.append(f"{p}: REMOVED")
            else: out.extend(diff(a[k], b[k], p))
        return out
    if isinstance(a, list):
        if len(a)!=len(b): out.append(f"{path}: LIST {len(a)} -> {len(b)}")
        # keyed compare (ids, names)
        def keyed(xs):
            res={}
            for it in xs:
                if isinstance(it,dict):
                    for key in ("id","challengeId","groupId","objectiveId","templateId","name"):
                        if key in it: res[(key,it[key])] = hsh(jdump(it).encode())[:8]; break
            return res
        ka, kb = keyed(a), keyed(b)
        for k in sorted(set(ka)|set(kb)):
            if k not in ka: out.append(f"{path}[{k}]: ADDED")
            elif k not in kb: out.append(f"{path}[{k}]: REMOVED")
        return out
    if a != b: out.append(f"{path}: {repr(a)} -> {repr(b)}")
    return out

async def fetch(session, name, url, cond):
    meta_p = META/f"{name}.json"
    headers={}
    if meta_p.exists():
        m=json.loads(meta_p.read_text())
        if m.get("etag"): headers["If-None-Match"]=m["etag"]
        if m.get("lm"): headers["If-Modified-Since"]=m["lm"]
    try:
        async with session.get(url, headers=headers) as r:
            if r.status==304: return None, {"not_modified":True}
            body = await r.read()
            meta={"etag":r.headers.get("ETag"), "lm":r.headers.get("Last-Modified"), "ts":now(), "status":r.status}
            meta_p.write_text(json.dumps(meta, indent=2))
            return body, meta
    except Exception as e:
        print(f"[{now()}] {name}: ERROR {e}")
        return None, {"error":True}

async def handle(session, t):
    name, url, typ = t["name"], t["url"], t.get("type","text")
    inc = (t.get("track_keys") or {}).get("include", [])
    exc = (t.get("track_keys") or {}).get("exclude", [])
    raw, meta = await fetch(session, name, url, {})
    if not raw or meta.get("not_modified"): return
    ext = "json" if typ=="json" else "txt"
    dirp = SNAPS/name; dirp.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    snap = dirp/f"{ts}.{ext}"; snap.write_bytes(raw)

    def parse(b):
        if typ=="json":
            try: obj=json.loads(b.decode("utf-8","ignore"))
            except: return None
            return scrub_json(obj, inc, exc) if (inc or exc) else obj
        else:
            txt=b.decode("utf-8","ignore")
            lines=[ln for ln in txt.splitlines() if re.search(r"(SBC|Objective|Promo|Challenge|Season|Pack)", ln, re.I)]
            return {"lines":lines}

    snaps=sorted(dirp.glob(f"*.{ext}"))
    cur=parse(raw)
    prev=parse(snaps[-2].read_bytes()) if len(snaps)>=2 else None
    if prev is None:
        print(f"[{now()}] {name}: first snapshot ({snap.name})"); return
    changes = diff(prev, cur) if typ=="json" else list(sorted(set(cur.get("lines",[]))-set(prev.get("lines",[]))))
    # noise gate
    interesting=[c for c in changes if not re.search(r"(lastUpdated|generatedAt|build|timestamp)", c, re.I)]
    if interesting:
        print(f"\n[{now()}] {name} CHANGED @ {ts}")
        for line in interesting[:120]: print(" -", line)

async def main():
    cfg=yaml.safe_load(open("endpoints.yaml","r",encoding="utf-8"))
    interval=int(cfg.get("poll_interval_seconds",90))
    targets=cfg.get("targets",[])
    timeout=aiohttp.ClientTimeout(total=20)
    headers={"User-Agent":"PrivateFUTWatcher/0.1","Accept":"*/*","Accept-Encoding":"gzip, deflate, br"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        while True:
            start=time.time()
            await asyncio.gather(*(handle(sess,t) for t in targets))
            await asyncio.sleep(max(0, interval-(time.time()-start)))

if __name__=="__main__":
    asyncio.run(main())
