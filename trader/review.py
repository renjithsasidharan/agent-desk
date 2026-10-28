"""Results of closed trades, broken down by setup, underlying, day and exit reason."""
from collections import defaultdict


def stats(trades):
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n = len(pnls)
    return {
        "trades": n,
        "wins": len(wins),
        "win_rate_pct": round(100 * len(wins) / n, 1) if n else None,
        "net_pnl": round(sum(pnls), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "expectancy_per_trade": round(sum(pnls) / n, 2) if n else None,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "largest_loss": round(min(pnls), 2) if pnls else None,
        "charges": round(sum(t.get("charges") or 0 for t in trades), 2),
    }


def max_drawdown(trades):
    peak = equity = worst = 0.0
    for t in sorted(trades, key=lambda t: t["closed_at"]):
        equity += t["pnl"]
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 2)


def review(trades, start, end):
    closed = [t for t in trades if t["status"] == "closed" and start <= t["day"] <= end]

    def by(key):
        groups = defaultdict(list)
        for t in closed:
            groups[key(t)].append(t)
        return {k: stats(v) for k, v in sorted(groups.items())}

    reasons = defaultdict(int)
    for t in closed:
        reasons[t["exit_reason"]] += 1
    days = by(lambda t: t["day"])
    return {
        "from": start, "to": end,
        "overall": stats(closed) | {"max_drawdown": max_drawdown(closed), "trading_days": len(days)},
        "by_setup": by(lambda t: t.get("setup") or "untagged"),
        "by_underlying": by(lambda t: t["underlying"]),
        "by_option_type": by(lambda t: t["contract"]["option_type"]),
        "exit_reasons": dict(reasons),
        "by_day": {d: {k: s[k] for k in ("trades", "net_pnl", "win_rate_pct")} for d, s in days.items()},
        "still_open": [t["id"] for t in trades if t["status"] != "closed"],
    }
