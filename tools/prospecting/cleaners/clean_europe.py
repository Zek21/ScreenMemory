"""
Clean Europe email results and generate final MD file.
Removes false positives, deduplicates, and creates europe_prospects_with_email.md
"""

import csv
import os
import re
from collections import Counter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_CSV = os.path.join(SCRIPT_DIR, "results", "europe", "_intermediate", "europe_prospects_with_email.csv")
OUTPUT_CSV = os.path.join(SCRIPT_DIR, "results", "europe", "_intermediate", "europe_prospects_clean.csv")
OUTPUT_MD = os.path.join(SCRIPT_DIR, "results", "europe", "europe_prospects_with_email.md")

# Government/institutional domains to exclude
GOV_DOMAINS = {'gov.uk', 'gov.ie', 'gov.pl', 'gov.ro', 'gov.pt', 'gov.es',
                'gov.it', 'gov.de', 'gov.at', 'gov.se', 'gov.dk', 'gov.cz',
                'gov.hu', 'gov.nl', 'gov.be', 'gov.ch', 'europa.eu'}

# Known noise domains (banks, large corps, irrelevant)
NOISE_DOMAINS = {'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
                 'googlemail.com', 'aol.com', 'icloud.com', 'me.com',
                 'protonmail.com', 'mail.com', 'live.com'}

EXCLUDE_PATTERNS = [
    r'.*@.*\.gov\.',  # any government
    r'.*@.*\.edu',    # educational
    r'.*@.*\.mil',    # military
    r'wixpress\.com', # Wix platform
    r'squarespace\.com',
    r'wordpress\.com',
    r'shopify\.com',
    r'sentry\.io',
    r'cloudflare\.com',
    r'.*\.png$',      # image filenames matching email regex
    r'.*\.jpg$',
    r'.*\.svg$',
    r'.*\.gif$',
    r'u003e.*',       # JSON unicode escaped prefix
]

# Known large chain / irrelevant emails
NOISE_EXACT = {
    'customerservices@imocarwash.com',
    'customerservice.uk@cac.mercedes-benz.com',
    'writeus@ph.mcd.com',
    'jlai@mistercarwash.com',
    'wash@baywash.com.na',
    'orders@baywash.com.na',
    'news-carwashpro@promedia.nl',
}

def is_valid_email(email):
    """Check if email looks like a real business email."""
    email = email.strip().lower()
    
    if not re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,6}$', email):
        return False
    
    domain = email.split('@')[1]
    
    # Exclude government domains
    if domain in GOV_DOMAINS:
        return False
    
    # Exclude personal email providers (we want business emails)
    if domain in NOISE_DOMAINS:
        return False
    
    # Exclude pattern matches
    for pattern in EXCLUDE_PATTERNS:
        if re.match(pattern, email):
            return False
    
    # Exclude known noise emails
    if email in NOISE_EXACT:
        return False
    
    # Exclude malformed (text appended to email)
    if len(domain.split('.')[-1]) > 6:
        return False
    
    return True

def clean_and_generate():
    # Load raw results
    businesses = []
    with open(INPUT_CSV, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            businesses.append(row)
    
    print(f"Loaded {len(businesses)} raw results")
    
    # Count email occurrences (emails appearing too often are noise)
    email_counts = Counter()
    for biz in businesses:
        emails = biz.get('email', '').split('; ')
        for e in emails:
            if e.strip():
                email_counts[e.strip().lower()] += 1
    
    # Emails appearing 3+ times across different businesses are noise
    noise_emails = {e for e, c in email_counts.items() if c >= 3}
    if noise_emails:
        print(f"Noise emails (3+ occurrences): {noise_emails}")
    
    # Clean
    cleaned = []
    seen_names = set()
    
    for biz in businesses:
        raw_emails = biz.get('email', '').split('; ')
        valid_emails = []
        
        for e in raw_emails:
            e = e.strip().lower()
            if e and is_valid_email(e) and e not in noise_emails:
                valid_emails.append(e)
        
        if valid_emails:
            # Deduplicate by business name + city
            key = f"{biz['name']}|{biz['city']}"
            if key not in seen_names:
                seen_names.add(key)
                biz['email'] = '; '.join(valid_emails)
                cleaned.append(biz)
    
    print(f"After cleaning: {len(cleaned)} businesses with valid emails")
    
    # Save cleaned CSV
    if cleaned:
        fieldnames = list(cleaned[0].keys())
        with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(cleaned)
    
    # Generate MD file
    generate_md(cleaned)
    
    return cleaned

def generate_md(businesses):
    """Generate a well-formatted Markdown file grouped by city."""
    # Group by city
    by_city = {}
    city_country = {}
    for biz in businesses:
        city = biz.get('city', 'Unknown')
        country = biz.get('country', '')
        if city not in by_city:
            by_city[city] = []
        by_city[city].append(biz)
        city_country[city] = country
    
    # Sort cities by country then city name
    sorted_cities = sorted(by_city.keys(), key=lambda c: (city_country.get(c, ''), c))
    
    lines = []
    lines.append("# 🇪🇺 European Business Prospects (Without Websites)")
    lines.append("")
    lines.append(f"**Total prospects with verified email: {len(businesses)}**")
    lines.append("")
    lines.append("These businesses were found on Google Maps WITHOUT a website.")
    lines.append("Each entry has a verified email address for outreach.")
    lines.append("")
    
    # Summary table
    lines.append("## Summary by City")
    lines.append("")
    lines.append("| City | Country | Count |")
    lines.append("|------|---------|-------|")
    for city in sorted_cities:
        country = city_country.get(city, '')
        lines.append(f"| {city} | {country} | {len(by_city[city])} |")
    lines.append(f"| **Total** | | **{len(businesses)}** |")
    lines.append("")
    
    # Details by city
    for city in sorted_cities:
        city_biz = by_city[city]
        country = city_country.get(city, '')
        lines.append(f"## {city}, {country} ({len(city_biz)} prospects)")
        lines.append("")
        lines.append("| # | Business | Category | Email | Phone | Rating |")
        lines.append("|---|----------|----------|-------|-------|--------|")
        for j, biz in enumerate(city_biz, 1):
            name = biz.get('name', 'N/A')
            category = biz.get('search_category', biz.get('category', biz.get('google_category', 'N/A')))
            email = biz.get('email', 'N/A')
            phone = biz.get('phone', 'N/A')
            rating = biz.get('rating', 'N/A')
            # Escape pipes in data
            name = name.replace('|', '\\|')
            lines.append(f"| {j} | {name} | {category} | {email} | {phone} | {rating} |")
        lines.append("")
    
    with open(OUTPUT_MD, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"Generated: {OUTPUT_MD}")

if __name__ == '__main__':
    clean_and_generate()
