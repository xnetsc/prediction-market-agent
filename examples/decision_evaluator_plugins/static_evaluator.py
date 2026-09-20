"""Minimal replaceable decision-evaluator plugin with no model dependency.

This deterministic example is deliberately conservative.  It demonstrates the
category contract; it is not enabled by the shipped configuration.
"""

from prediction_market_agent.agent.decision_evaluator import (
    CandidateAssessment,
    ContinuationAssessment,
)
from prediction_market_agent.plugin_system.discovery import (
    PluginInitializationContext,
    PluginSpec,
)


class StaticEvaluator:
    name = "static_evaluator"

    def evaluate_candidates(self, state, candidates):
        del state
        return [
            CandidateAssessment(
                candidate_id=str(item["candidate_id"]),
                action="NEEDS_DATA",
                quality=0.5,
                provider=self.name,
            )
            for item in candidates
        ]

    def assess_continuation(self, state, frontier, page):
        del state, frontier
        return ContinuationAssessment(
            action="CONTINUE_DISCOVERY" if page.get("has_more") else "SOURCE_EXHAUSTED",
            marginal_value=0.5 if page.get("has_more") else 0.0,
            provider=self.name,
        )

def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    return PluginSpec(
        "decision_evaluator",
        "static_evaluator",
        "不依赖模型的保守评估器示例，用于演示可替换的类型化评估契约。",
        str(context.module_path),
        lambda _config: StaticEvaluator(),
        None,
        lambda: None,
    )
