"""Check if email domains have live websites - Austria version."""
import csv
import requests
import re

INPUT = r'd:\Prospects\Site\results\austria\_intermediate\austria_emails_clean.csv'
OUTPUT = r'd:\Prospects\Site\results\austria\_intermediate\austria_site_check.csv'
FINAL = r'd:\Prospects\Site\results\austria\austria_final_no_website.csv'

FREE_PROVIDERS = {
    'gmail.com', 'yahoo.com', 'yahoo.de', 'hotmail.com', 'hotmail.de',
    'outlook.com', 'outlook.de', 'live.com', 'live.at', 'live.de',
    'gmx.net', 'gmx.at', 'gmx.de', 'aon.at', 'a1.net',
    'icloud.com', 'me.com', 'mail.com', 'protonmail.com',
    't-online.de', 'freenet.de', 'web.de', 'ymail.com',
    'msn.com', 'chello.at', 'aol.com', 'drei.at',
    'mein.gmx',  # malformed but treat as free
}

PARKING_PATTERNS = [
    'domain is for sale', 'buy this domain', 'parked domain',
    'godaddy', 'sedoparking', 'coming soon', 'under construction',
    'apache2 default', 'welcome to nginx', 'plesk default', 'cpanel',
    'this domain', 'domain parking', 'hugedomains', 'afternic',
    'domain has expired', 'registrar', 'namecheap',
]

rows = list(csv.DictReader(open(INPUT, encoding='utf-8')))
print(f"Checking {len(rows)} businesses...")

results = []
for i, r in enumerate(rows):
    email = r.get('email', '').lower().strip()
    # Take first email if multiple
    if ';' in email:
        email = email.split(';')[0].strip()
    
    domain = email.split('@')[1] if '@' in email else ''
    
    if domain in FREE_PROVIDERS:
        status = 'free_email'
        r['site_status'] = status
        r['site_domain'] = domain
        results.append(r)
        print(f"  [{i+1}] {r['name']}: {email} -> FREE EMAIL")
        continue
    
    # Check if domain has a live website
    status = 'unknown'
    for proto in ['https', 'http']:
        try:
            resp = requests.get(f'{proto}://{domain}', timeout=8, 
                              headers={'User-Agent': 'Mozilla/5.0'},
                              allow_redirects=True)
            body = resp.text.lower()[:5000]
            
            if any(p in body for p in PARKING_PATTERNS):
                status = 'parked'
            elif len(resp.text) < 200:
                status = 'minimal'
            else:
                status = 'live'
            break
        except Exception:  # signed: beta
            continue
    
    if status == 'unknown':
        status = 'no_site'
    
    r['site_status'] = status
    r['site_domain'] = domain
    results.append(r)
    print(f"  [{i+1}] {r['name']}: {domain} -> {status}")

# Save site check results
fieldnames = list(results[0].keys())
with open(OUTPUT, 'w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(results)

# Filter to only businesses WITHOUT websites
no_site = [r for r in results if r['site_status'] in ('free_email', 'no_site', 'parked', 'minimal')]
has_site = [r for r in results if r['site_status'] == 'live']

print(f"\n=== Results ===")
print(f"Free email (no site): {sum(1 for r in results if r['site_status']=='free_email')}")
print(f"No site (custom domain): {sum(1 for r in results if r['site_status']=='no_site')}")
print(f"Parked/coming soon: {sum(1 for r in results if r['site_status']=='parked')}")
print(f"Minimal page: {sum(1 for r in results if r['site_status']=='minimal')}")
print(f"LIVE website (REMOVE): {len(has_site)}")
print(f"\nFinal prospects: {len(no_site)}")

# Save final
with open(FINAL, 'w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(no_site)

if has_site:
    print("\nRemoved (has live website):")
    for r in has_site:
        print(f"  {r['name']} ({r['city']}) -> {r['site_domain']}")
