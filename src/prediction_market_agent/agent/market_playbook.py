"""Short, on-demand prediction-market guides shared by discovery and decision agents.

Only the index is in every prompt. A requested section is returned as an ordinary,
recorded business-tool result, so its content can be audited without inflating every round.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


PLAYBOOK_VERSION = "2026-09-24"

SECTIONS: dict[str, str] = {
    "selection": """Look for a specific, checkable reason a contract may be mispriced: a dated
public catalyst not yet incorporated, a misunderstood resolution clause, or a related contract
whose *executable* quotes disagree. These are research hypotheses, not automatic trades.
An event may contain hundreds of distinct contracts. A bad book on one contract or YES token
says nothing conclusive about its other contracts or NO token. Use TOPIC_DETAIL and a targeted
OUTCOME_BOOK for the actual thesis. Event-reported liquidity/volume is not executable depth;
inspect size at the touch and the order quantity's likely fill. Wide spreads make immediate
round trips expensive, but do not alone rule out a well-supported hold-to-resolution edge.
Do not infer a universal longshot/favorite strategy from historical averages; condition on
market family, remaining time, costs, and local settled outcomes. An absent fair-probability
estimate is a reason to investigate or defer, not proof the market is fairly priced.
Sources: https://docs.polymarket.com/concepts/prices-orderbook ;
https://business.columbia.edu/faculty/research/liquidity-and-prediction-market-efficiency""",
    "execution": """Compare a specific target quantity with available levels, not the displayed
midpoint or event-level liquidity. For a taker BUY held to resolution, the per-share expected
value is estimated win probability minus size-weighted purchase price minus entry fees and other
known costs. No exit bid or exit spread is paid if the position actually settles. For a planned
pre-resolution sale, compare expected size-weighted sale proceeds with the original purchase
cost, allowing for both legs' fees, price movement and adverse selection. Buying today's ask
and immediately selling today's bid crosses ONE full current spread (a half-spread on each leg
versus midpoint), not two full spreads. A maker order might avoid taker fees or improve price,
but execution is uncertain and adverse selection matters; never count an unfilled order as
profit. Polymarket fees may be market- and price-dependent, so a zero or unknown fee must not
be silently treated as a guaranteed zero cost. Recheck live quote and depth before execution.
Sources: https://docs.polymarket.com/concepts/prices-orderbook ;
https://docs.polymarket.com/trading/fees ; https://docs.polymarket.com/trading/place-orders""",
    "resolution": """Read the exact market question, rules, resolving source and any dispute
conditions. Distinguish event end time, the chosen contract's own end time, actual order
acceptance and eventual resolution: they need not coincide. A short remaining interval is not
automatically impossible, but there must be enough time for the actual research, order and
confirmation. A distant resolution locks capital and has opportunity cost; a preferred horizon
is not a hard expiry gate unless the operator explicitly made it one. If wording is ambiguous,
record the unresolved interpretation rather than inventing certainty.
Sources: https://docs.polymarket.com/market-data/market-details ;
https://help.polymarket.com/en/articles/13364518-how-are-prediction-markets-resolved""",
    "structure": """Displayed midpoint probabilities summing above or below one are a signal to
investigate, not proof of arbitrage. Check whether outcomes are actually mutually exclusive
and collectively exhaustive, whether negative-risk conversion applies, and whether every
required leg can be filled at its *ask* or *bid* in the same size after fees and slippage.
Cross-platform comparisons require equivalent wording, resolution source, currency and fees.
Kalshi exposes YES/NO bids, deriving the opposite ask from 1 minus the opposite bid; do not
apply one venue's raw book-array assumptions to another venue.
Sources: https://docs.polymarket.com/concepts/negative-risk-markets ;
https://docs.kalshi.com/getting_started/orderbook_responses""",
    "feedback": """Evaluator output only allocates research attention. Improve it from recorded
subsequent evidence, not from eloquent explanations or a handful of HOLDs: HOLD after a full
decision is a cost-usefulness proxy, not realized P&L, and excluded candidates have censored
outcomes. Keep a safety sample of deferred/rejected items for false-negative review. Compare
like market families, track sample size and later fills/settlement separately, and change one
screening instruction or threshold at a time. Do not auto-relax execution risk rules based on
coarse-screening statistics. Roll back a change if held-out false negatives or net results
worsen. Source: https://academic.oup.com/ej/article-abstract/123/568/491/5079498""",
    "field_notes": """First-person trading postmortems are hypotheses, not proof of a repeatable
edge. Several traders report that paper profits vanished when real orders were late, partially
filled or unfilled, that posting a maker order selected against them, and that public-data
signals were already priced by faster participants. Another LLM-bot postmortem found that vague
prompt advice was interpreted as a live stop-loss rule and that better reasoning did not
create information advantage. Use these reports to demand measured fill rate, decision-to-fill
latency, quoted-versus-actual price, fee, settlement result and false-negative samples by
market family. Do not copy a stranger's fixed price floor, spread cutoff, claimed win rate,
or profitable strategy without reproducing it on this venue and account size.
Social-media wallet-leaderboard claims can omit losing/unredeemed positions and fees; a seller
of data, bots, courses or referral links has an incentive to show attractive gross results.
Treat even a reproducible one-week pre-fee wallet study as a prompt to audit, not a trading rule.
First-person sources (unverified, not official venue data):
https://www.reddit.com/r/Polymarket/comments/1un85mg/i_spent_7_months_testing_every_strategy_on/ ;
https://www.reddit.com/r/Polymarket/comments/1twrp8y/750_a_day_on_paper_but_low_fill_rate_live/ ;
https://leeharden.com/posts/kalshi-trading-bot-after-action ;
https://rlafuente.com/posts/2025-3-5-i-lost-150-market-making-on-kalshi ;
https://telonex.io/research/top-crypto-traders-polymarket-15m""",
}

PLAYBOOK_HASH = hashlib.sha256(
    json.dumps(SECTIONS, sort_keys=True, ensure_ascii=False).encode("utf-8")
).hexdigest()[:12]


def read_market_playbook(arguments: dict[str, Any]) -> dict[str, Any]:
    section = str(arguments.get("section", "")).strip().lower()
    if section not in SECTIONS:
        return {"ok": False, "sections": list(SECTIONS), "error": "unknown playbook section"}
    return {
        "ok": True, "version": PLAYBOOK_VERSION, "hash": PLAYBOOK_HASH,
        "section": section, "guide": SECTIONS[section],
    }
