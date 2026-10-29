import copy
import os
import re
import datetime as dt
from zoneinfo import ZoneInfo

import yaml

CONFIG_PATH = os.environ.get("TRADER_CONFIG", "/opt/trader/config.yaml")
STATE_DIR = os.environ.get("TRADER_STATE", "/var/lib/trader")
SECRETS_PATH = os.environ.get("TRADER_SECRETS", "/etc/trader/secrets.env")

IST = ZoneInfo("Asia/Kolkata")

# Index options the program knows how to trade. Exchange is where the options are listed.
UNDERLYINGS = {
    "NIFTY": {"exchange": "NSE"},
    "BANKNIFTY": {"exchange": "NSE"},
    "SENSEX": {"exchange": "BSE"},
}
# Indices with a price and candles (underlyings plus India VIX).
INDICES = {**UNDERLYINGS, "INDIAVIX": {"exchange": "NSE"}}


def load():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def books(cfg):
    """Book names: one per agent, each with its own money, limits, trades and results."""
    return list((cfg.get("books") or {"main": {}}).keys())


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def book_cfg(cfg, name):
    """The config for one book: the top-level settings with that book's overrides on top."""
    bs = cfg.get("books") or {"main": {}}
    if name not in bs:
        raise KeyError(f"no book '{name}'; books: {list(bs)}")
    out = _merge({k: v for k, v in cfg.items() if k != "books"}, bs[name])
    out["book"] = name
    return out


def book_dir(name):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,20}", name):
        raise ValueError(f"bad book name {name!r}")
    return os.path.join(STATE_DIR, "books", name)


def secrets():
    out = {}
    with open(SECRETS_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def now():
    return dt.datetime.now(IST)


def today():
    return now().date().isoformat()


def hhmm(s):
    h, m = s.split(":")
    return dt.time(int(h), int(m))
