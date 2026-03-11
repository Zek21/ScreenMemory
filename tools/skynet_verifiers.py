"""Verifier framework for Skynet workers.

Provides a base Verifier class and concrete verifiers for validating
worker outputs (syntax checks, test runs, etc.).

Usage:
    python tools/skynet_verifiers.py syntax --files file1.py file2.py
    python tools/skynet_verifiers.py syntax --changed   # git-changed .py files
"""

import json
import os
import subprocess
import sys
import py_compile
import argparse
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Dict, Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent


class VerificationResult:
    """Result of a verification run."""

    def __init__(self, verifier: str, passed: bool, details: List[Dict[str, Any]]):
        self.verifier = verifier
        self.passed = passed
        self.details = details

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verifier": self.verifier,
            "passed": self.passed,
            "file_count": len(self.details),
            "failures": [d for d in self.details if not d.get("ok")],
            "details": self.details,
        }

    def __repr__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"VerificationResult({self.verifier}: {status}, {len(self.details)} files)"


class Verifier(ABC):
    """Base class for all verifiers."""

    name: str = "base"

    @abstractmethod
    def verify(self, files: List[Path]) -> VerificationResult:
        """Run verification on a list of files.

        Args:
            files: List of file paths to verify.

        Returns:
            VerificationResult with pass/fail and per-file details.
        """
        ...

    def get_changed_files(self, extension: str = ".py") -> List[Path]:
        """Get files changed in git working tree."""
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=ACM", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            files = []
            for line in result.stdout.strip().splitlines():
                p = REPO_ROOT / line.strip()
                if p.suffix == extension and p.exists():
                    files.append(p)
            return files
        except Exception:
            return []


class SyntaxVerifier(Verifier):
    """Verifies Python files compile without syntax errors."""

    name = "syntax"

    def verify(self, files: List[Path]) -> VerificationResult:
        details = []
        all_passed = True

        for f in files:
            f = f.resolve()
            try:
                display = str(f.relative_to(REPO_ROOT))
            except ValueError:
                display = str(f)
            try:
                py_compile.compile(str(f), doraise=True)
                details.append({"file": display, "ok": True})
            except py_compile.PyCompileError as e:
                all_passed = False
                details.append({
                    "file": display,
                    "ok": False,
                    "error": str(e),
                })

        return VerificationResult(self.name, all_passed, details)


# Registry of available verifiers
VERIFIERS: Dict[str, Verifier] = {
    "syntax": SyntaxVerifier(),
}


def run_verifier(name: str, files: Optional[List[Path]] = None) -> VerificationResult:
    """Run a named verifier.

    Args:
        name: Verifier name from VERIFIERS registry.
        files: Files to verify, or None to auto-detect changed files.

    Returns:
        VerificationResult.
    """
    verifier = VERIFIERS.get(name)
    if not verifier:
        raise ValueError(f"Unknown verifier '{name}'. Available: {list(VERIFIERS.keys())}")

    if files is None:
        files = verifier.get_changed_files()

    return verifier.verify(files)


def main():
    parser = argparse.ArgumentParser(description="Skynet Verifier Framework")
    sub = parser.add_subparsers(dest="cmd")

    syntax_p = sub.add_parser("syntax", help="Check Python syntax")
    syntax_p.add_argument("--files", nargs="*", default=None, help="Files to check")
    syntax_p.add_argument("--changed", action="store_true", help="Check git-changed files")

    list_p = sub.add_parser("list", help="List available verifiers")

    args = parser.parse_args()

    if args.cmd == "syntax":
        if args.files:
            files = [Path(f) for f in args.files]
        elif args.changed:
            files = SyntaxVerifier().get_changed_files()
        else:
            files = SyntaxVerifier().get_changed_files()

        if not files:
            print("No files to verify.")
            return

        result = run_verifier("syntax", files)
        print(json.dumps(result.to_dict(), indent=2))
        sys.exit(0 if result.passed else 1)

    elif args.cmd == "list":
        for name, v in VERIFIERS.items():
            print(f"  {name}: {v.__class__.__name__}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
