from __future__ import annotations

import logging
import time
from typing import Any, Callable

from ..agent.decision import DecisionProviderError
from ..agent.evolution import (
    DISCOVERY_EVOLUTION_KEY,
    LESSON_DEVIATION,
    LESSON_MIN_SAMPLE,
    prompt_json_payload,
    render_overlay_block,
    shrunk_weight,
)
from ..agent.market_discovery import (
    DiscoveryBudget,
    DiscoveryLesson,
    DiscoveryPrior,
    compose_discovery_payload,
)
from ..plugin_system.contracts import PredictionMarketApiPlugin, Topic
from .memory import SessionMemory

LOGGER = logging.getLogger(__name__)


DISCOVERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "selections": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "topic_id": {"type": "string", "minLength": 1},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 400},
                    "priors": {
                        "type": "array",
                        "maxItems": 4,
                        "items": {"type": "string", "maxLength": 80},
                    },
                },
                "required": ["topic_id", "reason", "priors"],
            },
        },
        "skipped_reason": {"type": "string", "maxLength": 400},
    },
    "required": ["selections", "skipped_reason"],
}

DISCOVERY_MISSION = (
    "Select the markets on this platform that deserve this cycle's decision slots. Return topic_id "
    "values taken verbatim from the candidate list. Returning fewer than the maximum, or none, is "
    "correct when nothing clears the gates."
)

DISCOVERY_CONTROL_MISSION = (
    "Choose one next action to sharpen the shortlist. Use DECIDE once the ordering is clear."
)

REVIEW_SETTLE_MS = 30 * 60 * 1000
"""How long after a selection its downstream decisions are considered final."""



def _band(value: float, edges: tuple[float, ...]) -> str:
    low = 0.0
    for edge in edges:
        if value < edge:
            return f"{low:g}-{edge:g}"
        low = edge
    return f"{low:g}+"


