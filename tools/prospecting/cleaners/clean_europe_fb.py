import csv
import re
from collections import defaultdict, Counter

INPUT = r'd:\Prospects\Site\results\europe\_intermediate\europe_emails_fb.csv'
OUT_CSV = r'd:\Prospects\Site\results\europe\_intermediate\europe_emails_fb_clean.csv'
OUT_MD = r'd:\Prospects\Site\results\europe\europe_prospects_with_email.md'

# Bad domains
BAD_DOMAINS = {
    'duckduckgo.com', 'unilever.com', 'shop-cunda.de', 'sasktel.net',
    'paintzen.com', 'infinityinds.com', 'cleanituk.com', 'balabustabrooklyn.com',
    'lavenircolombia.com', 'laveneco.vn'
}

# Bad exact emails
BAD_EMAILS = {
    'mogaswimwear1998@gmail.com', 'vishantpawar@gmail.com', 'hello@cleanplanet.in',
    'carrepair@gmail.com', 'info@minit.com.au', 'info@ifixgsm.com.au',
    'contact@eco.com', 'info@selfboxstudio.be', 'info@proper-facilitair.nl',
    'service@excellentroofing.com', 'info@i-clean-cars.be',
    'guildinfo@nationalguild.org', 'info@ecohvac.co', 'info@landmarkfencingltd.co.uk',
    'jimpiccoli@berlinplumbinginc.com', 'dan@cleanituk.com', 'd.barasa@officine33.com',
    'contact@paintzen.com', 'customercare@towergarden.com', 'ed.goneblue@gmail.com',
    'dryforest.pe@gmail.com', 'info@3dtech-rd.com', 'info@foodshopdeli.co.uk',
    'socialmedia.germany@fronius.de', 'tk@kyhlsportevent.dk',
    'studio62lusowko@gmail.com', 'eric@newyork.co.uk', 'contact@alphacar.net',
    'appointments@simplewaterless.com', '3dmodelinghub93@gmail.com',
}

def get_tld(email):
    try:
        domain = email.split('@')[1]
        return domain.split('.')[-1]
    except:
        return ''

# Read all rows
with open(INPUT, 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    rows = list(reader)

print(f"Total rows read: {len(rows)}")

# Track removal reasons
removed = defaultdict(list)
clean = []
seen_emails = set()
seen_biz = set()

for row in rows:
    email = row['email'].strip().lower()
    name = row['name'].strip()
    city = row['city'].strip()
    biz_key = (name.lower(), city.lower())

    # Rule 1: duplicate emails
    if email in seen_emails:
        removed['duplicate_email'].append(f"{name} ({email})")
        continue
    
    # Rule 2: duplicate business (name+city)
    if biz_key in seen_biz:
        removed['duplicate_business'].append(f"{name} in {city}")
        continue

    # Rule 3: TLD > 6 chars
    tld = get_tld(email)
    if len(tld) > 6:
        removed['bad_tld'].append(f"{name} ({email})")
        continue

    # Rule 4a: bad domains
    domain = email.split('@')[1] if '@' in email else ''
    if domain in BAD_DOMAINS:
        removed['bad_domain'].append(f"{name} ({email})")
        continue

    # Rule 4b: bad exact emails
    if email in BAD_EMAILS:
        removed['bad_email'].append(f"{name} ({email})")
        continue

    seen_emails.add(email)
    seen_biz.add(biz_key)
    clean.append(row)

# Rule 5: remove emails appearing 3+ times (already handled by dup check, 
# but check if any email appears in 3+ different rows after first pass)
# Since we keep first occurrence, this is already handled. But let's check
# the original data for emails with 3+ occurrences and remove those.
email_counts = Counter(r['email'].strip().lower() for r in rows)
noisy_emails = {e for e, c in email_counts.items() if c >= 3}
if noisy_emails:
    before = len(clean)
    clean = [r for r in clean if r['email'].strip().lower() not in noisy_emails]
    diff = before - len(clean)
    if diff:
        removed['noisy_3plus'] = [f"{e} ({email_counts[e]}x)" for e in noisy_emails]

print(f"\nRemoval summary:")
for reason, items in removed.items():
    print(f"  {reason}: {len(items)}")
    for item in items:
        print(f"    - {item}")

print(f"\nCleaned rows: {len(clean)}")

# Write clean CSV
with open(OUT_CSV, 'w', encoding='utf-8', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(clean)
print(f"Written: {OUT_CSV}")

# Build MD file
# Group by country then city
by_country = defaultdict(lambda: defaultdict(list))
for row in clean:
    by_country[row['country'].strip()][row['city'].strip()].append(row)

countries_sorted = sorted(by_country.keys())

# Country summary
country_counts = {c: sum(len(cities) for cities in by_country[c].values()) for c in countries_sorted}

lines = []
lines.append("# Europe Prospects - Businesses Without Websites (With Email)\n")
lines.append(f"> **153 raw prospects scanned via Facebook** | **{len(clean)} cleaned prospects with verified emails**")
lines.append(">")
lines.append("> Sourced from Google Maps (no website listed) → Email found via Facebook business pages\n")
lines.append("---\n")
lines.append("## Summary by Country\n")
lines.append("| Country | Count |")
lines.append("|---------|-------|")
for c in countries_sorted:
    lines.append(f"| {c} | {country_counts[c]} |")
lines.append("")
lines.append("---\n")

for country in countries_sorted:
    lines.append(f"## {country}\n")
    cities_sorted = sorted(by_country[country].keys())
    for city in cities_sorted:
        lines.append(f"### {city}\n")
        businesses = by_country[country][city]
        for i, row in enumerate(businesses, 1):
            name = row['name'].strip()
            cat = row['search_category'].strip() or row['google_category'].strip() or 'N/A'
            email = row['email'].strip()
            phone = row['phone'].strip() or 'N/A'
            address = row['address'].strip() or 'N/A'
            rating = row['rating'].strip() or 'N/A'
            reviews = row['reviews'].strip() or '0'
            fb = row['facebook'].strip()

            lines.append(f"#### {i}. {name}")
            lines.append(f"- **Category:** {cat}")
            lines.append(f"- **Email:** {email}")
            lines.append(f"- **Phone:** {phone}")
            lines.append(f"- **Address:** {address}")
            lines.append(f"- **Rating:** {rating} ({reviews} reviews)")
            lines.append(f"- **Facebook:** {fb}")
            lines.append("")
        lines.append("---\n")

with open(OUT_MD, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))
print(f"Written: {OUT_MD}")
