"""Episode verification scaffolding for Skynet.

Verifiers inspect episode results and classify them as success, failure,
or unknown.  Multiple verifiers can be registered; ``verify_episode()``
runs them all and returns a consensus outcome.
"""

import re
from abc import ABC, abstractmethod
from typing import Optional

from tools.skynet_episode import Outcome


class VerifierBase(ABC):
    """Base class for episode verifiers."""

    @abstractmethod
    def verify(self, episode: dict) -> Outcome:
        """Classify an episode's outcome.

        Args:
            episode: A dict with at least ``task``, ``result``, ``outcome`` keys.

        Returns:
            An ``Outcome`` value.
        """
        ...


# ---------------------------------------------------------------------------
# Built-in verifiers
# ---------------------------------------------------------------------------

_FAILURE_KEYWORDS = re.compile(
    r"traceback|exception|error|failed|failure|crash|fatal|"
    r"could not|unable to|broken|panic|abort",
    re.IGNORECASE,
)

_SUCCESS_KEYWORDS = re.compile(
    r"\bdone\b|\bsuccess\b|\bcomplete\b|\bpassed\b|\bok\b|\bfinished\b|\bdelivered\b",
    re.IGNORECASE,
)


class SimpleVerifier(VerifierBase):
    """Keyword-based heuristic verifier.

    Rules (evaluated in order):
        1. Result contains failure keywords → FAILURE.
        2. Result contains success keywords → SUCCESS.
        3. Otherwise → UNKNOWN.
    """

    def verify(self, episode: dict) -> Outcome:
        result = episode.get("result", "")
        if _FAILURE_KEYWORDS.search(result):
            return Outcome.FAILURE
        if _SUCCESS_KEYWORDS.search(result):
            return Outcome.SUCCESS
        return Outcome.UNKNOWN


# ---------------------------------------------------------------------------
# Registry & consensus
# ---------------------------------------------------------------------------

_VERIFIERS: list[VerifierBase] = []


def register_verifier(v: VerifierBase) -> None:
    """Add a verifier to the global registry."""
    _VERIFIERS.append(v)


def get_verifiers() -> list[VerifierBase]:
    """Return all registered verifiers."""
    return list(_VERIFIERS)


def clear_verifiers() -> None:
    """Remove all registered verifiers (useful for tests)."""
    _VERIFIERS.clear()


def _ensure_defaults() -> None:
    """Register default verifiers if the registry is empty."""
    if not _VERIFIERS:
        register_verifier(SimpleVerifier())


def verify_episode(episode: dict, verifiers: Optional[list[VerifierBase]] = None) -> Outcome:
    """Run all registered verifiers and return consensus outcome.

    Consensus rules:
        - If any verifier returns FAILURE → FAILURE (fail-fast).
        - If at least one returns SUCCESS and none FAILURE → SUCCESS.
        - Otherwise → UNKNOWN.

    Args:
        episode: The episode dict to verify.
        verifiers: Optional explicit list; if omitted uses global registry.

    Returns:
        Consensus ``Outcome``.
    """
    if verifiers is None:
        _ensure_defaults()
        verifiers = _VERIFIERS

    outcomes = [v.verify(episode) for v in verifiers]

    if not outcomes:
        return Outcome.UNKNOWN

    if Outcome.FAILURE in outcomes:
        return Outcome.FAILURE
    if Outcome.SUCCESS in outcomes:
        return Outcome.SUCCESS
    return Outcome.UNKNOWN


if __name__ == "__main__":
    sample = {"task": "test task", "result": "All tests passed. Done.", "outcome": "unknown"}
    _ensure_defaults()
    print(f"Verification: {verify_episode(sample).value}")
