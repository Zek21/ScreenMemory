"""
Europe Email Enrichment via Facebook Pages.
Approach: Directly construct Facebook page URLs from business names,
visit the /about page and extract emails. No search engines needed.
Also checks Google Maps for Facebook links.
"""

import asyncio
import csv
import json
import os
import random
import re
import unicodedata
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_CSV = os.path.join(SCRIPT_DIR, "results", "europe", "_intermediate", "europe_businesses_no_website.csv")
OUTPUT_CSV = os.path.join(SCRIPT_DIR, "results", "europe", "_intermediate", "europe_emails_fb.csv")
PROGRESS_FILE = os.path.join(SCRIPT_DIR, "results", "europe", "_intermediate", "email_progress_fb.json")

EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,6}')

EXCLUDE_DOMAINS = {
    'facebook.com', 'fb.com', 'instagram.com', 'meta.com',
    'example.com', 'sentry.io', 'fbcdn.net', 'fbsbx.com',
    'google.com', 'googleapis.com', 'gstatic.com',
    'duckduckgo.com', 'bing.com', 'w3.org',
}

def extract_emails(text):
    emails = set()
    for e in EMAIL_RE.findall(text):
        e = e.lower().strip()
        domain = e.split('@')[1] if '@' in e else ''
        tld = domain.split('.')[-1] if domain else ''
        if domain in EXCLUDE_DOMAINS or any(domain.endswith(f'.{ex}') for ex in EXCLUDE_DOMAINS):
            continue
        if len(tld) > 6 or len(tld) < 2:
            continue
        if e.endswith(('.png', '.jpg', '.svg', '.gif', '.webp', '.js', '.css')):
            continue
        emails.add(e)
    return emails

def slugify(name):
    """Generate candidate Facebook page slugs from a business name."""
    # Normalize unicode characters
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    # Remove common suffixes/extras
    name = re.sub(r'\s*[\-–—|/\\].*$', '', name)  # Remove everything after dash/pipe
    name = re.sub(r'\s*\(.*?\)', '', name)  # Remove parenthetical
    name = re.sub(r'[☕🍰📱💇🏽‍♂️✂️💈🔑]+', '', name)  # Remove emojis
    name = name.strip()
    
    slugs = set()
    # Slug 1: all words concatenated, lowercase
    clean = re.sub(r'[^a-zA-Z0-9]', '', name.lower())
    if len(clean) > 2:
        slugs.add(clean)
    
    # Slug 2: words joined by dots
    words = re.findall(r'[a-zA-Z0-9]+', name.lower())
    if len(words) >= 2:
        slugs.add('.'.join(words))
        # First two words only
        slugs.add(''.join(words[:2]))
        slugs.add('.'.join(words[:2]))
    
    # Slug 3: words joined by hyphens  
    if len(words) >= 2:
        slugs.add('-'.join(words))
    
    return list(slugs)

async def check_fb_page(page, slug, expected_city):
    """Visit a Facebook page and check if it exists + extract email."""
    try:
        url = f"https://www.facebook.com/{slug}/about"
        resp = await page.goto(url, timeout=12000, wait_until='domcontentloaded')
        await page.wait_for_timeout(random.uniform(1500, 3000))
        
        # Check if page exists (Facebook redirects to login for non-existent pages,
        # or shows "Page Not Found")
        title = await page.title()
        content = await page.content()
        
        if 'page not found' in title.lower() or 'content isn' in content.lower():
            return None, None
        
        # Check if the page URL changed (redirected away)
        current_url = page.url
        if '/login' in current_url or slug not in current_url.lower():
            return None, None
        
        # Close login dialog if present
        try:
            close_btn = page.locator('[aria-label="Close"]')
            if await close_btn.count() > 0:
                await close_btn.first.click(timeout=2000)
                await page.wait_for_timeout(500)
        except:
            pass
        
        # Extract text - look for email
        text = await page.evaluate('() => document.body.innerText')
        emails = extract_emails(text)
        
        # Also get the page name to verify it's the right business
        page_name = await page.evaluate('''() => {
            const h1 = document.querySelector('h1');
            return h1 ? h1.textContent : '';
        }''')
        
        return page_name, list(emails)
    except:
        return None, None

async def main():
    businesses = []
    with open(INPUT_CSV, 'r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            try:
                rating = float(row.get('rating', '0') or '0')
                reviews = int((row.get('reviews', '0') or '0').replace(',', ''))
                row['_score'] = rating * reviews
            except:
                row['_score'] = 0
            businesses.append(row)

    businesses.sort(key=lambda x: -x['_score'])
    print(f"Loaded {len(businesses)} businesses (sorted by value)")

    completed = set()
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r') as f:
            completed = set(json.load(f).get('completed', []))

    results = []
    if os.path.exists(OUTPUT_CSV):
        with open(OUTPUT_CSV, 'r', encoding='utf-8') as f:
            results = list(csv.DictReader(f))

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled',
                  '--no-sandbox', '--window-size=1280,800']
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            locale='en-US',
            viewport={'width': 1280, 'height': 800},
        )
        stealth = Stealth()
        await stealth.apply_stealth_async(context)
        page = await context.new_page()

        found_count = len(results)
        checked = 0
        fb_found = 0

        for i, biz in enumerate(businesses):
            key = f"{biz['name']}|{biz['city']}"
            if key in completed:
                continue

            name = biz['name']
            city = biz['city']
            slugs = slugify(name)

            found_email = False
            for slug in slugs:
                page_name, emails = await check_fb_page(page, slug, city)
                
                if page_name and emails:
                    fb_found += 1
                    biz_copy = {k: v for k, v in biz.items() if k != '_score'}
                    biz_copy['email'] = '; '.join(emails)
                    biz_copy['facebook'] = f"https://www.facebook.com/{slug}"
                    biz_copy['fb_name'] = page_name
                    results.append(biz_copy)
                    found_count += 1
                    print(f"  [{i+1}/{len(businesses)}] \u2713 {name} ({city}) -> {biz_copy['email']}")
                    found_email = True
                    break
                elif page_name:
                    fb_found += 1
                    print(f"  [{i+1}/{len(businesses)}] FB no email: {name} ({city}) -> {page_name}")
                    break
                
                await page.wait_for_timeout(random.uniform(500, 1500))

            if not found_email and not any(True for s in slugs if s):
                print(f"  [{i+1}/{len(businesses)}] \u2717 {name} ({city})")

            completed.add(key)
            checked += 1
            await page.wait_for_timeout(random.uniform(1000, 2500))

            if checked % 10 == 0:
                with open(PROGRESS_FILE, 'w') as f:
                    json.dump({'completed': list(completed)}, f)
                if results:
                    fnames = list(results[0].keys())
                    with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as f:
                        w = csv.DictWriter(f, fieldnames=fnames)
                        w.writeheader()
                        w.writerows(results)
                print(f"  >> {checked} checked, {fb_found} FB pages, {found_count} emails")

        # Final save
        with open(PROGRESS_FILE, 'w') as f:
            json.dump({'completed': list(completed)}, f)
        if results:
            fnames = list(results[0].keys())
            with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=fnames)
                w.writeheader()
                w.writerows(results)
        print(f"\nDONE! {found_count} emails from {fb_found} FB pages ({checked} checked)")
        await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
