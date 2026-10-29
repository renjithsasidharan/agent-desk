"""Engine + paper broker against fake prices. Run: python -m pytest -q tests  (or python tests/test_engine.py)"""
import copy
import datetime as dt
import os
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from trader import engine as engine_mod  # noqa: E402
from trader.brokers import paper as paper_mod  # noqa: E402
from trader.brokers.base import Broker, Contract  # noqa: E402
from trader.config import IST  # noqa: E402
from trader.engine import Engine, RiskError  # noqa: E402

CFG = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")))
CLOCK = [dt.datetime(2026, 11, 2, 10, 0, tzinfo=IST)]  # a Monday
for mod in (engine_mod, paper_mod):
    mod.now = lambda: CLOCK[0]
engine_mod.today = lambda: CLOCK[0].date().isoformat()


class FakeData(Broker):
    name = "fake"

    def __init__(self):
        self.spot = 22810.0
        self.px = {}

    def _c(self, strike, t):
        return Contract(f"NIFTY26N03{int(strike)}{t}", "NSE", "NIFTY", "2026-11-03", strike, t, 65, 0.05, 1800)

    def login(self): return {}
    def index_ltp(self, u): return self.spot
    def ltp(self, cs): return {c.symbol: self.px.get(c.symbol, 100.0) for c in cs}
    def expiries(self, u): return ["2026-11-03", "2026-11-10"]
    def contracts(self, u, e): return [self._c(s, t) for s in range(22500, 23150, 50) for t in ("CE", "PE")]
    def contract(self, s): raise NotImplementedError
    def place(self, *a, **k): raise AssertionError("paper must not place real orders")
    modify = cancel = order = place
    def candles(self, *a): return []
    def funds(self): return {}
    def positions(self): return []


def setup(**risk):
    cfg = copy.deepcopy(CFG)
    cfg["risk"].update(risk)
    d = tempfile.mkdtemp()
    data = FakeData()
    e = Engine(cfg, paper_mod.PaperBroker(data, d, cfg["paper"]["slippage_pct"]), d)
    return e, data


def test_enter_places_stop_and_stop_fills():
    e, data = setup()
    r = e.enter("NIFTY", "CE", "ATM", "nearest", 2, None, 25, 40, "t")
    t = r["trade"]
    assert t["symbol"] == "NIFTY26N0322800CE" and t["qty"] == 130
    assert t["entry_price"] == 100.5 and t["stop_trigger"] == 75.35 and t["target"] == 140.7
    data.px[t["symbol"]] = 74.0  # through the trigger, above the stop limit (71.55)
    e.tick()
    t = e.book()["trades"]["1"]
    assert t["status"] == "closed" and t["exit_reason"] == "stop" and t["pnl"] < 0


def test_target_exit():
    e, data = setup()
    t = e.enter("NIFTY", "PE", "ATM-1", "nearest", 1, None, 20, 30, "t")["trade"]
    data.px[t["symbol"]] = 135.0
    e.tick()
    t = e.book()["trades"]["1"]
    assert t["status"] == "closed" and t["exit_reason"] == "target" and t["pnl"] > 0


def test_gap_through_stop_still_exits():
    e, data = setup()
    t = e.enter("NIFTY", "CE", "ATM", "nearest", 1, None, 25, None, "t")["trade"]
    data.px[t["symbol"]] = 60.0  # below the stop limit price: the SL order cannot fill
    e.tick()
    t = e.book()["trades"]["1"]
    assert t["status"] == "closed" and t["exit_reason"] == "stop-gap"


