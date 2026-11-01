"""Alerts to the owner through AWS SNS (the topic emails, texts or pushes them on).

Live books: every entry and exit as it happens, and anything that may need the owner (halt, kill,
a stop problem, an exit that won't complete, a position mismatch, monitor errors). Every book: one
summary after the close. Runs as its own service (`trade alerts`), so a slow or failing send never
delays the monitor. It reads the books' journals and remembers how far it got in alerts.json.
"""
import datetime as dt
import json
import os
import time

from . import config

TRADE_EVENTS = {"entered", "closed", "entry-unfilled"}
URGENT_EVENTS = {"halted", "killed", "stop-lost", "stop-failed", "exit-incomplete", "entry-unsettled",
                 "monitor-error", "position-mismatch"}
STATE = os.path.join(config.STATE_DIR, "alerts.json")


def rs(x):
    return f"{'-' if x < 0 else '+'}₹{abs(x):,.0f}"


def _read(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def book_trades(book):
    return _read(os.path.join(config.book_dir(book), "trades.json"), {"trades": {}})["trades"]


def message(book, ev, trades):
    """(subject, body) for one journal event of a live book, or None if it isn't alerted."""
    name, tag = ev["event"], f"[{book} LIVE]"
    t = trades.get(str(ev.get("id")), {})
    if name == "entered":
        stop = t.get("stop_trigger")
        risk = (ev["price"] - stop) * ev["qty"] if stop else None
        body = (f"Bought {ev['qty']} {ev['symbol']} at ₹{ev['price']:,.2f} (₹{ev['price'] * ev['qty']:,.0f}), "
                f"setup {ev.get('setup')}, at {ev['ts'][11:16]}.\n"
                + (f"Stop ₹{stop:,.2f}: about {rs(-risk)} if it is hit.\n" if stop else "No stop recorded yet.\n")
                + (f"Target ₹{t['target']:,.2f}.\n" if t.get("target") else "")
                + (f"Note: {t['note']}\n" if t.get("note") else ""))
        return f"{tag} Bought {ev['symbol']}", body
    if name == "closed":
        body = (f"Sold {ev['qty']} {ev['symbol']}: in ₹{ev['entry']:,.2f}, out ₹{ev['exit']:,.2f}, "
                f"{rs(ev['pnl'])} after charges ({ev.get('reason')}), at {ev['ts'][11:16]}.\n"
                f"Setup {ev.get('setup')}.\n")
        return f"{tag} Closed {ev['symbol']} {rs(ev['pnl']).replace('₹', 'Rs ')}", body
    if name == "entry-unfilled":
        return f"{tag} Entry not filled {ev['symbol']}", f"The buy order for {ev['symbol']} did not fill and was cancelled. No position.\n"
    if name in URGENT_EVENTS:
        details = "\n".join(f"{k}: {v}" for k, v in ev.items() if k not in ("event", "note"))
        return (f"{tag} CHECK: {name}",
                f"{ev.get('note') or 'The monitor reported a problem that may need you.'}\n\n{details}\n\n"
                f"Ask the trader-ops session, or check the Groww app.\n")
    return None


def summary(cfg, day):
    """(subject, body) for the end-of-day summary of every book."""
    lines, parts = [], []
    for b in config.books(cfg):
        bc = config.book_cfg(cfg, b)
        acct = _read(os.path.join(config.book_dir(b), "account.json"), {"realized": 0.0})
        today = sorted((t for t in book_trades(b).values() if t["day"] == day), key=lambda t: t["id"])
        pnl = sum(t["pnl"] or 0 for t in today if t["status"] == "closed")
        cap, risk = bc["capital"], bc["risk"]
        floor = cap - (risk.get("max_total_loss") or cap * risk["max_total_loss_pct"] / 100)
        equity = cap + acct.get("realized", 0)
        mode = "LIVE" if bc["mode"] == "live" else "paper"
        lines.append(f"{b} ({mode}): {len(today)} trade(s), day {rs(pnl)}, equity ₹{equity:,.0f}, "
                     f"{rs(equity - floor).lstrip('+')} left before the total loss limit"
                     + (", HALTED today" if acct.get("halted_day") == day else "")
                     + (", KILLED" if acct.get("killed") else ""))
        for t in today:
            res = rs(t["pnl"]) + f" ({t.get('exit_reason')})" if t["status"] == "closed" else t["status"].upper()
            lines.append(f"  #{t['id']} {t['symbol']} {t.get('setup')}: {res}")
        parts.append(f"{b} {rs(pnl).replace('₹', 'Rs ')}")
    return f"Trading day {day}: " + ", ".join(parts), "\n".join(lines) + "\n"


class Alerter:
    def __init__(self, send, state_path=STATE):
        self.send = send            # send(subject, body); raises on failure
        self.path = state_path
        self.state = _read(state_path, {"offsets": {}, "summary_day": None})

    def save(self):
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.path)

    def check(self, cfg, now):
        day = now.date().isoformat()
        first_run = not self.state["offsets"]
        for b in config.books(cfg):
            if config.book_cfg(cfg, b)["mode"] != "live":
                continue
            path = os.path.join(config.book_dir(b), "journal", f"{day}.jsonl")
            if not os.path.exists(path):
                continue
            off = self.state["offsets"].get(b)
            if not off or off["day"] != day:
                # First look at today's journal. On the service's very first run, skip what's already there.
                off = {"day": day, "pos": os.path.getsize(path) if first_run else 0}
                self.state["offsets"][b] = off
                self.save()
            with open(path) as f:
                f.seek(off["pos"])
                chunk = f.read()
            trades = book_trades(b)
            for line in chunk.splitlines(keepends=True):
                if not line.endswith("\n"):
                    break           # a line still being written: next time
                ev = json.loads(line)
                if (now - dt.datetime.fromisoformat(ev["ts"])).total_seconds() < 5:
                    break           # give the stop order a moment to be recorded after an entry
                msg = message(b, ev, trades)
                if msg:
                    self.send(*msg)     # on failure the offset stays put and it's retried
                off["pos"] += len(line.encode())
                self.save()
        at = cfg.get("alerts", {}).get("summary_at", "15:40")
        if now.weekday() < 5 and now.strftime("%H:%M") >= at and self.state.get("summary_day") != day:
            self.send(*summary(cfg, day))
            self.state["summary_day"] = day
            self.save()


def sns_sender(topic):
    import boto3
    client = boto3.client("sns", region_name=topic.split(":")[3])

    def send(subject, body):
        client.publish(TopicArn=topic, Subject=subject[:99], Message=body)
    return send


def run():
    """The service loop: every 10 seconds; idles while no topic is configured."""
    alerter = None
    while True:
        try:
            cfg = config.load()
            topic = (cfg.get("alerts") or {}).get("sns_topic")
            if not topic:
                time.sleep(300)
                continue
            if alerter is None:
                alerter = Alerter(sns_sender(topic))
            alerter.check(cfg, config.now())
        except Exception as e:  # keep running; the next pass retries
            print(f"alerts: {type(e).__name__}: {e}", flush=True)
            time.sleep(50)
        time.sleep(10)


def test(cfg):
    topic = (cfg.get("alerts") or {}).get("sns_topic")
    if not topic:
        raise SystemExit("no alerts.sns_topic in the config")
    sns_sender(topic)("Test alert from the trading machine",
                      f"Alerts work. Sent {dt.datetime.now(config.IST):%Y-%m-%d %H:%M} IST.\n")
    return {"sent": True, "topic": topic}
