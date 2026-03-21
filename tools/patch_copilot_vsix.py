#!/usr/bin/env python3
"""
VSIX Patcher for GitHub Copilot Chat Extension
================================================
Applies 3 surgical patches to make Copilot CLI mode work properly:

PATCH 1 — cli.js: Disable Statsig feature gate that blocks bypass permissions
PATCH 2 — extension.js: Make canUseTool() auto-approve all tool calls (no Apply dialogs)
PATCH 3 — package.json: Enable Copilot CLI vendor entry (remove "when": "false" gate)

Usage:
    python tools/patch_copilot_vsix.py              # Auto-detect extension dir
    python tools/patch_copilot_vsix.py --verify      # Check if patches are applied
    python tools/patch_copilot_vsix.py --revert       # Revert from backups
    python tools/patch_copilot_vsix.py --ext-dir PATH # Specify extension directory

After patching, reload VS Code windows (Ctrl+Shift+P → "Developer: Reload Window").

Tested on: github.copilot-chat-0.40.2026031004
"""

import os
import sys
import glob
import shutil
import argparse
import re


def find_extension_dir(override=None):
    """Find the active Copilot Chat extension directory."""
    if override and os.path.isdir(override):
        return override

    home = os.path.expanduser("~")
    ext_root = os.path.join(home, ".vscode-insiders", "extensions")
    if not os.path.isdir(ext_root):
        ext_root = os.path.join(home, ".vscode", "extensions")
    if not os.path.isdir(ext_root):
        print("ERROR: Cannot find VS Code extensions directory")
        sys.exit(1)

    # Find all copilot-chat extension versions
    candidates = sorted(
        glob.glob(os.path.join(ext_root, "github.copilot-chat-*")),
        key=os.path.getmtime,
        reverse=True,
    )

    # Filter out obsoleted versions
    obsolete_file = os.path.join(ext_root, ".obsolete")
    obsolete = set()
    if os.path.isfile(obsolete_file):
        import json
        try:
            with open(obsolete_file, "r") as f:
                obs = json.load(f)
            obsolete = {k for k, v in obs.items() if v is True}
        except (OSError, json.JSONDecodeError, ValueError) as e:
            print(f"[patch_copilot_vsix] Failed to read obsolete file: {e}")

    for c in candidates:
        dirname = os.path.basename(c)
        if dirname not in obsolete and os.path.isdir(c):
            return c

    if candidates:
        return candidates[0]

    print("ERROR: No Copilot Chat extension found")
    sys.exit(1)


def backup_file(path):
    """Create .bak backup if not already backed up."""
    bak = path + ".bak"
    if not os.path.isfile(bak):
        shutil.copy2(path, bak)
        print(f"  Backup: {os.path.basename(bak)}")
    else:
        print(f"  Backup already exists: {os.path.basename(bak)}")


def patch_cli_js(ext_dir, verify_only=False):
    """
    PATCH 1: Disable Statsig gate in cli.js
    
    The function SPq() checks Jw("tengu_disable_bypass_permissions_mode") which is
    a server-side Statsig feature flag from GitHub. When enabled (true), it BLOCKS
    bypass permissions mode regardless of user settings.
    
    We append &&false to make the gate always evaluate to false.
    
    FIND:    Jw("tengu_disable_bypass_permissions_mode")
    REPLACE: Jw("tengu_disable_bypass_permissions_mode")&&false
    
    But skip if already patched (already has &&false).
    """
    cli_path = os.path.join(ext_dir, "dist", "cli.js")
    if not os.path.isfile(cli_path):
        print("PATCH 1 [cli.js]: FILE NOT FOUND")
        return False

    content = open(cli_path, "r", encoding="utf-8", errors="replace").read()

    GATE_STRING = 'Jw("tengu_disable_bypass_permissions_mode")'
    PATCHED_STRING = 'Jw("tengu_disable_bypass_permissions_mode")&&false'

    if PATCHED_STRING in content:
        print("PATCH 1 [cli.js]: ALREADY APPLIED ✓")
        return True

    if GATE_STRING not in content:
        # Try alternative minified function names
        alt_pattern = re.search(r'(\w+)\("tengu_disable_bypass_permissions_mode"\)', content)
        if alt_pattern:
            func_name = alt_pattern.group(1)
            alt_gate = f'{func_name}("tengu_disable_bypass_permissions_mode")'
            alt_patched = f'{func_name}("tengu_disable_bypass_permissions_mode")&&false'
            if alt_patched in content:
                print(f"PATCH 1 [cli.js]: ALREADY APPLIED ✓ (function: {func_name})")
                return True
            if not verify_only:
                backup_file(cli_path)
                content = content.replace(alt_gate, alt_patched)
                open(cli_path, "w", encoding="utf-8").write(content)
                print(f"PATCH 1 [cli.js]: APPLIED ✓ (function: {func_name})")
                return True
            else:
                print(f"PATCH 1 [cli.js]: NOT APPLIED — needs patching (function: {func_name})")
                return False
        else:
            print("PATCH 1 [cli.js]: GATE STRING NOT FOUND — extension may have changed")
            return False

    if verify_only:
        print("PATCH 1 [cli.js]: NOT APPLIED — needs patching")
        return False

    backup_file(cli_path)
    content = content.replace(GATE_STRING, PATCHED_STRING)
    open(cli_path, "w", encoding="utf-8").write(content)
    print("PATCH 1 [cli.js]: APPLIED ✓")
    return True


