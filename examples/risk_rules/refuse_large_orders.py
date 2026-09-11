"""Trusted Python rule for the business-risk (`risk`) category.

Load it with the risk `custom_rules` plugin, field MODULE_PATHS:
    MODULE_PATHS=examples/risk_rules/refuse_large_orders.py

This lane sees every market API call on every platform, whoever made it: the writes below, and
reads such as get_order_book, get_candles and list_topics. `operation` is the market API call name,
not a trade side, and `protected_target` is market:<platform>.

Business risk can only allow or refuse. It cannot shrink a call: the size is already bound to a
quote the platform issued, and a read has no size. To cap a size, write the rule for the
agent_policy category instead - see ../agent_policy_rules/cap_trade_size.py.
"""

WRITE_OPERATIONS = {"place_order", "cancel_orders", "redeem", "transfer"}


def evaluate(operation, context):
    if operation == "transfer":
        return {"outcome": "REJECT", "reason": "Example rule never allows a transfer"}
    if operation == "place_order" and float(context.get("notional") or 0) > 25.0:
        return {
            "outcome": "REJECT",
            "reason": "Example rule refuses any single order above 25 USDT",
        }
    return {"outcome": "ALLOW", "reason": "No additional restriction"}
