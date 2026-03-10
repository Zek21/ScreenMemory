"""Minimal smoke test for Chrome Bridge v3.0."""

from bridge import Hub


def main():
    with Hub() as hub:
        try:
            profiles = hub.wait_for_agent(timeout=15)
        except TimeoutError:
            print("No Chrome Bridge agent connected within 15s.")
            print("Load the unpacked extension from chrome-bridge/extension and keep Chrome running.")
            print("For a protocol-only test, run: python test_bridge.py --synthetic")
            return
        chrome = hub.chrome()

        print("Agents:")
        for profile in profiles:
            print(
                f"  {profile['profileId'][:12]}... "
                f"tabs={profile['tabs']} windows={profile['windows']} v={profile['version']}"
            )

        capabilities = chrome.capabilities()
        print(f"Transport: {capabilities['transport']}")
        print(f"Chrome minimum: {capabilities['chromeMinimum']}")

        tabs = chrome.tabs()
        if not tabs:
            print("No tabs are open in the connected browser.")
            return

        active = next((tab for tab in tabs if tab.get("active")), tabs[0])
        print(f"Active tab: [{active['id']}] {active.get('title', '')[:80]}")

        info = chrome.page_info(active["id"])
        print(f"Page title: {info.get('title')}")
        print(f"Page URL: {info.get('url')}")

        ping = chrome.ping()
        print(f"Ping: {ping.get('pong')} at {ping.get('timestamp')}")


if __name__ == "__main__":
    main()
