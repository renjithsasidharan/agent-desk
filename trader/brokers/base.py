"""The interface every broker adapter implements. The engine only talks to this."""
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict


class BrokerError(Exception):
    pass


@dataclass
class Contract:
    symbol: str          # broker trading symbol, e.g. NIFTY26N0322800CE
    exchange: str        # NSE / BSE
    underlying: str      # NIFTY / BANKNIFTY / SENSEX
    expiry: str          # YYYY-MM-DD
    strike: float
    option_type: str     # CE / PE
    lot_size: int
    tick: float
    freeze_qty: int

    def to_dict(self):
        return asdict(self)


@dataclass
class OrderResult:
    order_id: str
    status: str          # OPEN, FILLED, CANCELLED, REJECTED
    filled_qty: int
    avg_price: float | None
    message: str = ""


def round_tick(price, tick, mode="nearest"):
    n = price / tick
    if mode == "up":
        n = math.ceil(n - 1e-9)
    elif mode == "down":
        n = math.floor(n + 1e-9)
    else:
        n = round(n)
    return round(max(n, 1) * tick, 2)


class Broker(ABC):
    name = "base"

    @abstractmethod
    def login(self) -> dict:
        """Check credentials work; returns profile info."""

    @abstractmethod
    def index_ltp(self, underlying: str) -> float: ...

    @abstractmethod
    def ltp(self, contracts: list[Contract]) -> dict[str, float]:
        """Last traded price keyed by contract symbol."""

    @abstractmethod
    def expiries(self, underlying: str) -> list[str]: ...

    @abstractmethod
    def contracts(self, underlying: str, expiry: str) -> list[Contract]: ...

    @abstractmethod
    def contract(self, symbol: str) -> Contract: ...

    @abstractmethod
    def place(self, c: Contract, side: str, qty: int, order_type: str,
              price: float, trigger: float | None = None) -> str:
        """side BUY/SELL, order_type LIMIT/SL (stop-limit). Intraday product. Returns order id."""

    @abstractmethod
    def modify(self, order_id: str, c: Contract, qty: int, order_type: str,
               price: float, trigger: float | None = None) -> None: ...

    @abstractmethod
    def cancel(self, order_id: str, c: Contract) -> None: ...

    @abstractmethod
    def order(self, order_id: str, c: Contract) -> OrderResult: ...

    @abstractmethod
    def candles(self, spec: str, interval: str, start: str, end: str) -> list[list]:
        """Candles [ts, open, high, low, close, volume, oi] for an index (NIFTY, INDIAVIX), the nearest
        future (NIFTY-FUT) or an option symbol. start/end 'YYYY-MM-DD HH:MM:SS' IST."""

    def option_chain(self, underlying: str, expiry: str) -> dict:
        """{"spot": float, "strikes": {strike: {"CE": {...}, "PE": {...}}}} with symbol, ltp, oi,
        volume, iv, delta, gamma, theta, vega per side."""
        raise BrokerError(f"{self.name} has no option chain")

    def quote(self, symbol: str) -> dict:
        """Full quote for an option or future (FUT symbol or NIFTY-FUT): last price, bid/ask, depth,
        volume, open interest, day range."""
        raise BrokerError(f"{self.name} has no quotes")

    @abstractmethod
    def funds(self) -> dict: ...

    @abstractmethod
    def positions(self) -> list[dict]:
        """Broker's own view of F&O positions, raw, for reconciliation."""
