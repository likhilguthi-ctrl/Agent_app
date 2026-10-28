import urllib.request
import json
from pathlib import Path

env_lines = Path('backend/.env').read_text().splitlines()
api_key = None
for l in env_lines:
    if l.startswith('X_API_KEY=') or l.startswith('NOTIFICATION_SERVICE_API_KEY='):
        api_key = l.split('=', 1)[1].strip().strip('"').strip("'")
        break

url = 'http://handorg.duckdns.org/api/v1/payments/init'

tests = [
    ("header X-Tenant-Id: springfield-ott", {'X-Tenant-Id': 'springfield-ott'}, {'amount': 100, 'currency': 'INR'}),
    ("header X-Tenant-Id: tenant-1", {'X-Tenant-Id': 'tenant-1'}, {'amount': 100, 'currency': 'INR'}),
    ("payload tenant_id: tenant-1", {}, {'amount': 100, 'currency': 'INR', 'tenant_id': 'tenant-1'}),
    ("payload tenant: tenant-1", {}, {'amount': 100, 'currency': 'INR', 'tenant': 'tenant-1'}),
    ("payload tenantId: tenant-1", {}, {'amount': 100, 'currency': 'INR', 'tenantId': 'tenant-1'}),
    ("payload community: springfield-ott", {}, {'amount': 100, 'currency': 'INR', 'community': 'springfield-ott'}),
    ("header X-Tenant: tenant-1", {'X-Tenant': 'tenant-1'}, {'amount': 100, 'currency': 'INR'}),
    ("header X-Community-ID: springfield-ott", {'X-Community-ID': 'springfield-ott'}, {'amount': 100, 'currency': 'INR'}),
    ("header Host: springfield-ott.handorg.duckdns.org", {'Host': 'springfield-ott.handorg.duckdns.org'}, {'amount': 100, 'currency': 'INR', 'tenant': 'tenant-1'}),
]

for label, extra_headers, payload in tests:
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Community-OTT-Audit',
        'X-API-Key': api_key or ''
    }
    headers.update(extra_headers)
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=4) as resp:
            print(f"{label} -> {resp.status}: {resp.read().decode()[:100]}")
    except urllib.error.HTTPError as e:
        print(f"{label} -> HTTP {e.code}: {e.read().decode()[:100]}")
    except Exception as e:
        print(f"{label} -> Error: {e}")

