"""
Opens a Playwright Chromium browser for manual login.
The browser stays open until you close it or press Ctrl+C.
"""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--start-maximized"]
        )
        context = await browser.new_context(
            no_viewport=True
        )
        page = await context.new_page()
        await page.goto("https://mail.google.com")
        print("Browser is open. Log in to Gmail to check your draft email.")
        print("Press Ctrl+C or close the browser when done.")
        try:
            while True:
                await asyncio.sleep(1)
                if len(context.pages) == 0:
                    break
        except (KeyboardInterrupt, Exception):
            pass
        await browser.close()

asyncio.run(main())
