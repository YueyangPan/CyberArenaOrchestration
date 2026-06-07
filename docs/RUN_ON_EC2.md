# Running DEF CON ico on a fresh EC2

Reproducibility check: from a blank Amazon Linux 2023 EC2 to a
finished 3-round match.

**You need:** an EC2 box (x86_64, ≥ 4 GB disk), the `.pem` for it, a
`.env` from your local checkout (`ANTHROPIC_API_KEY_TEAM{1,2}` plus
matching `_BASE_URL_TEAM{1,2}`), and read access to the repo.

## 1. Bootstrap the box

```bash
ssh -i <key>.pem ec2-user@<host>

sudo dnf install -y docker git python3.11 python3.11-pip python3.11-devel
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
exit                         # re-SSH so docker group applies
```

Amazon Linux 2023 ships Python 3.9; we install 3.11 because the `mcp`
package requires 3.10+.

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

## 4. Copy `.env` from your local checkout

From your laptop (not the EC2):

```bash
scp -i <key>.pem .env ec2-user@<host>:~/cyber-arena-orche/.env
```

## 5. Build images and run the match

Back on the EC2:

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
