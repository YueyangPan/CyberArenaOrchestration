# How to run the sample challenge

Recipe for taking a blank Linux host (typically an EC2) to a finished
3-round match of the bundled DEF CON ico challenge. Same steps work
for your own challenges once they're wired in — just swap the config
file. Tested most recently on Amazon Linux 2023; should work on any
modern distro.

You need a Linux host (x86_64, ≥ 4 GB disk) with sudo, two Anthropic
API keys (one per team — don't share, the agents will starve each
other on rate limits), and read access to this repo. Run everything
below on the box.

## 1. Install docker + python 3.10+

```bash
# Amazon Linux 2023
sudo dnf install -y docker git python3.11 python3.11-pip python3.11-devel

# Debian / Ubuntu
sudo apt-get install -y docker.io git python3.11 python3.11-venv python3-pip
```

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
exit          # re-login so the docker group takes effect
```

`mcp` needs python 3.10+, and AL2023 / Ubuntu 22.04 ship 3.9 / 3.10 by
default — installing 3.11 explicitly avoids surprises.

## 2. Add your SSH key to GitHub

The repo is private. Generate a key, paste the public half at
https://github.com/settings/keys:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

## 3. Clone, install, sync vendor

```bash
git clone git@github.com:cyber-arena-ai/cyber-arena-orche.git
cd cyber-arena-orche
python3.11 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip && pip install -r requirements.txt
bash scripts/sync_nautilus.sh     # pins nautilus-institute/finals-2025 @ 366520c0cc43
```

## 4. Create `.env`

```bash
cat > .env <<'EOF'
export ANTHROPIC_BASE_URL_TEAM1=https://api.anthropic.com
export ANTHROPIC_API_KEY_TEAM1=sk-ant-...
export ANTHROPIC_BASE_URL_TEAM2=https://api.anthropic.com
export ANTHROPIC_API_KEY_TEAM2=sk-ant-...
EOF
chmod 600 .env
```

Two separate keys so the agents don't share rate limits. If you have
an internal LLM router or proxy, point `BASE_URL` at it instead.

## 5. Build and run

```bash
source .env
bash scripts/build_images.sh                            # cyberarena/dev-base
python main.py setup -c config/game_ico.yaml           # also builds chal-nautilus-ico
nohup python main.py start -c config/game_ico.yaml > /tmp/game.log 2>&1 &
tail -f /tmp/game.log
```

The match takes 12–15 minutes.

## 6. Verify

```bash
latest=$(ls -1t runs/ | head -1)
jq -r '.type' runs/$latest/events.jsonl | sort | uniq -c | sort -rn
jq -c 'select(.type=="GAME_END")' runs/$latest/events.jsonl
jq -c 'select(.type=="FLAG_SUBMITTED" and .payload.accepted==true)' runs/$latest/events.jsonl
```

Healthy run:

- `GAME_END` with `reason=completed`, `final_round=3`
- ≥ 1 accepted flag submission
- 6 × `SERVICE_RESTART_COMPLETED`, 0 × `SERVICE_RESTART_FAILED`
- 100+ × `SERVICE_HEALTH_CHECK`

Copy `runs/<ts>/` off the box if you want the `events.jsonl` and
`panel.html` after the instance is terminated.