def test_limits_refuse_entries():
    e, data = setup()
    for kwargs, msg in [
        (dict(lots=10), "per-trade limit"),           # 650 qty * 100 = 65k > 30% of 1L
        (dict(lots=1, sl=50), "stop-loss must be"),
    ]:
        try:
            e.enter("NIFTY", "CE", "ATM", "nearest", kwargs.get("lots"), None, kwargs.get("sl", 25), None, "")
            raise AssertionError("should have refused")
        except RiskError as ex:
            assert msg in str(ex), ex
    try:
        e.enter("SENSEX", "CE", "ATM", "nearest", 1, None, 25, None, "")
        raise AssertionError("SENSEX is disabled")
    except RiskError as ex:
        assert "not enabled" in str(ex)
    CLOCK[0] = CLOCK[0].replace(hour=15, minute=5)
    try:
        e.enter("NIFTY", "CE", "ATM", "nearest", 1, None, 25, None, "")
        raise AssertionError("outside window")
    except RiskError as ex:
        assert "entry window" in str(ex)
    finally:
        CLOCK[0] = CLOCK[0].replace(hour=10, minute=0)


def test_worst_case_must_fit_daily_limit():
    e, data = setup(max_daily_loss_pct=2)  # 2,000 rupees
    try:
        e.enter("NIFTY", "CE", "ATM", "nearest", 3, None, 40, None, "")  # 195*100*40% = 7.8k
        raise AssertionError("should refuse")
    except RiskError as ex:
        assert "today's loss limit" in str(ex)


def test_daily_loss_limit_halts_and_flattens():
    e, data = setup(max_daily_loss_pct=5)
    # Entries are sized so a stop-out fits the limit; only a gap through the stop can breach it.
    a = e.enter("NIFTY", "CE", "ATM", "nearest", 4, None, 15, None, "")["trade"]   # 260 qty, worst ~4k
    data.px[a["symbol"]] = 70.0   # gaps through the stop limit: ~ -8k
    e.tick()
    t = e.book()["trades"]["1"]
    assert t["status"] == "closed" and t["exit_reason"] == "stop-gap"
    assert e.account()["halted_day"] == "2026-11-02"
    try:
        e.enter("NIFTY", "CE", "ATM", "nearest", 1, None, 25, None, "")
        raise AssertionError("halted")
    except RiskError as ex:
        assert "halted" in str(ex)


def test_total_loss_limit_kills():
    e, data = setup(max_daily_loss_pct=5, max_total_loss_pct=5)
    a = e.enter("NIFTY", "CE", "ATM", "nearest", 4, None, 15, None, "")["trade"]
    data.px[a["symbol"]] = 70.0
    e.tick()
    assert e.account()["killed"]
    CLOCK[0] = CLOCK[0] + dt.timedelta(days=1)
    try:
        e.enter("NIFTY", "CE", "ATM", "nearest", 1, None, 25, None, "")
        raise AssertionError("killed")
    except RiskError as ex:
        assert "admin must reset" in str(ex)
    finally:
        CLOCK[0] = CLOCK[0] - dt.timedelta(days=1)


def test_square_off():
    e, data = setup()
    e.enter("NIFTY", "CE", "ATM", "nearest", 1, None, 25, None, "")
    CLOCK[0] = CLOCK[0].replace(hour=15, minute=15)
    try:
        e.tick()
    finally:
        CLOCK[0] = CLOCK[0].replace(hour=10, minute=0)
    assert e.book()["trades"]["1"]["exit_reason"] == "square-off"


def test_trail_only_up():
    e, data = setup()
    t = e.enter("NIFTY", "CE", "ATM", "nearest", 1, None, 25, None, "")["trade"]
    data.px[t["symbol"]] = 120.0
    assert e.trail(1, 100)["stop_trigger"] == 100
    try:
        e.trail(1, 90)
        raise AssertionError("lowered")
    except RiskError:
        pass
    data.px[t["symbol"]] = 99.0  # trailed stop triggers, fills above its limit (95)
    e.tick()
    t = e.book()["trades"]["1"]
    assert t["exit_reason"] == "stop" and t["pnl"] < 0 < t["exit_price"] - 95


