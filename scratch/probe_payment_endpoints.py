import urllib.request

base = 'http://handorg.duckdns.org'
paths = [
    '/api/v1/payments',
    '/api/v1/tenants',
    '/api/v1/payments/health',
    '/api/v1/health',
    '/actuator/health',
    '/checkout/api/details/1',
]

for p in paths:
    url = f"{base}{p}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Community-OTT-Audit'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            print(f"{p} -> {resp.status}: {resp.read().decode()[:150]}")
    except urllib.error.HTTPError as e:
        print(f"{p} -> HTTP {e.code}: {e.read().decode()[:150]}")
    except Exception as e:
        print(f"{p} -> Error: {e}")

