#!/usr/bin/env bash
# (Re)start the Brent alerts-only assistant on hermes-server. Run it yourself:
#   bash ~/ta-brent/scripts/brent_activate.sh
# - writes ~/ta-brent/brent.toml if missing (alerts OFF = shadow mode until you switch them on)
# - stops the running monitor and news collector cleanly (no duplicates: each holds a lock)
# - starts the news collector (continuous: it runs until stopped, no 72-hour limit)
# - starts the price monitor in tmux "brent", where you type the OANDA practice token
# Switch alerts on/off later WITHOUT a restart: set alerts_enabled in brent.toml (read every 60 s).
# It places no orders. It does not touch ~/TradingAgents, the morning bot or tbot.
set -euo pipefail
REPO=~/ta-brent
PY=/root/TradingAgents/.venv/bin/python
CFG=$REPO/brent.toml
cd "$REPO"
RUN="cd $REPO && PYTHONPATH=$REPO $PY -m tradingagents.brent --config $CFG"

if [ ! -f "$CFG" ]; then
  sed -e 's|^# site_dir = "/root/ta-dashboard/brent".*|site_dir = "/root/ta-dashboard/brent"|' \
      -e 's|^# report_base_url = \(.*\)   #.*|report_base_url = \1|' \
      brent.example.toml > "$CFG"
  mkdir -p /root/ta-dashboard/brent
  echo "wrote $CFG (alerts_enabled = false: shadow mode)"
fi
grep -E '^(alerts_enabled|site_dir|report_base_url|notify_command)' "$CFG"
echo "code: $(git -C "$REPO" log --oneline -1)"

# clean stops (SIGTERM), then wait for both locks to be free
bash -c "$RUN stop" || true
bash -c "$RUN stop --news" || true
for i in $(seq 1 20); do
  if ! bash -c "$RUN status" | grep -q "monitor running: yes" \
     && ! bash -c "$RUN status" | grep -q "news collector running: yes"; then break; fi
  sleep 1
done
tmux kill-session -t brent 2>/dev/null || true
tmux kill-session -t brentnews 2>/dev/null || true

tmux new -d -s brentnews "$RUN news-check --analyze 2>&1 | tee -a /root/brent-newscheck.log"
echo "news collector started (tmux brentnews, continuous)"
tmux new -d -s brent "$RUN run; echo 'monitor stopped'; read"
echo
echo "Now run:  tmux attach -t brent   -> type the OANDA practice token, then Ctrl+B then D"
echo "Check:    $RUN status"
