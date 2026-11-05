# Dashboard access

The dashboard is a static, read-only site rebuilt after every run:

- `~/.tradingagents/demo/dashboard.html`: open it on the runner PC (double-click). It works offline.
- `~/.tradingagents/demo/site/`: the same page plus `data.json`, saved reports and phone
  home-screen files, for hosting.

Nothing on the page can run analyses, settle orders or change the paper account.

## Recommended: private link through Tailscale on hermes-server (free)

The runner stays on the PC. After each run the PC copies only `site/` to the server, and
the server serves that folder privately over HTTPS on your tailnet. No port is opened to
the internet, nothing needs a domain, and no trading process or sign-in file is copied.

```
PC (runner) --after each run, copy site/ over SSH--> hermes-server:/root/ta-dashboard
phone / PC  --Tailscale (private, HTTPS)----------> https://<server>.<tailnet>.ts.net/
```

One-time setup:

1. Server: install Tailscale and sign in (`curl -fsSL https://tailscale.com/install.sh | sh`,
   then `tailscale up`, open the printed link). Free Personal plan.
2. In the Tailscale admin console, turn on MagicDNS and HTTPS certificates.
3. Server: `tailscale serve --bg /root/ta-dashboard` (serves the folder at
   `https://<server>.<tailnet>.ts.net/`, reachable only from your own devices).
4. Phone and PC: install the Tailscale app and sign in with the same account.
5. PC `demo.toml`:

   ```toml
   dashboard_sync_command = 'tar -C C:/Users/irakl/.tradingagents/demo/site -cf - . | ssh -o ClearAllForwardings=yes -o BatchMode=yes hermes-server "mkdir -p /root/ta-dashboard && tar -C /root/ta-dashboard -xf -"'
   ```

   then `python -m tradingagents.demo --config demo.toml dashboard --sync` once.

On the phone, open the link and use "Add to Home Screen". A cached copy always shows
when it was built and warns when it is stale.

## Start, stop, restart

- Rebuild now: `python -m tradingagents.demo --config demo.toml dashboard` (add `--sync` to push, `--open` to open).
- The page is files only, so there is nothing to start on the PC.
- Server sharing: `tailscale serve status`, stop with `tailscale serve reset`, start again
  with `tailscale serve --bg /root/ta-dashboard`.

## Local only (no server)

`python -m http.server 8765 --directory %USERPROFILE%\.tradingagents\demo\site --bind 127.0.0.1`
serves it at http://127.0.0.1:8765 on the PC itself. Only works while the PC is on;
do not bind it to 0.0.0.0.

## Stale-copy protection (2026-11-05)

The dashboard sync must never let an older snapshot overwrite a newer one (for example a PC that was
offline and then pushes yesterday's page after the server already published today's). The receiving
side now checks `built_at` first:

```
tar -C site -cf - . | ssh root@hermes-server "cd /root/ta-brent && python -m tradingagents.demo.syncguard receive /root/ta-dashboard"
```

It accepts equal or newer snapshots, refuses older ones (exit 3, logged as `refused_stale`), keeps the
previous copy as `/root/ta-dashboard.prev`, rejects archives with absolute or `..` paths or links, and logs
each decision in `/root/ta-dashboard.sync-log.jsonl`. Put this form in `dashboard_sync_command` in demo.toml.

Offline PC: the server copy is the record. When the PC returns, the Obsidian notes are written from the
server's newer files; a note whose server source is older than the vault copy is left alone.
