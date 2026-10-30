# Setup

This guide is written for a **coding agent** to carry out on your computer: Claude Code, OpenAI's Codex CLI, or any agent that can run shell commands. You do the few steps only a person can do: logins, the broker key, clicking an email link. A person can also follow it by hand.

It sets up the whole thing from nothing: an AWS machine in Mumbai, a Groww Trade API key, Claude Code on the machine, and paper trading. Going live comes last, and only you can do it. Read the [disclaimer](README.md#disclaimer) first.

## Start here: give your agent this prompt

```
Set up agent-desk for me: https://github.com/renjithsasidharan/agent-desk
Clone it, read README.md and SETUP.md in full, then follow SETUP.md step by step.
Do the steps marked "Agent" yourself and run each step's check before moving on.
For steps marked "You", tell me exactly what to do and wait until I say it's done.
Follow the "Rules for the agent" in SETUP.md at all times.
```

Allow about an hour, then at least two weeks of paper trading before any real money.

## Rules for the agent

- **Ask before spending money.** Before step 1 and step 5, tell the person what will be created and what it costs per month (see "Running costs"), and wait for a yes.
- **Never handle the broker keys.** The person types them into the machine themselves (step 3). Don't ask for them in the chat, don't read `/etc/trader/secrets.env`, and don't write keys anywhere else.
- **Never place real orders or switch anything to live.** Don't run `trade live-check`, don't set `mode: live`, and don't create `/etc/trader/desk/<book>.md`. Step 7 is the person's. You may explain it.
- **Don't weaken the security setup** to get past an error: leave the sudoers rule, file owners and permissions, users and the firewall as the scripts make them. If a check fails, stop and report the exact error and what you tried.
- **Where commands run:** "on your computer" means the person's computer, where you run. "On the machine" means over SSH: `ssh -i ~/.ssh/agent-desk.pem ubuntu@<IP> '<command>'`. Commands that need a terminal (`nano`, `claude` logins) are the person's: give them the exact `ssh -t ...` line to run in their own terminal.
- **Keep notes** of the instance ID, the IP and the key path, and finish with a short summary: what was set up, the daily schedule, how to watch the agents, and the commands to stop trading.

## What you need (You)

- **An AWS account,** with the [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) installed and logged in on your computer (`aws login`, or `aws configure` with an access key). Check: `aws sts get-caller-identity` prints your account.
- **A Groww account with F&O enabled and Trade API access.** Groww may charge a monthly fee for the API; check their current pricing.
- **A Claude account** that can use Claude Code (a Pro or Max plan, or an Anthropic API key). The trading agents run on it.
- **An email address** for alerts when the machine fails to start.

**Running costs:** about US$9 a month for AWS on the weekday schedule (machine, 20 GB disk, static IP). Claude usage depends on your plan. Real trades pay brokerage and taxes; a 1-lot NIFTY option round trip costs about ₹50-70.

## 1. Create the machine (Agent)

Ask the person whether their home IP is fixed. If it is, SSH can be limited to it (the default). If it changes often, pass `0.0.0.0/0`: login stays key-only, but anyone can try to connect.

On your computer:

```
git clone https://github.com/renjithsasidharan/agent-desk && cd agent-desk
bash deploy/aws/create-machine.sh            # or: bash deploy/aws/create-machine.sh 0.0.0.0/0
```

It creates the SSH key `~/.ssh/agent-desk.pem`, a firewall rule for SSH, a t4g.medium machine (2 vCPU, 4 GB, ARM) running Ubuntu 24.04 with a 20 GB encrypted disk, and a static IP (Elastic IP). Everything is named and tagged `agent-desk`; set `DESK_NAME` to use another name, and `AWS_REGION` for another region. It prints the instance ID and the IP.

**Check:** `ssh -i ~/.ssh/agent-desk.pem -o StrictHostKeyChecking=accept-new ubuntu@<IP> uptime` works. The machine may need a minute after starting.

## 2. Install the program (Agent)

On your computer:

```
rsync -a --exclude .git -e "ssh -i ~/.ssh/agent-desk.pem" ./ ubuntu@<IP>:trader-src/
ssh -i ~/.ssh/agent-desk.pem ubuntu@<IP> 'cd trader-src && sudo bash deploy/setup.sh'
```

`setup.sh` installs the packages, sets the clock to India time, creates the `trader` and `agent` users, installs the `trade` command, the monitor service, the session timers, Claude Code for the agent user, a Python with pandas for research, and one desk folder per book. It can be rerun at any time; it never overwrites `/opt/trader/config.yaml`, the broker keys or the agents' notes.

**Check,** on the machine:

```
/opt/trader/.venv/bin/python ~/trader-src/tests/test_engine.py | tail -1    # ends with "ok test_..."
systemctl is-active trader-monitor                                         # active
trade books                                                                # main, active
```

## 3. Connect Groww (You, then Agent checks)

The agent tells you the machine's IP. Then you:

1. On Groww, open the Trade API page and create an API key of the **TOTP** type. TOTP keys log in by themselves every morning; the other type needs approving in the Groww app every day.
2. Register the machine's **static IP** with the key. Groww refuses orders from any other address.
3. In your own terminal (not the agent's), open the keys file on the machine:
   ```
   ssh -t -i ~/.ssh/agent-desk.pem ubuntu@<IP> sudo nano /etc/trader/secrets.env
   ```
   Fill in `GROWW_API_KEY` and `GROWW_TOTP_SECRET` (the text behind the QR code), then save with Ctrl-O, Enter, Ctrl-X.

Only the `trader` user can read that file. Neither the trading agents nor the setup agent need to see it.

**Check (Agent),** on the machine:

```
trade login                    # prints your Groww profile
trade chain NIFTY --width 1    # prints option prices
```

If `trade login` fails, ask the person to check the key type, the IP registration and the two values. Don't look at the file.

## 4. Log in to Claude on the machine (You)

In your own terminal:

```
ssh -t -i ~/.ssh/agent-desk.pem ubuntu@<IP> sudo -iu agent claude
```

Log in, answer the first-run questions, then type `/exit`.

**Check (Agent),** on the machine:

```
sudo -iu agent claude -p "Reply with the single word OK"     # prints OK
```

Then ask the person which model and effort the trading agents should use, and set them in `/home/agent/.claude/settings.json` (keep the existing keys), for example:

```json
{"skipDangerousModePermissionPrompt": true, "model": "claude-opus-5-5", "effortLevel": "medium"}
```

## 5. Set the schedule (Agent, then You)

Ask the person for their alert email. On your computer:

```
bash deploy/aws/install-starter.sh <instance-id> <email>
```

Then the person clicks **Confirm subscription** in the email AWS sends (subject "AWS Notification - Subscription Confirmation"). Without that click, no alerts arrive.

**Check (Agent):**

```
aws scheduler list-schedules --region ap-south-1 --name-prefix agent-desk --query 'Schedules[].[Name,State]' --output text
aws sns list-subscriptions --region ap-south-1 --query 'Subscriptions[].SubscriptionArn' --output text
```

Three schedules (`-start`, `-check`, `-stop`) should be `ENABLED`, and the subscription should be a long ARN, not `PendingConfirmation`. AWS can take a minute to update it after the click.

From then on, Monday to Friday (India time):

| Time | What happens |
|---|---|
| 08:00 | The machine starts. If AWS has no t4g.medium free, it tries other ARM sizes. |
| 08:30 | Each trading agent starts with a morning brief |
| 08:45 | Email to you if the machine is still down |
| 09:20 | Entry window opens |
| 15:15 | Anything still open is sold |
| 15:35 | The agents write their results and lessons |
| 16:00 | The machine stops |

A stopped machine costs only its disk and static IP. Indian market holidays aren't in the schedule: the agents start, see nothing moving, and do nothing.

## 6. Paper trade (You)

Everything starts in `mode: paper`: live prices, simulated orders. Leave it that way for at least two weeks. To watch, SSH to the machine (`ssh -i ~/.ssh/agent-desk.pem ubuntu@<IP>`) during market hours:

```
trade status --all          # every book: equity, today's P&L, loss left
trade review --all          # results side by side
desk list                   # which agent sessions are running
desk attach main            # watch one (Ctrl-b then d to leave it running)
```

Your coding agent can run these for you too, and summarise how the books are doing.

Limits live in `/opt/trader/config.yaml` (edit with `sudo nano`, then `sudo systemctl restart trader-monitor`). Each trading agent's personality is `deploy/desk/personas/<book>.md`; the shared rules are `deploy/desk/RULES.md`. To add an agent, add a book under `books:` in the config, write its personality file, and rerun setup.

Before going live, ask yourself: after charges, is the book positive over 30+ trades? Were the losses contained? Did the agent follow its rules?

## 7. Going live (You only, optional)

1. **Test real orders.** In market hours, run `trade live-check --yes` on the machine. It buys 1 lot of a cheap NIFTY option, places a stop, moves it, and sells, through Groww, then prints what Groww reported. It costs about ₹50. The buy and the sell should say `EXECUTED`.
2. **Make one book live, small.** In `/opt/trader/config.yaml`, under that book: `mode: live`, a small `capital`, and rupee limits (`max_daily_loss`, `max_total_loss`). The template has a commented example. The live books' capital together must fit in your Groww balance.
3. **Tell the trading agent.** Copy `deploy/desk/live-note.example.md` to `/etc/trader/desk/<book>.md`, fill in your numbers, and rerun `sudo bash ~/trader-src/deploy/setup.sh`.
4. **Watch the first days.**

To stop at once:

```
trade --book main halt --exit      # sell everything in that book, no more trades today
sudo systemctl stop trader-monitor # stop the monitor (stop orders stay at the broker)
```

The monitor checks every 30 seconds that Groww's positions match the live books. A trade you place by hand in the same Groww account shows up as a mismatch, so keep manual trading in another account.

## Updating (Agent)

Pull the repo, run the tests on the machine, then repeat step 2's `rsync` and `setup.sh`. Changes to `deploy/aws/starter.py` need `install-starter.sh` again, with the same instance ID. If the machine is stopped, start it first (`aws ec2 start-instances --instance-ids <id>`) and stop it again afterwards.
