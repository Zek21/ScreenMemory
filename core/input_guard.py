"""
Input Guard — Prompt injection detection and input sanitization.

Implements the paper's real-time defensive architecture:
- Pattern-based detection for known injection vectors
- Semantic analysis for disguised attacks
- Input sanitization and neutralization
- Audit logging for forensic investigation

Designed as a lightweight filter that intercepts ALL untrusted inputs
before they reach the primary orchestration agent.
"""
import re
import time
import logging
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ThreatLevel(Enum):
    SAFE = "safe"
    SUSPICIOUS = "suspicious"
    DANGEROUS = "dangerous"
    BLOCKED = "blocked"


@dataclass
class ScanResult:
    """Result of input security scan."""
    threat_level: ThreatLevel
    score: float              # 0.0 = safe, 1.0 = definitely malicious
    triggers: List[str]       # Which patterns matched
    sanitized_input: str      # Cleaned version of input
    blocked: bool
    timestamp: float = field(default_factory=time.time)


class InputGuard:
    """
    Real-time defensive filter for prompt injection and data exfiltration.

    Three detection layers:
    1. Pattern matching — known injection templates
    2. Structural analysis — suspicious formatting
    3. Context violation — attempts to override system instructions
    """

    # Layer 1: Known injection patterns
    INJECTION_PATTERNS = [
        # Direct instruction override
        (re.compile(r'ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?|rules?|context)',
                     re.I), 0.95, "instruction_override"),
        (re.compile(r'disregard\s+(all\s+)?(previous|your)\s+(instructions?|programming|rules?)',
                     re.I), 0.95, "instruction_override"),
        (re.compile(r'forget\s+(everything|all|what)\s+(you|I)\s+(told|said|instructed)',
                     re.I), 0.90, "memory_wipe"),

        # Role hijacking
        (re.compile(r'you\s+are\s+now\s+(a|an|the)\s+', re.I), 0.70, "role_hijack"),
        (re.compile(r'act\s+as\s+(if\s+you\s+are|a|an)\s+', re.I), 0.50, "role_hijack"),
        (re.compile(r'pretend\s+(to\s+be|you\s+are)\s+', re.I), 0.60, "role_hijack"),
        (re.compile(r'from\s+now\s+on\s+you\s+(will|must|should)\s+', re.I), 0.80, "role_hijack"),

        # System prompt extraction
        (re.compile(r'(print|show|display|reveal|output|repeat)\s+(your|the)\s+(system\s+)?(prompt|instructions?|rules?)',
                     re.I), 0.90, "prompt_extraction"),
        (re.compile(r'what\s+(are|is)\s+your\s+(system\s+)?(prompt|instructions?|programming|rules?)',
                     re.I), 0.80, "prompt_extraction"),

        # Data exfiltration
        (re.compile(r'(send|post|upload|transmit|exfiltrate)\s+.*(to|via)\s+(http|ftp|email|webhook)',
                     re.I), 0.85, "data_exfil"),
        (re.compile(r'curl\s+.*(http|ftp)', re.I), 0.60, "data_exfil"),

        # Code injection
        (re.compile(r'```\s*(python|javascript|bash|powershell|cmd)\s*\n.*?(os\.|subprocess|exec|eval|system)\s*\(',
                     re.I | re.S), 0.75, "code_injection"),
        (re.compile(r'(import\s+os|import\s+subprocess|require\(.child_process.\))',
                     re.I), 0.50, "code_injection"),

        # Delimiter attacks
        (re.compile(r'<\|?(system|endoftext|im_start|im_end)\|?>', re.I), 0.95, "delimiter_attack"),
        (re.compile(r'\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>', re.I), 0.90, "delimiter_attack"),

        # Jailbreak markers
        (re.compile(r'(DAN|do\s+anything\s+now|developer\s+mode|god\s+mode)', re.I), 0.85, "jailbreak"),
        (re.compile(r'bypass\s+(your\s+)?(safety|filter|restriction|content\s+policy)',
                     re.I), 0.90, "jailbreak"),
    ]

    # Layer 2: Structural anomaly patterns
    STRUCTURAL_PATTERNS = [
        # Excessive special characters (obfuscation)
        (re.compile(r'[^\w\s]{10,}'), 0.40, "obfuscation"),
        # Unicode direction overrides (text camouflage)
        (re.compile(r'[\u200e\u200f\u202a-\u202e\u2066-\u2069]'), 0.70, "unicode_camouflage"),
        # Base64 encoded payloads
        (re.compile(r'[A-Za-z0-9+/]{50,}={0,2}'), 0.30, "base64_payload"),
        # Extremely long single words (hash/token stuffing)
        (re.compile(r'\b\w{100,}\b'), 0.40, "token_stuffing"),
    ]

    def __init__(self, block_threshold: float = 0.75,
                 warn_threshold: float = 0.40,
                 audit_all: bool = True):
        self.block_threshold = block_threshold
        self.warn_threshold = warn_threshold
        self.audit_all = audit_all
        self._audit_log: List[ScanResult] = []
        self._block_count = 0
        self._scan_count = 0

    def scan(self, input_text: str) -> ScanResult:
        """Scan input for injection attempts. Returns ScanResult."""
        t0 = time.perf_counter()
        self._scan_count += 1

        triggers = []
        max_score = 0.0

        # Layer 1: Known injection patterns
        for pattern, score, label in self.INJECTION_PATTERNS:
            if pattern.search(input_text):
                triggers.append(f"L1:{label}")
                max_score = max(max_score, score)

        # Layer 2: Structural anomalies
        for pattern, score, label in self.STRUCTURAL_PATTERNS:
            matches = pattern.findall(input_text)
            if matches:
                adjusted_score = min(1.0, score * (1 + len(matches) * 0.1))
                triggers.append(f"L2:{label}({len(matches)})")
                max_score = max(max_score, adjusted_score)

        # Layer 3: Context violation heuristics
        context_score = self._check_context_violations(input_text)
        if context_score > 0:
            triggers.append(f"L3:context_violation")
            max_score = max(max_score, context_score)

        # Determine threat level
        if max_score >= self.block_threshold:
            threat_level = ThreatLevel.BLOCKED
            blocked = True
            self._block_count += 1
        elif max_score >= self.warn_threshold:
            threat_level = ThreatLevel.SUSPICIOUS if max_score < 0.6 else ThreatLevel.DANGEROUS
            blocked = False
        else:
            threat_level = ThreatLevel.SAFE
            blocked = False

        # Sanitize input
        sanitized = self._sanitize(input_text) if triggers else input_text

        result = ScanResult(
            threat_level=threat_level,
            score=max_score,
            triggers=triggers,
            sanitized_input=sanitized,
            blocked=blocked,
        )

        # Audit log
        if self.audit_all or triggers:
            self._audit_log.append(result)

        elapsed = (time.perf_counter() - t0) * 1000
        if triggers:
            logger.warning(f"InputGuard: {threat_level.value} (score={max_score:.2f}) "
                           f"triggers={triggers} [{elapsed:.1f}ms]")
        else:
            logger.debug(f"InputGuard: SAFE [{elapsed:.1f}ms]")

        return result

    def _check_context_violations(self, text: str) -> float:
        """Check for attempts to manipulate conversation context."""
        score = 0.0

        # Excessive instruction-like formatting
        imperative_count = len(re.findall(
            r'^(you\s+must|you\s+will|you\s+should|always|never|do\s+not)\b',
            text, re.I | re.M
        ))
        if imperative_count > 3:
            score = max(score, 0.5 + imperative_count * 0.05)

        # Multiple persona switches in one input
        persona_switches = len(re.findall(
            r'(now\s+you\s+are|switch\s+to|change\s+to|become\s+a)',
            text, re.I
        ))
        if persona_switches > 1:
            score = max(score, 0.7)

        return min(1.0, score)

    def _sanitize(self, text: str) -> str:
        """Remove or neutralize detected threats while preserving intent."""
        sanitized = text

        # Remove delimiter attacks
        sanitized = re.sub(r'<\|?(system|endoftext|im_start|im_end)\|?>', '', sanitized)
        sanitized = re.sub(r'\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>', '', sanitized)

        # Remove unicode direction overrides
        sanitized = re.sub(r'[\u200e\u200f\u202a-\u202e\u2066-\u2069]', '', sanitized)

        # Neutralize instruction overrides (prefix with warning)
        sanitized = re.sub(
            r'ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?|rules?)',
            '[REDACTED: instruction override attempt]',
            sanitized, flags=re.I
        )

        return sanitized.strip()

    @property
    def stats(self) -> dict:
        return {
            "total_scans": self._scan_count,
            "blocks": self._block_count,
            "block_rate": round(self._block_count / max(1, self._scan_count), 3),
            "audit_log_size": len(self._audit_log),
        }

    def get_audit_log(self, limit: int = 50) -> List[dict]:
        """Return recent audit entries."""
        return [
            {
                "threat_level": r.threat_level.value,
                "score": r.score,
                "triggers": r.triggers,
                "blocked": r.blocked,
                "timestamp": r.timestamp,
            }
            for r in self._audit_log[-limit:]
        ]
