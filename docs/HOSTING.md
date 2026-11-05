# Hosting Brent on hermes-server (prepared, not installed)

Status 2026-11-05: Brent runs in two tmux sessions (`brent` monitor, news collector). A crash or
reboot leaves them stopped. The files in `deploy/systemd/` replace tmux with supervised services.
**Nothing here is installed yet.** Two decisions are Irakli's:

## 1. How the OANDA practice token survives a reboot (needs approval)

Today the monitor asks for the token at a hidden prompt; nothing stores it, so every restart needs a retype.
Proposal: a root-only file `/etc/tradingagents/brent.env` (owner root, mode 600, directory 700) with the
single line `OANDA_API_KEY=...`, read by systemd as `EnvironmentFile`. It is practice-account,
read-only use by this code (Brent never places orders). What this does and does not protect:

- It is not in code, logs, Telegram, URLs or the dashboard.
- Anyone with root on the server can read it; local file permissions do not protect against a
  compromised host administrator. Revoke the token on OANDA if the host is ever in doubt.
- Without it: crashes are still recovered if you start the service once with the file, but a
  reboot needs a retype. With it: crash and reboot recover alone.
- Alternative with no stored secret: keep the hidden prompt and accept manual restarts.

Until approved, `start-brent-monitor.bat` (double-click, retype once) remains the way to start it.

## 2. External heartbeat (needs approval; one account)

A heartbeat sent from the same server cannot report that the server died. Proposal: a free
Healthchecks.io check (or any similar service): `brent-health.timer` pings its URL every minute
only when `brent health` passes (`/fail` on a problem), and the service alerts Irakli (email or
its Telegram integration) when pings stop (whole-host outage) or `/fail` arrives (a process
problem). The URL is a secret, stored in `/etc/tradingagents/heartbeat-url` (mode 600).
Without a heartbeat URL the file is empty and only the local check runs.

## Install (after approval)

```
install -d -m 700 /etc/tradingagents
umask 077; read -rs -p 'OANDA token: ' T; echo "OANDA_API_KEY=$T" > /etc/tradingagents/brent.env; unset T
cp deploy/systemd/brent-*.service deploy/systemd/brent-health.timer /etc/systemd/system/
systemctl daemon-reload
# stop the tmux copies first (graceful: python -m tradingagents.brent stop; stop --news), then:
systemctl enable --now brent-news brent-monitor brent-health.timer
```

Rollback: `systemctl disable --now brent-monitor brent-news brent-health.timer`, then the tmux launchers.
Only these units are touched; `tbot`, `hermes-gateway`, `hermes-dashboard` and the hermes-operations
timers are left alone.
