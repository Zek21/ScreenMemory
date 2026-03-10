import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from credentials import cf_headers, cf_zone_id
import requests

headers = cf_headers()
ZONE_ID = cf_zone_id()

# Records to add
records = [
    {
        "type": "CNAME",
        "name": "iqmc6fkcfn45huv5c2q2z4ojs4zxr7lx._domainkey.exzilcalanza.info",
        "content": "iqmc6fkcfn45huv5c2q2z4ojs4zxr7lx.dkim.amazonses.com",
        "proxied": False,
        "ttl": 300,
    },
    {
        "type": "CNAME",
        "name": "oyyw74rdgyobpqkyz2o2mmytd32bbard._domainkey.exzilcalanza.info",
        "content": "oyyw74rdgyobpqkyz2o2mmytd32bbard.dkim.amazonses.com",
        "proxied": False,
        "ttl": 300,
    },
    {
        "type": "CNAME",
        "name": "sgdduhhtyq7fxkesxfdda2mfjhdhsncj._domainkey.exzilcalanza.info",
        "content": "sgdduhhtyq7fxkesxfdda2mfjhdhsncj.dkim.amazonses.com",
        "proxied": False,
        "ttl": 300,
    },
    {
        "type": "TXT",
        "name": "_amazonses.exzilcalanza.info",
        "content": "TFybV0ey5VaQ4qgVq72ATlbvLQxetF4Mbh1sEKuHoLY=",
        "ttl": 300,
    },
]

url = f"https://api.cloudflare.com/client/v4/zones/{ZONE_ID}/dns_records"

for rec in records:
    resp = requests.post(url, headers=headers, json=rec)
    data = resp.json()
    if data.get("success"):
        print(f"OK  {rec['type']:5s} {rec['name']}")
    else:
        errors = data.get("errors", [])
        print(f"ERR {rec['type']:5s} {rec['name']} -> {errors}")
