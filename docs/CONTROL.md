# Phone control page (installed 2026-11-05)

Live at `https://hermes-server.taildb3e4d.ts.net:8443/` (tailnet only) as `brent-control.service`.
`allowed_users` is Irakli's Tailscale login. Checked at install: no identity header gives 403, another login
gives 403, Irakli's gives 200. `restart_news_command = "systemctl restart brent-news"`.
Undo: `systemctl disable --now brent-control; tailscale serve --https=8443 off`.

A small page for your phone: current status of Brent and the stock demo, and three recovery buttons.

| Button | What it really does | How you know it worked |
|---|---|---|
| Pause / Resume Brent entry alerts | Rewrites only the `alerts_enabled` line of `brent.toml` (the monitor re-reads it every 60 s; no restart, no token) | The page shows the value read back from the file; the status panel shows the monitor's own state afterwards |
| Restart Brent news collector | Runs the single fixed `restart_news_command` from `control.toml` | The page shows the exit code and whether the collector is running afterwards |
| Retry a failed stock run | **Not available** until the stock runner is hosted here | Shown greyed out |

Not offered, on purpose: any shell, any broker order, any strategy or range change.

## Safety rules (enforced in code, tested)

- Listens on 127.0.0.1 only. Reach it with `tailscale serve --bg --https=8443 http://127.0.0.1:8787`
  (private to your tailnet, not Funnel). Your Tailscale login must be in `allowed_users`; the page
  refuses every other identity (and requests with no identity header) and logs the refusal.
- Viewing or refreshing only reads. Actions are POST with a one-time nonce issued to you for that action:
  forged, replayed (409), expired, cross-user, wrong-action and cross-origin requests do nothing.
- Every action attempt is appended to the audit log (`audit_path`): who, what, result, read-back.
- Root on the server can still do anything; this page does not defend against a compromised host.

## Install (after you approve)

```
cp control.example.toml /root/ta-brent/control.toml   # set allowed_users to your Tailscale login
cp deploy/systemd/brent-control.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now brent-control
tailscale serve --bg --https=8443 http://127.0.0.1:8787
```

Open `https://<server>.<tailnet>.ts.net:8443/` on your phone (Tailscale app signed in). Until
Brent runs under systemd, set `restart_news_command` to your existing tmux restart script.
