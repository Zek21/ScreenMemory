"""
Austria Business Finder - Searches Google Maps for businesses WITHOUT websites
across Austrian cities. Uses list-level pre-filtering for speed.
"""

import asyncio
import json
import os
import re
import sys
import csv
from datetime import datetime
from urllib.parse import quote_plus
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CATEGORIES_FILE = os.path.join(SCRIPT_DIR, "categories_europe.txt")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "results", "austria", "_intermediate")

CITIES = [
    "Vienna, Austria",
    "Graz, Austria",
    "Linz, Austria",
    "Salzburg, Austria",
    "Innsbruck, Austria",
    "Klagenfurt, Austria",
    "Villach, Austria",
    "Wels, Austria",
    "St. Pölten, Austria",
    "Dornbirn, Austria",
]

CITY_COUNTRY = {}
for c in CITIES:
    parts = c.split(", ")
    CITY_COUNTRY[parts[0]] = parts[1] if len(parts) > 1 else ""

CSV_FILE = os.path.join(OUTPUT_DIR, "austria_businesses_no_website.csv")
PROGRESS_FILE = os.path.join(OUTPUT_DIR, "progress.json")

CSV_HEADERS = [
    "name", "search_category", "google_category", "city", "country",
    "address", "phone", "rating", "reviews", "has_website", "found_date"
]

def load_categories():
    cats = []
    with open(CATEGORIES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cats.append(line)
    return cats

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            return json.load(f)
    return {"completed": [], "total_found": 0}

def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)

def append_to_csv(businesses):
    file_exists = os.path.exists(CSV_FILE) and os.path.getsize(CSV_FILE) > 0
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if not file_exists:
            writer.writeheader()
        for b in businesses:
            writer.writerow(b)

async def check_feed_for_no_website(page, timeout=3000):
    """Quick check: does this result item mention a website in the feed?"""
    try:
        feed = page.locator('div[role="feed"]')
        await feed.wait_for(state="attached", timeout=timeout)
        items = feed.locator('a[href*="/maps/place/"]')
        count = await items.count()
        return count
    except Exception:  # signed: beta
        return 0

async def has_website_in_detail(page):
    """Check if the detail panel shows a website link."""
    try:
        auth = page.locator('a[data-item-id="authority"]')
        if await auth.count() > 0:
            return True
        auth_btn = page.locator('button[data-item-id="authority"]')
        if await auth_btn.count() > 0:
            return True
    except Exception:  # signed: beta
        pass
    return False

async def _extract_name(page):
    """Extract business name from the detail panel h1 elements."""
    try:
        h1s = page.locator("h1")
        count = await h1s.count()
        for i in range(count):
            el = h1s.nth(i)
            box = await el.bounding_box()
            if box and box["x"] > 400:
                return await el.inner_text()
    except Exception:  # signed: beta
        pass
    return None


async def _extract_detail_fields(page, info):
    """Populate category, address, phone, rating, and reviews into info dict."""
    try:
        cat_btn = page.locator('button[jsaction*="category"]')
        if await cat_btn.count() > 0:
            info["google_category"] = await cat_btn.first.inner_text()
    except Exception:  # signed: beta
        pass
    try:
        addr_btn = page.locator('button[data-item-id="address"]')
        if await addr_btn.count() > 0:
            info["address"] = await addr_btn.first.get_attribute("aria-label") or ""
            info["address"] = info["address"].replace("Address: ", "")
    except Exception:  # signed: beta
        pass
    try:
        phone_btn = page.locator('button[data-item-id^="phone"]')
        if await phone_btn.count() > 0:
            info["phone"] = await phone_btn.first.get_attribute("aria-label") or ""
            info["phone"] = info["phone"].replace("Phone: ", "")
    except Exception:  # signed: beta
        pass
    try:
        rating_el = page.locator('div.F7nice span[aria-hidden="true"]')
        if await rating_el.count() > 0:
            info["rating"] = await rating_el.first.inner_text()
        reviews_el = page.locator('div.F7nice span[aria-label*="review"]')
        if await reviews_el.count() > 0:
            label = await reviews_el.first.get_attribute("aria-label") or ""
            nums = re.findall(r"[\d,]+", label)
            if nums:
                info["reviews"] = nums[0]
    except Exception:  # signed: beta
        pass


