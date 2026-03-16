"""Skynet Bee Algorithm — Scout+Recruitment for Codebase Improvement.

Implements the Artificial Bee Colony (ABC) algorithm adapted for multi-worker
codebase improvement:

  - **ScoutBee**: explores the codebase for improvement opportunities (TODO
    comments, missing tests, code smells, long functions, missing docstrings).
  - **WorkerBee**: executes assigned improvements via skynet_dispatch.
  - **DanceCommunication**: scouts report findings with quality scores;
    workers are recruited proportional to quality.
  - **FoodSource**: represents an improvement opportunity with location,
    quality, and exploitation count.
  - **Hive**: manages scout/worker allocation, abandonment thresholds,
    and the full ABC lifecycle.

Usage::

    from tools.skynet_bee import Hive
    hive = Hive()
    sources = hive.scout()              # discover food sources
    assignments = hive.recruit()        # recruit workers to best sources
    hive.status()                       # print hive state

CLI::

    python tools/skynet_bee.py scout
    python tools/skynet_bee.py recruit
    python tools/skynet_bee.py status
    python tools/skynet_bee.py dance-log

# signed: beta
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Paths ──────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "data"
HIVE_FILE = DATA_DIR / "bee_hive.json"
DANCE_LOG_FILE = DATA_DIR / "bee_dance_log.jsonl"

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────
# signed: beta
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]
ABANDONMENT_THRESHOLD = 5       # visits without quality gain → abandon
MAX_FOOD_SOURCES = 50           # cap on tracked sources
MIN_QUALITY = 0.1               # floor for source quality
MAX_QUALITY = 10.0              # cap for source quality
SCOUT_RATIO = 0.3               # 30% of bees are scouts
DEFAULT_SCAN_DIRS = ["tools", "core", "tests"]

# Quality multipliers by finding category
# signed: beta
QUALITY_WEIGHTS: Dict[str, float] = {
    "todo_comment":      1.0,
    "missing_test":      2.5,
    "missing_docstring": 0.8,
    "long_function":     1.5,
    "code_smell":        2.0,
    "syntax_error":      3.0,
    "bare_except":       1.8,
    "unused_import":     0.5,
}

# Severity multipliers
SEVERITY_MULTIPLIER: Dict[str, float] = {
    "low":      1.0,
    "medium":   1.5,
    "high":     2.0,
    "critical": 3.0,
}

_lock = threading.Lock()


# ── Data Structures ────────────────────────────────────────────────

@dataclass
class FoodSource:
    """An improvement opportunity discovered by a scout bee.

    Attributes:
        source_id:          Unique hash of file+category+detail.
        file_path:          Relative path to the file.
        category:           Finding category (todo_comment, missing_test, etc.).
        detail:             Human-readable description.
        line:               Line number (0 if file-level).
        quality:            Estimated impact score (higher = more valuable).
        severity:           low / medium / high / critical.
        exploitation_count: Times a worker bee has been assigned this source.
        last_exploited_at:  ISO timestamp of last assignment.
        discovered_at:      ISO timestamp of discovery.
        discovered_by:      Scout bee / scanner name.
        status:             active / assigned / completed / abandoned.
        assigned_to:        Worker name if currently assigned.
    # signed: beta
    """
    source_id: str = ""
    file_path: str = ""
    category: str = ""
    detail: str = ""
    line: int = 0
    quality: float = 1.0
    severity: str = "low"
    exploitation_count: int = 0
    last_exploited_at: str = ""
    discovered_at: str = ""
    discovered_by: str = "scout"
    status: str = "active"
    assigned_to: str = ""

    def __post_init__(self):
        if not self.source_id:
            raw = f"{self.file_path}:{self.category}:{self.detail[:80]}"
            self.source_id = hashlib.sha256(raw.encode()).hexdigest()[:12]
        if not self.discovered_at:
            self.discovered_at = _now_iso()

    def effective_quality(self) -> float:
        """Quality decays with repeated exploitation."""
        decay = max(0.1, 1.0 - 0.15 * self.exploitation_count)
        return round(self.quality * decay, 3)

    def should_abandon(self) -> bool:
        """True if source has been exploited too many times."""
        return self.exploitation_count >= ABANDONMENT_THRESHOLD

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FoodSource":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class DanceRecord:
    """A scout's waggle dance — communicates source quality to the hive.

    # signed: beta
    """
    scout: str
    source_id: str
    file_path: str
    category: str
    quality: float
    recruited_count: int = 0
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = _now_iso()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Utility ────────────────────────────────────────────────────────

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
# signed: beta


def _load_hive_state() -> Dict[str, Any]:
    """Load persisted hive state."""
    if HIVE_FILE.exists():
        try:
            with open(HIVE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "food_sources": [],
        "metrics": {
            "total_scouted": 0,
            "total_recruited": 0,
            "total_completed": 0,
            "total_abandoned": 0,
            "sources_active": 0,
            "dances_performed": 0,
        },
        "updated_at": "",
    }
# signed: beta


def _save_hive_state(state: Dict[str, Any]) -> None:
    """Atomically persist hive state."""
    state["updated_at"] = _now_iso()
    tmp = HIVE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(str(tmp), str(HIVE_FILE))
# signed: beta


def _append_dance_log(record: DanceRecord) -> None:
    """Append a dance record to the JSONL log."""
    with open(DANCE_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record.to_dict(), default=str) + "\n")
# signed: beta


# ── ScoutBee — Codebase Exploration ────────────────────────────────

class ScoutBee:
    """Explores the codebase for improvement opportunities.

    Scanning strategies:
      1. TODO/FIXME/HACK/XXX/BUG comments
      2. Missing test files for modules with public functions
      3. Long functions (>50 lines)
      4. Bare except / except-pass blocks
      5. Missing module/class/function docstrings

    Each finding becomes a FoodSource with a quality score.

    # signed: beta
    """

    # Patterns for TODO-like comments
    _TODO_RE = re.compile(
        r"#\s*(TODO|FIXME|HACK|XXX|BUG)\b[:\s]*(.*)", re.IGNORECASE
    )
    _BARE_EXCEPT_RE = re.compile(r"^\s*except\s*:", re.MULTILINE)
    _EXCEPT_PASS_RE = re.compile(
        r"except\s+[\w.,\s()]+:\s*\n\s+pass\s*$", re.MULTILINE
    )

    def __init__(self, scan_dirs: Optional[List[str]] = None):
        self.scan_dirs = scan_dirs or DEFAULT_SCAN_DIRS
        self.findings: List[FoodSource] = []

    def scan(self) -> List[FoodSource]:
        """Run all scanning strategies and return discovered food sources."""
        self.findings = []
        py_files = self._collect_python_files()

        for fpath in py_files:
            rel = str(fpath.relative_to(ROOT)).replace("\\", "/")
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            self._scan_todos(rel, content)
            self._scan_bare_excepts(rel, content)
            self._scan_ast(rel, content)

        self._scan_missing_tests(py_files)
        self._deduplicate()

        logger.info("ScoutBee scan complete: %d findings", len(self.findings))
        return self.findings
    # signed: beta

    def _collect_python_files(self) -> List[Path]:
        """Collect all .py files from scan directories."""
        files: List[Path] = []
        for d in self.scan_dirs:
            scan_path = ROOT / d
            if scan_path.is_dir():
                files.extend(scan_path.rglob("*.py"))
            elif scan_path.is_file() and scan_path.suffix == ".py":
                files.append(scan_path)
        return sorted(set(files))

    def _scan_todos(self, rel_path: str, content: str) -> None:
        """Find TODO/FIXME/HACK/XXX/BUG comments."""
        for i, line in enumerate(content.splitlines(), 1):
            m = self._TODO_RE.search(line)
            if m:
                tag = m.group(1).upper()
                detail = m.group(2).strip()[:120] or f"{tag} comment"
                severity = "high" if tag in ("BUG", "FIXME") else "medium"
                self.findings.append(FoodSource(
                    file_path=rel_path,
                    category="todo_comment",
                    detail=f"[{tag}] {detail}",
                    line=i,
                    quality=QUALITY_WEIGHTS["todo_comment"]
                            * SEVERITY_MULTIPLIER.get(severity, 1.0),
                    severity=severity,
                    discovered_by="scout_todo",
                ))
    # signed: beta

    def _scan_bare_excepts(self, rel_path: str, content: str) -> None:
        """Find bare except: and except-pass blocks."""
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped == "except:" or self._BARE_EXCEPT_RE.match(line):
                self.findings.append(FoodSource(
                    file_path=rel_path,
                    category="bare_except",
                    detail="Bare except: block swallows all errors",
                    line=i,
                    quality=QUALITY_WEIGHTS["bare_except"]
                            * SEVERITY_MULTIPLIER["high"],
                    severity="high",
                    discovered_by="scout_except",
                ))
        # Also check except-pass pattern
        for m in self._EXCEPT_PASS_RE.finditer(content):
            lineno = content[:m.start()].count("\n") + 1
            self.findings.append(FoodSource(
                file_path=rel_path,
                category="code_smell",
                detail="Silent except-pass block hides errors",
                line=lineno,
                quality=QUALITY_WEIGHTS["code_smell"]
                        * SEVERITY_MULTIPLIER["medium"],
                severity="medium",
                discovered_by="scout_smell",
            ))
    # signed: beta

    def _scan_ast(self, rel_path: str, content: str) -> None:
        """AST-based scanning: long functions, missing docstrings."""
        try:
            tree = ast.parse(content)
        except SyntaxError:
            self.findings.append(FoodSource(
                file_path=rel_path,
                category="syntax_error",
                detail="File has syntax errors (cannot parse AST)",
                quality=QUALITY_WEIGHTS["syntax_error"]
                        * SEVERITY_MULTIPLIER["critical"],
                severity="critical",
                discovered_by="scout_ast",
            ))
            return

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Skip private/dunder functions for docstring check
                is_public = not node.name.startswith("_")
                body_lines = (node.end_lineno or 0) - (node.lineno or 0)

                # Long function check
                if body_lines > 50:
                    self.findings.append(FoodSource(
                        file_path=rel_path,
                        category="long_function",
                        detail=f"Function '{node.name}' is {body_lines} lines",
                        line=node.lineno,
                        quality=QUALITY_WEIGHTS["long_function"]
                                * min(body_lines / 50.0, 3.0),
                        severity="medium" if body_lines < 100 else "high",
                        discovered_by="scout_ast",
                    ))

                # Missing docstring check (public functions only)
                if is_public and not ast.get_docstring(node):
                    self.findings.append(FoodSource(
                        file_path=rel_path,
                        category="missing_docstring",
                        detail=f"Public function '{node.name}' has no docstring",
                        line=node.lineno,
                        quality=QUALITY_WEIGHTS["missing_docstring"],
                        severity="low",
                        discovered_by="scout_ast",
                    ))

            elif isinstance(node, ast.ClassDef):
                if not node.name.startswith("_") and not ast.get_docstring(node):
                    self.findings.append(FoodSource(
                        file_path=rel_path,
                        category="missing_docstring",
                        detail=f"Class '{node.name}' has no docstring",
                        line=node.lineno,
                        quality=QUALITY_WEIGHTS["missing_docstring"] * 1.5,
                        severity="low",
                        discovered_by="scout_ast",
                    ))
    # signed: beta

    def _scan_missing_tests(self, py_files: List[Path]) -> None:
        """Find modules that have no corresponding test file."""
        test_dir = ROOT / "tests"
        existing_tests = set()
        if test_dir.is_dir():
            for tf in test_dir.rglob("test_*.py"):
                # test_foo.py → foo
                name = tf.stem.replace("test_", "", 1)
                existing_tests.add(name)

        for fpath in py_files:
            if fpath.parent.name == "tests" or fpath.name.startswith("test_"):
                continue
            if fpath.name.startswith("_"):
                continue
            module_name = fpath.stem
            if module_name not in existing_tests:
                rel = str(fpath.relative_to(ROOT)).replace("\\", "/")
                self.findings.append(FoodSource(
                    file_path=rel,
                    category="missing_test",
                    detail=f"No test file for '{module_name}'",
                    quality=QUALITY_WEIGHTS["missing_test"],
                    severity="medium",
                    discovered_by="scout_test",
                ))
    # signed: beta

    def _deduplicate(self) -> None:
        """Remove duplicate findings by source_id."""
        seen: set = set()
        unique: List[FoodSource] = []
        for fs in self.findings:
            if fs.source_id not in seen:
                seen.add(fs.source_id)
                unique.append(fs)
        self.findings = unique


# ── WorkerBee — Task Execution ─────────────────────────────────────

class WorkerBee:
    """Dispatches improvement tasks to Skynet workers.

    Takes an assigned FoodSource and dispatches it to the appropriate
    worker via skynet_dispatch.

    # signed: beta
    """

    def __init__(self, worker_name: str):
        self.worker_name = worker_name
        self.tasks_dispatched: int = 0
        self.tasks_completed: int = 0

    def execute(self, source: FoodSource) -> bool:
        """Dispatch an improvement task for this food source.

        Returns True if dispatch succeeded, False otherwise.
        """
        task = self._build_task(source)
        try:
            from tools.skynet_dispatch import dispatch_to_worker
            ok = dispatch_to_worker(self.worker_name, task)
            if ok:
                self.tasks_dispatched += 1
                source.exploitation_count += 1
                source.last_exploited_at = _now_iso()
                source.status = "assigned"
                source.assigned_to = self.worker_name
                logger.info("WorkerBee dispatched %s to %s",
                            source.source_id, self.worker_name)
            return ok
        except Exception as e:
            logger.warning("WorkerBee dispatch failed for %s: %s",
                           source.source_id, e)
            return False
    # signed: beta

    @staticmethod
    def _build_task(source: FoodSource) -> str:
        """Build a task description from a food source."""
        parts = [
            f"[BEE-TASK] Improve {source.file_path}",
            f"Category: {source.category}",
            f"Issue: {source.detail}",
        ]
        if source.line > 0:
            parts.append(f"Line: {source.line}")
        parts.append(f"Quality/Impact: {source.quality:.1f}")
        parts.append(f"Severity: {source.severity}")
        parts.append("")
        parts.append("Fix this issue directly. Verify with py_compile after.")
        parts.append("Post result to bus when done.")
        return "\n".join(parts)
    # signed: beta


# ── DanceCommunication — Quality Reporting & Recruitment ───────────

class DanceCommunication:
    """Implements the waggle dance: scouts communicate source quality.

    Higher-quality sources recruit more worker bees (proportional
    recruitment). The dance is logged for audit and replayed on restart.

    # signed: beta
    """

    def __init__(self):
        self._dances: List[DanceRecord] = []

    def perform_dance(self, scout: str, source: FoodSource,
                      available_workers: int = 4) -> DanceRecord:
        """Perform a waggle dance for a discovered food source.

        The number of workers recruited is proportional to quality:
          recruited = ceil(quality / max_quality * available_workers)
        Capped to at least 1 and at most available_workers.

        Returns:
            DanceRecord with recruitment count.
        """
        quality = source.effective_quality()
        ratio = min(1.0, quality / MAX_QUALITY)
        recruited = max(1, min(available_workers,
                               int(ratio * available_workers + 0.5)))

        record = DanceRecord(
            scout=scout,
            source_id=source.source_id,
            file_path=source.file_path,
            category=source.category,
            quality=quality,
            recruited_count=recruited,
        )
        self._dances.append(record)

        with _lock:
            _append_dance_log(record)

        logger.info("Dance: scout=%s source=%s quality=%.2f recruited=%d",
                     scout, source.source_id[:8], quality, recruited)
        return record
    # signed: beta

    def get_dances(self) -> List[DanceRecord]:
        """Return all dances performed in this session."""
        return list(self._dances)

    @staticmethod
    def load_dance_log(limit: int = 50) -> List[Dict[str, Any]]:
        """Load recent dance records from the persistent log."""
        if not DANCE_LOG_FILE.exists():
            return []
        records: List[Dict[str, Any]] = []
        try:
            with open(DANCE_LOG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            return []
        return records[-limit:]
    # signed: beta


# ── Hive — Colony Management ──────────────────────────────────────

class Hive:
    """Manages the full Artificial Bee Colony lifecycle.

    The Hive coordinates scout bees (exploration), worker bees
    (exploitation), dance communication (quality signaling), and
    source abandonment (forgetting low-value targets).

    Lifecycle per cycle:
      1. **Scout phase**: ScoutBees scan the codebase for food sources.
      2. **Dance phase**: Scouts perform waggle dances to report quality.
      3. **Recruit phase**: Workers are assigned to top sources
         proportional to quality.
      4. **Abandon phase**: Sources with too many exploitations and
         declining quality are abandoned.

    State is persisted to data/bee_hive.json for crash recovery.

    Args:
        scan_dirs:  Directories for scouts to scan.
        max_sources: Maximum tracked food sources.
        abandonment_threshold: Visits before abandoning a source.

    # signed: beta
    """

    def __init__(
        self,
        scan_dirs: Optional[List[str]] = None,
        max_sources: int = MAX_FOOD_SOURCES,
        abandonment_threshold: int = ABANDONMENT_THRESHOLD,
    ):
        self.scan_dirs = scan_dirs or DEFAULT_SCAN_DIRS
        self.max_sources = max_sources
        self.abandonment_threshold = abandonment_threshold
        self.scout = ScoutBee(scan_dirs=self.scan_dirs)
        self.dance = DanceCommunication()
        self._sources: Dict[str, FoodSource] = {}
        self._load_sources()

    def _load_sources(self) -> None:
        """Load persisted food sources from hive state, respecting max_sources."""
        with _lock:
            state = _load_hive_state()
        for sd in state.get("food_sources", []):
            if len(self._sources) >= self.max_sources:
                break
            try:
                fs = FoodSource.from_dict(sd)
                if fs.status in ("active", "assigned"):
                    self._sources[fs.source_id] = fs
            except Exception:
                continue
    # signed: beta

    def _save(self) -> None:
        """Persist current hive state."""
        with _lock:
            state = _load_hive_state()
            state["food_sources"] = [
                s.to_dict() for s in self._sources.values()
            ]
            state["metrics"]["sources_active"] = sum(
                1 for s in self._sources.values() if s.status == "active"
            )
            _save_hive_state(state)
    # signed: beta

    def _update_metric(self, key: str, increment: int = 1) -> None:
        """Atomically increment a hive metric."""
        with _lock:
            state = _load_hive_state()
            state["metrics"][key] = state["metrics"].get(key, 0) + increment
            _save_hive_state(state)

    # ── Scout Phase ────────────────────────────────────────────────

    def scout_phase(self) -> List[FoodSource]:
        """Run scout bees to discover new food sources.

        Merges new findings with existing sources, deduplicating
        by source_id. Caps total sources at max_sources.

        Returns:
            List of newly discovered food sources.
        """
        new_findings = self.scout.scan()
        added: List[FoodSource] = []

        for fs in new_findings:
            if fs.source_id not in self._sources:
                if len(self._sources) < self.max_sources:
                    self._sources[fs.source_id] = fs
                    added.append(fs)

        self._update_metric("total_scouted", len(added))
        self._save()

        logger.info("Scout phase: %d new sources (total %d)",
                     len(added), len(self._sources))
        return added
    # signed: beta

    # ── Dance Phase ────────────────────────────────────────────────

    def dance_phase(self, sources: Optional[List[FoodSource]] = None,
                    available_workers: int = 4) -> List[DanceRecord]:
        """Scouts perform waggle dances for discovered sources.

        Args:
            sources: Sources to dance about (default: all active).
            available_workers: Number of workers available for recruitment.

        Returns:
            List of DanceRecords with recruitment counts.
        """
        if sources is None:
            sources = [s for s in self._sources.values()
                       if s.status == "active"]

        # Sort by quality descending — best sources get danced first
        sources.sort(key=lambda s: s.effective_quality(), reverse=True)

        records: List[DanceRecord] = []
        for src in sources:
            rec = self.dance.perform_dance(
                scout=src.discovered_by,
                source=src,
                available_workers=available_workers,
            )
            records.append(rec)

        self._update_metric("dances_performed", len(records))
        return records
    # signed: beta

    # ── Recruit Phase ──────────────────────────────────────────────

    def recruit_phase(
        self,
        workers: Optional[List[str]] = None,
        max_assignments: int = 4,
        dry_run: bool = False,
    ) -> List[Dict[str, Any]]:
        """Recruit worker bees to exploit the best food sources.

        Assigns workers to sources proportional to quality:
        higher-quality sources get more workers. Each worker is
        assigned at most one source.

        Args:
            workers:          Available worker names (default: all 4).
            max_assignments:  Maximum simultaneous assignments.
            dry_run:          If True, plan assignments without dispatching.

        Returns:
            List of assignment dicts: {worker, source_id, file_path,
            quality, dispatched}.
        """
        workers = workers or list(WORKER_NAMES)
        active = sorted(
            [s for s in self._sources.values() if s.status == "active"],
            key=lambda s: s.effective_quality(),
            reverse=True,
        )

        if not active:
            logger.info("Recruit: no active food sources")
            return []

        assignments: List[Dict[str, Any]] = []
        worker_idx = 0

        for source in active[:max_assignments]:
            if worker_idx >= len(workers):
                break

            worker = workers[worker_idx]
            worker_idx += 1

            assignment = {
                "worker": worker,
                "source_id": source.source_id,
                "file_path": source.file_path,
                "category": source.category,
                "detail": source.detail,
                "quality": source.effective_quality(),
                "dispatched": False,
            }

            if not dry_run:
                bee = WorkerBee(worker)
                ok = bee.execute(source)
                assignment["dispatched"] = ok
                if ok:
                    self._update_metric("total_recruited")
                    self._save()

            assignments.append(assignment)

        return assignments
    # signed: beta

    # ── Abandon Phase ──────────────────────────────────────────────

    def abandon_phase(self) -> List[FoodSource]:
        """Abandon food sources that have been over-exploited.

        Sources with exploitation_count >= abandonment_threshold
        are marked 'abandoned' and removed from the active pool.

        Returns:
            List of abandoned sources.
        """
        abandoned: List[FoodSource] = []
        for sid, source in list(self._sources.items()):
            if source.should_abandon():
                source.status = "abandoned"
                abandoned.append(source)
                del self._sources[sid]

        if abandoned:
            self._update_metric("total_abandoned", len(abandoned))
            self._save()
            logger.info("Abandon phase: %d sources abandoned", len(abandoned))

        return abandoned
    # signed: beta

    # ── Full Cycle ─────────────────────────────────────────────────

    def run_cycle(
        self,
        workers: Optional[List[str]] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Run a complete ABC cycle: scout → dance → recruit → abandon.

        Returns:
            Summary dict with counts and assignments.
        """
        # 1. Scout
        new_sources = self.scout_phase()

        # 2. Dance
        dances = self.dance_phase(available_workers=len(workers or WORKER_NAMES))

        # 3. Recruit
        assignments = self.recruit_phase(
            workers=workers, dry_run=dry_run
        )

        # 4. Abandon
        abandoned = self.abandon_phase()

        return {
            "new_sources": len(new_sources),
            "dances": len(dances),
            "assignments": [a for a in assignments],
            "abandoned": len(abandoned),
            "active_sources": sum(
                1 for s in self._sources.values() if s.status == "active"
            ),
            "total_sources": len(self._sources),
        }
    # signed: beta

    # ── Completion Tracking ────────────────────────────────────────

    def mark_completed(self, source_id: str, worker: str = "") -> bool:
        """Mark a food source as completed (improvement applied).

        Args:
            source_id: The source to mark.
            worker:    Worker that completed it.

        Returns:
            True if source was found and marked, False otherwise.
        """
        source = self._sources.get(source_id)
        if not source:
            return False
        source.status = "completed"
        source.assigned_to = worker or source.assigned_to
        self._update_metric("total_completed")
        self._save()
        return True
    # signed: beta

    # ── Status & Queries ───────────────────────────────────────────

    def get_active_sources(self) -> List[FoodSource]:
        """Return all active food sources sorted by quality."""
        active = [s for s in self._sources.values() if s.status == "active"]
        active.sort(key=lambda s: s.effective_quality(), reverse=True)
        return active

    def get_source(self, source_id: str) -> Optional[FoodSource]:
        """Look up a food source by ID."""
        return self._sources.get(source_id)

    def get_metrics(self) -> Dict[str, Any]:
        """Return hive metrics."""
        with _lock:
            state = _load_hive_state()
            return state.get("metrics", {})

    def status(self) -> Dict[str, Any]:
        """Full hive status report."""
        active = [s for s in self._sources.values() if s.status == "active"]
        assigned = [s for s in self._sources.values() if s.status == "assigned"]

        by_category: Dict[str, int] = {}
        for s in self._sources.values():
            by_category[s.category] = by_category.get(s.category, 0) + 1

        top_sources = sorted(active, key=lambda s: s.effective_quality(),
                             reverse=True)[:10]

        return {
            "total_sources": len(self._sources),
            "active": len(active),
            "assigned": len(assigned),
            "by_category": by_category,
            "metrics": self.get_metrics(),
            "top_sources": [
                {
                    "id": s.source_id[:8],
                    "file": s.file_path,
                    "category": s.category,
                    "quality": s.effective_quality(),
                    "exploitations": s.exploitation_count,
                }
                for s in top_sources
            ],
        }
    # signed: beta


