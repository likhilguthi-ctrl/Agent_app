import urllib.request
import json
from pathlib import Path

env_lines = Path('backend/.env').read_text().splitlines()
api_key = None
for l in env_lines:
    if l.startswith('X_API_KEY=') or l.startswith('NOTIFICATION_SERVICE_API_KEY='):
        api_key = l.split('=', 1)[1].strip().strip('"').strip("'")
        break

url = 'http://handorg.duckdns.org/api/v1/health'

headers_to_test = [
    {'X-Tenant-ID': 'springfield-ott'},
    {'X-Tenant': 'springfield-ott'},
    {'X-Tenant-Slug': 'springfield-ott'},
    {'X-Community': 'springfield-ott'},
    {'X-Community-Slug': 'springfield-ott'},
    {'X-Community-Id': 'springfield-ott'},
    {'Tenant': 'springfield-ott'},
    {'X-Tenant-ID': 'default'},
    {'X-Tenant': 'default'},
    {'X-Community': 'default'},
    {'X-Tenant-ID': 'community-ott'},
    {'X-Tenant': 'community-ott'},
    {'X-Community': 'community-ott'},
    {'X-Tenant-ID': 'avanflix'},
    {'X-Tenant': 'avanflix'},
    {'X-Community': 'avanflix'},
    {'Host': 'handorg.duckdns.org'},
    {'X-Forwarded-Host': 'springfield-ott.handorg.duckdns.org'},
    {'X-Forwarded-Host': 'springfield-ott'},
    {'X-Subdomain': 'springfield-ott'},
    {'X-Tenant-Domain': 'springfield-ott.local'},
]

for h in headers_to_test:
    name = list(h.keys())[0] + ": " + list(h.values())[0]
    headers = {
        'User-Agent': 'Community-OTT-Audit',
        'X-API-Key': api_key or ''
    }
    headers.update(h)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=3) as resp:
            print(f"SUCCESS {name} -> {resp.status}: {resp.read().decode()[:100]}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if "TENANT_NOT_FOUND" not in body:
            print(f"DIFF ERROR {name} -> HTTP {e.code}: {body[:100]}")
        # else it's the same TENANT_NOT_FOUND
    except Exception as e:
        print(f"ERR {name} -> {e}")

