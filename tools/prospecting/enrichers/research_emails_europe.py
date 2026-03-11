"""
Europe Email Enrichment - Searches Bing/DuckDuckGo/Google for business emails.
Reads from europe_businesses_no_website.csv, outputs europe_prospects_with_email.csv
Uses Playwright browser to avoid CAPTCHA blocks.
"""

import asyncio
import csv
import json
import os
import re
import sys
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_CSV = os.path.join(SCRIPT_DIR, "results", "europe", "_intermediate", "europe_businesses_no_website.csv")
OUTPUT_CSV = os.path.join(SCRIPT_DIR, "results", "europe", "_intermediate", "europe_prospects_with_email.csv")
PROGRESS_FILE = os.path.join(SCRIPT_DIR, "results", "europe", "_intermediate", "email_progress.json")

EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,6}')

# Emails to exclude (noise from search results)
EXCLUDE_DOMAINS = {
    'example.com', 'sentry.io', 'email.com', 'domain.com',
    'yourcompany.com', 'company.com', 'test.com',
    'duckduckgo.com', 'bing.com', 'google.com', 'microsoft.com',
    'w3.org', 'schema.org', 'googleapis.com', 'gstatic.com',
    'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
    'googlemail.com', 'live.com', 'aol.com', 'icloud.com',
    'protonmail.com', 'yandex.com', 'mail.com',
}
EXCLUDE_EMAILS = {
    'NO@EMAIL.ADDRES', 'name@domain.com', 'info@example.com',
}

async def search_bing(page, query):
    """Search Bing and extract emails from results."""
    emails = set()
    try:
        url = f"https://www.bing.com/search?q={query}"
        await page.goto(url, timeout=15000, wait_until='domcontentloaded')
        await page.wait_for_timeout(2000)
        
        content = await page.content()
        if 'captcha' in content.lower() or 'are you a robot' in content.lower():
            return emails, 'captcha'
        
        found = EMAIL_RE.findall(content)
        for e in found:
            e_lower = e.lower()
            domain = e_lower.split('@')[1] if '@' in e_lower else ''
            if domain not in EXCLUDE_DOMAINS and e not in EXCLUDE_EMAILS:
                emails.add(e_lower)
        return emails, 'ok'
    except Exception:
        return emails, 'error'

async def search_ddg(page, query):
    """Search DuckDuckGo and extract emails from results."""
    emails = set()
    try:
        url = f"https://duckduckgo.com/?q={query}"
        await page.goto(url, timeout=15000, wait_until='domcontentloaded')
        await page.wait_for_timeout(3000)
        
        content = await page.content()
        found = EMAIL_RE.findall(content)
        for e in found:
            e_lower = e.lower()
            domain = e_lower.split('@')[1] if '@' in e_lower else ''
            if domain not in EXCLUDE_DOMAINS and e not in EXCLUDE_EMAILS:
                emails.add(e_lower)
        return emails, 'ok'
    except Exception:
        return emails, 'error'

async def search_google(page, query):
    """Search Google and extract emails from results."""
    emails = set()
    try:
        url = f"https://www.google.com/search?q={query}"
        await page.goto(url, timeout=15000, wait_until='domcontentloaded')
        await page.wait_for_timeout(2000)
        
        content = await page.content()
        if 'captcha' in content.lower() or 'unusual traffic' in content.lower():
            return emails, 'captcha'
        
        found = EMAIL_RE.findall(content)
        for e in found:
            e_lower = e.lower()
            domain = e_lower.split('@')[1] if '@' in e_lower else ''
            if domain not in EXCLUDE_DOMAINS and e not in EXCLUDE_EMAILS:
                emails.add(e_lower)
        return emails, 'ok'
    except Exception:
        return emails, 'error'

async def find_email(page, business_name, city, category):
    """Try multiple search engines to find email for a business."""
    queries = [
        f'"{business_name}" {city} email',
        f'"{business_name}" {city} contact email address',
    ]
    
    all_emails = set()
    bing_blocked = False
    google_blocked = False
    
    for query in queries:
        if not bing_blocked:
            emails, status = await search_bing(page, query)
            if status == 'captcha':
                bing_blocked = True
            all_emails.update(emails)
            if all_emails:
                break
            await page.wait_for_timeout(1000)
        
        emails, status = await search_ddg(page, query)
        all_emails.update(emails)
        if all_emails:
            break
        await page.wait_for_timeout(1000)
        
        if not google_blocked:
            emails, status = await search_google(page, query)
            if status == 'captcha':
                google_blocked = True
            all_emails.update(emails)
            if all_emails:
                break
            await page.wait_for_timeout(1000)
    
    return list(all_emails)

def _load_businesses():
    """Load businesses from input CSV."""
    businesses = []
    with open(INPUT_CSV, 'r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            businesses.append(row)
    print(f"Loaded {len(businesses)} businesses from CSV")
    return businesses


def _load_resume_state():
    """Load completed set and existing results for resume support."""
    completed = set()
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r') as f:
            completed = set(json.load(f).get('completed', []))
        print(f"Resuming - {len(completed)} already processed")
    results = []
    if os.path.exists(OUTPUT_CSV):
        with open(OUTPUT_CSV, 'r', encoding='utf-8') as f:
            results = list(csv.DictReader(f))
    return completed, results


def _compute_fieldnames(businesses):
    """Determine CSV fieldnames, ensuring 'email' is included."""
    fieldnames = list(businesses[0].keys())
    if 'email' not in fieldnames:
        fieldnames.append('email')
    return fieldnames


def _save_checkpoint(completed, results, fieldnames):
    """Save progress and results to disk."""
    if results:
        with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(results)
    with open(PROGRESS_FILE, 'w') as f:
        json.dump({'completed': list(completed)}, f)


async def main():
    businesses = _load_businesses()
    completed, results = _load_resume_state()
    fieldnames = _compute_fieldnames(businesses)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        page = await context.new_page()

        found_count = len([r for r in results if r.get('email')])
        total = len(businesses)

        for i, biz in enumerate(businesses):
            key = f"{biz['name']}|{biz['city']}"
            if key in completed:
                continue

            emails = await find_email(page, biz['name'], biz['city'], biz.get('category', biz.get('search_category', '')))

            if emails:
                biz['email'] = '; '.join(emails[:3])
                results.append(biz)
                found_count += 1
                print(f"  [{i+1}/{total}] \u2713 {biz['name']} ({biz['city']}) -> {biz['email']}")
            else:
                print(f"  [{i+1}/{total}] \u2717 {biz['name']} ({biz['city']})")

            completed.add(key)

            if len(completed) % 10 == 0:
                _save_checkpoint(completed, results, fieldnames)
                print(f"  >> Progress: {len(completed)}/{total} checked, {found_count} emails found")

            await page.wait_for_timeout(500)

        _save_checkpoint(completed, results, fieldnames)
        await browser.close()

    print(f"\n{'='*60}")
    print(f"  COMPLETE: {found_count} businesses with emails out of {total}")
    print(f"  Output: {OUTPUT_CSV}")
    print(f"{'='*60}")

if __name__ == '__main__':
    asyncio.run(main())