# ── Module-Level Convenience Functions ─────────────────────────────
# signed: beta

_default_hive: Optional[Hive] = None
_hive_lock = threading.Lock()


def _get_hive() -> Hive:
    """Get or create the singleton Hive instance."""
    global _default_hive
    with _hive_lock:
        if _default_hive is None:
            _default_hive = Hive()
        return _default_hive


def bee_scout(scan_dirs: Optional[List[str]] = None) -> List[FoodSource]:
    """Run scout phase and return newly discovered food sources."""
    hive = _get_hive()
    if scan_dirs:
        hive.scout = ScoutBee(scan_dirs=scan_dirs)
    return hive.scout_phase()


def bee_recruit(
    workers: Optional[List[str]] = None,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Run recruit phase — assign workers to best food sources."""
    hive = _get_hive()
    dances = hive.dance_phase(available_workers=len(workers or WORKER_NAMES))
    return hive.recruit_phase(workers=workers, dry_run=dry_run)


def bee_status() -> Dict[str, Any]:
    """Get full hive status report."""
    return _get_hive().status()


def bee_cycle(
    workers: Optional[List[str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run a complete scout → dance → recruit → abandon cycle."""
    return _get_hive().run_cycle(workers=workers, dry_run=dry_run)


# ── CLI ────────────────────────────────────────────────────────────
# signed: beta

def _cli():
    parser = argparse.ArgumentParser(
        description="Skynet Bee Algorithm — Scout+Recruitment for Codebase Improvement",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python tools/skynet_bee.py scout
    python tools/skynet_bee.py scout --dirs tools core
    python tools/skynet_bee.py recruit
    python tools/skynet_bee.py recruit --dry-run
    python tools/skynet_bee.py status
    python tools/skynet_bee.py dance-log
    python tools/skynet_bee.py cycle --dry-run
""",
    )
    sub = parser.add_subparsers(dest="command")

    # scout
    scout_p = sub.add_parser("scout", help="Run scout bees to find improvements")
    scout_p.add_argument("--dirs", nargs="*", default=None,
                         help="Directories to scan (default: tools core tests)")
    scout_p.add_argument("--json", action="store_true", help="JSON output")

    # recruit
    recruit_p = sub.add_parser("recruit", help="Recruit workers to best sources")
    recruit_p.add_argument("--workers", nargs="*", default=None,
                           help="Worker names (default: all)")
    recruit_p.add_argument("--dry-run", action="store_true",
                           help="Plan only, no dispatch")
    recruit_p.add_argument("--json", action="store_true", help="JSON output")

    # status
    status_p = sub.add_parser("status", help="Show hive status")
    status_p.add_argument("--json", action="store_true", help="JSON output")

    # dance-log
    dance_p = sub.add_parser("dance-log", help="Show recent dance records")
    dance_p.add_argument("--limit", type=int, default=20,
                         help="Number of records to show")
    dance_p.add_argument("--json", action="store_true", help="JSON output")

    # cycle
    cycle_p = sub.add_parser("cycle", help="Run full ABC cycle")
    cycle_p.add_argument("--workers", nargs="*", default=None)
    cycle_p.add_argument("--dry-run", action="store_true")
    cycle_p.add_argument("--json", action="store_true", help="JSON output")

    args = parser.parse_args()

    if args.command == "scout":
        sources = bee_scout(scan_dirs=args.dirs)
        if getattr(args, "json", False):
            print(json.dumps([s.to_dict() for s in sources], indent=2))
        else:
            print(f"Scout Phase: {len(sources)} new food sources discovered")
            print("=" * 60)
            for s in sorted(sources, key=lambda x: x.quality, reverse=True)[:20]:
                print(f"  [{s.severity:>8}] {s.category:<20} "
                      f"q={s.quality:.1f}  {s.file_path}:{s.line}")
                print(f"           {s.detail[:80]}")

    elif args.command == "recruit":
        assignments = bee_recruit(
            workers=args.workers,
            dry_run=args.dry_run,
        )
        if getattr(args, "json", False):
            print(json.dumps(assignments, indent=2))
        else:
            mode = "DRY RUN" if args.dry_run else "LIVE"
            print(f"Recruit Phase ({mode}): {len(assignments)} assignments")
            print("=" * 60)
            for a in assignments:
                status = "DISPATCHED" if a["dispatched"] else "PLANNED"
                print(f"  {a['worker']:<8} -> {a['file_path']:<40} "
                      f"q={a['quality']:.1f} [{status}]")

    elif args.command == "status":
        st = bee_status()
        if getattr(args, "json", False):
            print(json.dumps(st, indent=2))
        else:
            print("Hive Status")
            print("=" * 60)
            print(f"  Total sources:  {st['total_sources']}")
            print(f"  Active:         {st['active']}")
            print(f"  Assigned:       {st['assigned']}")
            print()
            if st.get("by_category"):
                print("  By Category:")
                for cat, cnt in sorted(st["by_category"].items()):
                    print(f"    {cat:<25} {cnt}")
            print()
            m = st.get("metrics", {})
            if m:
                print("  Metrics:")
                for k, v in sorted(m.items()):
                    label = k.replace("_", " ").title()
                    print(f"    {label:<30} {v}")
            print()
            if st.get("top_sources"):
                print("  Top Sources:")
                for t in st["top_sources"][:10]:
                    print(f"    [{t['id']}] {t['file']:<40} "
                          f"q={t['quality']:.1f} exploited={t['exploitations']}")

    elif args.command == "dance-log":
        limit = getattr(args, "limit", 20)
        records = DanceCommunication.load_dance_log(limit=limit)
        if getattr(args, "json", False):
            print(json.dumps(records, indent=2))
        else:
            print(f"Dance Log (last {len(records)} records)")
            print("=" * 60)
            for r in records:
                print(f"  [{r.get('timestamp', '?')}] scout={r.get('scout', '?'):<15} "
                      f"q={r.get('quality', 0):.1f} recruited={r.get('recruited_count', 0)} "
                      f"{r.get('file_path', '?')}")

    elif args.command == "cycle":
        result = bee_cycle(
            workers=args.workers,
            dry_run=args.dry_run,
        )
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print("Full ABC Cycle Complete")
            print("=" * 60)
            print(f"  New sources:    {result['new_sources']}")
            print(f"  Dances:         {result['dances']}")
            print(f"  Assignments:    {len(result['assignments'])}")
            print(f"  Abandoned:      {result['abandoned']}")
            print(f"  Active sources: {result['active_sources']}")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: beta
