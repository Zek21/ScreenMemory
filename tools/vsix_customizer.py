#!/usr/bin/env python3
"""VSIX Customizer — extract, modify defaults, and repackage VS Code extensions.

A VSIX file is a ZIP archive with this structure:
    [Content_Types].xml          — OPC content-type manifest (XML)
    extension.vsixmanifest       — Extension metadata (XML)
    extension/                   — Extension payload
        package.json             — Extension manifest with settings/defaults
        dist/                    — Compiled JS bundles
        ...

Key facts:
- All files use DEFLATED compression in the original Copilot Chat VSIX.
- There are NO digital signatures inside the VSIX. VS Code does NOT validate
  VSIX signatures for sideloaded extensions (--install-extension).
- [Content_Types].xml maps file extensions to MIME types. It must be preserved
  exactly unless new file types are added.
- extension.vsixmanifest contains publisher, version, and asset references.
  It does NOT need modification for default-value changes in package.json.

Usage:
    # Extract and inspect
    python tools/vsix_customizer.py inspect --input original.vsix

    # Modify defaults and repackage
    python tools/vsix_customizer.py repack --input original.vsix --output custom.vsix \\
        --defaults defaults.json

    # Roundtrip test (extract + rezip, no changes)
    python tools/vsix_customizer.py roundtrip --input original.vsix --output roundtrip.vsix

    # Extract to directory for manual inspection
    python tools/vsix_customizer.py extract --input original.vsix --output-dir ./extracted

defaults.json format:
    {
        "github.copilot.chat.cli.isolationOption.enabled": false,
        "github.copilot.chat.claudeAgent.allowDangerouslySkipPermissions": true
    }

Install the custom VSIX:
    code-insiders --install-extension custom.vsix --force

# signed: delta
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


def extract_vsix(vsix_path: str, output_dir: str) -> str:
    """Extract a VSIX (ZIP) to a directory. Returns the output directory path."""
    vsix_path = os.path.abspath(vsix_path)
    if not os.path.exists(vsix_path):
        raise FileNotFoundError(f"VSIX not found: {vsix_path}")

    os.makedirs(output_dir, exist_ok=True)
    with zipfile.ZipFile(vsix_path, "r") as zf:
        zf.extractall(output_dir)

    return output_dir


def read_package_json(extracted_dir: str) -> dict:
    """Read and parse package.json from an extracted VSIX directory."""
    pkg_path = os.path.join(extracted_dir, "extension", "package.json")
    if not os.path.exists(pkg_path):
        raise FileNotFoundError(f"package.json not found at {pkg_path}")
    with open(pkg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_package_json(extracted_dir: str, pkg: dict) -> None:
    """Write package.json back to the extracted VSIX directory."""
    pkg_path = os.path.join(extracted_dir, "extension", "package.json")
    with open(pkg_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(pkg, f, indent=2, ensure_ascii=False)
        f.write("\n")


def get_all_settings(pkg: dict) -> dict:
    """Extract all configuration settings from package.json.

    Returns dict of {setting_key: {default, type, description}}.
    """
    config = pkg.get("contributes", {}).get("configuration", {})
    configs = [config] if isinstance(config, dict) else config

    settings = {}
    for section in configs:
        props = section.get("properties", {})
        for key, val in props.items():
            settings[key] = {
                "default": val.get("default"),
                "type": val.get("type", "unknown"),
                "description": val.get("description", val.get("markdownDescription", "")),
            }
    return settings


def modify_vendor_visibility(pkg: dict) -> dict:
    """Make Copilot CLI visible in the model provider dropdown and set it as preferred session type.

    Modifications:
    A) In contributes.languageModelChatProviders, remove the 'when': 'false' gate
       on the copilotcli vendor so it appears in the provider dropdown by default.
    B) In contributes.chatSessions, remove the 'when' gate on the copilotcli session
       type so it is always available, and lower its 'order' to make it appear first.

    Returns:
        Dict of changes applied for logging.
    """  # signed: alpha
    contributes = pkg.get("contributes", {})
    changes = {}

    # A) Vendor visibility: remove 'when' gate from copilotcli provider
    providers = contributes.get("languageModelChatProviders", [])
    for provider in providers:
        if provider.get("vendor") == "copilotcli":
            old_when = provider.get("when")
            if "when" in provider:
                del provider["when"]
            changes["vendor_copilotcli_when"] = {"old": old_when, "new": "(removed)"}
            break

    # B) Chat sessions: make copilotcli always available and first in order
    sessions = contributes.get("chatSessions", [])
    for session in sessions:
        if session.get("type") == "copilotcli":
            # Remove conditional 'when' gate so it is always visible
            old_when = session.get("when")
            if "when" in session:
                del session["when"]
            changes["session_copilotcli_when"] = {"old": old_when, "new": "(removed)"}

            # Set order to 0 to make it the first/default session type
            old_order = session.get("order")
            session["order"] = 0
            changes["session_copilotcli_order"] = {"old": old_order, "new": 0}
            break

    return changes


def read_manifest(extracted_dir: str) -> str:
    """Read extension.vsixmanifest as text from an extracted VSIX directory."""
    manifest_path = os.path.join(extracted_dir, "extension.vsixmanifest")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"extension.vsixmanifest not found at {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as f:
        return f.read()


def write_manifest(extracted_dir: str, content: str) -> None:
    """Write extension.vsixmanifest back to the extracted VSIX directory."""
    manifest_path = os.path.join(extracted_dir, "extension.vsixmanifest")
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def modify_identity(pkg: dict, manifest: str, name: str = None,
                    display_name: str = None, engine_version: str = None) -> tuple:
    """Modify extension identity in both package.json and vsixmanifest.

    Args:
        pkg: The parsed package.json dict (modified in place).
        manifest: The raw XML text of extension.vsixmanifest.
        name: New extension ID (e.g. 'skynetAgents'). Applied to package.json
              'name' field and vsixmanifest Identity Id attribute.
        display_name: Human-friendly name (e.g. 'Skynet Agents'). Applied to
                      package.json 'displayName' and vsixmanifest DisplayName element.
                      If not provided but name is, auto-generates from name.
        engine_version: Minimum VS Code version (e.g. '^1.100.0'). Applied to
                        package.json engines.vscode and vsixmanifest Engine property.

    Returns:
        Tuple of (modified_manifest_str, changes_dict).
    """  # signed: alpha
    changes = {}

    if name:
        # Auto-generate display_name from name if not provided
        if not display_name:
            # Convert camelCase/kebab-case to title: skynetAgents -> Skynet Agents
            display_name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
            display_name = display_name.replace('-', ' ').replace('_', ' ').title()

        # package.json: name field
        old_name = pkg.get("name", "")
        pkg["name"] = name
        changes["pkg_name"] = {"old": old_name, "new": name}

        # package.json: displayName field
        old_display = pkg.get("displayName", "")
        pkg["displayName"] = display_name
        changes["pkg_displayName"] = {"old": old_display, "new": display_name}

        # vsixmanifest: Identity Id attribute
        manifest, count = re.subn(
            r'(<Identity\b[^>]*\bId=")[^"]*(")',
            rf'\g<1>{name}\2',
            manifest
        )
        if count:
            changes["manifest_identity_id"] = {"old": old_name, "new": name}

        # vsixmanifest: DisplayName element
        manifest, count = re.subn(
            r'(<DisplayName>)[^<]*(</DisplayName>)',
            rf'\g<1>{display_name}\2',
            manifest
        )
        if count:
            changes["manifest_displayName"] = {"old": old_display, "new": display_name}

    if engine_version:
        # package.json: engines.vscode
        engines = pkg.get("engines", {})
        old_engine = engines.get("vscode", "")
        engines["vscode"] = engine_version
        pkg["engines"] = engines
        changes["pkg_engines_vscode"] = {"old": old_engine, "new": engine_version}

        # vsixmanifest: Microsoft.VisualStudio.Code.Engine Property Value
        manifest, count = re.subn(
            r'(<Property\s+Id="Microsoft\.VisualStudio\.Code\.Engine"\s+Value=")[^"]*(")',
            rf'\g<1>{engine_version}\2',
            manifest
        )
        if count:
            changes["manifest_engine"] = {"old": old_engine, "new": engine_version}

    return manifest, changes


def modify_defaults(pkg: dict, new_defaults: dict) -> dict:
    """Modify default values in package.json configuration.

    Args:
        pkg: The parsed package.json dict.
        new_defaults: Dict of {setting_key: new_default_value}.

    Returns:
        Dict of {setting_key: {old: old_val, new: new_val}} for changes applied.

    Raises:
        KeyError: If a setting key is not found in any configuration section.
    """
    config = pkg.get("contributes", {}).get("configuration", {})
    configs = [config] if isinstance(config, dict) else config

    changes = {}
    not_found = set(new_defaults.keys())

    for section in configs:
        props = section.get("properties", {})
        for key, new_val in new_defaults.items():
            if key in props:
                old_val = props[key].get("default")
                props[key]["default"] = new_val
                changes[key] = {"old": old_val, "new": new_val}
                not_found.discard(key)

    if not_found:
        raise KeyError(f"Settings not found in package.json: {', '.join(sorted(not_found))}")

    return changes


def repackage_vsix(extracted_dir: str, output_path: str) -> str:
    """Repackage an extracted VSIX directory into a new .vsix file.

    Preserves the original ZIP structure:
    - [Content_Types].xml and extension.vsixmanifest at root
    - All extension/ files with relative paths
    - DEFLATED compression for all files
    """
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # Collect all files with their archive paths
    archive_entries = []
    for root, _dirs, files in os.walk(extracted_dir):
        for fname in files:
            full_path = os.path.join(root, fname)
            # Archive path is relative to extracted_dir
            arc_name = os.path.relpath(full_path, extracted_dir).replace("\\", "/")
            archive_entries.append((full_path, arc_name))

    # Sort to ensure [Content_Types].xml comes first (OPC convention),
    # then extension.vsixmanifest, then everything else alphabetically
    def sort_key(entry):
        arc = entry[1]
        if arc == "[Content_Types].xml":
            return (0, arc)
        if arc == "extension.vsixmanifest":
            return (1, arc)
        return (2, arc)

    archive_entries.sort(key=sort_key)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for full_path, arc_name in archive_entries:
            zf.write(full_path, arc_name)

    return output_path


def inspect_vsix(vsix_path: str) -> dict:
    """Inspect a VSIX file and return metadata without full extraction."""
    with zipfile.ZipFile(vsix_path, "r") as zf:
        names = zf.namelist()
        pkg = json.loads(zf.read("extension/package.json"))

        # Check for signatures
        sig_files = [n for n in names if any(s in n.lower() for s in [".sig", ".p7s", "signature"])]

        # Compression stats
        methods = {}
        total_size = 0
        compressed_size = 0
        for info in zf.infolist():
            total_size += info.file_size
            compressed_size += info.compress_size
            mname = {0: "STORED", 8: "DEFLATED", 12: "BZIP2", 14: "LZMA"}.get(
                info.compress_type, f"OTHER({info.compress_type})"
            )
            methods[mname] = methods.get(mname, 0) + 1

        settings = get_all_settings(pkg)

        return {
            "name": pkg.get("name"),
            "display_name": pkg.get("displayName"),
            "version": pkg.get("version"),
            "publisher": pkg.get("publisher"),
            "file_count": len(names),
            "total_size_mb": round(total_size / 1024 / 1024, 1),
            "compressed_size_mb": round(compressed_size / 1024 / 1024, 1),
            "compression_methods": methods,
            "has_signatures": len(sig_files) > 0,
            "signature_files": sig_files,
            "settings_count": len(settings),
            "settings": settings,
        }


def cmd_inspect(args):
    """Handle the 'inspect' subcommand."""
    info = inspect_vsix(args.input)

    print(f"=== VSIX Inspection: {os.path.basename(args.input)} ===")
    print(f"Name:        {info['publisher']}.{info['name']}")
    print(f"Display:     {info['display_name']}")
    print(f"Version:     {info['version']}")
    print(f"Files:       {info['file_count']}")
    print(f"Size:        {info['total_size_mb']} MB ({info['compressed_size_mb']} MB compressed)")
    print(f"Compression: {info['compression_methods']}")
    print(f"Signatures:  {'YES: ' + str(info['signature_files']) if info['has_signatures'] else 'NONE (unsigned)'}")
    print(f"Settings:    {info['settings_count']} configuration properties")

    if args.settings:
        print("\n=== All Settings ===")
        for key, val in sorted(info["settings"].items()):
            print(f"  {key} = {val['default']}  ({val['type']})")

    if args.filter:
        print(f"\n=== Settings matching '{args.filter}' ===")
        for key, val in sorted(info["settings"].items()):
            if args.filter.lower() in key.lower():
                desc = val["description"][:100] if val["description"] else ""
                print(f"  {key} = {val['default']}  ({val['type']})")
                if desc:
                    print(f"    {desc}")


def cmd_extract(args):
    """Handle the 'extract' subcommand."""
    out = extract_vsix(args.input, args.output_dir)
    print(f"Extracted to: {out}")
    file_count = sum(len(files) for _, _, files in os.walk(out))
    print(f"Total files: {file_count}")


def cmd_repack(args):
    """Handle the 'repack' subcommand."""
    # Load defaults to apply
    if args.defaults:
        with open(args.defaults, "r", encoding="utf-8") as f:
            new_defaults = json.load(f)
    else:
        new_defaults = {}

    if args.set:
        for item in args.set:
            key, val_str = item.split("=", 1)
            # Parse value: true/false -> bool, numbers -> int/float, else string
            val_str = val_str.strip()
            if val_str.lower() == "true":
                new_defaults[key.strip()] = True
            elif val_str.lower() == "false":
                new_defaults[key.strip()] = False
            elif val_str.isdigit():
                new_defaults[key.strip()] = int(val_str)
            else:
                try:
                    new_defaults[key.strip()] = float(val_str)
                except ValueError:
                    new_defaults[key.strip()] = val_str

    if not new_defaults:
        print("WARNING: No defaults specified. Use --defaults FILE or --set KEY=VALUE.")
        print("Repackaging without changes (roundtrip).")

    # Extract to temp dir
    with tempfile.TemporaryDirectory(prefix="vsix_custom_") as tmpdir:
        print(f"Extracting {os.path.basename(args.input)}...")
        extract_vsix(args.input, tmpdir)

        pkg = read_package_json(tmpdir)

        # Apply identity/engine modifications if requested
        manifest_text = read_manifest(tmpdir)
        if getattr(args, 'name', None) or getattr(args, 'engine', None):
            print("Applying identity/engine modifications...")
            manifest_text, id_changes = modify_identity(
                pkg, manifest_text,
                name=getattr(args, 'name', None),
                display_name=getattr(args, 'display_name', None),
                engine_version=getattr(args, 'engine', None),
            )
            for key, change in id_changes.items():
                print(f"  {key}: {change['old']} -> {change['new']}")
            write_manifest(tmpdir, manifest_text)

        # Apply vendor visibility and session type modifications
        print("Applying Skynet vendor/session modifications...")
        vendor_changes = modify_vendor_visibility(pkg)
        for key, change in vendor_changes.items():
            print(f"  {key}: {change['old']} -> {change['new']}")

        if new_defaults:
            print(f"Modifying {len(new_defaults)} setting default(s)...")
            changes = modify_defaults(pkg, new_defaults)
            for key, change in changes.items():
                print(f"  {key}: {change['old']} -> {change['new']}")

        write_package_json(tmpdir, pkg)

        print(f"Repackaging to {args.output}...")
        result = repackage_vsix(tmpdir, args.output)
        size_mb = os.path.getsize(result) / 1024 / 1024
        print(f"Done: {result} ({size_mb:.1f} MB)")

    return result


def cmd_roundtrip(args):
    """Handle the 'roundtrip' subcommand — extract and rezip without changes."""
    with tempfile.TemporaryDirectory(prefix="vsix_rt_") as tmpdir:
        print(f"Extracting {os.path.basename(args.input)}...")
        extract_vsix(args.input, tmpdir)

        print(f"Repackaging to {args.output}...")
        result = repackage_vsix(tmpdir, args.output)

        orig_size = os.path.getsize(args.input)
        new_size = os.path.getsize(result)
        diff_pct = abs(new_size - orig_size) / orig_size * 100

        print(f"\n=== Roundtrip Results ===")
        print(f"Original:  {orig_size / 1024 / 1024:.2f} MB")
        print(f"Roundtrip: {new_size / 1024 / 1024:.2f} MB")
        print(f"Size diff: {diff_pct:.1f}% ({'larger' if new_size > orig_size else 'smaller'})")

        # Verify contents match
        with zipfile.ZipFile(args.input, "r") as z1, zipfile.ZipFile(result, "r") as z2:
            names1 = set(z1.namelist())
            names2 = set(z2.namelist())
            missing = names1 - names2
            extra = names2 - names1
            if missing:
                print(f"MISSING files: {missing}")
            if extra:
                print(f"EXTRA files: {extra}")
            if not missing and not extra:
                print(f"File list: IDENTICAL ({len(names1)} files)")

                # Spot-check package.json content
                pkg1 = json.loads(z1.read("extension/package.json"))
                pkg2 = json.loads(z2.read("extension/package.json"))
                if pkg1 == pkg2:
                    print("package.json: IDENTICAL")
                else:
                    print("package.json: DIFFERS (unexpected for roundtrip!)")

        print(f"\nTo install: code-insiders --install-extension {result} --force")

    return result


def cmd_diff(args):
    """Compare two VSIX files to show setting differences."""
    with zipfile.ZipFile(args.input, "r") as z1, zipfile.ZipFile(args.second, "r") as z2:
        pkg1 = json.loads(z1.read("extension/package.json"))
        pkg2 = json.loads(z2.read("extension/package.json"))

    # Compare configuration settings
    settings1 = get_all_settings(pkg1)
    settings2 = get_all_settings(pkg2)

    diffs = []
    all_keys = sorted(set(settings1.keys()) | set(settings2.keys()))
    for key in all_keys:
        s1 = settings1.get(key, {})
        s2 = settings2.get(key, {})
        d1 = s1.get("default")
        d2 = s2.get("default")
        if d1 != d2:
            diffs.append((key, d1, d2))

    if not diffs:
        print("No setting differences found.")
    else:
        print(f"=== {len(diffs)} Setting Differences ===")
        for key, old, new in diffs:
            print(f"  {key}: {old} -> {new}")


def main():
    parser = argparse.ArgumentParser(
        description="VSIX Customizer — extract, modify, and repackage VS Code extensions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Inspect extension settings
  %(prog)s inspect --input copilot-chat.vsix --filter cli

  # Repack with modified defaults (from JSON file)
  %(prog)s repack --input copilot-chat.vsix --output custom.vsix --defaults my_defaults.json

  # Repack with inline setting overrides
  %(prog)s repack --input copilot-chat.vsix --output custom.vsix \\
      --set "github.copilot.chat.cli.isolationOption.enabled=false"

  # Roundtrip test (no modifications)
  %(prog)s roundtrip --input copilot-chat.vsix --output test.vsix

  # Compare two VSIX files
  %(prog)s diff --input original.vsix --second modified.vsix
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # inspect
    p_inspect = subparsers.add_parser("inspect", help="Inspect VSIX metadata and settings")
    p_inspect.add_argument("--input", "-i", required=True, help="Path to .vsix file")
    p_inspect.add_argument("--settings", "-s", action="store_true", help="Show all settings")
    p_inspect.add_argument("--filter", "-f", help="Filter settings by keyword")

    # extract
    p_extract = subparsers.add_parser("extract", help="Extract VSIX to directory")
    p_extract.add_argument("--input", "-i", required=True, help="Path to .vsix file")
    p_extract.add_argument("--output-dir", "-o", required=True, help="Output directory")

    # repack
    p_repack = subparsers.add_parser("repack", help="Modify defaults and repackage VSIX")
    p_repack.add_argument("--input", "-i", required=True, help="Path to original .vsix file")
    p_repack.add_argument("--output", "-o", required=True, help="Path for modified .vsix file")
    p_repack.add_argument("--defaults", "-d", help="JSON file with {setting_key: new_default}")
    p_repack.add_argument("--set", action="append", help="Inline setting: KEY=VALUE (repeatable)")
    p_repack.add_argument("--name", help="New extension ID (e.g. 'skynetAgents')")
    p_repack.add_argument("--display-name", dest="display_name",
                          help="Human-friendly display name (auto-generated from --name if omitted)")
    p_repack.add_argument("--engine", help="Minimum VS Code engine version (e.g. '^1.100.0')")

    # roundtrip
    p_roundtrip = subparsers.add_parser("roundtrip", help="Extract and rezip without changes (test)")
    p_roundtrip.add_argument("--input", "-i", required=True, help="Path to original .vsix file")
    p_roundtrip.add_argument("--output", "-o", required=True, help="Path for roundtrip .vsix file")

    # diff
    p_diff = subparsers.add_parser("diff", help="Compare settings between two VSIX files")
    p_diff.add_argument("--input", "-i", required=True, help="First .vsix file")
    p_diff.add_argument("--second", "-s", required=True, help="Second .vsix file")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    commands = {
        "inspect": cmd_inspect,
        "extract": cmd_extract,
        "repack": cmd_repack,
        "roundtrip": cmd_roundtrip,
        "diff": cmd_diff,
    }

    try:
        commands[args.command](args)
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
