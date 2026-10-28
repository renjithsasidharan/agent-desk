# groww-trader

Risk-checked intraday index-option trading (NIFTY, BANKNIFTY, SENSEX) for a Claude agent, on one EC2 machine in ap-south-1.

- `trader/`: the `trade` command. `engine.py` holds the order handling and risk rules, `brokers/` the broker adapters (Groww, plus paper trading on live prices), and `review.py` the results by setup.
- `config.yaml`: capital, loss limits, entry window, instruments. The live copy is `/opt/trader/config.yaml`; setup never overwrites it.
- `deploy/`: machine setup. It creates the `trader` and `agent` users, the sudoers rule, the monitor service, the Claude session timers (`desk`), and Claude's instructions (`desk/CLAUDE.md`).

## How it fits together

- **`trader` user:** the only user that can read the broker keys (`/etc/trader/secrets.env`) and place orders.
- **`agent` user:** Claude runs here, in tmux session `desk`, and can only trade through `trade` (`sudo -u trader`). It cannot change the config or the code.
- **`trader-monitor` service:** every 2 seconds it fires planned entries and watches stops and targets. It sells everything at the square-off time and stops trading when a loss limit is hit.
- **Event messages:** monitor events go to `/var/lib/trader/events.jsonl`, and `desk watch` types them into Claude's session.

## Install or update on the machine

```
rsync -a --exclude .git ./ ubuntu@<host>:trader-src/
ssh ubuntu@<host> 'cd trader-src && /opt/trader/.venv/bin/python tests/test_engine.py && sudo bash deploy/setup.sh'
```

## Before switching `mode: live`

Run `trade live-check --yes` in market hours: one real 1-lot round trip through the broker. Then set `capital` to the real account balance.
