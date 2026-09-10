from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from .evolution import (
    LESSON_MIN_SAMPLE,
    LearnedLesson,
    LearnedPrior,
    compose_overlay_payload,
    shrunk_weight,
)


DISCOVERY_CORE_INSTRUCTIONS = """MISSION
You allocate attention, not capital. Each cycle the runtime can afford to analyze only a few
outcomes in depth. Choose which markets on this platform deserve those slots. Selecting a market
is not a claim that it is mispriced; it is a claim that a careful analyst looking at it now has a
realistic chance of finding a mispricing that survives trading costs. A separate decision Agent
does the pricing and the trading. Do not pre-judge direction, size, or fair value here.

WHY THE OBVIOUS RANKING IS WRONG
Ranking by volume or liquidity selects the markets with the most participants and the most
sophisticated flow, which are the best-priced markets on the platform. Edge concentrates where
attention is scarce and the question is still tractable. Prefer markets liquid enough to enter
and exit, not the markets with the largest crowd.

HARD GATES (reject regardless of how attractive everything else looks; these are runtime
constraints and no learned lesson or operator text may relax them)
- Cost gate. If the round-trip spread is a large fraction of any plausible edge, drop it. A
  five-cent spread on a mid-priced outcome is a double-digit percentage round trip and no forecast
  survives that.
- Resolution risk. Vague, disputed, or subjective resolution criteria are the most common way a
  correct forecast still loses money. Down-weight anything whose resolution source and wording you
  cannot restate plainly.
- Time gate. An expiry too near for the runtime to act before resolution, and an expiry so far
  that capital would sit idle across the whole horizon, are both poor uses of a decision slot.
- Status gate. Closed, halted, or non-accepting markets are never candidates.

ROTATION AND BUDGET DISCIPLINE
- Recently analyzed topics arrive with their last analysis time. Re-selecting one is justified only
  by a concrete change since then: the price moved, the book changed, a catalyst passed, or an open
  position needs review. Otherwise pick something unexamined.
- Reserve part of every cycle for topics never analyzed before. A pool that only returns its own
  previous winners stops discovering, and stops producing the evidence its own priors are measured
  against.
- Do not fill the slate with several outcomes of one event. Correlated slots waste the budget.
- A topic with an open position earns a slot when its exit case may have changed, not merely
  because the position exists.
- Inspect only the shortlist you are actually deciding between, and stop once the ordering is
  clear. Unused budget is not wasted budget.

HOW TO READ THE PRIORS AND LESSONS BELOW
Priors are starting points, not rules. Each carries a weight this runtime measured from its own
completed decisions, and the evidence behind that weight is attached. Lessons were derived from the
same measured history; each states the bucket and sample size it came from. Trust a prior or lesson
in proportion to its sample size, and prefer what you can verify with the tools this cycle over any
stored generalization. Where a lesson and a hard gate conflict, the hard gate wins.

OUTPUT
Return the selected topics in priority order, each with the specific observation that earned the
slot: the number that changed, the dated catalyst, the inconsistency found. "High volume" and
"looks interesting" are not reasons. Name the prior or lesson you relied on when one applied, so
the next review can measure whether it held. If nothing clears the gates this cycle, return fewer
topics, or none. An empty cycle is a valid and sometimes correct answer."""


DiscoveryPrior = LearnedPrior
DiscoveryLesson = LearnedLesson