def test_plan_fires_and_group_cancels_sibling():
    e, data = setup()
    e.add_plan("NIFTY", "CE", "ATM", "nearest", 1, None, 25, 40, 22850, None, "11:30", "orb", "long")
    e.add_plan("NIFTY", "PE", "ATM", "nearest", 1, None, 25, 40, None, 22700, "11:30", "orb", "short")
    e.tick()
    assert [p["status"] for p in e.plans()] == ["active", "active"]
    data.spot = 22861.0
    e.tick()
    p1, p2 = e.plans()
    assert p1["status"] == "fired" and p2["status"] == "cancelled"
    t = e.book()["trades"][str(p1["trade_id"])]
    assert t["symbol"] == "NIFTY26N0322850CE" and t["stop_order"] and t["target"]  # ATM worked out at fire time
    events = open(os.path.join(e.dir, "events.jsonl")).read()
    assert '"plan-fired"' in events and '"plan-added"' not in events


def test_plan_refuses_trigger_already_hit():
    e, data = setup()
    try:
        e.add_plan("NIFTY", "CE", "ATM", "nearest", 1, None, 25, None, 22800, None, None, None, "")
        raise AssertionError("should refuse")
    except RiskError as ex:
        assert "already at" in str(ex)


def test_plan_expires_and_waits_for_window():
    e, data = setup()
    CLOCK[0] = CLOCK[0].replace(hour=9, minute=16)
    try:
        e.add_plan("NIFTY", "CE", "ATM", "nearest", 1, None, 25, None, 22850, None, "09:30", None, "")
        data.spot = 22900.0
        e.tick()  # before the 09:20 entry window: no fire
        assert e.plans()[0]["status"] == "active"
        CLOCK[0] = CLOCK[0].replace(minute=31)
        e.tick()
        assert e.plans()[0]["status"] == "expired"
    finally:
        CLOCK[0] = CLOCK[0].replace(hour=10, minute=0)


def test_plan_fails_on_risk_limit():
    e, data = setup()
    e.add_plan("NIFTY", "CE", "ATM", "nearest", 10, None, 25, None, 22850, None, None, None, "")
    data.spot = 22870.0
    e.tick()
    p = e.plans()[0]
    assert p["status"] == "failed" and "per-trade limit" in p["reason"]
    assert '"plan-failed"' in open(os.path.join(e.dir, "events.jsonl")).read()


def test_setup_label_and_review():
    from trader.review import review
    e, data = setup()
    try:
        e.enter("NIFTY", "CE", "ATM", "nearest", 1, None, 25, 30, "", "Bad Name!")
        raise AssertionError("bad name accepted")
    except RiskError:
        pass
    a = e.enter("NIFTY", "CE", "ATM", "nearest", 1, None, 25, 30, "", "orb-breakout")["trade"]
    data.px[a["symbol"]] = 135.0
    e.tick()                                   # target: win
    data.px[a["symbol"]] = 100.0
    e.add_plan("NIFTY", "PE", "ATM", "nearest", 1, None, 20, None, None, 22700, None, None, "", "vwap-pullback")
    data.spot = 22690.0
    e.tick()                                   # plan fires with its setup
    b = e.trades(statuses=("open",))[0]
    assert b["setup"] == "vwap-pullback"
    data.px[b["symbol"]] = 79.0
    e.tick()                                   # stop: loss
    r = review(e.trades(), "2026-11-01", "2026-11-02")
    assert r["overall"]["trades"] == 2 and r["overall"]["wins"] == 1
    assert r["by_setup"]["orb-breakout"]["net_pnl"] > 0 > r["by_setup"]["vwap-pullback"]["net_pnl"]
    assert r["exit_reasons"] == {"target": 1, "stop": 1} and r["overall"]["max_drawdown"] < 0


