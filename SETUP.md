# Setting it up yourself

This guide sets up the whole thing from nothing: an AWS machine in Mumbai, a Groww Trade API key, Claude Code, and then paper trading. Going live comes last and is optional. Read the [disclaimer](README.md#disclaimer) first.

Allow about an hour, plus at least two weeks of paper trading before real money.

## What you need

- **An AWS account,** with the [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) logged in on your computer.
- **A Groww account with F&O enabled and Trade API access.** Groww may charge a monthly fee for the API; check their current pricing.
- **A Claude account** that can use Claude Code: a Pro or Max plan, or an Anthropic API key.

**Running costs:** about US$9 a month for AWS on the weekday schedule (machine, disk, static IP). Claude usage depends on your plan. Real trades pay brokerage and taxes; a 1-lot NIFTY option round trip costs about ₹50-70.

## 1. Create the machine

```
git clone <this repo> && cd agent-desk
bash deploy/aws/create-machine.sh
```

It creates the SSH key `~/.ssh/agent-desk.pem`, a firewall rule allowing SSH from your current IP, a t4g.medium machine (2 vCPU, 4 GB, ARM) running Ubuntu 24.04, and a static IP (Elastic IP). It prints the machine ID and the IP.

If your home IP changes often, run `bash deploy/aws/create-machine.sh 0.0.0.0/0` instead. Login stays key-only, so this is reasonably safe, but anyone can then try to connect.

## 2. Install the program

```
rsync -a --exclude .git -e "ssh -i ~/.ssh/agent-desk.pem" ./ ubuntu@<IP>:trader-src/
ssh -i ~/.ssh/agent-desk.pem ubuntu@<IP>
cd trader-src && sudo bash deploy/setup.sh
```

`setup.sh` installs the packages, sets the clock to India time, creates the `trader` and `agent` users, installs the `trade` command, the monitor service, the session timers, Claude Code for the agent, and one desk folder per book. You can rerun it at any time; it never overwrites `/opt/trader/config.yaml`, the broker keys or the agents' notes.

## 3. Connect Groww

1. On Groww, open the Trade API page and create an API key of the **TOTP** type. TOTP keys log in by themselves every morning; the other type needs approving in the Groww app daily.
2. Register the machine's **static IP** with the key. Orders from any other address are refused.
3. Put the key and the TOTP secret (the text behind the QR code) on the machine:
   ```
   sudo nano /etc/trader/secrets.env
   ```
4. Check it works:
   ```
   trade login
   trade chain NIFTY
   ```

Only the `trader` user can read `/etc/trader/secrets.env`. The agent can't.

## 4. Log in to Claude

```
sudo -iu agent
claude
```

Log in, answer the first-run questions, then exit Claude and the agent user (`exit` twice).

Optionally pin the model and effort the agents use, in `/home/agent/.claude/settings.json`, for example:

```json
{"skipDangerousModePermissionPrompt": true, "model": "claude-opus-5-5", "effortLevel": "medium"}
```

## 5. Set the schedule

On your computer:

```
bash deploy/aws/install-starter.sh <instance-id> <your email>
```

AWS emails you a confirmation link; click it, or you won't get alerts. From then on, Monday to Friday (India time):

| Time | What happens |
|---|---|
| 08:00 | The machine starts. If AWS has no t4g.medium free, it tries other ARM sizes. |
| 08:30 | Each agent starts with a morning brief |
| 08:45 | You get an email if the machine is still down |
| 09:20 | Entry window opens |
| 15:15 | Anything still open is sold |
| 15:35 | Agents write their results and lessons |
| 16:00 | The machine stops |

A stopped machine costs nothing but its disk and static IP. Indian market holidays aren't in the schedule: the agents start, find no prices moving, and do nothing.

## 6. Paper trade

Everything starts in `mode: paper`: live prices, simulated orders. Leave it that way for at least two weeks.

```
trade status --all          # every book: equity, today's P&L, loss left
trade review --all          # results side by side
desk list                   # which agent sessions are running
desk attach main            # watch one (Ctrl-b then d to leave it running)
```

Limits live in `/opt/trader/config.yaml` (edit with `sudo nano`, then `sudo systemctl restart trader-monitor`). Each agent's personality is `deploy/desk/personas/<book>.md`; the shared rules are `deploy/desk/RULES.md`. To add an agent, add a book under `books:` in the config, write its personality file, and rerun setup.

Before going live, ask yourself: after charges, is the book positive over 30+ trades? Were the losses contained? Did the agent follow its rules?

## 7. Going live (optional)

1. **Test real orders.** In market hours, run `trade live-check --yes`. It buys 1 lot of a cheap NIFTY option, places a stop, moves it, and sells, through Groww, then prints what Groww reported. It costs about ₹50. Read the output: the buy and the sell should say `EXECUTED`.
2. **Make one book live, small.** In `/opt/trader/config.yaml`, under that book: `mode: live`, a small `capital`, and rupee limits (`max_daily_loss`, `max_total_loss`). The template has a commented example. The live books' capital together must fit in your Groww balance.
3. **Tell the agent.** Copy `deploy/desk/live-note.example.md` to `/etc/trader/desk/<book>.md`, fill in your numbers, and rerun `sudo bash deploy/setup.sh`.
4. **Watch the first days.**

To stop at once:

```
trade --book main halt --exit      # sell everything in that book, no more trades today
sudo systemctl stop trader-monitor # stop the monitor (stop orders stay at the broker)
```

The monitor checks every 30 seconds that Groww's positions match the live books. A trade you place by hand in the same Groww account shows up as a mismatch, so keep manual trading in another account.

## Updating

Pull the repo, then repeat the `rsync` and `sudo bash deploy/setup.sh` from step 2. Run the tests first if you changed code: `/opt/trader/.venv/bin/python tests/test_engine.py`. Changes to `starter.py` need `install-starter.sh` again.