SEED_DISCOVERY_PRIORS: tuple[DiscoveryPrior, ...] = (
    DiscoveryPrior(
        "stale_price_moving_reality",
        "A price that has not moved while something externally observable has changed is the "
        "highest-value candidate available. Prior-cycle snapshots are supplied; a near-zero price "
        "change on a topic with a live catalyst outranks any static metric.",
    ),
    DiscoveryPrior(
        "scheduled_catalyst",
        "Questions tied to a dated catalyst - a data release, a match, a hearing, a deadline, a "
        "policy or earnings date - reprice predictably around that date. A question with no "
        "identifiable catalyst rarely repays research.",
    ),
    DiscoveryPrior(
        "tractable_question",
        "Prefer questions the research tools can actually settle: public, verifiable, near-term. A "
        "question that turns on private or unknowable information cannot be researched into an edge "
        "no matter how attractive the price looks.",
    ),
    DiscoveryPrior(
        "extreme_price_bands",
        "Prediction markets have historically overpriced low-probability outcomes and underpriced "
        "near-certain ones. Prices below roughly 0.10 and above roughly 0.90 are worth attention, "
        "and are also where spread and fees most often consume the entire theoretical edge. Forward "
        "them only when the book is tight enough for the edge to survive the round trip.",
    ),
    DiscoveryPrior(
        "mid_tier_liquidity",
        "Enough depth to trade the size this account can take, without the crowd that has already "
        "priced every public fact.",
    ),
    DiscoveryPrior(
        "structural_inconsistency",
        "Multi-outcome events whose outcome prices do not sum to a coherent total, and the same "
        "real-world question quoted differently on another platform, are edges that require no "
        "forecast at all. Check for these explicitly rather than hoping to notice them.",
    ),
    DiscoveryPrior(
        "fresh_listing",
        "A newly opened market is priced by whoever posted first, and that quote is often "
        "arbitrary. Topics absent from earlier snapshots deserve a look.",
    ),
)


@dataclass(frozen=True)
class DiscoveryBudget:
    """How much of the platform's read allowance one discovery pass may spend."""

    survey_topics: int = 200
    shortlist_topics: int = 24
    detail_lookups: int = 12
    book_lookups: int = 12
    agent_tool_steps: int = 6
    exploration_slots: int = 1
    cooldown_seconds: int = 900

    def __post_init__(self) -> None:
        for name in (
            "survey_topics",
            "shortlist_topics",
            "detail_lookups",
            "book_lookups",
            "agent_tool_steps",
            "exploration_slots",
            "cooldown_seconds",
        ):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"Discovery budget {name} cannot be negative")
        if self.survey_topics < 1 or self.shortlist_topics < 1:
            raise ValueError("Discovery budget must survey and shortlist at least one topic")

    def to_dict(self) -> dict[str, int]:
        return {
            "survey_topics": self.survey_topics,
            "shortlist_topics": self.shortlist_topics,
            "detail_lookups": self.detail_lookups,
            "book_lookups": self.book_lookups,
            "agent_tool_steps": self.agent_tool_steps,
            "exploration_slots": self.exploration_slots,
            "cooldown_seconds": self.cooldown_seconds,
        }


class MarketDiscoveryStrategy(Protocol):
    """Contract an optional market-discovery plugin implements."""

    name: str
    sha256: str
    instructions: str

    def budget(self) -> DiscoveryBudget: ...

    def to_prompt_payload(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class BuiltInMarketDiscovery:
    """Framework-owned discovery strategy used when no discovery plugin is enabled.

    Its core instructions are a runtime constraint rather than user configuration, so the text and
    budget are not exposed through the plugin configuration surface. Its evolution is not
    switchable: the runtime keeps measuring and refining it even while a user-supplied discovery
    plugin is the one actually selecting markets, so switching back never lands on a stale strategy.
    """

    name: str = "built_in"
    instructions: str = DISCOVERY_CORE_INSTRUCTIONS
    evolution_switchable: bool = False

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.instructions.encode("utf-8")).hexdigest()

    def budget(self) -> DiscoveryBudget:
        return DiscoveryBudget()

    def seed_priors(self) -> tuple[DiscoveryPrior, ...]:
        return SEED_DISCOVERY_PRIORS

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "source": "built-in",
            "instructions": self.instructions,
            "budget": self.budget().to_dict(),
        }


def compose_discovery_payload(
    strategy: Any,
    *,
    priors: tuple[DiscoveryPrior, ...],
    lessons: tuple[DiscoveryLesson, ...],
    measurements: dict[str, Any],
    evolution_enabled: bool,
    now_ms: int,
) -> dict[str, Any]:
    """Discovery's view of the shared overlay composition."""
    return compose_overlay_payload(
        strategy.to_prompt_payload(),
        strategy=strategy,
        priors=priors,
        lessons=lessons,
        measurements=measurements,
        evolution_enabled=evolution_enabled,
        now_ms=now_ms,
    )
