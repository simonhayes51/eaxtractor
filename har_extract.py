# har_extract.py
import json, re, sys, yaml, urllib.parse
from collections import OrderedDict, defaultdict

ALLOWED = re.compile(r'\.(json|js)$|service-worker\.js|asset-manifest|manifest\.json', re.I)
BLOCK = re.compile(r'(analytics|telemetry|sentry|amplitude|google|facebook|optimizely)', re.I)

def guess_type(url):
    if url.endswith('.json'): return 'json'
    return 'text'

def main(har_path):
    with open(har_path, 'r', encoding='utf-8') as f:
        har = json.load(f)
    seen = OrderedDict()
    for e in har.get('log', {}).get('entries', []):
        req = e.get('request', {})
        url = req.get('url','')
        if not url or BLOCK.search(url): 
            continue
        if not ALLOWED.search(url): 
            continue
        # keep only queryless CDN URLs
        pu = urllib.parse.urlparse(url)
        clean = pu._replace(query='', fragment='').geturl()
        seen[clean] = guess_type(clean)

    targets = []
    for url, typ in seen.items():
        name = re.sub(r'[^a-z0-9]+','_', urllib.parse.urlparse(url).path.split('/')[-1].lower()).strip('_') or 'endpoint'
        targets.append({
            "name": name[:64],
            "url": url,
            "type": typ,
            # tighten later if noisy
            "track_keys": {"include": ["sbc","challenge","objective","pack","catalog","group","template","isEnabled","availability","start","end","requirements"], "exclude": ["lastUpdated","generatedAt","build","timestamp"]}
        })

    cfg = {
        "poll_interval_seconds": 90,
        "targets": targets[:150],  # safety cap
    }
    with open('endpoints.yaml', 'w', encoding='utf-8') as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    print(f"Wrote endpoints.yaml with {len(targets)} targets")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python har_extract.py capture.har"); sys.exit(1)
    import yaml  # lazy import check
    main(sys.argv[1])
