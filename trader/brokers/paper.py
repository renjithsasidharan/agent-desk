"""Paper trading: real prices from a live broker, simulated orders and fills."""
import json
import os
import uuid

from ..config import now
from .base import Broker, BrokerError, Contract, OrderResult


class PaperBroker(Broker):
    def __init__(self, data: Broker, state_dir, slippage_pct):
        self.data = data
        self.name = f"paper({data.name})"
        self.path = os.path.join(state_dir, "paper_orders.json")
        self.slip = slippage_pct / 100

    # Prices and contracts come from the real broker.
    def login(self): return self.data.login()
    def index_ltp(self, u): return self.data.index_ltp(u)
    def ltp(self, cs): return self.data.ltp(cs)
    def expiries(self, u): return self.data.expiries(u)
    def contracts(self, u, e): return self.data.contracts(u, e)
    def contract(self, s): return self.data.contract(s)
    def candles(self, *a): return self.data.candles(*a)
    def option_chain(self, u, e): return self.data.option_chain(u, e)
    def quote(self, s): return self.data.quote(s)
    def positions(self): return []

    def funds(self):
        return {"paper": True}

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path) as f:
            return json.load(f)

    def _save(self, orders):
        with open(self.path + ".tmp", "w") as f:
            json.dump(orders, f, indent=1)
        os.replace(self.path + ".tmp", self.path)

    def _try_fill(self, o, ltp):
        if o["status"] != "OPEN":
            return
        buy = o["side"] == "BUY"
        if o["type"] == "SL":
            if ltp > o["trigger"]:
                return
            o["type"] = "LIMIT"  # triggered: now a resting limit order, which may not fill on a gap
            o["triggered_at"] = now().isoformat()
        px = ltp * (1 + self.slip) if buy else ltp * (1 - self.slip)
        if (buy and px <= o["price"]) or (not buy and px >= o["price"]):
            o.update(status="FILLED", filled_qty=o["qty"], avg_price=round(px, 2), filled_at=now().isoformat())

    def place(self, c, side, qty, order_type, price, trigger=None):
        if order_type not in ("LIMIT", "SL"):
            raise BrokerError(f"paper broker does not support {order_type}")
        orders = self._load()
        oid = "P" + uuid.uuid4().hex[:10]
        o = dict(id=oid, symbol=c.symbol, side=side, qty=qty, type=order_type, price=price,
                 trigger=trigger, status="OPEN", filled_qty=0, avg_price=None, placed_at=now().isoformat())
        self._try_fill(o, self.data.ltp([c])[c.symbol])
        orders[oid] = o
        self._save(orders)
        return oid

    def modify(self, order_id, c, qty, order_type, price, trigger=None):
        orders = self._load()
        o = orders.get(order_id)
        if not o or o["status"] != "OPEN":
            raise BrokerError(f"cannot modify {order_id}: {o and o['status']}")
        o.update(qty=qty, type=order_type, price=price, trigger=trigger)
        self._try_fill(o, self.data.ltp([c])[c.symbol])
        self._save(orders)

    def cancel(self, order_id, c):
        orders = self._load()
        o = orders.get(order_id)
        if o and o["status"] == "OPEN":
            o["status"] = "CANCELLED"
            self._save(orders)

    def order(self, order_id, c):
        orders = self._load()
        o = orders.get(order_id)
        if not o:
            raise BrokerError(f"unknown paper order {order_id}")
        if o["status"] == "OPEN":
            self._try_fill(o, self.data.ltp([c])[c.symbol])
            self._save(orders)
        return OrderResult(order_id, o["status"], o["filled_qty"], o["avg_price"], o["type"])
