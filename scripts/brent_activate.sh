#!/usr/bin/env bash
# Start the Brent alerts-only assistant on hermes-server. Run it yourself:
#   bash ~/ta-brent/scripts/brent_activate.sh
# What it does:
#  - writes ~/ta-brent/brent.toml if missing (alerts on)
#  - restarts the news check on the new code
#  - sends one Telegram test and prints how long the API took to accept it
#  - opens the monitor in tmux "brent", where you type the OANDA practice token
# It places no orders. It does not touch ~/TradingAgents or tbot.
set -euo pipefail
REPO=~/ta-brent
PY=/root/TradingAgents/.venv/bin/python
CFG=$REPO/brent.toml
cd "$REPO"

if [ ! -f "$CFG" ]; then
  sed -e 's/^alerts_enabled = false/alerts_enabled = true/' \
      -e 's|^# site_dir = "/root/ta-dashboard/brent".*|site_dir = "/root/ta-dashboard/brent"|' \
      -e 's|^# report_base_url = \(.*\)   #.*|report_base_url = \1|' \
      brent.example.toml > "$CFG"
  mkdir -p /root/ta-dashboard/brent
  echo "wrote $CFG"
fi
grep -E '^(alerts_enabled|site_dir|report_base_url|notify_command)' "$CFG"

RUN="cd $REPO && PYTHONPATH=$REPO $PY -m tradingagents.brent --config $CFG"

# restart the news check on the new code
if tmux has-session -t brentnews 2>/dev/null; then
  tmux send-keys -t brentnews C-c; sleep 5; tmux kill-session -t brentnews || true
fi
tmux new -d -s brentnews "$RUN news-check --minutes 4320 --analyze 2>&1 | tee -a /root/brent-newscheck.log"
echo "news check restarted (tmux brentnews)"

# Telegram test: measures API acceptance only, not when the phone shows it
t0=$(date +%s.%N)
hermes send --to telegram --quiet "[BRENT TEST] readiness check $(date -u +%H:%M:%SZ). No action needed." \
  && rc=0 || rc=$?
t1=$(date +%s.%N)
echo "telegram exit $rc, accepted in $($PY -c "print(round($t1 - $t0, 2))") s"

# monitor: the token prompt is hidden; nothing is saved
tmux new -d -s brent "$RUN run; echo 'monitor stopped'; read"
echo
echo "Now run:  tmux attach -t brent   -> type the OANDA practice token, then Ctrl+B then D"
