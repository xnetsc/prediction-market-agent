"""Trusted in-process risk plugin example.

Configured with RISK_FILTER_PY_MODULES=examples/risk_rules/cap_trade_size.py.
It can only make the coordinator's result more restrictive; ALLOW cannot override a base rejection.
"""


def evaluate(operation, context):
    if operation == "BUY" and context.get("protected_target") == "agent:actions":
        requested = float(context.get("requested_value") or 0)
        if requested > 1.0:
            return {
                "outcome": "ADJUST",
                "reason": "Example dynamic rule caps each Agent BUY request at 1 USDT",
                "adjusted_value": 1.0,
            }
    return {"outcome": "ALLOW", "reason": "No additional restriction"}
