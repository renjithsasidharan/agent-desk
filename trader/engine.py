"""Order handling and the risk rules. Every order goes through here."""
import json
import math
import re
import os
import time

from .brokers import BrokerError, Contract, round_tick
from .config import hhmm, now, today


class RiskError(Exception):
    pass


# Journal events that are also sent to Claude's session as they happen.
NOTIFY = {"plan-fired", "plan-failed", "plan-expired", "closed", "halted", "killed",
          "stop-lost", "stop-failed", "exit-incomplete", "entry-unfilled", "entry-unsettled",
          "monitor-error", "position-mismatch"}
MAX_ACTIVE_PLANS = 6


def open_qty(trades):
    """Quantity still held per symbol, from the program's own records."""
    out = {}
    for t in trades:
        if t["status"] in ("open", "exiting"):
            left = t["qty"] - sum(q for q, _ in t["exit_fills"])
            out[t["symbol"]] = out.get(t["symbol"], 0) + left
    return out


def position_diffs(expected, broker_positions):
    """Symbols where the broker's net quantity differs from what the program thinks it holds."""
    held = {p["trading_symbol"]: int(p.get("quantity") or 0) for p in broker_positions}
    return {s: {"program": expected.get(s, 0), "broker": held.get(s, 0)}
            for s in set(expected) | set(held) if expected.get(s, 0) != held.get(s, 0)}


def check_setup(name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,29}", name or ""):
        raise RiskError("setup must be a short lowercase name like orb-breakout or vwap-pullback")


def charges(cfg, entry, exit_, qty):
    ch = cfg["charges"]
    buy, sell = entry * qty, exit_ * qty
    brokerage = 2 * ch["brokerage_per_order"]
    txn = (buy + sell) * ch["txn_pct"] / 100
    sebi = (buy + sell) * ch["sebi_pct"] / 100
    return round(brokerage + txn + sebi
                 + sell * ch["stt_sell_pct"] / 100
                 + (brokerage + txn + sebi) * ch["gst_pct"] / 100
                 + buy * ch["stamp_buy_pct"] / 100, 2)


