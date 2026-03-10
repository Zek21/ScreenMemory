"""Prove Chrome Bridge CRX packaging and Chrome install behavior."""

from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
SOURCE_DIR = ROOT / "extension"
SOURCE_MANIFEST_PATH = SOURCE_DIR / "manifest.json"
DIST_DIR = ROOT / "dist"
PROOF_DIR = DIST_DIR / "proof"
ARTIFACT_PROOF_PATH = PROOF_DIR / "artifact-proof.json"
INSTALL_PROOF_PATH = PROOF_DIR / "install-proof.json"
INSTALL_SCREENSHOT_PATH = PROOF_DIR / "install-proof.png"
INSTALL_HTML_PATH = PROOF_DIR / "install-page.html"
PROFILE_DIR = PROOF_DIR / "chrome-profile"
DEFAULT_DEBUG_PORT = 9229
DEFAULT_DEVTOOLS_TIMEOUT = 20.0
CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
]
BLOCKED_PHRASES = [
    "apps, extensions, and user scripts cannot be added from this website",
    "extensions, apps, and themes can only be added from the chrome web store",
    "can only be added from the chrome web store",
    "cannot be added from this website",
    "blocked by your administrator",
    "extension install blocked",
    "not from this website",
]

PAGE_INSPECT_JS = r"""
(() => {
  const textChunks = [];
  const seen = new Set();

  function pushText(value) {
    const normalized = String(value || '').replace(/\s+/g, ' ').trim();
    if (normalized) textChunks.push(normalized);
  }

  function walk(node) {
    if (!node || seen.has(node)) return;
    seen.add(node);

    if (node.nodeType === Node.TEXT_NODE) {
      pushText(node.textContent);
      return;
    }

    if (node.nodeType === Node.ELEMENT_NODE && node.shadowRoot) {
      walk(node.shadowRoot);
    }

    const children = node.childNodes ? Array.from(node.childNodes) : [];
    for (const child of children) {
      walk(child);
    }
  }

  walk(document.documentElement);

  const extensionItems = Array.from(document.querySelectorAll('extensions-item')).map((host) => {
    const root = host.shadowRoot;
    const label = root && (root.querySelector('#name') || root.querySelector('.name'));
    const text = label
      ? (label.innerText || label.textContent || '')
      : (root ? (root.innerText || root.textContent || '') : (host.innerText || host.textContent || ''));
    return text.replace(/\s+/g, ' ').trim();
  }).filter(Boolean);

  return {
    title: document.title,
    url: location.href,
    bodyText: Array.from(new Set(textChunks)).join('\n'),
    html: document.documentElement.outerHTML,
    extensionItems,
  };
})()
"""


class ProofError(RuntimeError):
    """Raised when proof setup fails."""


def ensure_websockets():
    try:
        import websockets as _websockets
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
        import websockets as _websockets
    return _websockets