def topic_features(
    topic: Topic,
    *,
    previous: dict[str, Any] | None,
    attention: dict[str, Any] | None,
    now_ms: int,
) -> dict[str, Any]:
    """Cheap, list-payload-only features. Buckets are what outcome attribution aggregates on."""
    buckets: list[str] = [
        f"liquidity:{_band(float(topic.liquidity_usdt or 0.0), (1_000, 10_000, 100_000))}",
        f"volume:{_band(float(topic.volume_usdt or 0.0), (10_000, 100_000, 1_000_000))}",
        f"status:{str(topic.status).upper() or 'UNKNOWN'}",
    ]
    category = str(topic.category or "").strip().lower()[:40]
    if category:
        buckets.append(f"category:{category}")

    features: dict[str, Any] = {"liquidity_usdt": float(topic.liquidity_usdt or 0.0)}
    if previous is None:
        buckets.append("listing:new")
        features["first_seen"] = True
    else:
        buckets.append("listing:known")
        previous_volume = float(previous.get("volume_usdt") or 0.0)
        volume_change = float(topic.volume_usdt or 0.0) - previous_volume
        features["volume_change"] = volume_change
        features["seconds_since_observed"] = max(
            0, (now_ms - int(previous.get("observed_at", now_ms))) // 1000
        )
        moved = previous_volume > 0 and abs(volume_change) / previous_volume > 0.01
        buckets.append("flow:moving" if moved else "flow:quiet")

    if attention:
        last = int(attention.get("last_selected_at", 0))
        features["seconds_since_selected"] = max(0, (now_ms - last) // 1000)
        features["previous_selections"] = int(attention.get("selections", 0))
    else:
        features["never_selected"] = True
        buckets.append("attention:never")

    features["buckets"] = buckets
    return features


class DiscoveryEngine:
    """Framework-owned market discovery: the platform says when, this decides what."""

    def __init__(
        self,
        *,
        memory: SessionMemory,
        strategy: Any,
        provider: Any,
        evolution_enabled: bool,
        cross_platform_search: Callable[[str, int], dict[str, Any]] | None = None,
    ) -> None:
        self.memory = memory
        self.strategy = strategy
        self.provider = provider
        self.evolution_enabled = evolution_enabled
        self.cross_platform_search = cross_platform_search

    # ------------------------------------------------------------------ survey

    def _survey(
        self, plugin: PredictionMarketApiPlugin, budget: DiscoveryBudget
    ) -> list[Topic]:
        page_size = max(1, int(plugin.topic_page_size()))
        topics: list[Topic] = []
        offset = 0
        while len(topics) < budget.survey_topics:
            page = plugin.list_topics(
                offset=offset, limit=min(page_size, budget.survey_topics - len(topics))
            )
            topics.extend(page.topics)
            if not page.has_more or not page.topics:
                break
            offset = page.next_offset
        return topics

    def _prior_weights(self) -> dict[str, float]:
        if not self.evolution_enabled:
            return {}
        return {
            item["prior_id"]: float(item["weight"])
            for item in self.memory.load_discovery_priors(DISCOVERY_EVOLUTION_KEY)
        }

    def _priors(self) -> tuple[DiscoveryPrior, ...]:
        seeds = getattr(self.strategy, "seed_priors", None)
        base = tuple(seeds()) if callable(seeds) else ()
        if not self.evolution_enabled:
            return base
        stored = {item["prior_id"]: item for item in self.memory.load_discovery_priors(DISCOVERY_EVOLUTION_KEY)}
        merged = [
            DiscoveryPrior(
                prior.prior_id,
                prior.text,
                float(stored.get(prior.prior_id, {}).get("weight", prior.weight)),
                int(stored.get(prior.prior_id, {}).get("sample_size", 0)),
            )
            for prior in base
        ]
        return tuple(merged)

    def _lessons(self) -> tuple[DiscoveryLesson, ...]:
        if not self.evolution_enabled:
            return ()
        return tuple(
            DiscoveryLesson(
                lesson_id=item["lesson_id"],
                text=item["text"],
                bucket=item["bucket"],
                sample_size=item["sample_size"],
                support=item["support"],
                recorded_at=item["recorded_at"],
                confirmed_at=item["confirmed_at"],
            )
            for item in self.memory.load_discovery_lessons(DISCOVERY_EVOLUTION_KEY)
        )

    def _prescore(
        self, features: dict[str, Any], weights: dict[str, float], budget: DiscoveryBudget
    ) -> float:
        """Rank candidates cheaply so the Agent reads a shortlist, not the whole platform.

        This orders what the Agent looks at. It never decides what is selected.
        """
        buckets = set(features.get("buckets", ()))
        score = 1.0
        if "listing:new" in buckets:
            score += weights.get("fresh_listing", 1.0)
        if "flow:quiet" in buckets and not features.get("first_seen"):
            score += 0.5 * weights.get("stale_price_moving_reality", 1.0)
        if "flow:moving" in buckets:
            score += weights.get("stale_price_moving_reality", 1.0)
        if "attention:never" in buckets:
            score += weights.get("mid_tier_liquidity", 1.0)
        liquidity = float(features.get("liquidity_usdt", 0.0))
        if 1_000 <= liquidity <= 100_000:
            score += 0.5 * weights.get("mid_tier_liquidity", 1.0)
        seconds_since_selected = features.get("seconds_since_selected")
        if seconds_since_selected is not None and seconds_since_selected < budget.cooldown_seconds:
            score *= 0.25
        return score

    # ------------------------------------------------------------- discovery

    def discover(
        self, *, platform: str, plugin: PredictionMarketApiPlugin, maximum_topics: int
    ) -> tuple[Topic, ...]:
        budget = self.strategy.budget()
        now_ms = int(time.time() * 1000)
        surveyed = self._survey(plugin, budget)
        if not surveyed:
            return ()

        previous = self.memory.previous_topic_observations(platform=platform, before_ms=now_ms)
        attention = self.memory.topic_attention_history(platform=platform)
        weights = self._prior_weights()

        features_by_topic: dict[str, dict[str, Any]] = {}
        observations: list[dict[str, Any]] = []
        for topic in surveyed:
            features = topic_features(
                topic,
                previous=previous.get(topic.topic_id),
                attention=attention.get(topic.topic_id),
                now_ms=now_ms,
            )
            features_by_topic[topic.topic_id] = features
            observations.append(
                {
                    "market_topic_id": topic.topic_id,
                    "title": topic.title,
                    "status": topic.status,
                    "liquidity_usdt": topic.liquidity_usdt,
                    "volume_usdt": topic.volume_usdt,
                    "features": features,
                }
            )
        self.memory.record_topic_observations(platform=platform, observations=observations)

        ranked = sorted(
            surveyed,
            key=lambda item: self._prescore(features_by_topic[item.topic_id], weights, budget),
            reverse=True,
        )
        shortlist = ranked[: max(1, budget.shortlist_topics)]
        by_id = {topic.topic_id: topic for topic in surveyed}

        selections, mode = self._select(
            platform=platform,
            plugin=plugin,
            shortlist=shortlist,
            features_by_topic=features_by_topic,
            budget=budget,
            maximum_topics=maximum_topics,
            now_ms=now_ms,
        )
        strategy_name = self.strategy.name if mode == "agent" else f"{self.strategy.name}:{mode}"

        chosen: list[Topic] = []
        for position, item in enumerate(selections, start=1):
            topic = by_id.get(str(item.get("topic_id", "")))
            if topic is None or topic in chosen:
                continue
            self.memory.record_discovery_selection(
                platform=platform,
                strategy=strategy_name,
                market_topic_id=topic.topic_id,
                position=position,
                reason=str(item.get("reason", "")),
                priors=[str(name) for name in item.get("priors", [])][:4],
                features=features_by_topic.get(topic.topic_id, {}),
            )
            chosen.append(topic)
            if len(chosen) >= maximum_topics:
                break
        LOGGER.info(
            "platform=%s surveyed=%d shortlist=%d selected=%d strategy=%s evolution=%s",
            platform,
            len(surveyed),
            len(shortlist),
            len(chosen),
            strategy_name,
            self.evolution_enabled,
        )
        return tuple(chosen)

    def _select(
        self,
        *,
        platform: str,
        plugin: PredictionMarketApiPlugin,
        shortlist: list[Topic],
        features_by_topic: dict[str, dict[str, Any]],
        budget: DiscoveryBudget,
        maximum_topics: int,
        now_ms: int,
    ) -> tuple[list[dict[str, Any]], str]:
        payload = compose_discovery_payload(
            self.strategy,
            priors=self._priors(),
            lessons=self._lessons(),
            measurements=self.measurements(),
            evolution_enabled=self.evolution_enabled,
            now_ms=now_ms,
        )
        candidates = [
            {
                "topic_id": topic.topic_id,
                "title": topic.title,
                "category": topic.category,
                "status": topic.status,
                "liquidity_usdt": topic.liquidity_usdt,
                "volume_usdt": topic.volume_usdt,
                "signals": {
                    key: value
                    for key, value in features_by_topic.get(topic.topic_id, {}).items()
                    if key != "buckets"
                },
            }
            for topic in shortlist
        ]
        request = {
            "platform": platform,
            "platform_capabilities": plugin.capabilities.to_dict(),
            "maximum_selections": maximum_topics,
            "discovery_strategy": prompt_json_payload(payload),
            "candidates": candidates,
        }
        toolbox = _DiscoveryToolbox(
            plugin=plugin,
            memory=self.memory,
            platform=platform,
            budget=budget,
            cross_platform_search=self.cross_platform_search,
            measurements=self.measurements,
        )
        try:
            result = self.provider.run(
                request,
                schema=DISCOVERY_SCHEMA,
                schema_name="market_discovery",
                mission=DISCOVERY_MISSION,
                control_mission=DISCOVERY_CONTROL_MISSION,
                instructions=render_overlay_block(payload, "MARKET_DISCOVERY_STRATEGY"),
                max_tool_steps=budget.agent_tool_steps,
                final_step_name="FINAL_SELECTION",
                tool_executor=toolbox.execute,
                tool_descriptions=toolbox.descriptions,
            )
        except (DecisionProviderError, AttributeError, TypeError) as error:
            # Losing the Agent must not stop the platform from trading. Fall back to the
            # deterministic prescore order and mark the cycle so the audit trail shows which
            # selections were never actually reasoned about.
            LOGGER.error(
                "market discovery agent unavailable on %s; falling back to prescore order: %s",
                platform,
                error,
            )
            return (
                [
                    {
                        "topic_id": topic.topic_id,
                        "reason": f"prescore order; discovery agent unavailable: {error}"[:400],
                        "priors": [],
                    }
                    for topic in shortlist[:maximum_topics]
                ],
                "mechanical",
            )
        selections = result.value.get("selections", [])
        return [item for item in selections if isinstance(item, dict)], "agent"

    # -------------------------------------------------------------- evolution

    def measurements(self) -> dict[str, Any]:
        """Per-bucket outcome rates measured from this runtime's own completed decisions."""
        rows = self.memory.reviewed_selection_outcomes()
        if not rows:
            return {"samples": 0, "baseline_useful_rate": 0.0, "buckets": {}}
        useful_total = sum(1 for row in rows if row["outcome"].get("useful"))
        baseline = useful_total / len(rows)
        buckets: dict[str, dict[str, float]] = {}
        for row in rows:
            for bucket in row["features"].get("buckets", ()):
                entry = buckets.setdefault(str(bucket), {"selections": 0, "useful": 0})
                entry["selections"] += 1
                if row["outcome"].get("useful"):
                    entry["useful"] += 1
        return {
            "samples": len(rows),
            "baseline_useful_rate": round(baseline, 4),
            "buckets": {
                name: {
                    "selections": int(entry["selections"]),
                    "useful_rate": round(entry["useful"] / entry["selections"], 4),
                }
                for name, entry in sorted(buckets.items())
                if entry["selections"] > 0
            },
        }

    def review(self, *, now_ms: int | None = None) -> dict[str, int]:
        """Close out settled selections, re-weight priors, and rewrite measured lessons.

        This runs regardless of which strategy is selecting markets and regardless of whether any
        strategy currently applies the result, so the measured overlay is never stale when it is
        switched back on.
        """
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        pending = self.memory.unreviewed_selections(settled_before_ms=now - REVIEW_SETTLE_MS)
        for item in pending:
            outcome = self.memory.selection_downstream(
                platform=item["platform"],
                market_topic_id=item["market_topic_id"],
                since_ms=item["selected_at"],
                until_ms=item["selected_at"] + REVIEW_SETTLE_MS,
            )
            self.memory.mark_selection_reviewed(item["id"], outcome)
        rows = self.memory.reviewed_selection_outcomes()
        if not rows:
            return {"reviewed": len(pending), "priors": 0, "lessons": 0, "retired": 0}
        baseline = sum(1 for row in rows if row["outcome"].get("useful")) / len(rows)
        priors = self._reweight_priors(rows, baseline)
        written, retired = self._rewrite_lessons(baseline)
        return {
            "reviewed": len(pending),
            "priors": priors,
            "lessons": written,
            "retired": retired,
        }

    def _reweight_priors(self, rows: list[dict[str, Any]], baseline: float) -> int:
        """Measure each prior by what happened to the selections that cited it."""
        tally: dict[str, dict[str, int]] = {}
        for row in rows:
            for prior_id in row.get("priors", ()):
                entry = tally.setdefault(str(prior_id), {"selections": 0, "useful": 0})
                entry["selections"] += 1
                if row["outcome"].get("useful"):
                    entry["useful"] += 1
        seeds = getattr(self.strategy, "seed_priors", None)
        texts = {prior.prior_id: prior.text for prior in (seeds() if callable(seeds) else ())}
        written = 0
        for prior_id, entry in tally.items():
            rate = entry["useful"] / entry["selections"]
            weight = shrunk_weight(rate, baseline, entry["selections"])
            self.memory.save_discovery_prior(
                strategy=DISCOVERY_EVOLUTION_KEY,
                prior_id=prior_id,
                text=texts.get(prior_id, ""),
                weight=weight,
                sample_size=entry["selections"],
                support={
                    "useful_rate": round(rate, 4),
                    "baseline_useful_rate": round(baseline, 4),
                    "useful": entry["useful"],
                },
            )
            written += 1
        return written

    def _rewrite_lessons(self, baseline: float) -> tuple[int, int]:
        """State what the buckets actually measured. Templates, not model prose.

        A lesson generated from a template can always be traced back to its sample; a model asked to
        summarize a handful of trades will write a confident generalization out of noise.
        """
        measured = self.measurements()["buckets"]
        active = {item["lesson_id"]: item for item in self.memory.load_discovery_lessons(DISCOVERY_EVOLUTION_KEY)}
        written = 0
        qualifying: set[str] = set()
        for bucket, entry in measured.items():
            samples = int(entry["selections"])
            rate = float(entry["useful_rate"])
            if samples < LESSON_MIN_SAMPLE or abs(rate - baseline) < LESSON_DEVIATION:
                continue
            qualifying.add(bucket)
            direction = "more" if rate > baseline else "less"
            self.memory.save_discovery_lesson(
                strategy=DISCOVERY_EVOLUTION_KEY,
                lesson_id=bucket,
                text=(
                    f"Candidates in {bucket} produced an actionable decision "
                    f"{rate:.0%} of the time against a {baseline:.0%} baseline over {samples} "
                    f"selections, so treat this bucket as {direction} promising than average."
                ),
                bucket=bucket,
                sample_size=samples,
                support={
                    "useful_rate": rate,
                    "baseline_useful_rate": round(baseline, 4),
                    "deviation": round(rate - baseline, 4),
                },
            )
            written += 1
        retired = 0
        for lesson_id in active:
            if lesson_id not in qualifying:
                self.memory.retire_discovery_lesson(
                    strategy=DISCOVERY_EVOLUTION_KEY,
                    lesson_id=lesson_id,
                    reason="bucket no longer deviates from the baseline",
                )
                retired += 1
        return written, retired


class _DiscoveryToolbox:
    """Read-only tools the discovery Agent drives, all through plugin standard interfaces."""

    def __init__(
        self,
        *,
        plugin: PredictionMarketApiPlugin,
        memory: SessionMemory,
        platform: str,
        budget: DiscoveryBudget,
        cross_platform_search: Callable[[str, int], dict[str, Any]] | None,
        measurements: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.measurements = measurements
        self.plugin = plugin
        self.memory = memory
        self.platform = platform
        self.budget = budget
        self.cross_platform_search = cross_platform_search
        self._detail_calls = 0
        self._book_calls = 0
        self.descriptions: dict[str, Any] = {
            "TOPIC_DETAIL": {
                "purpose": "Read one topic's markets, outcomes, resolution data and end time.",
                "arguments": {"topic_id": "required string from the candidate list"},
            },
            "OUTCOME_BOOK": {
                "purpose": "Read the order book for one outcome to check the real spread.",
                "arguments": {"market_id": "required string", "outcome_id": "required string"},
            },
            "TOPIC_HISTORY": {
                "purpose": "Recall when this topic was last selected and what came of it.",
                "arguments": {"topic_id": "required string"},
            },
            "RECALL_MEASUREMENTS": {
                "purpose": (
                    "Read the complete measured outcome table; the prompt carries only the "
                    "largest buckets."
                ),
                "arguments": {"prefix": "optional bucket name prefix filter"},
            },
        }
        if cross_platform_search is not None:
            self.descriptions["SEARCH_OTHER_PLATFORMS"] = {
                "purpose": "Find the same question quoted on other registered platforms.",
                "arguments": {"query": "required string", "max_results_per_platform": "optional integer"},
            }

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = {
            "TOPIC_DETAIL": self._topic_detail,
            "OUTCOME_BOOK": self._outcome_book,
            "TOPIC_HISTORY": self._topic_history,
            "RECALL_MEASUREMENTS": self._recall_measurements,
            "SEARCH_OTHER_PLATFORMS": self._search,
        }.get(name.strip().upper())
        if handler is None:
            raise ValueError(f"Unknown discovery tool: {name}")
        return handler(arguments)

    def _topic_detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._detail_calls >= self.budget.detail_lookups:
            return {"ok": False, "error": "detail lookup budget exhausted for this cycle"}
        self._detail_calls += 1
        detail = self.plugin.get_topic(str(arguments["topic_id"]))
        return {
            "ok": True,
            "end_time_ms": detail.end_time_ms,
            "fee_bps": detail.fee_bps,
            "resolution": detail.resolution,
            "reference_symbol": detail.reference_symbol,
            "markets": [
                {
                    "market_id": market.market_id,
                    "question": market.question[:300],
                    "status": market.status,
                    "liquidity_usdt": market.liquidity_usdt,
                    "outcomes": [
                        {
                            "outcome_id": outcome.outcome_id,
                            "name": outcome.name,
                            "displayed_probability": outcome.displayed_probability,
                        }
                        for outcome in market.outcomes
                    ],
                }
                for market in detail.markets[:8]
            ],
        }

    def _outcome_book(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._book_calls >= self.budget.book_lookups:
            return {"ok": False, "error": "order book budget exhausted for this cycle"}
        self._book_calls += 1
        book = self.plugin.get_order_book(
            str(arguments["market_id"]), str(arguments["outcome_id"])
        )
        bids = [level.price for level in book.bids]
        asks = [level.price for level in book.asks]
        if not bids or not asks:
            return {"ok": True, "two_sided": False}
        best_bid, best_ask = max(bids), min(asks)
        return {
            "ok": True,
            "two_sided": True,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": round(best_ask - best_bid, 6),
        }

    def _topic_history(self, arguments: dict[str, Any]) -> dict[str, Any]:
        topic_id = str(arguments["topic_id"])
        attention = self.memory.topic_attention_history(platform=self.platform).get(topic_id, {})
        return {
            "ok": True,
            "topic_id": topic_id,
            "attention": attention,
            "downstream": self.memory.selection_downstream(
                platform=self.platform,
                market_topic_id=topic_id,
                since_ms=0,
                until_ms=int(time.time() * 1000),
            ),
        }

    def _recall_measurements(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.measurements is None:
            return {"ok": False, "error": "no measurements are available"}
        measured = self.measurements()
        prefix = str(arguments.get("prefix", "") or "")
        buckets = measured.get("buckets", {})
        if prefix:
            buckets = {name: value for name, value in buckets.items() if name.startswith(prefix)}
        return {"ok": True, **{k: v for k, v in measured.items() if k != "buckets"}, "buckets": buckets}

    def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.cross_platform_search is None:
            return {"ok": False, "error": "cross-platform search is not available"}
        maximum = int(arguments.get("max_results_per_platform", 5) or 5)
        return self.cross_platform_search(str(arguments["query"]), max(1, min(maximum, 20)))
