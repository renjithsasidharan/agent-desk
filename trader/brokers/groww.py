"""Groww adapter (growwapi SDK)."""
import contextlib
import datetime as dt
import io
import json
import os
import re
import time

import pandas as pd
import requests
from growwapi import GrowwAPI
from growwapi.groww.exceptions import (GrowwAPIAuthenticationException, GrowwAPIException,
                                       GrowwAPIRateLimitException)

from ..config import INDICES, IST, UNDERLYINGS, today
from .base import Broker, BrokerError, Contract, OrderResult

INSTRUMENTS_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"
FILLED = {"EXECUTED", "COMPLETED"}
REJECTED = {"REJECTED", "FAILED"}
CANCELLED = {"CANCELLED"}


def _token_day():
    # Groww access tokens expire at 06:00 IST every day.
    return (dt.datetime.now(IST) - dt.timedelta(hours=6)).date().isoformat()


class GrowwBroker(Broker):
    name = "groww"

    def __init__(self, secrets, state_dir):
        self.secrets = secrets
        self.private = os.path.join(state_dir, "private")
        self.cache = os.path.join(state_dir, "cache")
        os.makedirs(self.private, mode=0o700, exist_ok=True)
        os.makedirs(self.cache, exist_ok=True)
        self._api = None
        self._inst = None
        self.last_raw = {}  # order id -> last raw order detail, for checking field names

    # --- auth -------------------------------------------------------------

    def _token(self, fresh=False):
        path = os.path.join(self.private, "groww_token.json")
        if not fresh and os.path.exists(path):
            with open(path) as f:
                saved = json.load(f)
            if saved.get("day") == _token_day():
                return saved["token"]
        key = self.secrets.get("GROWW_API_KEY")
        if not key:
            raise BrokerError("GROWW_API_KEY missing from secrets file")
        try:
            if self.secrets.get("GROWW_TOTP_SECRET"):
                import pyotp
                totp = pyotp.TOTP(self.secrets["GROWW_TOTP_SECRET"]).now()
                token = GrowwAPI.get_access_token(api_key=key, totp=totp)
            elif self.secrets.get("GROWW_API_SECRET"):
                token = GrowwAPI.get_access_token(api_key=key, secret=self.secrets["GROWW_API_SECRET"])
            else:
                raise BrokerError("secrets file needs GROWW_TOTP_SECRET or GROWW_API_SECRET")
        except GrowwAPIException as e:
            raise BrokerError(f"Groww login failed: {e}") from e
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"day": _token_day(), "token": token}, f)
        os.replace(tmp, path)
        return token

    @property
    def api(self):
        if self._api is None:
            with contextlib.redirect_stdout(io.StringIO()):  # the SDK prints a banner
                self._api = GrowwAPI(self._token())
        return self._api

    def _call(self, fn, *args, **kwargs):
        # Groww allows about 10 requests a second per group; on a rate-limit reply, back off and retry.
        for wait in (0.3, 0.7, 1.5):
            try:
                return self._call_once(fn, *args, **kwargs)
            except GrowwAPIRateLimitException:
                time.sleep(wait)
        return self._call_once(fn, *args, **kwargs)

    def _call_once(self, fn, *args, **kwargs):
        try:
            return fn(self.api, *args, **kwargs)
        except GrowwAPIRateLimitException:
            raise
        except GrowwAPIAuthenticationException:
            with contextlib.redirect_stdout(io.StringIO()):
                self._api = GrowwAPI(self._token(fresh=True))
            return fn(self.api, *args, **kwargs)
        except GrowwAPIException as e:
            raise BrokerError(f"Groww: {e}") from e
        except requests.RequestException as e:
            raise BrokerError(f"Groww network error: {e}") from e

    def login(self):
        return self._call(lambda a: a.get_user_profile(timeout=10))

    # --- instruments ------------------------------------------------------

    def _instruments(self):
        if self._inst is None:
            path = os.path.join(self.cache, f"groww_instruments_{today()}.csv")
            if not os.path.exists(path):
                r = requests.get(INSTRUMENTS_URL, timeout=60)
                r.raise_for_status()
                tmp = f"{path}.{os.getpid()}.tmp"
                with open(tmp, "wb") as f:
                    f.write(r.content)
                os.replace(tmp, path)
                for old in os.listdir(self.cache):
                    if old.startswith("groww_instruments_") and old.endswith(".csv") and old != os.path.basename(path):
                        os.remove(os.path.join(self.cache, old))
            df = pd.read_csv(path, low_memory=False)
            self._inst = df[(df.segment == "FNO") & df.instrument_type.isin(["CE", "PE", "FUT"])
                            & df.underlying_symbol.isin(list(UNDERLYINGS))]
        return self._inst

    def _options(self):
        df = self._instruments()
        return df[df.instrument_type.isin(["CE", "PE"])]

    def _row(self, symbol):
        """Instrument row for an option/future symbol, or 'NIFTY-FUT' for the nearest future."""
        df = self._instruments()
        if symbol.upper().endswith("-FUT"):
            u = symbol.upper()[:-4]
            futs = df[(df.underlying_symbol == u) & (df.instrument_type == "FUT") & (df.expiry_date >= today())]
            rows = futs.sort_values("expiry_date").head(1)
        else:
            rows = df[df.trading_symbol == symbol]
        if rows.empty:
            raise BrokerError(f"unknown symbol {symbol}")
        return next(rows.itertuples())

    @staticmethod
    def _to_contract(row):
        return Contract(
            symbol=row.trading_symbol, exchange=row.exchange, underlying=row.underlying_symbol,
            expiry=row.expiry_date, strike=float(row.strike_price), option_type=row.instrument_type,
            lot_size=int(row.lot_size), tick=float(row.tick_size),
            freeze_qty=int(row.freeze_quantity) if pd.notna(row.freeze_quantity) else 0,
        )

    def expiries(self, underlying):
        df = self._options()
        return sorted(e for e in df[df.underlying_symbol == underlying].expiry_date.unique() if e >= today())

    def contracts(self, underlying, expiry):
        df = self._options()
        rows = df[(df.underlying_symbol == underlying) & (df.expiry_date == expiry)]
        return [self._to_contract(r) for r in rows.itertuples()]

    def contract(self, symbol):
        df = self._options()
        rows = df[df.trading_symbol == symbol]
        if rows.empty:
            raise BrokerError(f"unknown contract {symbol}")
        return self._to_contract(next(rows.itertuples()))

    # --- prices -----------------------------------------------------------

    def index_ltp(self, underlying):
        if underlying not in INDICES:
            raise BrokerError(f"unknown index {underlying}; known: {list(INDICES)}")
        key = f"{INDICES[underlying]['exchange']}_{underlying}"
        out = self._call(lambda a: a.get_ltp((key,), segment="CASH", timeout=10))
        if key not in out:
            raise BrokerError(f"no index price for {underlying}: {out}")
        return float(out[key])

    def ltp(self, contracts):
        result = {}
        keys = [f"{c.exchange}_{c.symbol}" for c in contracts]
        for i in range(0, len(keys), 50):
            chunk = tuple(keys[i:i + 50])
            out = self._call(lambda a: a.get_ltp(chunk, segment="FNO", timeout=10))
            for k, v in out.items():
                result[k.split("_", 1)[1]] = float(v)
        return result

    def candles(self, spec, interval, start, end):
        if spec.upper() in INDICES:
            ex = INDICES[spec.upper()]["exchange"]
            seg, sym = "CASH", f"{ex}-{spec.upper()}"
        else:
            row = self._row(spec)
            ex, seg, sym = row.exchange, "FNO", row.groww_symbol

        def fetch(s, e):
            for attempt in range(3):  # Groww's candle service times out now and then
                try:
                    return self._call(lambda a: a.get_historical_candles(
                        exchange=ex, segment=seg, groww_symbol=sym,
                        start_time=s, end_time=e, candle_interval=interval, timeout=30)).get("candles", [])
                except BrokerError as err:
                    if "timeout" not in str(err).lower() or attempt == 2:
                        raise
                    time.sleep(2)

        try:
            return fetch(start, end)
        except BrokerError as err:
            # Groww caps the range per request ("... maximum of 30 days ..."): fetch it in pieces.
            m = re.search(r"maximum of (\d+) days", str(err))
            if not m:
                raise
        fmt = "%Y-%m-%d %H:%M:%S"
        step = dt.timedelta(days=int(m.group(1)) - 1)
        s, last = dt.datetime.strptime(start, fmt), dt.datetime.strptime(end, fmt)
        rows = []
        while s <= last:
            e = min(s + step, last)
            rows += fetch(s.strftime(fmt), e.strftime(fmt))
            s = e + dt.timedelta(seconds=1)
        return rows

    def option_chain(self, underlying, expiry):
        ex = UNDERLYINGS[underlying]["exchange"]
        raw = self._call(lambda a: a.get_option_chain(exchange=ex, underlying=underlying,
                                                      expiry_date=expiry, timeout=15))
        strikes = {}
        for k, sides in raw.get("strikes", {}).items():
            row = {}
            for side, d in sides.items():
                g = d.get("greeks") or {}
                row[side] = {"symbol": d.get("trading_symbol"), "ltp": d.get("ltp"),
                             "oi": d.get("open_interest"), "volume": d.get("volume"),
                             **{x: g.get(x) for x in ("iv", "delta", "gamma", "theta", "vega")}}
            strikes[float(k)] = row
        return {"spot": raw.get("underlying_ltp"), "strikes": strikes}

    def quote(self, symbol):
        row = self._row(symbol)
        q = self._call(lambda a: a.get_quote(trading_symbol=row.trading_symbol, exchange=row.exchange,
                                             segment="FNO", timeout=10))
        depth = q.get("depth") or {}
        bids = [(d["price"], d["quantity"]) for d in depth.get("buy", []) if d.get("price")]
        asks = [(d["price"], d["quantity"]) for d in depth.get("sell", []) if d.get("price")]
        bid, ask = (bids[0][0] if bids else None), (asks[0][0] if asks else None)
        keep = ("last_price", "ohlc", "day_change_perc", "volume", "open_interest", "oi_day_change",
                "oi_day_change_percentage", "total_buy_quantity", "total_sell_quantity",
                "upper_circuit_limit", "lower_circuit_limit")
        return {"symbol": row.trading_symbol, **{k: q.get(k) for k in keep},
                "bid": bid, "ask": ask,
                "spread_pct": round((ask - bid) / ask * 100, 2) if bid and ask else None,
                "bids": bids, "asks": asks}

    # --- orders -----------------------------------------------------------

    def place(self, c, side, qty, order_type, price, trigger=None):
        out = self._call(lambda a: a.place_order(
            validity="DAY", exchange=c.exchange, order_type=order_type, product="MIS",
            quantity=qty, segment="FNO", trading_symbol=c.symbol, transaction_type=side,
            price=price, trigger_price=trigger, timeout=10))
        oid = out.get("groww_order_id")
        if not oid:
            raise BrokerError(f"order not accepted: {out}")
        return oid

    def modify(self, order_id, c, qty, order_type, price, trigger=None):
        self._call(lambda a: a.modify_order(
            order_type=order_type, segment="FNO", groww_order_id=order_id,
            quantity=qty, price=price, trigger_price=trigger, timeout=10))

    def cancel(self, order_id, c):
        self._call(lambda a: a.cancel_order(groww_order_id=order_id, segment="FNO", timeout=10))

    def order(self, order_id, c):
        d = self._call(lambda a: a.get_order_detail(segment="FNO", groww_order_id=order_id, timeout=10))
        self.last_raw[order_id] = d
        raw = (d.get("order_status") or "").upper()
        filled = int(d.get("filled_quantity") or 0)
        qty = int(d.get("quantity") or 0)
        if raw in FILLED and filled >= qty:
            status = "FILLED"
        elif raw in REJECTED:
            status = "REJECTED"
        elif raw in CANCELLED:
            status = "CANCELLED"
        else:
            status = "OPEN"
        avg = d.get("average_fill_price")
        return OrderResult(order_id, status, filled, float(avg) if avg else None,
                           f"{raw} {d.get('remark') or ''}".strip())

    def funds(self):
        return self._call(lambda a: a.get_available_margin_details(timeout=10))

    def positions(self):
        return self._call(lambda a: a.get_positions_for_user(segment="FNO", timeout=10)).get("positions", [])