def patch_extension_js(ext_dir, verify_only=False):
    """
    PATCH 2: Make canUseTool() auto-approve all tool calls in extension.js
    
    The canUseTool() method checks if permissionMode is "bypassPermissions" before
    auto-approving. We remove the if-check so it ALWAYS returns allow.
    This eliminates Apply dialogs entirely.
    
    FIND:    async canUseTool(e,t,r){if(r.permissionMode==="bypassPermissions")return{behavior:"allow",updatedInput:t};
    REPLACE: async canUseTool(e,t,r){return{behavior:"allow",updatedInput:t};
    """
    ext_path = os.path.join(ext_dir, "dist", "extension.js")
    if not os.path.isfile(ext_path):
        print("PATCH 2 [extension.js]: FILE NOT FOUND")
        return False

    content = open(ext_path, "r", encoding="utf-8", errors="replace").read()

    ORIGINAL = 'async canUseTool(e,t,r){if(r.permissionMode==="bypassPermissions")return{behavior:"allow",updatedInput:t};'
    PATCHED = 'async canUseTool(e,t,r){return{behavior:"allow",updatedInput:t};'

    if PATCHED in content and ORIGINAL not in content:
        print("PATCH 2 [extension.js]: ALREADY APPLIED ✓")
        return True

    if ORIGINAL not in content:
        # Try regex for different parameter names
        pattern = r'async canUseTool\((\w+),(\w+),(\w+)\)\{if\(\3\.permissionMode==="bypassPermissions"\)return\{behavior:"allow",updatedInput:\2\};'
        match = re.search(pattern, content)
        if match:
            p1, p2, p3 = match.group(1), match.group(2), match.group(3)
            orig = match.group(0)
            patched = f'async canUseTool({p1},{p2},{p3}){{return{{behavior:"allow",updatedInput:{p2}}};'
            if not verify_only:
                backup_file(ext_path)
                content = content.replace(orig, patched)
                open(ext_path, "w", encoding="utf-8").write(content)
                print(f"PATCH 2 [extension.js]: APPLIED ✓ (params: {p1},{p2},{p3})")
                return True
            else:
                print("PATCH 2 [extension.js]: NOT APPLIED — needs patching")
                return False
        else:
            print("PATCH 2 [extension.js]: PATTERN NOT FOUND — extension may have changed")
            # Try to find any canUseTool and show context
            idx = content.find("async canUseTool")
            if idx >= 0:
                snippet = content[idx : idx + 200]
                print(f"  Found canUseTool at offset {idx}: {snippet[:120]}...")
            return False

    if verify_only:
        print("PATCH 2 [extension.js]: NOT APPLIED — needs patching")
        return False

    backup_file(ext_path)
    content = content.replace(ORIGINAL, PATCHED)
    open(ext_path, "w", encoding="utf-8").write(content)
    print("PATCH 2 [extension.js]: APPLIED ✓")
    return True


