# Running DEF CON ico from scratch

Reproducibility check: from a blank Linux box (typically an EC2) to a
finished 3-round match. Distro-agnostic; tested most recently on
Amazon Linux 2023.

**You need:** a Linux host (x86_64, ≥ 4 GB disk) with sudo, two
Anthropic API keys (separate keys for TEAM1 and TEAM2 so the agents
don't share rate limits), and read access to the repo. Everything
below runs on the box.

## 1. Prereqs on the host

You need:

- Docker daemon running, your user in the `docker` group
- `git`
- Python **3.10+** with `venv` available (`mcp` package requires 3.10+;
  bare Amazon Linux 2023 / Ubuntu 22.04 default to 3.9 / 3.10 — install
  `python3.11` if you have any doubt)

Install via your distro's package manager, for example:

```bash
# Amazon Linux 2023
sudo dnf install -y docker git python3.11 python3.11-pip python3.11-devel

# Debian / Ubuntu
sudo apt-get install -y docker.io git python3.11 python3.11-venv python3-pip
```

Then start docker and add yourself to the group:

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
exit                                   # re-login so docker group applies
```

## 2. Auth GitHub (private repo)

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C "ec2-cyberarena-repro"
cat ~/.ssh/id_ed25519.pub
# Paste the printed line at https://github.com/settings/keys
```

## 3. Clone, install, sync vendor

```bash
git clone git@github.com:cyber-arena-ai/cyber-arena-orche.git
cd cyber-arena-orche
python3.11 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip && pip install -r requirements.txt
bash scripts/sync_nautilus.sh        # pin-clones nautilus-institute/finals-2025 @ 366520c0cc43
```

## 4. Create `.env` with your own API keys

```bash
cat > .env <<'EOF'
export ANTHROPIC_BASE_URL_TEAM1=https://api.anthropic.com
export ANTHROPIC_API_KEY_TEAM1=sk-ant-...your-team1-key...
export ANTHROPIC_BASE_URL_TEAM2=https://api.anthropic.com
export ANTHROPIC_API_KEY_TEAM2=sk-ant-...your-team2-key...
EOF
chmod 600 .env
```

Use two separate keys (one per team) so the agents don't share rate
limits. If you route through an internal LLM proxy / router, point the
`BASE_URL`s at it instead.

## 5. Build images and run the match

```bash
source .env
bash scripts/build_images.sh                              # cyberarena/dev-base
python main.py setup -c config/game_ico.yaml             # also builds cyberarena/chal-nautilus-ico
nohup python main.py start -c config/game_ico.yaml > /tmp/game.log 2>&1 &
tail -f /tmp/game.log
```

The match takes ~12–15 minutes (3 rounds × 2 teams, Claude on both
sides).

## 6. Verify

```bash
latest=$(ls -1t runs/ | head -1)
jq -r '.type' runs/$latest/events.jsonl | sort | uniq -c | sort -rn
jq -c 'select(.type=="GAME_END")' runs/$latest/events.jsonl
jq -c 'select(.type=="FLAG_SUBMITTED" and .payload.accepted==true)' runs/$latest/events.jsonl
```

A healthy run shows:

- `GAME_END` with `reason=completed` and `final_round=3`
- at least one accepted flag submission
- 6 × `SERVICE_RESTART_COMPLETED`, 0 × `SERVICE_RESTART_FAILED`
- 100+ × `SERVICE_HEALTH_CHECK`

## Cleanup

Remove the ephemeral SSH key from
https://github.com/settings/keys. Copy `runs/<ts>/` off the box if
you want the artifacts (`events.jsonl`, `panel.html`) — it has
everything you need to inspect the run after termination.
