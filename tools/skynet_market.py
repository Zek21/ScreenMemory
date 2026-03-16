"""Skynet Market-Based Task Coordination — auction-driven task allocation.

Workers bid on tasks based on capability and load.  The MarketMaker runs
auctions (first-price, Vickrey second-price, Dutch descending) and awards
tasks to the best bidder.  Worker reputation from ``worker_scores.json``
influences bid credibility.

Usage:
    python tools/skynet_market.py list
    python tools/skynet_market.py post "Refactor auth module" --min-cap 0.5
    python tools/skynet_market.py bid LISTING_ID --worker alpha --price 3.0
    python tools/skynet_market.py award LISTING_ID
    python tools/skynet_market.py complete LISTING_ID --success
    python tools/skynet_market.py stats
    python tools/skynet_market.py history --limit 20

# signed: delta
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Paths ──────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
MARKET_FILE = DATA_DIR / "task_market.json"
SCORES_FILE = DATA_DIR / "worker_scores.json"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

_lock = threading.Lock()


# ── Enums ──────────────────────────────────────────────────────────

class AuctionType(str, Enum):
    FIRST_PRICE = "first_price"   # highest bidder wins, pays own bid
    VICKREY = "vickrey"           # highest bidder wins, pays second-highest
    DUTCH = "dutch"               # descending price, first to accept wins
    # signed: delta


class ListingStatus(str, Enum):
    OPEN = "OPEN"
    AWARDED = "AWARDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    # signed: delta


# ── Data structures ───────────────────────────────────────────────

@dataclass
class Bid:
    """A worker's bid on a task listing.

    Attributes:
        worker:          Worker name.
        price:           Estimated effort (lower = cheaper for requester).
        capability_score: Self-assessed capability for this task (0.0-1.0).
        delivery_time:   Estimated seconds to complete.
        reputation:      Worker's reputation score (from worker_scores.json).
        submitted_at:    Timestamp of bid submission.
    # signed: delta
    """
    worker: str
    price: float
    capability_score: float = 0.5
    delivery_time: float = 120.0
    reputation: float = 0.0
    submitted_at: str = ""

    def __post_init__(self):
        if not self.submitted_at:
            self.submitted_at = _now()

    def to_dict(self) -> dict:
        return {
            "worker": self.worker,
            "price": self.price,
            "capability_score": self.capability_score,
            "delivery_time": self.delivery_time,
            "reputation": self.reputation,
            "submitted_at": self.submitted_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Bid":
        return cls(
            worker=d["worker"],
            price=d.get("price", 1.0),
            capability_score=d.get("capability_score", 0.5),
            delivery_time=d.get("delivery_time", 120.0),
            reputation=d.get("reputation", 0.0),
            submitted_at=d.get("submitted_at", ""),
        )

    @property
    def composite_score(self) -> float:
        """Combined score: capability and reputation boost, price discount.

        Higher is better for the bidder.  The market maker maximises this
        when selecting a winner.
        """
        rep_factor = 1.0 + max(0.0, self.reputation) * 0.05
        return (self.capability_score * rep_factor) / max(self.price, 0.01)
    # signed: delta


@dataclass
class TaskListing:
    """A task posted to the market for bidding.

    Attributes:
        listing_id:      Unique identifier.
        description:     Human-readable task description.
        auction_type:    Auction mechanism to use.
        min_capability:  Minimum capability score to bid.
        deadline:        Seconds from creation until listing expires.
        reserve_price:   Maximum price the requester will pay.
        requester:       Who posted the listing.
        status:          Current lifecycle state.
        bids:            Collected bids.
        winner:          Winning worker name (after award).
        final_price:     Price actually paid (auction-dependent).
        created_at:      Creation timestamp.
        awarded_at:      Award timestamp.
        completed_at:    Completion timestamp.
        success:         Whether the winner completed successfully.
    # signed: delta
    """
    listing_id: str
    description: str
    auction_type: AuctionType = AuctionType.FIRST_PRICE
    min_capability: float = 0.0
    deadline: float = 300.0
    reserve_price: float = 10.0
    requester: str = "orchestrator"
    status: ListingStatus = ListingStatus.OPEN
    bids: List[dict] = field(default_factory=list)
    winner: Optional[str] = None
    final_price: Optional[float] = None
    created_at: str = ""
    awarded_at: Optional[str] = None
    completed_at: Optional[str] = None
    success: Optional[bool] = None

    def __post_init__(self):
        if not self.created_at:
            self.created_at = _now()

    def to_dict(self) -> dict:
        return {
            "listing_id": self.listing_id,
            "description": self.description,
            "auction_type": self.auction_type.value,
            "min_capability": self.min_capability,
            "deadline": self.deadline,
            "reserve_price": self.reserve_price,
            "requester": self.requester,
            "status": self.status.value,
            "bids": self.bids,
            "winner": self.winner,
            "final_price": self.final_price,
            "created_at": self.created_at,
            "awarded_at": self.awarded_at,
            "completed_at": self.completed_at,
            "success": self.success,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskListing":
        return cls(
            listing_id=d["listing_id"],
            description=d.get("description", ""),
            auction_type=AuctionType(d.get("auction_type", "first_price")),
            min_capability=d.get("min_capability", 0.0),
            deadline=d.get("deadline", 300.0),
            reserve_price=d.get("reserve_price", 10.0),
            requester=d.get("requester", "orchestrator"),
            status=ListingStatus(d.get("status", "OPEN")),
            bids=d.get("bids", []),
            winner=d.get("winner"),
            final_price=d.get("final_price"),
            created_at=d.get("created_at", ""),
            awarded_at=d.get("awarded_at"),
            completed_at=d.get("completed_at"),
            success=d.get("success"),
        )

    @property
    def is_expired(self) -> bool:
        try:
            created = time.mktime(time.strptime(self.created_at,
                                                 "%Y-%m-%dT%H:%M:%S"))
            return (time.time() - created) > self.deadline
        except (ValueError, OverflowError):
            return False
    # signed: delta


# ── State persistence ─────────────────────────────────────────────

def _load_market() -> Dict[str, Any]:
    if MARKET_FILE.exists():
        try:
            with open(MARKET_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "listings": {},
        "history": [],
        "analytics": {
            "total_listed": 0,
            "total_awarded": 0,
            "total_completed": 0,
            "total_failed": 0,
            "total_bids": 0,
            "avg_bids_per_listing": 0.0,
            "avg_final_price": 0.0,
        },
        "worker_stats": {},
        "updated_at": "",
    }


def _save_market(state: Dict[str, Any]) -> None:
    state["updated_at"] = _now()
    tmp = MARKET_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(str(tmp), str(MARKET_FILE))
    # signed: delta


def _get_reputation(worker: str) -> float:
    """Read worker reputation score from worker_scores.json."""
    if not SCORES_FILE.exists():
        return 0.0
    try:
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        scores = data.get("scores", {})
        entry = scores.get(worker, {})
        return float(entry.get("total", 0.0))
    except (json.JSONDecodeError, OSError, TypeError):
        return 0.0
    # signed: delta


# ── MarketMaker ───────────────────────────────────────────────────

class MarketMaker:
    """Manages the task market: post listings, collect bids, run auctions.

    The MarketMaker is the central coordinator.  It posts task listings,
    accepts bids from workers, runs the configured auction type to
    determine winners, and tracks completion.

    Example::

        mm = MarketMaker()
        lid = mm.post_listing("Refactor auth", min_capability=0.6)
        mm.submit_bid(lid, "alpha", price=2.0, capability=0.8)
        mm.submit_bid(lid, "beta",  price=3.0, capability=0.9)
        result = mm.award(lid)
        # result["winner"] = "beta" (highest composite score)
    # signed: delta
    """

    def __init__(self):
        pass

    @staticmethod
    def _gen_id(desc: str) -> str:
        ts = time.strftime("%Y%m%d_%H%M%S")
        h = hashlib.sha256(f"{desc}{time.time()}".encode()).hexdigest()[:8]
        return f"mkt_{ts}_{h}"

    # ── Listing management ────────────────────────────────────────

    def post_listing(
        self,
        description: str,
        auction_type: str = "first_price",
        min_capability: float = 0.0,
        deadline: float = 300.0,
        reserve_price: float = 10.0,
        requester: str = "orchestrator",
    ) -> str:
        """Post a new task listing to the market.

        Returns:
            The listing_id.
        """
        lid = self._gen_id(description)
        listing = TaskListing(
            listing_id=lid,
            description=description,
            auction_type=AuctionType(auction_type),
            min_capability=min_capability,
            deadline=deadline,
            reserve_price=reserve_price,
            requester=requester,
        )

        with _lock:
            state = _load_market()
            state["listings"][lid] = listing.to_dict()
            state["analytics"]["total_listed"] = (
                state["analytics"].get("total_listed", 0) + 1
            )
            _save_market(state)

        return lid
        # signed: delta

    def submit_bid(
        self,
        listing_id: str,
        worker: str,
        price: float,
        capability_score: float = 0.5,
        delivery_time: float = 120.0,
    ) -> Dict[str, Any]:
        """Submit a bid on a listing.

        Returns:
            Dict with bid details and acceptance status.

        Raises:
            ValueError: If listing not found, not open, or capability too low.
        """
        reputation = _get_reputation(worker)

        bid = Bid(
            worker=worker,
            price=price,
            capability_score=capability_score,
            delivery_time=delivery_time,
            reputation=reputation,
        )

        with _lock:
            state = _load_market()
            ld = state["listings"].get(listing_id)
            if not ld:
                raise ValueError(f"Listing '{listing_id}' not found")
            listing = TaskListing.from_dict(ld)

            if listing.status != ListingStatus.OPEN:
                raise ValueError(f"Listing is {listing.status.value}, not OPEN")

            if listing.is_expired:
                listing.status = ListingStatus.EXPIRED
                state["listings"][listing_id] = listing.to_dict()
                _save_market(state)
                raise ValueError("Listing has expired")

            if capability_score < listing.min_capability:
                raise ValueError(
                    f"Capability {capability_score} below minimum "
                    f"{listing.min_capability}"
                )

            # Prevent duplicate bids from same worker
            for existing in listing.bids:
                if existing.get("worker") == worker:
                    raise ValueError(f"Worker '{worker}' already bid")

            listing.bids.append(bid.to_dict())
            state["listings"][listing_id] = listing.to_dict()
            state["analytics"]["total_bids"] = (
                state["analytics"].get("total_bids", 0) + 1
            )

            # Update per-worker stats
            ws = state.setdefault("worker_stats", {})
            ws.setdefault(worker, {"bids": 0, "wins": 0, "completions": 0,
                                    "failures": 0, "total_spent": 0.0})
            ws[worker]["bids"] = ws[worker].get("bids", 0) + 1

            _save_market(state)

        return {
            "listing_id": listing_id,
            "worker": worker,
            "price": price,
            "capability_score": capability_score,
            "composite_score": bid.composite_score,
            "reputation": reputation,
            "accepted": True,
        }
        # signed: delta

    def award(self, listing_id: str) -> Dict[str, Any]:
        """Run the auction and award the task to the winning bidder.

        Returns:
            Dict with winner, final_price, auction_type, and all bids.

        Raises:
            ValueError: If listing not found, not open, or no bids.
        """
        with _lock:
            state = _load_market()
            ld = state["listings"].get(listing_id)
            if not ld:
                raise ValueError(f"Listing '{listing_id}' not found")
            listing = TaskListing.from_dict(ld)

            if listing.status != ListingStatus.OPEN:
                raise ValueError(f"Listing is {listing.status.value}")

            if not listing.bids:
                raise ValueError("No bids submitted")

            bids = [Bid.from_dict(b) for b in listing.bids]
            winner, final_price = self._run_auction(
                listing.auction_type, bids, listing.reserve_price
            )

            listing.winner = winner.worker
            listing.final_price = final_price
            listing.status = ListingStatus.AWARDED
            listing.awarded_at = _now()
            state["listings"][listing_id] = listing.to_dict()

            state["analytics"]["total_awarded"] = (
                state["analytics"].get("total_awarded", 0) + 1
            )

            # Update winner stats
            ws = state.setdefault("worker_stats", {})
            ws.setdefault(winner.worker, {"bids": 0, "wins": 0,
                                           "completions": 0, "failures": 0,
                                           "total_spent": 0.0})
            ws[winner.worker]["wins"] = ws[winner.worker].get("wins", 0) + 1

            # Update running averages
            awarded = state["analytics"]["total_awarded"]
            old_avg = state["analytics"].get("avg_final_price", 0.0)
            state["analytics"]["avg_final_price"] = round(
                old_avg + (final_price - old_avg) / awarded, 4
            )

            total_bids = state["analytics"].get("total_bids", 0)
            total_listed = state["analytics"].get("total_listed", 1)
            state["analytics"]["avg_bids_per_listing"] = round(
                total_bids / max(total_listed, 1), 2
            )

            _save_market(state)

        return {
            "listing_id": listing_id,
            "auction_type": listing.auction_type.value,
            "winner": winner.worker,
            "final_price": final_price,
            "winner_bid_price": winner.price,
            "winner_capability": winner.capability_score,
            "winner_reputation": winner.reputation,
            "winner_composite": winner.composite_score,
            "total_bids": len(bids),
            "all_bids": [b.to_dict() for b in bids],
        }
        # signed: delta

    def complete_task(self, listing_id: str, success: bool = True
                      ) -> Dict[str, Any]:
        """Record task completion (or failure) after award.

        Returns:
            Updated listing summary.
        """
        with _lock:
            state = _load_market()
            ld = state["listings"].get(listing_id)
            if not ld:
                raise ValueError(f"Listing '{listing_id}' not found")
            listing = TaskListing.from_dict(ld)

            if listing.status != ListingStatus.AWARDED:
                raise ValueError(f"Listing is {listing.status.value}, not AWARDED")

            listing.status = (ListingStatus.COMPLETED if success
                              else ListingStatus.FAILED)
            listing.completed_at = _now()
            listing.success = success
            state["listings"][listing_id] = listing.to_dict()

            key = "total_completed" if success else "total_failed"
            state["analytics"][key] = state["analytics"].get(key, 0) + 1

            # Update worker stats
            ws = state.setdefault("worker_stats", {})
            if listing.winner:
                ws.setdefault(listing.winner, {"bids": 0, "wins": 0,
                                                "completions": 0, "failures": 0,
                                                "total_spent": 0.0})
                if success:
                    ws[listing.winner]["completions"] = (
                        ws[listing.winner].get("completions", 0) + 1
                    )
                else:
                    ws[listing.winner]["failures"] = (
                        ws[listing.winner].get("failures", 0) + 1
                    )
                ws[listing.winner]["total_spent"] = round(
                    ws[listing.winner].get("total_spent", 0.0)
                    + (listing.final_price or 0), 4
                )

            # Archive to history
            state.setdefault("history", []).append({
                "listing_id": listing_id,
                "description": listing.description[:100],
                "auction_type": listing.auction_type.value,
                "winner": listing.winner,
                "final_price": listing.final_price,
                "success": success,
                "bids_count": len(listing.bids),
                "completed_at": listing.completed_at,
            })
            if len(state["history"]) > 500:
                state["history"] = state["history"][-500:]

            _save_market(state)

        return {
            "listing_id": listing_id,
            "winner": listing.winner,
            "success": success,
            "final_price": listing.final_price,
            "status": listing.status.value,
        }
        # signed: delta

    def cancel_listing(self, listing_id: str) -> None:
        """Cancel an open listing."""
        with _lock:
            state = _load_market()
            ld = state["listings"].get(listing_id)
            if not ld:
                raise ValueError(f"Listing '{listing_id}' not found")
            listing = TaskListing.from_dict(ld)
            if listing.status != ListingStatus.OPEN:
                raise ValueError(f"Cannot cancel: status is {listing.status.value}")
            listing.status = ListingStatus.CANCELLED
            state["listings"][listing_id] = listing.to_dict()
            _save_market(state)
        # signed: delta

    # ── Auction algorithms ────────────────────────────────────────

    @staticmethod
    def _run_auction(
        auction_type: AuctionType,
        bids: List[Bid],
        reserve_price: float,
    ) -> tuple:
        """Run the auction and return (winning_bid, final_price).

        Args:
            auction_type: Which auction mechanism to use.
            bids:         List of Bid objects.
            reserve_price: Maximum acceptable price.

        Returns:
            Tuple of (winning Bid, final price to pay).
        """
        # Filter bids within reserve price
        eligible = [b for b in bids if b.price <= reserve_price]
        if not eligible:
            eligible = bids  # fallback: allow all if none under reserve

        if auction_type == AuctionType.FIRST_PRICE:
            return MarketMaker._auction_first_price(eligible)
        elif auction_type == AuctionType.VICKREY:
            return MarketMaker._auction_vickrey(eligible)
        elif auction_type == AuctionType.DUTCH:
            return MarketMaker._auction_dutch(eligible)
        else:
            return MarketMaker._auction_first_price(eligible)
        # signed: delta

    @staticmethod
    def _auction_first_price(bids: List[Bid]) -> tuple:
        """Highest composite score wins, pays own bid price."""
        ranked = sorted(bids, key=lambda b: b.composite_score, reverse=True)
        winner = ranked[0]
        return winner, winner.price

    @staticmethod
    def _auction_vickrey(bids: List[Bid]) -> tuple:
        """Highest composite score wins, pays second-highest price.

        The Vickrey (second-price sealed-bid) auction incentivises
        truthful bidding because the winner pays the price of the
        runner-up, not their own bid.
        """
        ranked = sorted(bids, key=lambda b: b.composite_score, reverse=True)
        winner = ranked[0]
        if len(ranked) >= 2:
            final_price = ranked[1].price
        else:
            final_price = winner.price  # solo bid pays own price
        return winner, final_price

    @staticmethod
    def _auction_dutch(bids: List[Bid]) -> tuple:
        """First bidder with lowest price wins (descending price auction).

        In a Dutch auction the price starts high and descends.  The first
        bidder to accept wins.  We simulate this by selecting the bid
        with the lowest price (earliest acceptor in a descending clock).
        Ties broken by submission time.
        """
        ranked = sorted(bids, key=lambda b: (b.price, b.submitted_at))
        winner = ranked[0]
        return winner, winner.price
    # signed: delta

    # ── Queries ───────────────────────────────────────────────────

    @staticmethod
    def get_open_listings() -> List[Dict]:
        """Return all OPEN listings."""
        with _lock:
            state = _load_market()
        result = []
        for ld in state.get("listings", {}).values():
            if ld.get("status") == "OPEN":
                listing = TaskListing.from_dict(ld)
                if listing.is_expired:
                    continue
                result.append(ld)
        return result
        # signed: delta

    @staticmethod
    def get_listing(listing_id: str) -> Dict:
        with _lock:
            state = _load_market()
        ld = state.get("listings", {}).get(listing_id)
        if not ld:
            raise ValueError(f"Listing '{listing_id}' not found")
        return ld

    @staticmethod
    def get_analytics() -> Dict[str, Any]:
        """Return market analytics and per-worker stats."""
        with _lock:
            state = _load_market()
        analytics = dict(state.get("analytics", {}))

        # Compute derived metrics
        ws = state.get("worker_stats", {})
        worker_metrics = {}
        for worker, stats in ws.items():
            wins = stats.get("wins", 0)
            bids_count = stats.get("bids", 0)
            completions = stats.get("completions", 0)
            failures = stats.get("failures", 0)
            total_tasks = completions + failures

            worker_metrics[worker] = {
                "bids": bids_count,
                "wins": wins,
                "win_rate": round(wins / max(bids_count, 1), 4),
                "completions": completions,
                "failures": failures,
                "completion_rate": round(
                    completions / max(total_tasks, 1), 4
                ),
                "total_spent": stats.get("total_spent", 0.0),
                "reputation": _get_reputation(worker),
            }

        analytics["worker_metrics"] = worker_metrics

        # Price trends from history
        history = state.get("history", [])
        if history:
            prices = [h["final_price"] for h in history
                      if h.get("final_price") is not None]
            if prices:
                analytics["price_trends"] = {
                    "min": min(prices),
                    "max": max(prices),
                    "avg": round(sum(prices) / len(prices), 4),
                    "recent_5": prices[-5:],
                }

        return analytics
        # signed: delta

    @staticmethod
    def get_history(limit: int = 20) -> List[Dict]:
        with _lock:
            state = _load_market()
        return state.get("history", [])[-limit:]
        # signed: delta


# ── Convenience functions ─────────────────────────────────────────

def post_task(description: str, **kwargs) -> str:
    """Convenience: post a task listing."""
    return MarketMaker().post_listing(description, **kwargs)


def bid_on_task(listing_id: str, worker: str, price: float,
                capability: float = 0.5, delivery_time: float = 120.0
                ) -> Dict:
    """Convenience: submit a bid."""
    return MarketMaker().submit_bid(listing_id, worker, price,
                                     capability, delivery_time)


def award_task(listing_id: str) -> Dict:
    """Convenience: run auction and award."""
    return MarketMaker().award(listing_id)


def complete(listing_id: str, success: bool = True) -> Dict:
    """Convenience: mark task completed."""
    return MarketMaker().complete_task(listing_id, success)
    # signed: delta


# ── Helpers ───────────────────────────────────────────────────────

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ── CLI ───────────────────────────────────────────────────────────

def _cli():
    parser = argparse.ArgumentParser(
        description="Skynet Market-Based Task Coordination",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Auction types:
  first_price  Highest composite score wins, pays own bid
  vickrey      Highest composite score wins, pays second-highest price
  dutch        Lowest price wins (descending clock simulation)

Examples:
  python tools/skynet_market.py post "Refactor auth" --auction vickrey --min-cap 0.6
  python tools/skynet_market.py list
  python tools/skynet_market.py bid LISTING_ID --worker alpha --price 2.0 --cap 0.8
  python tools/skynet_market.py award LISTING_ID
  python tools/skynet_market.py complete LISTING_ID --success
  python tools/skynet_market.py stats
  python tools/skynet_market.py history
""",
    )
    sub = parser.add_subparsers(dest="command")

    # post
    post_p = sub.add_parser("post", help="Post a new task listing")
    post_p.add_argument("description", help="Task description")
    post_p.add_argument("--auction", default="first_price",
                        choices=["first_price", "vickrey", "dutch"])
    post_p.add_argument("--min-cap", type=float, default=0.0,
                        help="Minimum capability score")
    post_p.add_argument("--deadline", type=float, default=300.0,
                        help="Seconds until listing expires")
    post_p.add_argument("--reserve", type=float, default=10.0,
                        help="Maximum acceptable price")
    post_p.add_argument("--requester", default="orchestrator")

    # list
    sub.add_parser("list", help="Show open listings")

    # bid
    bid_p = sub.add_parser("bid", help="Submit a bid on a listing")
    bid_p.add_argument("listing_id")
    bid_p.add_argument("--worker", required=True)
    bid_p.add_argument("--price", type=float, required=True)
    bid_p.add_argument("--cap", type=float, default=0.5,
                       help="Capability score (0.0-1.0)")
    bid_p.add_argument("--time", type=float, default=120.0,
                       help="Delivery time estimate (seconds)")

    # award
    award_p = sub.add_parser("award", help="Run auction and award task")
    award_p.add_argument("listing_id")

    # complete
    comp_p = sub.add_parser("complete", help="Mark task completed or failed")
    comp_p.add_argument("listing_id")
    comp_p.add_argument("--success", action="store_true", default=True)
    comp_p.add_argument("--failed", action="store_true")

    # cancel
    canc_p = sub.add_parser("cancel", help="Cancel an open listing")
    canc_p.add_argument("listing_id")

    # stats
    sub.add_parser("stats", help="Show market analytics")

    # history
    hist_p = sub.add_parser("history", help="Show completed task history")
    hist_p.add_argument("--limit", type=int, default=20)

    # show
    show_p = sub.add_parser("show", help="Show a specific listing")
    show_p.add_argument("listing_id")

    args = parser.parse_args()
    mm = MarketMaker()

    if args.command == "post":
        lid = mm.post_listing(
            args.description,
            auction_type=args.auction,
            min_capability=args.min_cap,
            deadline=args.deadline,
            reserve_price=args.reserve,
            requester=args.requester,
        )
        print(f"Listed: {lid}")

    elif args.command == "list":
        listings = mm.get_open_listings()
        if not listings:
            print("No open listings.")
            return
        print(f"{'ID':<45} {'Auction':<13} {'Bids':>4}  "
              f"{'Min Cap':>7}  {'Reserve':>7}  Description")
        print("-" * 110)
        for ld in listings:
            print(f"{ld['listing_id']:<45} {ld['auction_type']:<13} "
                  f"{len(ld.get('bids', [])):>4}  "
                  f"{ld.get('min_capability', 0):>7.2f}  "
                  f"{ld.get('reserve_price', 0):>7.2f}  "
                  f"{ld['description'][:40]}")

    elif args.command == "bid":
        result = mm.submit_bid(args.listing_id, args.worker, args.price,
                                args.cap, args.time)
        print(f"Bid accepted: worker={result['worker']} price={result['price']} "
              f"composite={result['composite_score']:.4f} "
              f"reputation={result['reputation']:.2f}")

    elif args.command == "award":
        result = mm.award(args.listing_id)
        print(f"AWARDED to {result['winner']}")
        print(f"  Auction: {result['auction_type']}")
        print(f"  Final price: {result['final_price']}")
        print(f"  Bid price: {result['winner_bid_price']}")
        print(f"  Composite: {result['winner_composite']:.4f}")
        print(f"  Total bids: {result['total_bids']}")

    elif args.command == "complete":
        success = not args.failed
        result = mm.complete_task(args.listing_id, success=success)
        print(f"Task {result['status']}: winner={result['winner']} "
              f"price={result['final_price']}")

    elif args.command == "cancel":
        mm.cancel_listing(args.listing_id)
        print(f"Listing {args.listing_id} cancelled.")

    elif args.command == "stats":
        analytics = mm.get_analytics()
        print("Market Analytics")
        print("=" * 50)
        for k in ["total_listed", "total_awarded", "total_completed",
                   "total_failed", "total_bids", "avg_bids_per_listing",
                   "avg_final_price"]:
            v = analytics.get(k, 0)
            label = k.replace("_", " ").title()
            print(f"  {label:<30} {v}")

        trends = analytics.get("price_trends")
        if trends:
            print(f"\nPrice Trends")
            print(f"  Min: {trends['min']}  Max: {trends['max']}  "
                  f"Avg: {trends['avg']}")
            print(f"  Recent: {trends['recent_5']}")

        wm = analytics.get("worker_metrics", {})
        if wm:
            print(f"\nWorker Performance")
            print(f"  {'Worker':<12} {'Bids':>5} {'Wins':>5} {'WinRate':>8} "
                  f"{'Done':>5} {'Fail':>5} {'CompRate':>9} {'Rep':>6}")
            print("  " + "-" * 62)
            for w, m in sorted(wm.items()):
                print(f"  {w:<12} {m['bids']:>5} {m['wins']:>5} "
                      f"{m['win_rate']:>8.1%} {m['completions']:>5} "
                      f"{m['failures']:>5} {m['completion_rate']:>9.1%} "
                      f"{m['reputation']:>6.2f}")

    elif args.command == "history":
        history = mm.get_history(limit=args.limit)
        if not history:
            print("No market history.")
            return
        for h in history:
            status = "OK" if h.get("success") else "FAIL"
            print(f"  {h.get('completed_at', '?')} | {status:<4} | "
                  f"winner={h.get('winner', '?'):<8} | "
                  f"price={h.get('final_price', 0):<6} | "
                  f"{h.get('auction_type', '?'):<12} | "
                  f"{h.get('description', '?')[:40]}")

    elif args.command == "show":
        ld = mm.get_listing(args.listing_id)
        print(json.dumps(ld, indent=2, default=str))

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: delta
