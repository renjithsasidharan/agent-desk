"""`trade` command. Prints JSON (CSV for candles). Run `trade -h` for the list."""
import argparse
import contextlib
import copy
import csv
import datetime as dt
import fcntl
import json
import os
import sys
import time

from . import config
from .brokers import BrokerError, get_broker
from .engine import Engine, RiskError
from .review import review

AGENT_USER = "agent"


@contextlib.contextmanager
def state_lock(d):
    """One lock per book: commands and the monitor never change a book at the same time."""
    with open(os.path.join(d, "lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def build(book, cfg=None):
    bc = config.book_cfg(cfg or config.load(), book)
    d = config.book_dir(book)
    os.makedirs(d, exist_ok=True)
    return Engine(bc, get_broker(bc, config.secrets(), config.STATE_DIR, d), d)


def out(obj):
    print(json.dumps(obj, indent=1, default=str))


def is_agent():
    return os.environ.get("SUDO_USER") == AGENT_USER


def cmd_status(e, a):
    if a.all:
        cfg = config.load()
        rows = {}
        for b in config.books(cfg):
            st = status_of(build(b, cfg))
            rows[b] = {k: st[k] for k in ("mode", "equity", "day_pnl", "day_loss_left", "trades_today",
                                          "halted_today", "killed")} | {
                "open": len(st["open"]), "plans": len(st["active_plans"])}
        out(rows)
    else:
        out(status_of(e))


def status_of(e):
    s = e.snapshot()
    risk = e.cfg["risk"]
    rows = []
    for t in s["open"]:
        ltp = s["marks"].get(t["symbol"])
        rows.append({k: t.get(k) for k in ("id", "setup", "symbol", "qty", "entry_price", "stop_trigger", "target", "status", "opened_at")}
                    | {"ltp": ltp, "pnl": round(e.unrealized(t, ltp), 2) if ltp else None})
    acct = s["account"]
    return {
        "book": e.cfg["book"], "mode": e.cfg["mode"], "broker": e.broker.name, "now": config.now().isoformat(timespec="seconds"),
        "capital": s["capital"], "equity": s["equity"], "day_pnl": s["day_pnl"],
        "day_loss_left": round(s["daily_loss_limit"] + s["day_pnl"], 2),
        "total_loss_left": round(s["equity"] - s["equity_floor"], 2),
        "trades_today": f"{s['trades_today']}/{risk['max_trades_per_day']}",
        "max_premium_per_trade": round(s["equity"] * risk["max_position_pct"] / 100, 2),
        "entry_window": risk["entry_window"], "square_off_at": risk["square_off_at"],
        "enabled": [k for k, v in e.cfg["instruments"].items() if v.get("enabled")],
        "halted_today": acct.get("halted_day") == config.today() and acct.get("halt_reason"),
        "killed": acct.get("killed"),
        "open": rows,
        "active_plans": [{k: p.get(k) for k in ("id", "setup", "underlying", "type", "strike", "above", "below", "from", "until", "group")}
                         for p in e.plans(statuses=("active",))],
    }


def cmd_spot(e, a):
    names = ["INDIAVIX" if u.upper() == "VIX" else u.upper() for u in a.underlying]
    out({u: e.broker.index_ltp(u) for u in names})


def cmd_expiries(e, a):
    out(e.broker.expiries(a.underlying.upper())[:a.n])


def cmd_chain(e, a):
    u = a.underlying.upper()
    exps = e.broker.expiries(u)
    expiry = {"nearest": exps[0], "next": exps[1]}.get(a.expiry, a.expiry)
    ch = e.broker.option_chain(u, expiry)
    spot = ch["spot"] or e.broker.index_ltp(u)
    strikes = sorted(ch["strikes"])
    atm = min(range(len(strikes)), key=lambda k: abs(strikes[k] - spot))
    oi = {side: {k: (ch["strikes"][k].get(side) or {}).get("oi") or 0 for k in strikes} for side in ("CE", "PE")}
    total = {side: sum(v.values()) for side, v in oi.items()}
    top = {side: sorted(v, key=v.get, reverse=True)[:3] for side, v in oi.items()}
    rows = [{"strike": k, "offset": i - atm, **ch["strikes"][k]}
            for i, k in enumerate(strikes) if abs(i - atm) <= a.width]
    out({"underlying": u, "spot": spot, "expiry": expiry,
         "lot_size": e.broker.contracts(u, expiry)[0].lot_size,
         "pcr_oi": round(total["PE"] / total["CE"], 2) if total["CE"] else None,
         "top_call_oi_strikes": top["CE"], "top_put_oi_strikes": top["PE"],
         "rows": rows})


def cmd_quote(e, a):
    out(e.broker.quote(a.symbol))


def cmd_candles(e, a):
    end = a.to or config.now().strftime("%Y-%m-%d %H:%M:%S")
    start = getattr(a, "from") or (config.now() - dt.timedelta(days=a.days)).strftime("%Y-%m-%d 09:00:00")
    rows = e.broker.candles(a.spec, a.interval, start, end)
    w = csv.writer(sys.stdout)
    w.writerow(["time", "open", "high", "low", "close", "volume", "oi"])
    for r in rows:
        ts = r[0]
        if isinstance(ts, (int, float)):
            ts = dt.datetime.fromtimestamp(ts, config.IST).strftime("%Y-%m-%d %H:%M")
        w.writerow([ts, *(list(r[1:7]) + [None] * 6)[:6]])


def cmd_plan(e, a):
    if a.action == "list":
        out(e.plans(day=a.day or config.today()))
        return
    with state_lock(e.dir):
        if a.action == "cancel":
            out(e.cancel_plan(a.id, a.reason or "cancelled by agent"))
        else:
            out(e.add_plan(a.underlying, a.type, a.strike, a.expiry, a.lots, a.amount, a.sl, a.target,
                           a.above, a.below, a.until, a.group, a.note, a.setup, getattr(a, "from")))


def cmd_enter(e, a):
    with state_lock(e.dir):
        out(e.enter(a.underlying, a.type, a.strike, a.expiry, a.lots, a.amount, a.sl, a.target, a.note, a.setup))


def cmd_exit(e, a):
    with state_lock(e.dir):
        t = e.book()["trades"].get(str(a.id))
        if not t or t["status"] == "closed":
            raise RiskError(f"trade {a.id} is not open")
        out(e.close(t, a.reason or "manual"))


def cmd_exit_all(e, a):
    with state_lock(e.dir):
        out({"errors": e.close_all(a.reason or "manual-all")})


def cmd_trail(e, a):
    with state_lock(e.dir):
        out(e.trail(a.id, a.stop))


def cmd_target(e, a):
    with state_lock(e.dir):
        out(e.set_target(a.id, a.price))


def cmd_trades(e, a):
    out(e.trades(day=a.day or config.today()))


def cmd_review(e, a):
    end = a.to or config.today()
    start = getattr(a, "from") or (dt.date.fromisoformat(end) - dt.timedelta(days=a.days - 1)).isoformat()
    if not a.all:
        out(review(e.trades(), start, end))
        return
    cfg = config.load()
    per = {b: review(build(b, cfg).trades(), start, end) for b in config.books(cfg)}
    every = [t for b in config.books(cfg) for t in build(b, cfg).trades()]
    combined = review(every, start, end)
    if a.json:
        out({"from": start, "to": end, "books": per, "combined": combined})
        return
    print(review_table(per, combined, start, end))


def review_table(per, combined, start, end):
    cols = [("trades", "trades"), ("win_rate_pct", "win %"), ("net_pnl", "net P&L"), ("avg_win", "avg win"),
            ("avg_loss", "avg loss"), ("expectancy_per_trade", "per trade"), ("largest_loss", "worst"),
            ("max_drawdown", "drawdown"), ("charges", "charges")]

    def line(name, st):
        cells = ["-" if st.get(k) is None else (f"{st[k]:,.0f}" if isinstance(st[k], float) and k != "win_rate_pct"
                                                 else str(st[k])) for k, _ in cols]
        return f"{name:<24}" + "".join(f"{c:>11}" for c in cells)

    out_lines = [f"Results {start} to {end} (net of estimated charges, rupees)", "",
                 f"{'':<24}" + "".join(f"{h:>11}" for _, h in cols)]
    for b, r in per.items():
        out_lines.append(line(b, r["overall"]))
    out_lines.append(line("ALL BOOKS", combined["overall"]))
    for b, r in per.items():
        if r["by_setup"]:
            out_lines += ["", f"{b}: by setup"]
            out_lines += [line("  " + k, v) for k, v in r["by_setup"].items()]
    return "\n".join(out_lines)


def cmd_journal(e, a):
    path = os.path.join(e.dir, "journal", f"{a.day or config.today()}.jsonl")
    if os.path.exists(path):
        with open(path) as f:
            sys.stdout.writelines(f.readlines()[-a.tail:])


def cmd_halt(e, a):
    with state_lock(e.dir):
        errors = e.close_all("halt") if a.exit else []
        e.halt(a.reason)
        out({"halted": config.today(), "errors": errors})


def cmd_login(e, a):
    out({"broker": e.broker.name, "profile": e.broker.login()})


def cmd_funds(e, a):
    out(e.broker.funds())


def cmd_monitor(e, a):
    """Watch every book: plans, stops, targets, square-off and loss limits. Runs as a service."""
    if is_agent():
        raise RiskError("the monitor runs as a service; the agent cannot start it")
    cfg = config.load()
    for b in config.books(cfg):
        build(b, cfg).journal("monitor-start", mode=config.book_cfg(cfg, b)["mode"])
    failures = {}
    while True:
        n = config.now()
        if n.weekday() >= 5 or not dt.time(9, 14) <= n.time() <= dt.time(15, 35):
            time.sleep(30)
            continue
        try:
            cfg = config.load()  # limit changes apply without a restart
            names = config.books(cfg)
        except Exception:
            time.sleep(5)
            continue
        for b in names:  # one book's trouble never stops the others being watched
            eng = None
            try:
                eng = build(b, cfg)
                with state_lock(eng.dir):
                    snap = eng.tick()
                status = {k: v for k, v in snap.items() if k != "open"} | {
                    "open_ids": [t["id"] for t in snap["open"]], "updated": n.isoformat(timespec="seconds")}
                tmp = os.path.join(eng.dir, "status.json.tmp")
                with open(tmp, "w") as f:
                    json.dump(status, f, default=str)
                os.replace(tmp, os.path.join(eng.dir, "status.json"))
                failures[b] = 0
            except Exception as ex:
                failures[b] = failures.get(b, 0) + 1
                k = failures[b]
                if eng and (k in (1, 5) or k % 30 == 0):
                    eng.journal("monitor-error", error=f"{type(ex).__name__}: {ex}", failures=k)
        time.sleep(cfg["execution"]["monitor_interval_seconds"] if not any(failures.values())
                   else min(10, max(failures.values())))


def cmd_admin(e, a):
    if is_agent():
        raise RiskError("admin commands are for the owner only")
    with state_lock(e.dir):
        acct = e.account()
        if a.action == "resume":
            acct["halted_day"] = None
        elif a.action == "reset":
            acct.setdefault("history", []).append({"realized": acct["realized"], "killed": acct.get("killed"),
                                                   "reset_at": config.now().isoformat(timespec="seconds")})
            acct.update(realized=0.0, killed=None, halted_day=None)
        elif a.action == "positions":
            out(e.broker.positions())
            return
        e.save_account(acct)
        e.journal(f"admin-{a.action}")
        out(acct)


def cmd_live_check(e, a):
    """One real 1-lot round trip through the live broker: buy, stop placed, stop raised, sold."""
    if is_agent():
        raise RiskError("admin commands are for the owner only")
    cfg = copy.deepcopy(e.cfg)
    cfg["mode"] = "live"
    d = os.path.join(config.STATE_DIR, "livecheck")
    os.makedirs(d, exist_ok=True)
    broker = get_broker(cfg, config.secrets(), config.STATE_DIR, d)
    eng = Engine(cfg, broker, d)
    u = a.underlying.upper()
    expiry = broker.expiries(u)[0]
    calls = [c for c in broker.contracts(u, expiry) if c.option_type == "CE"]
    px = broker.ltp(calls)
    cheap = [c for c in calls if a.min_price <= px.get(c.symbol, 0) <= a.max_price]
    if not cheap:
        raise RiskError(f"no {u} {expiry} call priced {a.min_price}-{a.max_price}")
    c = min(cheap, key=lambda c: abs(px[c.symbol] - (a.min_price + a.max_price) / 2))
    plan = {"contract": c.symbol, "ltp": px[c.symbol], "qty": c.lot_size,
            "premium": round(px[c.symbol] * c.lot_size, 2), "funds": broker.funds()}
    if not a.yes:
        out({"dry_run": True, **plan, "next": "add --yes to place the real orders"})
        return
    steps = {"plan": plan}
    t = None
    try:
        with state_lock(eng.dir):
            r = eng.enter(u, "CE", c.strike, expiry, 1, None, 40, None, "live-check", "live-check")
            steps["enter"] = r
            if not r["ok"]:
                return
            t = r["trade"]
            steps["entry_order_raw"] = broker.last_raw.get(t["entry_order"])
            broker.order(t["stop_order"], c)
            steps["stop_order_raw"] = broker.last_raw.get(t["stop_order"])
            time.sleep(2)
            ltp = broker.ltp([c])[c.symbol]
            new = round(min(t["entry_price"] * 0.7, ltp * 0.9), 2)
            try:
                steps["trail"] = eng.trail(t["id"], new)
                broker.order(t["stop_order"], c)
                steps["stop_after_trail_raw"] = broker.last_raw.get(t["stop_order"])
            except (RiskError, BrokerError) as ex:
                steps["trail"] = f"skipped/failed: {ex}"
            time.sleep(2)
            t = eng.close(eng.book()["trades"][str(t["id"])], "live-check")
            steps["closed"] = {k: t[k] for k in ("entry_price", "exit_price", "qty", "charges", "pnl", "exit_fills")}
            steps["exit_orders_raw"] = {k: v for k, v in broker.last_raw.items()
                                        if k not in (t["entry_order"],)}
            steps["broker_positions"] = broker.positions()
    except Exception as ex:
        steps["error"] = f"{type(ex).__name__}: {ex}"
        if t:
            try:
                eng.close_all("live-check-error")
            except Exception as ex2:
                steps["cleanup_error"] = f"{type(ex2).__name__}: {ex2} -- CHECK THE GROWW APP NOW"
    finally:
        out(steps)


def main():
    p = argparse.ArgumentParser(prog="trade", description="Risk-checked option trading. Output is JSON.")
    p.add_argument("--book", default="main", help="which agent's book (set for you in an agent's session)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="equity, today's P&L, limits left, open trades")
    s.add_argument("--all", action="store_true", help="one line per book")
    s.set_defaults(fn=cmd_status)
    sub.add_parser("books", help="list the books").set_defaults(
        fn=lambda e, a: print("\n".join(config.books(config.load()))))
    s = sub.add_parser("spot", help="index price(s): NIFTY BANKNIFTY SENSEX INDIAVIX")
    s.add_argument("underlying", nargs="+")
    s.set_defaults(fn=cmd_spot)
    s = sub.add_parser("quote", help="bid/ask, depth, volume, OI for an option or future (NIFTY-FUT = nearest)")
    s.add_argument("symbol")
    s.set_defaults(fn=cmd_quote)
    s = sub.add_parser("expiries", help="upcoming expiries")
    s.add_argument("underlying")
    s.add_argument("-n", type=int, default=6)
    s.set_defaults(fn=cmd_expiries)
    s = sub.add_parser("chain", help="option chain around the money: LTP, OI, volume, IV, greeks, PCR")
    s.add_argument("underlying")
    s.add_argument("--expiry", default="nearest", help="nearest | next | YYYY-MM-DD")
    s.add_argument("--width", type=int, default=5, help="strikes each side of ATM")
    s.set_defaults(fn=cmd_chain)
    s = sub.add_parser("candles", help="candles as CSV: index (NIFTY, INDIAVIX), NIFTY-FUT, or an option symbol")
    s.add_argument("spec")
    s.add_argument("--interval", default="5minute", help="1minute 3minute 5minute 15minute 1hour 1day ...")
    s.add_argument("--days", type=int, default=5)
    s.add_argument("--from", help="YYYY-MM-DD HH:MM:SS")
    s.add_argument("--to", help="YYYY-MM-DD HH:MM:SS")
    s.set_defaults(fn=cmd_candles)
    s = sub.add_parser("enter", help="buy an option with a stop-loss")
    s.add_argument("underlying", help="NIFTY | BANKNIFTY | SENSEX")
    s.add_argument("type", choices=["CE", "PE"])
    s.add_argument("--strike", default="ATM", help="ATM, ATM+1, ATM-2 (steps) or a number")
    s.add_argument("--expiry", default="nearest", help="nearest | next | YYYY-MM-DD")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--lots", type=int)
    g.add_argument("--amount", type=float, help="rupees of premium; rounded down to whole lots")
    s.add_argument("--sl", type=float, required=True, help="stop-loss, %% below entry")
    s.add_argument("--target", type=float, help="target, %% above entry (the monitor sells there)")
    s.add_argument("--setup", required=True, help="setup name, e.g. orb-breakout (reuse names; see SETUPS.md)")
    s.add_argument("--note", default="", help="why: setup, reasoning")
    s.set_defaults(fn=cmd_enter)
    s = sub.add_parser("plan", help="planned entries the monitor fires when the index crosses a level")
    ps = s.add_subparsers(dest="action", required=True)
    pa = ps.add_parser("add", help="buy an option when the index goes above/below a level")
    pa.add_argument("underlying")
    pa.add_argument("type", choices=["CE", "PE"])
    pa.add_argument("--strike", default="ATM", help="ATM, ATM+1, ATM-2 (resolved when it fires) or a number")
    pa.add_argument("--expiry", default="nearest")
    g = pa.add_mutually_exclusive_group(required=True)
    g.add_argument("--lots", type=int)
    g.add_argument("--amount", type=float)
    pa.add_argument("--sl", type=float, required=True, help="stop-loss, %% below entry")
    pa.add_argument("--target", type=float, help="target, %% above entry")
    w = pa.add_mutually_exclusive_group(required=True)
    w.add_argument("--above", type=float, help="fire when the index is at or above this")
    w.add_argument("--below", type=float, help="fire when the index is at or below this")
    pa.add_argument("--from", help="HH:MM IST; not active before this (default: right away)")
    pa.add_argument("--until", help="HH:MM IST; expires after (default: entry window end)")
    pa.add_argument("--group", help="plans sharing a group cancel each other when one fires")
    pa.add_argument("--setup", required=True, help="setup name, e.g. orb-breakout (reuse names; see SETUPS.md)")
    pa.add_argument("--note", default="")
    pl = ps.add_parser("list")
    pl.add_argument("--day")
    pc = ps.add_parser("cancel")
    pc.add_argument("id", type=int)
    pc.add_argument("--reason")
    s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("exit", help="sell an open trade now")
    s.add_argument("id", type=int)
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_exit)
    s = sub.add_parser("exit-all", help="sell every open trade now")
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_exit_all)
    s = sub.add_parser("trail", help="raise a trade's stop-loss trigger (never lower)")
    s.add_argument("id", type=int)
    s.add_argument("--stop", type=float, required=True, help="new trigger price")
    s.set_defaults(fn=cmd_trail)
    s = sub.add_parser("target", help="set or change a trade's target price")
    s.add_argument("id", type=int)
    s.add_argument("--price", type=float, required=True)
    s.set_defaults(fn=cmd_target)
    s = sub.add_parser("trades", help="trades for a day")
    s.add_argument("--day")
    s.set_defaults(fn=cmd_trades)
    s = sub.add_parser("review", help="results by setup, underlying, day and exit reason")
    s.add_argument("--all", action="store_true", help="all books side by side, plus the total (a table)")
    s.add_argument("--json", action="store_true", help="with --all: JSON instead of a table")
    s.add_argument("--days", type=int, default=14, help="calendar days back from --to (default 14)")
    s.add_argument("--from", help="YYYY-MM-DD")
    s.add_argument("--to", help="YYYY-MM-DD (default today)")
    s.set_defaults(fn=cmd_review)
    s = sub.add_parser("journal", help="event log for a day")
    s.add_argument("--day")
    s.add_argument("--tail", type=int, default=50)
    s.set_defaults(fn=cmd_journal)
    s = sub.add_parser("halt", help="stop new entries for the rest of today")
    s.add_argument("--reason", default="agent")
    s.add_argument("--exit", action="store_true", help="also sell everything")
    s.set_defaults(fn=cmd_halt)
    sub.add_parser("login", help="check the broker login works").set_defaults(fn=cmd_login)
    sub.add_parser("funds", help="broker margin/funds").set_defaults(fn=cmd_funds)
    sub.add_parser("monitor", help="(service) watch stops, targets and limits").set_defaults(fn=cmd_monitor)
    s = sub.add_parser("admin", help="(owner) resume | reset | positions")
    s.add_argument("action", choices=["resume", "reset", "positions"])
    s.set_defaults(fn=cmd_admin)
    s = sub.add_parser("live-check", help="(owner) one real 1-lot buy+stop+sell to test the live broker")
    s.add_argument("--underlying", default="NIFTY")
    s.add_argument("--min-price", type=float, default=1.0)
    s.add_argument("--max-price", type=float, default=4.0)
    s.add_argument("--yes", action="store_true", help="really place the orders")
    s.set_defaults(fn=cmd_live_check)
    a = p.parse_args()
    try:
        a.fn(build(a.book), a)
    except (RiskError, BrokerError, KeyError, ValueError, IndexError) as ex:
        out({"ok": False, "error": f"{type(ex).__name__}: {ex}"})
        sys.exit(1)


if __name__ == "__main__":
    main()
