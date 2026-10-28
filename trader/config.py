import os
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