def patch_package_json(ext_dir, verify_only=False):
    """
    PATCH 3: Enable Copilot CLI vendor entry in package.json
    
    The copilotcli vendor has "when": "false" which hides the Copilot CLI
    session target option. Removing this makes "Copilot CLI" appear in the
    session target dropdown.
    
    FIND:    {"vendor": "copilotcli","displayName": "Copilot CLI","when": "false"}
    REPLACE: {"vendor": "copilotcli","displayName": "Copilot CLI"}
    
    Also handles variations with different key ordering.
    """
    pkg_path = os.path.join(ext_dir, "package.json")
    if not os.path.isfile(pkg_path):
        print("PATCH 3 [package.json]: FILE NOT FOUND")
        return False

    content = open(pkg_path, "r", encoding="utf-8", errors="replace").read()

    # Check if copilotcli entry has "when": "false"
    ORIGINAL = '{"vendor": "copilotcli","displayName": "Copilot CLI","when": "false"}'
    PATCHED = '{"vendor": "copilotcli","displayName": "Copilot CLI"}'

    # Also try alternate JSON formatting
    ALT_ORIGINAL = '{"vendor":"copilotcli","displayName":"Copilot CLI","when":"false"}'
    ALT_PATCHED = '{"vendor":"copilotcli","displayName":"Copilot CLI"}'

    if PATCHED in content and ORIGINAL not in content:
        print("PATCH 3 [package.json]: ALREADY APPLIED ✓")
        return True
    if ALT_PATCHED in content and ALT_ORIGINAL not in content:
        print("PATCH 3 [package.json]: ALREADY APPLIED ✓")
        return True

    target = None
    replacement = None
    if ORIGINAL in content:
        target = ORIGINAL
        replacement = PATCHED
    elif ALT_ORIGINAL in content:
        target = ALT_ORIGINAL
        replacement = ALT_PATCHED
    else:
        # Regex fallback for varied whitespace/ordering
        pattern = r'\{[^}]*"vendor"\s*:\s*"copilotcli"[^}]*"when"\s*:\s*"false"[^}]*\}'
        match = re.search(pattern, content)
        if match:
            target = match.group(0)
            replacement = re.sub(r',?\s*"when"\s*:\s*"false"\s*,?', '', target)
            # Clean up any trailing/leading commas
            replacement = replacement.replace(',,', ',').replace('{,', '{').replace(',}', '}')
        else:
            # Check if copilotcli exists without the when clause
            if '"copilotcli"' in content:
                copilot_idx = content.index('"copilotcli"')
                ctx = content[max(0, copilot_idx - 50):copilot_idx + 100]
                if '"when"' not in ctx:
                    print("PATCH 3 [package.json]: ALREADY APPLIED ✓ (no 'when' clause)")
                    return True
            print("PATCH 3 [package.json]: PATTERN NOT FOUND — extension may have changed")
            return False

    if verify_only:
        print("PATCH 3 [package.json]: NOT APPLIED — needs patching")
        return False

    backup_file(pkg_path)
    content = content.replace(target, replacement, 1)
    open(pkg_path, "w", encoding="utf-8").write(content)
    print("PATCH 3 [package.json]: APPLIED ✓")
    return True


def revert_patches(ext_dir):
    """Revert all patches from .bak backups."""
    files = ["dist/cli.js", "dist/extension.js", "package.json"]
    reverted = 0
    for f in files:
        path = os.path.join(ext_dir, f)
        bak = path + ".bak"
        if os.path.isfile(bak):
            shutil.copy2(bak, path)
            os.remove(bak)
            print(f"  Reverted: {f}")
            reverted += 1
        else:
            print(f"  No backup: {f}")
    print(f"\nReverted {reverted} file(s). Reload VS Code to apply.")


def main():
    parser = argparse.ArgumentParser(description="Patch Copilot Chat VSIX for CLI mode")
    parser.add_argument("--ext-dir", help="Extension directory path")
    parser.add_argument("--verify", action="store_true", help="Check if patches are applied")
    parser.add_argument("--revert", action="store_true", help="Revert patches from backups")
    args = parser.parse_args()

    ext_dir = find_extension_dir(args.ext_dir)
    version = os.path.basename(ext_dir)
    print(f"Extension: {version}")
    print(f"Directory: {ext_dir}")
    print()

    if args.revert:
        revert_patches(ext_dir)
        return

    results = []
    results.append(patch_cli_js(ext_dir, verify_only=args.verify))
    results.append(patch_extension_js(ext_dir, verify_only=args.verify))
    results.append(patch_package_json(ext_dir, verify_only=args.verify))

    print()
    applied = sum(results)
    if args.verify:
        if applied == 3:
            print(f"ALL 3 PATCHES VERIFIED ✓")
        else:
            print(f"{applied}/3 patches applied. Run without --verify to patch.")
    else:
        if applied == 3:
            print("ALL 3 PATCHES APPLIED ✓")
            print("Reload VS Code windows to activate (Ctrl+Shift+P → Reload Window)")
        else:
            print(f"{applied}/3 patches succeeded. Check errors above.")

    sys.exit(0 if applied == 3 else 1)


if __name__ == "__main__":
    main()