class DevToolsClient:
    def __init__(self, websocket_url: str):
        self.websocket_url = websocket_url
        self._next_id = 0

    async def call(self, ws, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        msg_id = self._next_id
        await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))

        while True:
            raw = await ws.recv()
            message = json.loads(raw)
            if message.get("id") != msg_id:
                continue
            if "error" in message:
                raise ProofError(f"DevTools {method} failed: {message['error']}")
            return message.get("result", {})

    async def capture_extensions_page(self) -> dict[str, Any]:
        websockets = ensure_websockets()
        async with websockets.connect(self.websocket_url, max_size=50 * 1024 * 1024) as ws:
            await self.call(ws, "Page.enable")
            await self.call(ws, "Runtime.enable")
            await self.call(ws, "Page.bringToFront")

            runtime = await self.call(
                ws,
                "Runtime.evaluate",
                {
                    "expression": PAGE_INSPECT_JS,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            if runtime.get("exceptionDetails"):
                raise ProofError(f"Runtime evaluation failed: {runtime['exceptionDetails']}")

            screenshot = await self.call(
                ws,
                "Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": True, "fromSurface": True},
            )
            return {
                "page": runtime["result"]["value"],
                "screenshot_b64": screenshot["data"],
            }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def locate_artifacts() -> tuple[dict[str, Any], Path, Path]:
    source_manifest = load_json(SOURCE_MANIFEST_PATH)
    version = source_manifest["version"]
    crx_path = DIST_DIR / f"chrome-bridge-extension-{version}.crx"
    zip_path = DIST_DIR / f"chrome-bridge-extension-{version}.zip"

    if not crx_path.exists():
        raise ProofError(f"Missing CRX artifact: {crx_path}. Run build_extension.ps1 first.")
    if not zip_path.exists():
        raise ProofError(f"Missing ZIP artifact: {zip_path}. Run build_extension.ps1 first.")

    return source_manifest, crx_path, zip_path


def inspect_artifact() -> dict[str, Any]:
    source_manifest, crx_path, zip_path = locate_artifacts()
    crx_bytes = crx_path.read_bytes()

    if len(crx_bytes) < 12:
        raise ProofError(f"CRX file is too short: {crx_path}")

    magic = crx_bytes[:4].decode("ascii", errors="replace")
    crx_version = struct.unpack("<I", crx_bytes[4:8])[0]
    header_size = struct.unpack("<I", crx_bytes[8:12])[0]
    zip_start = 12 + header_size

    if magic != "Cr24":
        raise ProofError(f"Unexpected CRX magic: {magic}")
    if crx_version != 3:
        raise ProofError(f"Unexpected CRX version: {crx_version}")
    if zip_start >= len(crx_bytes):
        raise ProofError("CRX header points past the end of the file")
    if crx_bytes[zip_start:zip_start + 2] != b"PK":
        raise ProofError("Embedded ZIP payload is missing or corrupt")

    with zipfile.ZipFile(BytesIO(crx_bytes[zip_start:])) as embedded_zip:
        embedded_entries = sorted(embedded_zip.namelist())
        embedded_manifest = json.loads(embedded_zip.read("manifest.json"))

    with zipfile.ZipFile(zip_path) as packaged_zip:
        zip_entries = sorted(packaged_zip.namelist())
        zip_manifest = json.loads(packaged_zip.read("manifest.json"))

    if embedded_manifest["version"] != source_manifest["version"]:
        raise ProofError("Embedded CRX manifest version does not match source manifest")
    if zip_manifest["version"] != source_manifest["version"]:
        raise ProofError("ZIP manifest version does not match source manifest")

    artifact = {
        "generatedAt": utc_now(),
        "sourceManifestVersion": source_manifest["version"],
        "crx": {
            "path": str(crx_path),
            "sizeBytes": len(crx_bytes),
            "sha256": sha256_file(crx_path),
            "magic": magic,
            "version": crx_version,
            "headerSize": header_size,
            "zipStartOffset": zip_start,
            "embeddedZipEntries": embedded_entries,
            "embeddedManifestVersion": embedded_manifest["version"],
        },
        "zip": {
            "path": str(zip_path),
            "sizeBytes": zip_path.stat().st_size,
            "sha256": sha256_file(zip_path),
            "entries": zip_entries,
            "manifestVersion": zip_manifest["version"],
        },
    }
    write_json(ARTIFACT_PROOF_PATH, artifact)
    return artifact


def detect_chrome_path(override: str | None) -> Path:
    if override:
        path = Path(override)
        if path.exists():
            return path
        raise ProofError(f"Chrome binary does not exist: {path}")

    for candidate in CHROME_CANDIDATES:
        if candidate.exists():
            return candidate
    raise ProofError("Chrome binary not found. Pass --chrome-path explicitly.")


def ensure_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise ProofError(f"Remote debugging port {port} is already in use.")


def fetch_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_devtools(port: int, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_error = None

    while time.time() < deadline:
        try:
            return fetch_json(f"http://127.0.0.1:{port}/json/version")
        except (urllib.error.URLError, ConnectionError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.25)

    raise ProofError(f"Could not connect to Chrome DevTools on port {port}: {last_error}")


def list_targets(port: int) -> list[dict[str, Any]]:
    return fetch_json(f"http://127.0.0.1:{port}/json/list")


def wait_for_extensions_target(port: int, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_targets: list[dict[str, Any]] = []
    last_error = None

    while time.time() < deadline:
        try:
            targets = list_targets(port)
            last_targets = targets
        except (urllib.error.URLError, ConnectionError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.25)
            continue
        for target in targets:
            if target.get("type") == "page" and str(target.get("url", "")).startswith("chrome://extensions"):
                return target
        time.sleep(0.25)

    raise ProofError(
        f"chrome://extensions target not found on port {port}. "
        f"Last error: {last_error}. Targets: {last_targets}"
    )


async def navigate_target_to_extensions(websocket_url: str) -> None:
    cdp = DevToolsClient(websocket_url)
    async with ensure_websockets().connect(websocket_url, max_size=50 * 1024 * 1024) as ws:
        await cdp.call(ws, "Page.enable")
        await cdp.call(ws, "Page.bringToFront")
        await cdp.call(ws, "Page.navigate", {"url": "chrome://extensions/"})


def ensure_extensions_target(port: int) -> dict[str, Any]:
    try:
        return wait_for_extensions_target(port, 3.0)
    except ProofError:
        pass

    targets = list_targets(port)
    page_targets = [target for target in targets if target.get("type") == "page"]
    if not page_targets:
        raise ProofError(f"No page targets available to navigate to chrome://extensions/. Targets: {targets}")

    asyncio.run(navigate_target_to_extensions(page_targets[0]["webSocketDebuggerUrl"]))
    time.sleep(1.0)
    return wait_for_extensions_target(port, DEFAULT_DEVTOOLS_TIMEOUT)


def load_installed_extensions(profile_dir: Path) -> list[dict[str, Any]]:
    prefs_path = profile_dir / "Default" / "Preferences"
    if not prefs_path.exists():
        return []

    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    settings = prefs.get("extensions", {}).get("settings", {})
    extensions = []
    for extension_id, data in settings.items():
        manifest = data.get("manifest", {})
        name = manifest.get("name")
        if not name:
            continue
        extensions.append(
            {
                "id": extension_id,
                "name": name,
                "version": manifest.get("version"),
                "state": data.get("state"),
                "location": data.get("location"),
            }
        )
    return sorted(extensions, key=lambda item: (item["name"].lower(), item["id"]))


def collect_matching_snippets(text: str, phrases: list[str]) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matches = []
    for line in lines:
        lowered = line.lower()
        if any(phrase in lowered for phrase in phrases):
            matches.append(line)
    return matches[:20]


def classify_install(page: dict[str, Any], installed_extensions: list[dict[str, Any]]) -> tuple[str, list[str]]:
    installed_names = [item["name"] for item in installed_extensions]
    installed_names_lower = [name.lower() for name in installed_names]
    extension_items = [item for item in page.get("extensionItems", []) if item]
    extension_items_lower = [item.lower() for item in extension_items]
    page_text = page.get("bodyText", "")
    page_text_lower = page_text.lower()

    if "chrome bridge" in installed_names_lower:
        return "accepted", [f"Installed in profile preferences: Chrome Bridge"]
    if any("chrome bridge" in item for item in extension_items_lower):
        return "accepted", extension_items[:10]
    if "chrome bridge" in page_text_lower:
        return "accepted", collect_matching_snippets(page_text, ["chrome bridge"])

    blocked = [phrase for phrase in BLOCKED_PHRASES if phrase in page_text_lower]
    if blocked:
        return "blocked_by_policy", collect_matching_snippets(page_text, blocked) or blocked

    return "unknown", []


def launch_chrome(chrome_path: Path, debug_port: int) -> subprocess.Popen[Any]:
    if PROFILE_DIR.exists():
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    args = [
        str(chrome_path),
        f"--user-data-dir={PROFILE_DIR}",
        f"--remote-debugging-port={debug_port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--new-window",
        "chrome://extensions/",
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_dist_folder() -> None:
    explorer = Path(r"C:\Windows\explorer.exe")
    if explorer.exists():
        subprocess.Popen([str(explorer), str(DIST_DIR)])


def stop_process(process: subprocess.Popen[Any] | None) -> None:
    if not process or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


async def capture_install_state(debug_port: int) -> tuple[dict[str, Any], dict[str, Any]]:
    target = ensure_extensions_target(debug_port)
    cdp = DevToolsClient(target["webSocketDebuggerUrl"])
    capture = await cdp.capture_extensions_page()
    installed_extensions = load_installed_extensions(PROFILE_DIR)
    return capture, {"target": target, "installedExtensions": installed_extensions}


def run_full_proof(artifact: dict[str, Any], chrome_path: Path, debug_port: int) -> dict[str, Any]:
    ensure_port_available(debug_port)
    process = None
    try:
        process = launch_chrome(chrome_path, debug_port)
        version_info = wait_for_devtools(debug_port, DEFAULT_DEVTOOLS_TIMEOUT)
        target = ensure_extensions_target(debug_port)
        open_dist_folder()

        crx_path = Path(artifact["crx"]["path"])
        print(f"Chrome launched: {chrome_path}")
        print(f"Disposable profile: {PROFILE_DIR}")
        print(f"Extensions page target: {target['url']}")
        print(f"Drag this file onto chrome://extensions and complete the install flow:\n{crx_path}")
        print("Press Enter after the drag/install interaction is complete.")
        input()

        capture, extra = asyncio.run(capture_install_state(debug_port))
        page = capture["page"]
        screenshot_bytes = base64.b64decode(capture["screenshot_b64"])
        INSTALL_SCREENSHOT_PATH.write_bytes(screenshot_bytes)
        INSTALL_HTML_PATH.write_text(page.get("html", ""), encoding="utf-8")

        result, snippets = classify_install(page, extra["installedExtensions"])
        install_proof = {
            "generatedAt": utc_now(),
            "artifact": artifact,
            "chrome": {
                "executablePath": str(chrome_path),
                "profilePath": str(PROFILE_DIR),
                "debugPort": debug_port,
                "browserVersion": version_info.get("Browser"),
                "userAgent": version_info.get("User-Agent"),
            },
            "result": result,
            "evidence": {
                "screenshotPath": str(INSTALL_SCREENSHOT_PATH),
                "htmlPath": str(INSTALL_HTML_PATH),
                "detectedTextSnippets": snippets,
                "pageTitle": page.get("title"),
                "pageUrl": page.get("url"),
                "extensionItems": page.get("extensionItems", []),
                "installedExtensions": extra["installedExtensions"],
                "targetId": extra["target"].get("id"),
                "targetUrl": extra["target"].get("url"),
                "capturedAt": utc_now(),
            },
        }
        write_json(INSTALL_PROOF_PATH, install_proof)
        return install_proof
    finally:
        stop_process(process)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-only", action="store_true", help="Only verify and write artifact-proof.json")
    parser.add_argument("--chrome-path", help="Override the Chrome executable path")
    parser.add_argument("--debug-port", type=int, default=DEFAULT_DEBUG_PORT, help=f"Remote debugging port (default: {DEFAULT_DEBUG_PORT})")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        PROOF_DIR.mkdir(parents=True, exist_ok=True)
        artifact = inspect_artifact()
        print(f"Artifact proof written: {ARTIFACT_PROOF_PATH}")

        if args.artifact_only:
            return 0

        chrome_path = detect_chrome_path(args.chrome_path)
        install_proof = run_full_proof(artifact, chrome_path, args.debug_port)
        print(f"Install proof written: {INSTALL_PROOF_PATH}")
        print(f"Classification: {install_proof['result']}")
        return 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except ProofError as exc:
        print(f"Proof failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