async def extract_business_info(page, category, city):
    """Extract business details from the detail panel."""
    name = await _extract_name(page)
    if not name:
        return None
    info = {
        "name": name,
        "search_category": category,
        "city": city,
        "country": CITY_COUNTRY.get(city, "Austria"),
        "has_website": "No",
        "found_date": datetime.now().strftime("%Y-%m-%d"),
    }
    await _extract_detail_fields(page, info)
    return info

async def _scroll_feed(page):
    """Scroll the results feed to load more items."""
    for _ in range(5):
        await page.evaluate('''
            const feed = document.querySelector('div[role="feed"]');
            if (feed) feed.scrollTop = feed.scrollHeight;
        ''')
        await asyncio.sleep(1)


async def _item_has_website(parent):
    """Check if a feed item has a website indicator."""
    try:
        web_links = parent.locator('a[aria-label*="Visit"]')
        if await web_links.count() > 0:
            return True
    except Exception:  # signed: beta
        pass
    parent_text= await parent.inner_text() if await parent.count() > 0 else ""
    return "Website" in parent_text


async def _process_feed_item(link, page, category, city):
    """Process a single feed item: check for website, extract info if none."""
    parent = link.locator("xpath=ancestor::div[contains(@class, 'Nv2PK')]")
    if await parent.count() == 0:
        parent = link.locator("xpath=ancestor::div[3]")

    if await _item_has_website(parent):
        return None

    await link.click()
    await asyncio.sleep(1.5)

    if await has_website_in_detail(page):
        return None

    info = await extract_business_info(page, category, city)
    if info and info.get("name"):
        print(f"    ✅ {info['name']}")
        return info
    return None


async def search_category_city(page, category, city, progress):
    """Search one category in one city."""
    key = f"{category}|{city}"
    if key in progress["completed"]:
        return []

    query = f"{category} in {city}"
    url = f"https://www.google.com/maps/search/{quote_plus(query)}"

    businesses = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)

        feed = page.locator('div[role="feed"]')
        try:
            await feed.wait_for(state="attached", timeout=5000)
        except Exception:  # signed: beta
            progress["completed"].append(key)
            save_progress(progress)
            return []

        await _scroll_feed(page)
        links = feed.locator('a[href*="/maps/place/"]')
        link_count = await links.count()

        for i in range(min(link_count, 40)):
            try:
                info = await _process_feed_item(links.nth(i), page, category, city)
                if info:
                    businesses.append(info)
            except Exception:
                continue

    except Exception as e:
        print(f"  ⚠️ Error: {str(e)[:60]}")

    progress["completed"].append(key)
    progress["total_found"] += len(businesses)
    save_progress(progress)

    if businesses:
        append_to_csv(businesses)

    return businesses

async def _launch_browser(p):
    """Launch a headless Chromium browser and return (browser, page)."""
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page = await context.new_page()
    return browser, page


async def _accept_cookies(page):
    """Navigate to Google Maps and accept cookies if prompted."""
    try:
        await page.goto("https://www.google.com/maps", wait_until="domcontentloaded", timeout=10000)
        await asyncio.sleep(2)
        accept_btn = page.locator('button:has-text("Accept all")')
        if await accept_btn.count() > 0:
            await accept_btn.first.click()
            await asyncio.sleep(1)
    except Exception:  # signed: beta
        pass


async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    categories = load_categories()
    progress = load_progress()

    total_combos = len(categories) * len(CITIES)
    done_combos = len(progress["completed"])
    print(f"Austria Business Finder")
    print(f"Categories: {len(categories)}, Cities: {len(CITIES)}")
    print(f"Total combinations: {total_combos}, Already done: {done_combos}")
    print(f"Previously found: {progress['total_found']} businesses")
    print("=" * 60)

    async with async_playwright() as p:
        browser, page = await _launch_browser(p)
        await _accept_cookies(page)

        combo_num = done_combos
        for cat_idx, category in enumerate(categories):
            for city_name in [c.split(", ")[0] for c in CITIES]:
                key = f"{category}|{city_name}"
                if key in progress["completed"]:
                    continue

                combo_num += 1
                print(f"\n[{combo_num}/{total_combos}] {category} in {city_name}")

                found = await search_category_city(page, category, city_name, progress)
                if found:
                    print(f"  → Found {len(found)} businesses")
                else:
                    print(f"  → 0 results")

                await asyncio.sleep(0.5)

        await browser.close()

    print(f"\n{'=' * 60}")
    print(f"COMPLETE! Total businesses found: {progress['total_found']}")
    print(f"Output: {CSV_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
