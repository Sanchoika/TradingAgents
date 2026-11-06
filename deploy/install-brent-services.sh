#!/usr/bin/env bash
# Install supervised Brent services on hermes-server (run as root, from a TTY, from the repo checkout).
# Irakli approved the root-only token file on 2026-11-05. The token is typed at a hidden prompt and written to
# /etc/tradingagents/brent.env (mode 600). It is never echoed, logged or passed on a command line.
# Re-run safe. Rollback at the bottom. Touches only the brent-* units.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
UNITS="$HERE/systemd"
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
[ -t 0 ] || { echo "needs a terminal for the hidden token prompt"; exit 1; }

install -d -m 700 /etc/tradingagents
if [ ! -s /etc/tradingagents/brent.env ]; then
  umask 077
  read -rs -p 'OANDA practice token (hidden): ' T; echo
  [ -n "$T" ] || { echo "empty token, nothing written"; exit 1; }
  printf 'OANDA_API_KEY=%s\n' "$T" > /etc/tradingagents/brent.env
  unset T
fi
chmod 600 /etc/tradingagents/brent.env
[ -e /etc/tradingagents/heartbeat-url ] || { umask 077; : > /etc/tradingagents/heartbeat-url; }

cp "$UNITS"/brent-monitor.service "$UNITS"/brent-news.service "$UNITS"/brent-health.service \
   "$UNITS"/brent-health.timer "$UNITS"/brent-weekly.service "$UNITS"/brent-weekly.timer \
   "$UNITS"/brent-backup.service "$UNITS"/brent-backup.timer "$UNITS"/ops-nightly.service \
   "$UNITS"/ops-nightly.timer /etc/systemd/system/
systemctl daemon-reload

# Stop the tmux copies gracefully, then hand over to systemd.
cd /root/ta-brent
export PYTHONPATH=/root/ta-brent
PY=/root/TradingAgents/.venv/bin/python
"$PY" -m tradingagents.brent --config /root/ta-brent/brent.toml stop || true
"$PY" -m tradingagents.brent --config /root/ta-brent/brent.toml stop --news || true
sleep 3
tmux kill-session -t brent 2>/dev/null || true
tmux kill-session -t brentnews 2>/dev/null || true

systemctl enable --now brent-news brent-monitor brent-health.timer brent-weekly.timer \
   brent-backup.timer ops-nightly.timer
sleep 5
systemctl --no-pager --lines=0 status brent-news brent-monitor || true
"$PY" -m tradingagents.brent --config /root/ta-brent/brent.toml status | head -20

# Rollback: systemctl disable --now brent-monitor brent-news brent-health.timer brent-weekly.timer \
#   brent-backup.timer ops-nightly.timer
#           then start-brent-monitor.sh / start-brent-news.bat as before.