class RacyPaper(paper_mod.PaperBroker):
    """Cancels are slow: the order stays OPEN for `hold` polls, then `outcome` happens."""
    hold, outcome = 0, "CANCELLED"

    def cancel(self, order_id, c):
        self.pending = getattr(self, "pending", {})
        self.pending[order_id] = self.hold

    def order(self, order_id, c):
        n = getattr(self, "pending", {}).get(order_id)
        if n is not None:
            if n > 0:
                self.pending[order_id] = n - 1
            else:
                orders = self._load()
                o = orders[order_id]
                del self.pending[order_id]
                if self.outcome == "FILLED":
                    o.update(status="FILLED", filled_qty=o["qty"], avg_price=o["price"])
                else:
                    o["status"] = "CANCELLED"
                self._save(orders)
        return super().order(order_id, c)


def racy_setup(hold, outcome):
    cfg = copy.deepcopy(CFG)
    cfg["execution"]["cancel_wait_seconds"] = 1
    d = tempfile.mkdtemp()
    data = FakeData()
    b = RacyPaper(data, d, cfg["paper"]["slippage_pct"])
    b.hold, b.outcome = hold, outcome
    return Engine(cfg, b, d), data, b


def test_stop_that_fills_during_cancel_is_not_sold_twice():
    e, data, b = racy_setup(hold=1, outcome="FILLED")
    t = e.enter("NIFTY", "CE", "ATM", "nearest", 1, None, 25, None, "", "x-test")["trade"]
    t = e.close(t, "manual")
    sells = [o for o in b._load().values() if o["side"] == "SELL" and o["status"] == "FILLED"]
    assert len(sells) == 1 and t["exit_reason"] == "stop" and t["status"] == "closed"


def test_unsettled_cancel_waits_then_monitor_finishes_exit():
    e, data, b = racy_setup(hold=10, outcome="CANCELLED")   # cancel not confirmed within 1s
    t = e.enter("NIFTY", "CE", "ATM", "nearest", 1, None, 25, None, "", "x-test")["trade"]
    try:
        e.close(t, "manual")
        raise AssertionError("should not sell while the stop is unsettled")
    except paper_mod.BrokerError:
        pass
    t = e.book()["trades"]["1"]
    assert t["status"] == "exiting" and t["stop_order"]
    assert not [o for o in b._load().values() if o["side"] == "SELL" and o["type"] == "LIMIT"]
    b.pending = {k: 0 for k in b.pending}                  # broker confirms the cancel
    e.tick()
    t = e.book()["trades"]["1"]
    assert t["status"] == "closed" and t["exit_reason"] == "manual"


def test_plan_from_time():
    e, data = setup()
    CLOCK[0] = CLOCK[0].replace(hour=9, minute=21)
    try:
        e.add_plan("NIFTY", "CE", "ATM", "nearest", 1, None, 25, None, 22850, None, "11:00", None, "", "x-test", "09:30")
        data.spot = 22900.0
        e.tick()
        assert e.plans()[0]["status"] == "active"      # before --from
        CLOCK[0] = CLOCK[0].replace(minute=30)
        e.tick()
        assert e.plans()[0]["status"] == "fired"
        try:
            e.add_plan("NIFTY", "PE", "ATM", "nearest", 1, None, 25, None, None, 22000, "10:00", None, "", "x-test", "10:00")
            raise AssertionError("from >= until accepted")
        except RiskError:
            pass
    finally:
        CLOCK[0] = CLOCK[0].replace(hour=10, minute=0)


def test_groww_retries_rate_limit():
    try:
        from trader.brokers import groww
    except ImportError:
        print("skip (no growwapi)")
        return
    b = groww.GrowwBroker.__new__(groww.GrowwBroker)
    b._api = object()
    calls = []

    def flaky(api):
        calls.append(1)
        if len(calls) < 3:
            raise groww.GrowwAPIRateLimitException()
        return {"ok": 1}
    groww.time.sleep = lambda s: None
    assert b._call(flaky) == {"ok": 1} and len(calls) == 3


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
