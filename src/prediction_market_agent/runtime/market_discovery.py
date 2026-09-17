from __future__ import annotations

import logging
import time
from typing import Any, Callable

from ..agent.decision import DecisionCancelled, DecisionProviderError
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
        "next_scan_seconds": {"type": "integer", "minimum": 0, "maximum": 86400},
        "next_survey_queries": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 120},
        },
        "pacing_reason": {"type": "string", "maxLength": 300},
        "headline": {"type": "string", "minLength": 1, "maxLength": 90},
    },
    "required": [
        "selections", "skipped_reason", "next_scan_seconds", "next_survey_queries",
        "pacing_reason", "headline",
    ],
}

DISCOVERY_MISSION = (
    "Select the markets on this platform that deserve this cycle's decision slots. Return topic_id "
    "values taken verbatim from the candidate list. Returning fewer than the maximum, or none, is "
    "correct when nothing clears the gates. The headline is the one line a trader reads first: what "
    "this round found, or why it found nothing - for example \"Picked 2 of 24: NATO clash volume up "
    "67%\" or \"Nothing picked: every live book is wider than the edge\"."
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
        research_contributions: list[Any] | None = None,
    ) -> None:
        self.memory = memory
        self.strategy = strategy
        self.provider = provider
        self.evolution_enabled = evolution_enabled
        self.cross_platform_search = cross_platform_search
        # The gates ask about resolution wording and deadlines, and a candidate that does not carry
        # them was being rejected as unverifiable - while the tools that could have gone and found
        # them were offered only at the decision stage. Discovery could see that something was
        # missing and had no way to go and get it.
        self.research_contributions = list(research_contributions or [])

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
        return self._with_requested(plugin, topics, budget)

    def _with_requested(
        self,
        plugin: PredictionMarketApiPlugin,
        topics: list[Topic],
        budget: DiscoveryBudget,
    ) -> list[Topic]:
        """Add what the agent asked to look for, alongside what the venue happens to list first.

        The listing is one fixed opinion - most traded first - and a robot that only ever sees that
        can only ever find something there. What is worth looking at is a judgement about the
        moment: a catalyst due this week, a category that moved, a question it saw quoted elsewhere.
        Asking is free; it already reads this platform every round.

        These are added to the listing, never instead of it. A query that finds nothing leaves the
        round exactly as it was.
        """
        queries = self.memory.survey_plan(plugin.name).get("queries") or []
        if not queries:
            return topics
        seen = {topic.topic_id for topic in topics}
        for query in queries:
            if len(topics) >= budget.survey_topics:
                break
            try:
                found = plugin.search_market_candidates(query, budget.shortlist_topics)
            except Exception as error:
                LOGGER.warning("requested survey %r failed on %s: %s", query, plugin.name, error)
                continue
            for candidate in found:
                topic = getattr(candidate, "topic", None)
                if topic is None or topic.topic_id in seen:
                    continue
                seen.add(topic.topic_id)
                topics.append(topic)
                if len(topics) >= budget.survey_topics:
                    break
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

        selections, mode, skipped_reason = self._select(
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
        if not chosen:
            # The model is asked for this and was handing it back all along; dropping it turned a
            # stated judgement into a silent zero, which reads as a broken cycle rather than as a
            # round where nothing was worth a decision slot.
            LOGGER.info(
                "platform=%s selected nothing from %d shortlisted: %s",
                platform,
                len(shortlist),
                skipped_reason or "no reason was given",
            )
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

    def _verify(
        self,
        toolbox: "_DiscoveryToolbox",
        shortlist: list[Topic],
        now_ms: int,
    ) -> dict[str, dict[str, Any]]:
        """Spend part of the read allowance up front on what the gates actually ask about.

        The allowance existed only as a ceiling on what the agent could request for itself, and the
        agent's tool budget is far smaller than the shortlist - so most candidates arrived with no
        deadline and no spread and were refused as unverifiable, which is a construction rather than
        a judgement. Buying those facts here, through the same toolbox so the platform is read once
        and not twice, means the gates can be applied to every candidate this reaches. A candidate
        that could not be read says so, instead of arriving indistinguishable from one nobody looked at.
        """
        verified: dict[str, dict[str, Any]] = {}
        for topic in shortlist:
            # A candidate the venue will not describe is one candidate the agent has to judge
            # without help. It is not a reason to abandon the round - which is exactly what it
            # became when one topic on a shortlist of twenty-four had no order book and the 404
            # took the whole cycle down with it.
            try:
                detail = toolbox.execute("TOPIC_DETAIL", {"topic_id": topic.topic_id})
            except Exception as error:
                verified[topic.topic_id] = {
                    "verified": False, "lookup_error": str(error)[:200]
                }
                continue
            if not detail.get("ok"):
                # The allowance is gone; the rest of the shortlist is honestly unexamined.
                break
            entry: dict[str, Any] = {"verified": True, "resolution": detail.get("resolution")}
            end_time = detail.get("end_time_ms")
            if end_time:
                entry["seconds_remaining"] = max(0, (int(end_time) - now_ms) // 1000)
            markets = detail.get("markets") or []
            entry["markets"] = int(detail.get("markets_total", len(markets)))
            entry["markets_open"] = int(detail.get("markets_open", 0))
            # Only an open market has a book. Asking for one on a closed market is a guaranteed 404
            # that spends the allowance and tells the agent nothing - whereas "nothing here is open"
            # is itself the fact a status gate needs.
            priced = next(
                (market for market in markets if str(market.get("status", "")).upper() == "OPEN"),
                None,
            )
            if priced is None:
                entry["tradeable"] = False
                entry["why_not_priced"] = "no market in this event is open for trading"
            outcomes = (priced.get("outcomes") or []) if priced else []
            if outcomes:
                entry["tradeable"] = True
                # Which market the price belongs to matters: in a ladder of deadlines, a spread on
                # "by December" says nothing about "by June".
                entry["priced_market"] = str(priced.get("question", ""))[:160]
                entry["priced_outcome"] = outcomes[0].get("name")
                try:
                    book = toolbox.execute("OUTCOME_BOOK", {
                        "market_id": priced.get("market_id"),
                        "outcome_id": outcomes[0].get("outcome_id"),
                    })
                except Exception as error:
                    book = {"ok": False, "error": str(error)[:200]}
                if book.get("ok"):
                    entry.update({
                        key: book[key] for key in ("best_bid", "best_ask", "spread")
                        if key in book
                    })
                    bid, ask = book.get("best_bid"), book.get("best_ask")
                    if bid and ask:
                        mid = (float(bid) + float(ask)) / 2
                        entry["spread_pct_of_mid"] = (
                            round((float(ask) - float(bid)) / mid * 100, 2) if mid else None
                        )
                    else:
                        # A book with one side has no spread to report, and saying nothing made it
                        # look like a candidate nobody had checked. It is a finding: there is no
                        # price to buy at, or none to sell at.
                        entry["book_one_sided"] = (
                            "nobody is selling" if not ask else "nobody is buying"
                        ) if (bid or ask) else "the book is empty"
                else:
                    entry["book_error"] = book.get("error", "")
            verified[topic.topic_id] = entry
        return verified

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
    ) -> tuple[list[dict[str, Any]], str, str]:
        payload = compose_discovery_payload(
            self.strategy,
            priors=self._priors(),
            lessons=self._lessons(),
            measurements=self.measurements(),
            evolution_enabled=self.evolution_enabled,
            now_ms=now_ms,
        )
        toolbox = _DiscoveryToolbox(
            plugin=plugin,
            memory=self.memory,
            platform=platform,
            budget=budget,
            cross_platform_search=self.cross_platform_search,
            measurements=self.measurements,
            research=self.research_contributions,
        )
        verified = self._verify(toolbox, shortlist, now_ms)
        candidates = [
            {
                "topic_id": topic.topic_id,
                "title": topic.title,
                # The gates ask about the resolution wording, the deadline and the round-trip cost.
                # None of that was here, so every candidate had to be bought with a tool step, and
                # a shortlist far larger than the tool budget meant most of it was refused as
                # unverifiable - by construction rather than by judgement. The question and the
                # description were already in hand and cost nothing to pass on.
                "question": topic.question,
                "description": topic.description[:600],
                "category": topic.category,
                "status": topic.status,
                "liquidity_usdt": topic.liquidity_usdt,
                "volume_usdt": topic.volume_usdt,
                "signals": {
                    key: value
                    for key, value in features_by_topic.get(topic.topic_id, {}).items()
                    if key != "buckets"
                },
                **verified.get(topic.topic_id, {"verified": False}),
            }
            for topic in shortlist
        ]
        # Candidates that settle past the horizon are dropped here rather than argued about by the
        # model: it costs nothing to leave them out, and a shortlist made mostly of them wastes the
        # round on markets no decision would be allowed to trade anyway. How many went, and why, is
        # part of what the round is told, so an empty shortlist is not a mystery.
        horizon_days = int(getattr(self.strategy, "horizon_days", 0) or 0)
        too_far = 0
        if horizon_days > 0:
            limit = horizon_days * 86400
            kept = []
            for candidate in candidates:
                seconds = candidate.get("seconds_remaining")
                if isinstance(seconds, (int, float)) and seconds > limit:
                    too_far += 1
                    continue
                kept.append(candidate)
            candidates = kept
        request = {
            "platform": platform,
            "platform_capabilities": plugin.capabilities.to_dict(),
            "maximum_selections": maximum_topics,
            "discovery_strategy": prompt_json_payload(payload),
            "candidates": candidates,
            **({"dropped_settling_after_horizon": too_far} if too_far else {}),
        }
        # Every call to a model leaves a row, this one included. It used to leave none: a discovery
        # round that failed, or that judged nothing worth a slot, produced no ledger entry at all,
        # so the only visible trace was a cycle that quietly did nothing. An operator reading the
        # ledger could not tell "the model was asked and declined" from "nothing ran".
        decision_id = self.memory.begin_decision(
            platform=platform,
            market_topic_id="",
            market_id="",
            token_id="",
            strategy_name=f"{self.strategy.name}:discovery",
            strategy_sha256=self.strategy.sha256,
            context={"stage": "discovery", "candidates": candidates,
                     "maximum_selections": maximum_topics},
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
                should_stop=lambda: self.memory.is_cancelled(decision_id),
            )
        except DecisionCancelled:
            # The round's record was deleted while the agent was still choosing. Its choices have
            # nowhere to be recorded, so this round selects nothing rather than acting on them.
            LOGGER.info("discovery round %s on %s was deleted; stopped", decision_id, platform)
            return [], "cancelled", "deleted while running"
        except (DecisionProviderError, AttributeError, TypeError) as error:
            # Losing the Agent must not stop the platform from trading. Fall back to the
            # deterministic prescore order and mark the cycle so the audit trail shows which
            # selections were never actually reasoned about.
            LOGGER.error(
                "market discovery agent unavailable on %s; falling back to prescore order: %s",
                platform,
                error,
            )
            self.memory.complete_decision(
                decision_id,
                provider=getattr(self.provider, "name", ""),
                model_raw_output=getattr(error, "raw_output", ""),
                status="PROVIDER_ERROR",
                error=str(error),
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
                "",
            )
        if self.memory.is_cancelled(decision_id):
            LOGGER.info("discovery round %s on %s was deleted after answering", decision_id, platform)
            return [], "cancelled", "deleted while running"
        selections = [item for item in result.value.get("selections", []) if isinstance(item, dict)]
        skipped_reason = str(result.value.get("skipped_reason", ""))
        # How soon to come back and what to go looking for are judgements about this platform right
        # now - how fast its prices move, what catalyst is due - and the only thing here that has
        # looked at that is the agent that just read it.
        self.memory.save_survey_plan(
            platform=platform,
            queries=[
                str(item) for item in result.value.get("next_survey_queries", [])
                if str(item).strip()
            ],
            next_scan_seconds=int(result.value.get("next_scan_seconds", 0) or 0),
            reason=str(result.value.get("pacing_reason", "")),
        )
        self.memory.complete_decision(
            decision_id,
            provider=result.provider,
            research=result.research_trace,
            model_raw_output=result.raw_output,
            final_decision={
                "selections": selections,
                "skipped_reason": skipped_reason,
                "headline": str(result.value.get("headline", "")).strip()[:90],
                "next_scan_seconds": int(result.value.get("next_scan_seconds", 0) or 0),
                "next_survey_queries": list(result.value.get("next_survey_queries", []) or []),
                "pacing_reason": str(result.value.get("pacing_reason", "")),
            },
            status="OK" if selections else "NO_ACTION",
            error="" if selections else skipped_reason,
        )
        return selections, "agent", skipped_reason

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
        research: list[Any] | None = None,
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
        # Whatever the research plugins contribute - searching the web, reading a page - offered
        # here as well as at the decision stage. "I could not verify the resolution wording" is a
        # statement about what was reachable, and this is what makes it reachable.
        self._research: dict[str, Any] = {}
        for contribution in research or []:
            executor = contribution.create(None)
            for name, description in executor.descriptions.items():
                key = str(name).strip().upper()
                if key in self.descriptions or key in self._research:
                    continue
                self.descriptions[key] = description
                self._research[key] = executor

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = {
            "TOPIC_DETAIL": self._topic_detail,
            "OUTCOME_BOOK": self._outcome_book,
            "TOPIC_HISTORY": self._topic_history,
            "RECALL_MEASUREMENTS": self._recall_measurements,
            "SEARCH_OTHER_PLATFORMS": self._search,
        }.get(name.strip().upper())
        if handler is None:
            executor = self._research.get(name.strip().upper())
            if executor is not None:
                return executor.execute(name.strip().upper(), arguments)
            raise ValueError(f"Unknown discovery tool: {name}")
        return handler(arguments)

    def _topic_detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._detail_calls >= self.budget.detail_lookups:
            return {"ok": False, "error": "detail lookup budget exhausted for this cycle"}
        self._detail_calls += 1
        detail = self.plugin.get_topic(str(arguments["topic_id"]))
        # Tradeable markets first, most liquid first, and only then cut to eight. Events that ladder
        # one question across deadlines - "by June", "by September", "by December" - list the
        # earliest first, and the earliest has usually already closed: reading them in the venue's
        # order put dead markets at the front and, past eight, dropped the live ones entirely.
        markets = sorted(
            detail.markets,
            key=lambda market: (
                str(market.status).upper() != "OPEN",
                -float(market.liquidity_usdt or 0.0),
            ),
        )
        return {
            "ok": True,
            "end_time_ms": detail.end_time_ms,
            "fee_bps": detail.fee_bps,
            "resolution": detail.resolution,
            "reference_symbol": detail.reference_symbol,
            "markets_total": len(detail.markets),
            "markets_open": sum(1 for market in detail.markets if str(market.status).upper() == "OPEN"),
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
                for market in markets[:8]
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
