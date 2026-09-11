"""Trusted Python rule for the agent_policy category.

Load it with the agent_policy `custom_rules` plugin, field MODULE_PATHS:
    MODULE_PATHS=examples/agent_policy_rules/cap_trade_size.py

This lane runs before the platform is quoted, so ADJUST genuinely reduces the size. The same rule
placed in the business-risk lane could only refuse: there the size is already bound to a quote.

A rule may only restrict. ALLOW does not override another plugin's refusal - the chain is a
conjunction, so one refusal anywhere fails the action.
"""


def evaluate(operation, context):
    if context.get("kind") == "trade" and operation == "BUY":
        requested = float(context.get("requested_value") or 0)
        if requested > 1.0:
            return {
                "outcome": "ADJUST",
                "reason": "Example rule caps each Agent BUY request at 1 USDT",
                "adjusted_value": 1.0,
            }
    return {"outcome": "ALLOW", "reason": "No additional restriction"}