class Engine:
    def __init__(self, cfg, broker, state_dir):
        self.cfg = cfg
        self.broker = broker
        self.dir = state_dir
        os.makedirs(os.path.join(state_dir, "journal"), exist_ok=True)

    # --- storage ----------------------------------------------------------

    def _read(self, name, default):
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            return default
        with open(path) as f:
            return json.load(f)

    def _write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path + ".tmp", "w") as f:
            json.dump(data, f, indent=1, default=str)
        os.replace(path + ".tmp", path)

    def account(self):
        return self._read("account.json", {"realized": 0.0, "killed": None, "halted_day": None})

    def save_account(self, a):
        self._write("account.json", a)

    def book(self):
        return self._read("trades.json", {"next_id": 1, "trades": {}})

    def save_trade(self, t):
        b = self.book()
        b["trades"][str(t["id"])] = t
        self._write("trades.json", b)

    def journal(self, event, **kw):
        line = json.dumps({"ts": now().isoformat(timespec="seconds"), "event": event, **kw}, default=str)
        with open(os.path.join(self.dir, "journal", f"{today()}.jsonl"), "a") as f:
            f.write(line + "\n")
        if event in NOTIFY:
            with open(os.path.join(self.dir, "events.jsonl"), "a") as f:
                f.write(line + "\n")

    def trades(self, day=None, statuses=None):
        out = []
        for t in self.book()["trades"].values():
            if day and t["day"] != day:
                continue
            if statuses and t["status"] not in statuses:
                continue
            out.append(t)
        return sorted(out, key=lambda t: t["id"])

    # --- numbers ----------------------------------------------------------

    def marks(self, open_trades):
        if not open_trades:
            return {}
        cs = {t["symbol"]: Contract(**t["contract"]) for t in open_trades}
        return self.broker.ltp(list(cs.values()))

    def unrealized(self, t, ltp):
        qty = t["qty"] - sum(q for q, _ in t["exit_fills"])
        return (ltp - t["entry_price"]) * qty - charges(self.cfg, t["entry_price"], ltp, qty)

    def snapshot(self, marks=None):
        open_ = self.trades(statuses=("open", "exiting"))
        marks = self.marks(open_) if marks is None else marks
        acct = self.account()
        day = self.trades(day=today())
        realized_today = sum(t["pnl"] for t in day if t["status"] == "closed")
        unreal = sum(self.unrealized(t, marks[t["symbol"]]) for t in open_ if t["symbol"] in marks)
        capital = self.cfg["capital"]
        risk = self.cfg["risk"]
        # Loss limits in rupees (max_daily_loss / max_total_loss) win over the percentages.
        daily = risk.get("max_daily_loss") or capital * risk["max_daily_loss_pct"] / 100
        floor = capital - (risk.get("max_total_loss") or capital * risk["max_total_loss_pct"] / 100)
        return {
            "capital": capital,
            "equity": round(capital + acct["realized"] + unreal, 2),
            "day_pnl": round(realized_today + unreal, 2),
            "day_realized": round(realized_today, 2),
            "unrealized": round(unreal, 2),
            "daily_loss_limit": round(daily, 2),
            "equity_floor": round(floor, 2),
            "trades_today": len(day),
            "open": open_,
            "marks": marks,
            "account": acct,
        }

    # --- contract lookup --------------------------------------------------

    def resolve(self, underlying, option_type, strike, expiry):
        underlying = underlying.upper()
        exps = self.broker.expiries(underlying)
        if not exps:
            raise RiskError(f"no expiries found for {underlying}")
        if expiry in (None, "nearest"):
            expiry = exps[0]
        elif expiry == "next":
            expiry = exps[1]
        elif expiry not in exps:
            raise RiskError(f"{underlying} has no expiry {expiry}; available: {exps[:6]}")
        cs = [c for c in self.broker.contracts(underlying, expiry) if c.option_type == option_type.upper()]
        strikes = sorted({c.strike for c in cs})
        s = str(strike).upper()
        if s.startswith("ATM"):
            spot = self.broker.index_ltp(underlying)
            i = min(range(len(strikes)), key=lambda k: abs(strikes[k] - spot))
            i += int(s[3:] or 0)
            if not 0 <= i < len(strikes):
                raise RiskError(f"strike {strike} out of range")
            target = strikes[i]
        else:
            target = float(strike)
        for c in cs:
            if c.strike == target:
                return c
        raise RiskError(f"no {underlying} {target} {option_type} for {expiry}")

    # --- risk -------------------------------------------------------------

    def check_can_enter(self, underlying):
        acct = self.account()
        risk = self.cfg["risk"]
        if acct.get("killed"):
            raise RiskError(f"trading stopped: {acct['killed']['reason']}. An admin must reset it.")
        if acct.get("halted_day") == today():
            raise RiskError(f"trading halted for today: {acct.get('halt_reason')}")
        if not self.cfg["instruments"].get(underlying, {}).get("enabled"):
            raise RiskError(f"{underlying} is not enabled in config")
        t = now()
        start, end = (hhmm(x) for x in risk["entry_window"])
        if t.weekday() >= 5 or not start <= t.time() <= end:
            raise RiskError(f"outside entry window {risk['entry_window']} IST")
        if len(self.trades(statuses=("open", "exiting"))) >= risk["max_open_positions"]:
            raise RiskError(f"already {risk['max_open_positions']} open positions")
        if len(self.trades(day=today())) >= risk["max_trades_per_day"]:
            raise RiskError(f"already {risk['max_trades_per_day']} trades today")

    # --- actions ----------------------------------------------------------

    def _wait(self, oid, c, seconds):
        deadline = time.time() + seconds
        while True:
            r = self.broker.order(oid, c)
            if r.status != "OPEN" or time.time() >= deadline:
                return r
            time.sleep(0.5)

    def _cancel(self, oid, c):
        """Cancel and wait until the broker settles the order (CANCELLED, FILLED or REJECTED).
        A cancel is only a request: the order can still fill until the broker confirms."""
        try:
            self.broker.cancel(oid, c)
        except BrokerError:
            pass  # usually already filled or cancelled; the status below tells
        seconds = self.cfg["execution"].get("cancel_wait_seconds", 10)
        r = self._wait(oid, c, seconds)
        if r.status == "OPEN":
            raise BrokerError(f"order {oid} not settled {seconds}s after cancel ({r.message}, filled {r.filled_qty})")
        return r

    def enter(self, underlying, option_type, strike, expiry, lots, amount, sl_pct, target_pct, note,
              setup="untagged"):
        underlying = underlying.upper()
        check_setup(setup)
        risk, ex = self.cfg["risk"], self.cfg["execution"]
        self.check_can_enter(underlying)
        if not sl_pct or sl_pct <= 0 or sl_pct > risk["max_sl_pct"]:
            raise RiskError(f"stop-loss must be between 0 and {risk['max_sl_pct']}% of entry")
        c = self.resolve(underlying, option_type, strike, expiry)
        ltp = self.broker.ltp([c])[c.symbol]
        if lots:
            qty = int(lots) * c.lot_size
        elif amount:
            qty = math.floor(amount / (ltp * c.lot_size)) * c.lot_size
        else:
            raise RiskError("give --lots or --amount")
        if qty <= 0:
            raise RiskError(f"one lot costs {ltp * c.lot_size:.0f}, more than the amount")
        if c.freeze_qty and qty > c.freeze_qty:
            raise RiskError(f"quantity {qty} above exchange freeze limit {c.freeze_qty}")

        snap = self.snapshot()
        cost = qty * ltp
        max_cost = snap["equity"] * risk["max_position_pct"] / 100
        if cost > max_cost:
            max_lots = int(max_cost // (ltp * c.lot_size))
            raise RiskError(f"premium {cost:.0f} above per-trade limit {max_cost:.0f} (max {max_lots} lots at {ltp})")
        worst = cost * sl_pct / 100 + charges(self.cfg, ltp, ltp * (1 - sl_pct / 100), qty)
        room_day = snap["daily_loss_limit"] + snap["day_pnl"]
        if worst > room_day:
            raise RiskError(f"worst case loss {worst:.0f} exceeds what is left of today's loss limit ({room_day:.0f})")
        room_total = snap["equity"] - snap["equity_floor"]
        if worst > room_total:
            raise RiskError(f"worst case loss {worst:.0f} would breach the total loss floor ({room_total:.0f} left)")

        limit = round_tick(ltp * (1 + ex["entry_buffer_pct"] / 100), c.tick, "up")
        oid = self.broker.place(c, "BUY", qty, "LIMIT", limit)
        self.journal("entry-order", symbol=c.symbol, qty=qty, limit=limit, ltp=ltp, order=oid, note=note)
        r = self._wait(oid, c, ex["fill_wait_seconds"])
        if r.status == "OPEN":
            try:
                r = self._cancel(oid, c)
            except BrokerError as e:
                self.journal("entry-unsettled", symbol=c.symbol, order=oid, error=str(e))
                raise BrokerError(f"{e}. It may still fill: check `trade admin positions` / the broker") from e
        if not r.filled_qty:
            self.journal("entry-unfilled", symbol=c.symbol, order=oid, status=r.status, message=r.message)
            return {"ok": False, "reason": f"entry not filled ({r.status} {r.message})", "order": oid}

        entry = r.avg_price or limit
        b = self.book()
        t = {
            "id": b["next_id"], "day": today(), "underlying": underlying, "symbol": c.symbol,
            "contract": c.to_dict(), "qty": r.filled_qty, "entry_price": entry, "entry_order": oid,
            "stop_order": None, "stop_trigger": None, "stop_price": None, "target": None,
            "status": "open", "exit_fills": [], "exit_reason": None, "pnl": None,
            "opened_at": now().isoformat(timespec="seconds"), "closed_at": None, "note": note, "setup": setup,
        }
        b["next_id"] += 1
        b["trades"][str(t["id"])] = t
        self._write("trades.json", b)
        self.journal("entered", id=t["id"], symbol=c.symbol, qty=t["qty"], price=entry, setup=setup)

        trigger = round_tick(entry * (1 - sl_pct / 100), c.tick, "down")
        try:
            self._place_stop(t, c, trigger)
        except BrokerError as e:
            self.journal("stop-failed", id=t["id"], error=str(e))
            self.close(t, "stop-placement-failed")
            raise
        if target_pct:
            t["target"] = round_tick(entry * (1 + target_pct / 100), c.tick, "up")
        self.save_trade(t)
        return {"ok": True, "trade": t}

    def _place_stop(self, t, c, trigger):
        price = round_tick(trigger * (1 - self.cfg["execution"]["stop_limit_gap_pct"] / 100), c.tick, "down")
        qty = t["qty"] - sum(q for q, _ in t["exit_fills"])
        t["stop_order"] = self.broker.place(c, "SELL", qty, "SL", price, trigger)
        t["stop_trigger"], t["stop_price"] = trigger, price
        self.save_trade(t)
        self.journal("stop-placed", id=t["id"], trigger=trigger, price=price, order=t["stop_order"])

    def trail(self, trade_id, new_trigger):
        t = self.book()["trades"].get(str(trade_id))
        if not t or t["status"] != "open":
            raise RiskError(f"trade {trade_id} is not open")
        c = Contract(**t["contract"])
        new_trigger = round_tick(new_trigger, c.tick, "down")
        if new_trigger <= t["stop_trigger"]:
            raise RiskError(f"stop can only move up (now {t['stop_trigger']})")
        ltp = self.broker.ltp([c])[c.symbol]
        if new_trigger >= ltp:
            raise RiskError(f"stop {new_trigger} must be below the current price {ltp}")
        price = round_tick(new_trigger * (1 - self.cfg["execution"]["stop_limit_gap_pct"] / 100), c.tick, "down")
        qty = t["qty"] - sum(q for q, _ in t["exit_fills"])
        self.broker.modify(t["stop_order"], c, qty, "SL", price, new_trigger)
        old = t["stop_trigger"]
        t["stop_trigger"], t["stop_price"] = new_trigger, price
        self.save_trade(t)
        self.journal("stop-moved", id=t["id"], old=old, new=new_trigger)
        return t

    def set_target(self, trade_id, price):
        t = self.book()["trades"].get(str(trade_id))
        if not t or t["status"] != "open":
            raise RiskError(f"trade {trade_id} is not open")
        t["target"] = price
        self.save_trade(t)
        self.journal("target-set", id=t["id"], target=price)
        return t

    def close(self, t, reason):
        """Exit a trade: cancel its stop, then sell what is left, widening the price on each try."""
        c = Contract(**t["contract"])
        ex = self.cfg["execution"]
        if t["status"] == "open":
            t["status"], t["exit_reason"] = "exiting", reason
            self.save_trade(t)
        # Settle the stop and any earlier exit order before selling again, or both could fill.
        # If one will not settle, this raises with the order id still saved; the monitor retries.
        for key, fallback in (("stop_order", t.get("stop_price")), ("exit_order", t.get("exit_price_try"))):
            if not t.get(key):
                continue
            r = self.broker.order(t[key], c)
            if r.status == "OPEN":
                r = self._cancel(t[key], c)
            if r.filled_qty:
                t["exit_fills"].append([r.filled_qty, r.avg_price or fallback])
                if key == "stop_order" and r.status == "FILLED":
                    t["exit_reason"] = "stop"
            t[key] = None
            self.save_trade(t)
        left = t["qty"] - sum(q for q, _ in t["exit_fills"])
        attempt = 0
        while left > 0 and attempt < 4:
            ltp = self.broker.ltp([c])[c.symbol]
            px = round_tick(ltp * (1 - (ex["exit_buffer_pct"] + 3 * attempt) / 100), c.tick, "down")
            oid = self.broker.place(c, "SELL", left, "LIMIT", px)
            t["exit_order"], t["exit_price_try"] = oid, px
            self.save_trade(t)
            r = self._wait(oid, c, 5)
            if r.status == "OPEN":
                r = self._cancel(oid, c)
            if r.filled_qty:
                t["exit_fills"].append([r.filled_qty, r.avg_price or px])
                left -= r.filled_qty
            t["exit_order"] = None
            self.save_trade(t)
            attempt += 1
        if left > 0:
            self.journal("exit-incomplete", id=t["id"], left=left)
            raise BrokerError(f"trade {t['id']}: {left} still unsold after 4 tries; the monitor keeps trying")
        self._finish(t)
        return t

    def _finish(self, t):
        sold = sum(q for q, _ in t["exit_fills"])
        avg = sum(q * p for q, p in t["exit_fills"]) / sold
        ch = charges(self.cfg, t["entry_price"], avg, t["qty"])
        t.update(status="closed", exit_price=round(avg, 2), charges=ch,
                 pnl=round((avg - t["entry_price"]) * t["qty"] - ch, 2),
                 closed_at=now().isoformat(timespec="seconds"))
        self.save_trade(t)
        acct = self.account()
        acct["realized"] = round(acct["realized"] + t["pnl"], 2)
        self.save_account(acct)
        self.journal("closed", id=t["id"], symbol=t["symbol"], setup=t.get("setup"), reason=t["exit_reason"],
                     entry=t["entry_price"], exit=t["exit_price"], qty=t["qty"], pnl=t["pnl"])

    def close_all(self, reason):
        errors = []
        for t in self.trades(statuses=("open", "exiting")):
            try:
                self.close(t, reason)
            except BrokerError as e:
                errors.append(str(e))
        return errors

    def halt(self, reason):
        acct = self.account()
        acct["halted_day"], acct["halt_reason"] = today(), reason
        self.save_account(acct)
        self.journal("halted", reason=reason)

    # --- planned entries: Claude sets them, the monitor fires them ---------

    def plan_book(self):
        return self._read("plans.json", {"next_id": 1, "plans": {}})

    def plans(self, statuses=None, day=None):
        ps = self.plan_book()["plans"].values()
        return sorted((p for p in ps if (not statuses or p["status"] in statuses) and (not day or p["day"] == day)),
                      key=lambda p: p["id"])

    def _save_plan(self, p):
        b = self.plan_book()
        b["plans"][str(p["id"])] = p
        self._write("plans.json", b)

    def add_plan(self, underlying, option_type, strike, expiry, lots, amount, sl_pct, target_pct,
                 above, below, until, group, note, setup="untagged", start=None):
        underlying = underlying.upper()
        check_setup(setup)
        risk = self.cfg["risk"]
        acct = self.account()
        if acct.get("killed") or acct.get("halted_day") == today():
            raise RiskError("trading is stopped; no new plans")
        if not self.cfg["instruments"].get(underlying, {}).get("enabled"):
            raise RiskError(f"{underlying} is not enabled in config")
        if (above is None) == (below is None):
            raise RiskError("give exactly one of --above / --below")
        if not sl_pct or sl_pct <= 0 or sl_pct > risk["max_sl_pct"]:
            raise RiskError(f"stop-loss must be between 0 and {risk['max_sl_pct']}% of entry")
        if not (lots or amount):
            raise RiskError("give --lots or --amount")
        last = risk["entry_window"][1]
        until = until or last
        if hhmm(until) > hhmm(last):
            raise RiskError(f"--until must be at or before the entry window end {last}")
        if start and hhmm(start) >= hhmm(until):
            raise RiskError(f"--from {start} must be before --until {until}")
        if len(self.plans(statuses=("active",))) >= MAX_ACTIVE_PLANS:
            raise RiskError(f"already {MAX_ACTIVE_PLANS} active plans; cancel some first")
        spot = self.broker.index_ltp(underlying)
        if (above is not None and spot >= above) or (below is not None and spot <= below):
            raise RiskError(f"{underlying} is already at {spot}; the trigger would fire at once (use enter)")
        b = self.plan_book()
        p = {"id": b["next_id"], "day": today(), "status": "active", "underlying": underlying,
             "type": option_type.upper(), "strike": str(strike), "expiry": expiry, "lots": lots,
             "amount": amount, "sl": sl_pct, "target": target_pct, "above": above, "below": below,
             "from": start, "until": until, "group": group, "note": note, "setup": setup, "spot_at_creation": spot,
             "created_at": now().isoformat(timespec="seconds")}
        b["next_id"] += 1
        b["plans"][str(p["id"])] = p
        self._write("plans.json", b)
        self.journal("plan-added", **{k: p[k] for k in ("id", "underlying", "type", "strike", "above", "below", "from", "until", "group")})
        return p

    def cancel_plan(self, plan_id, reason="cancelled"):
        p = self.plan_book()["plans"].get(str(plan_id))
        if not p or p["status"] != "active":
            raise RiskError(f"plan {plan_id} is not active")
        p.update(status="cancelled", reason=reason)
        self._save_plan(p)
        self.journal("plan-cancelled", id=p["id"], reason=reason)
        return p

    def check_plans(self):
        active = self.plans(statuses=("active",))
        if not active:
            return
        acct = self.account()
        if acct.get("killed") or acct.get("halted_day") == today():
            for p in active:
                self.cancel_plan(p["id"], "trading stopped")
            return
        t = now()
        live = []
        for p in active:
            if p["day"] != today() or t.time() > hhmm(p["until"]):
                p.update(status="expired")
                self._save_plan(p)
                self.journal("plan-expired", id=p["id"], until=p["until"])
            else:
                live.append(p)
        spots = {u: self.broker.index_ltp(u) for u in {p["underlying"] for p in live}}
        if t.time() < hhmm(self.cfg["risk"]["entry_window"][0]):
            return
        for p in live:
            if self.plan_book()["plans"][str(p["id"])]["status"] != "active":
                continue  # cancelled by a sibling that fired this tick
            if p.get("from") and t.time() < hhmm(p["from"]):
                continue
            spot = spots[p["underlying"]]
            hit = (p["above"] is not None and spot >= p["above"]) or (p["below"] is not None and spot <= p["below"])
            if not hit:
                continue
            try:
                r = self.enter(p["underlying"], p["type"], p["strike"], p["expiry"], p["lots"], p["amount"],
                               p["sl"], p["target"], f"plan {p['id']}: {p['note']}", p.get("setup", "untagged"))
            except (RiskError, BrokerError) as e:
                p.update(status="failed", reason=str(e), fired_at=t.isoformat(timespec="seconds"))
                self._save_plan(p)
                self.journal("plan-failed", id=p["id"], spot=spot, error=str(e))
                continue
            if r["ok"]:
                tr = r["trade"]
                p.update(status="fired", trade_id=tr["id"], fired_at=t.isoformat(timespec="seconds"))
                self._save_plan(p)
                self.journal("plan-fired", id=p["id"], spot=spot, trade=tr["id"], symbol=tr["symbol"],
                             qty=tr["qty"], price=tr["entry_price"], stop=tr["stop_trigger"], target=tr["target"])
                if p.get("group"):
                    for other in self.plans(statuses=("active",)):
                        if other.get("group") == p["group"]:
                            self.cancel_plan(other["id"], f"group {p['group']}: plan {p['id']} fired")
            else:
                p.update(status="failed", reason=r["reason"], fired_at=t.isoformat(timespec="seconds"))
                self._save_plan(p)
                self.journal("plan-failed", id=p["id"], spot=spot, error=r["reason"])

    # --- the monitor's job, every few seconds -----------------------------

    def _watch_trade(self, t, ltp):
        c = Contract(**t["contract"])
        if t["status"] == "exiting":
            self.close(t, t["exit_reason"])
            return
        r = self.broker.order(t["stop_order"], c)
        if r.status == "FILLED":
            t["exit_fills"].append([r.filled_qty, r.avg_price or t["stop_price"]])
            t["exit_reason"], t["stop_order"] = "stop", None
            self._finish(t)
        elif r.status in ("CANCELLED", "REJECTED"):
            self.journal("stop-lost", id=t["id"], status=r.status, message=r.message)
            try:
                self._place_stop(t, c, t["stop_trigger"])
            except BrokerError:
                self.close(t, "stop-lost")
        elif ltp is not None and ltp < t["stop_price"]:
            self.close(t, "stop-gap")  # stop triggered but price fell through its limit
        elif ltp is not None and t["target"] and ltp >= t["target"]:
            self.close(t, "target")

    def tick(self):
        open_ = self.trades(statuses=("open", "exiting"))
        marks = self.marks(open_)
        for t in open_:
            try:
                self._watch_trade(t, marks.get(t["symbol"]))
            except BrokerError as e:
                # Retried every tick; tell Claude at most once a minute per trade.
                last = getattr(self, "_reported", {}).get(t["id"], 0)
                if time.time() - last > 60:
                    self.journal("exit-incomplete", id=t["id"], error=str(e))
                    self._reported = {**getattr(self, "_reported", {}), t["id"]: time.time()}

        snap = self.snapshot(marks)
        acct = snap["account"]
        if snap["open"] and now().time() >= hhmm(self.cfg["risk"]["square_off_at"]):
            self.close_all("square-off")
        if snap["day_pnl"] <= -snap["daily_loss_limit"] and acct.get("halted_day") != today():
            self.close_all("daily-loss-limit")
            self.halt(f"daily loss {snap['day_pnl']} hit limit {snap['daily_loss_limit']}")
        if snap["equity"] <= snap["equity_floor"] and not acct.get("killed"):
            self.close_all("total-loss-limit")
            acct = self.account()
            acct["killed"] = {"reason": f"equity {snap['equity']} at or below floor {snap['equity_floor']}",
                              "at": now().isoformat(timespec="seconds")}
            self.save_account(acct)
            self.journal("killed", **acct["killed"])
        self.check_plans()
        return snap
