"""
Austria Email Enrichment via Facebook Pages.
Adapted from Europe version for Austria-specific data.
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
INPUT_CSV = os.path.join(SCRIPT_DIR, "results", "austria", "_intermediate", "austria_businesses_deduped.csv")
OUTPUT_CSV = os.path.join(SCRIPT_DIR, "results", "austria", "_intermediate", "austria_emails_fb.csv")
PROGRESS_FILE = os.path.join(SCRIPT_DIR, "results", "austria", "_intermediate", "email_progress_fb.json")

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
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    name = re.sub(r'\s*[\-\u2013\u2014|/\\].*$', '', name)
    name = re.sub(r'\s*\(.*?\)', '', name)
    name = re.sub(r'[^\w\s.]', '', name)
    name = name.strip()
    
    slugs = set()
    clean = re.sub(r'[^a-zA-Z0-9]', '', name.lower())
    if len(clean) > 2:
        slugs.add(clean)
    
    words = re.findall(r'[a-zA-Z0-9]+', name.lower())
    if len(words) >= 2:
        slugs.add('.'.join(words))
        slugs.add(''.join(words[:2]))
        slugs.add('.'.join(words[:2]))
        slugs.add('-'.join(words))
    
    return list(slugs)

# Generic slugs to skip
SKIP_SLUGS = {'photo', 'profile', 'groups', 'events', 'marketplace', 'watch',
              'gaming', 'pages', 'login', 'help', 'about', 'business',
              'search', 'home', 'settings', 'bookmarks', 'memories'}

async def check_fb_page(page, slug, expected_city):
    """Visit a Facebook page and check if it exists + extract email."""
    if slug.lower() in SKIP_SLUGS:
        return None, None
    try:
        url = f"https://www.facebook.com/{slug}/about"
        await page.goto(url, timeout=12000, wait_until='domcontentloaded')
        await page.wait_for_timeout(random.uniform(1500, 3000))
        
        title = await page.title()
        content = await page.content()
        
        if 'page not found' in title.lower() or 'content isn' in content.lower():
            return None, None
        
        current_url = page.url
        if '/login' in current_url or slug not in current_url.lower():
            return None, None
        
        try:
            close_btn = page.locator('[aria-label="Close"]')
            if await close_btn.count() > 0:
                await close_btn.first.click(timeout=2000)
                await page.wait_for_timeout(500)
        except Exception:  # signed: beta
            pass
        
        text = await page.evaluate('() => document.body.innerText')
        emails = extract_emails(text)
        
        page_name = await page.evaluate('''() => {
            const h1 = document.querySelector('h1');
            return h1 ? h1.textContent : '';
        }''')
        
        return page_name, list(emails)
    except Exception:  # signed: beta
        return None, None

def _load_businesses_scored():
    """Load businesses from CSV, compute score, sort by value."""
    businesses = []
    with open(INPUT_CSV, 'r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            try:
                rating = float(row.get('rating', '0') or '0')
                reviews = int((row.get('reviews', '0') or '0').replace(',', ''))
                row['_score'] = rating * reviews
            except (ValueError, TypeError):  # signed: beta
                row['_score'] = 0
            businesses.append(row)
    businesses.sort(key=lambda x: -x['_score'])
    print(f"Loaded {len(businesses)} businesses (sorted by value)")
    return businesses


def _load_resume_state():
    """Load completed set and existing results for resume support."""
    completed = set()
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r') as f:
            completed = set(json.load(f).get('completed', []))
    results = []
    if os.path.exists(OUTPUT_CSV):
        with open(OUTPUT_CSV, 'r', encoding='utf-8') as f:
            results = list(csv.DictReader(f))
    return completed, results


def _save_checkpoint(completed, results):
    """Save progress and results to disk."""
    with open(PROGRESS_FILE, 'w') as f:
        json.dump({'completed': list(completed)}, f)
    if results:
        fnames = list(results[0].keys())
        with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=fnames)
            w.writeheader()
            w.writerows(results)


async def _search_fb_for_business(page, biz, i, total, results, stats):
    """Search Facebook for a single business. Updates results and stats in place."""
    name, city = biz['name'], biz['city']
    for slug in slugify(name):
        page_name, emails = await check_fb_page(page, slug, city)
        if page_name and emails:
            stats['fb_found'] += 1
            biz_copy = {k: v for k, v in biz.items() if k != '_score'}
            biz_copy['email'] = '; '.join(emails)
            biz_copy['facebook'] = f"https://www.facebook.com/{slug}"
            biz_copy['fb_name'] = page_name
            results.append(biz_copy)
            stats['found'] += 1
            print(f"  [{i+1}/{total}] \u2713 {name} ({city}) -> {biz_copy['email']}")
            return True
        elif page_name:
            stats['fb_found'] += 1
            print(f"  [{i+1}/{total}] FB no email: {name} ({city}) -> {page_name}")
            return False
        await page.wait_for_timeout(random.uniform(500, 1500))
    print(f"  [{i+1}/{total}] \u2717 {name} ({city})")
    return False


async def main():
    businesses = _load_businesses_scored()
    completed, results = _load_resume_state()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled',
                  '--no-sandbox', '--window-size=1280,800']
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            locale='de-AT',
            viewport={'width': 1280, 'height': 800},
        )
        stealth = Stealth()
        await stealth.apply_stealth_async(context)
        page = await context.new_page()

        stats = {'found': len(results), 'fb_found': 0, 'checked': 0}

        for i, biz in enumerate(businesses):
            key = f"{biz['name']}|{biz['city']}"
            if key in completed:
                continue

            await _search_fb_for_business(page, biz, i, len(businesses), results, stats)
            completed.add(key)
            stats['checked'] += 1
            await page.wait_for_timeout(random.uniform(1000, 2500))

            if stats['checked'] % 10 == 0:
                _save_checkpoint(completed, results)
                print(f"  >> {stats['checked']} checked, {stats['fb_found']} FB pages, {stats['found']} emails")

        _save_checkpoint(completed, results)
        print(f"\nDONE! {stats['found']} emails from {stats['fb_found']} FB pages ({stats['checked']} checked)")
        await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
