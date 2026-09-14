# Binduno price-history server

Runs on the project's own VPS (binduno.com), independent of any single
Binduno installation. Purpose: build up a Cardmarket EUR price history
longer than MTGJSON's fixed 90-day window, by logging daily since the day
this was first set up. Fresh Binduno installs try this server first (see
`binduno.py`'s `_backfill_from_binduno_server()`), falling back to MTGJSON
automatically if it's unreachable.

`sync.py` imports the project's own `binduno.py` directly and reuses its
existing catalog/price functions unchanged — no separate implementation to
keep in sync:

1. `refresh_cards(bulk_type="default_cards")` — today's Scryfall catalog.
   Uses `default_cards` (smaller than the desktop app's `all_cards`) since
   this server never needs foreign-language card names, only prices — a
   real difference in peak memory on a small VPS.
2. `backfill_price_history(auto=True)` — one-time, ~90 days from MTGJSON.
3. `log_price_history()` / `downsample_price_history()` — same daily
   logging every desktop install already does.
4. Exports just the `price_history` table as a small gzipped SQLite file
   to `/var/www/binduno-pricedata/history.sqlite.gz`.

## Server layout

- `/opt/binduno-pricelogger/` — `binduno.py` + `sync.py`, owned by a
  dedicated system user `binduno-price` (no login shell).
- `/opt/binduno-pricelogger/data/` — this instance's own SQLite DB
  (`BINDUNO_DATA`), entirely separate from anyone's real collection.
- `/var/www/binduno-pricedata/` — the published export, served by nginx.

## Deployment (already set up on the VPS, kept here for reference)

```bash
sudo useradd --system --home-dir /opt/binduno-pricelogger --create-home \
  --shell /usr/sbin/nologin binduno-price
sudo mkdir -p /opt/binduno-pricelogger/data /var/www/binduno-pricedata
sudo cp binduno.py pricelogger/sync.py /opt/binduno-pricelogger/
sudo chown -R binduno-price:binduno-price /opt/binduno-pricelogger /var/www/binduno-pricedata
```

nginx (added to the `binduno.com` server block, alongside the WordPress
site it shares the VPS with):

```nginx
location /pricedata/ {
    alias /var/www/binduno-pricedata/;
    autoindex off;
    add_header Cache-Control "public, max-age=3600";
}
```

systemd (`/etc/systemd/system/binduno-pricelogger.{service,timer}`), run
once daily:

```ini
# binduno-pricelogger.service
[Unit]
Description=Binduno central price-history sync
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=binduno-price
ExecStart=/usr/bin/python3 /opt/binduno-pricelogger/sync.py
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
```

```ini
# binduno-pricelogger.timer
[Unit]
Description=Run the Binduno price-history sync daily

[Timer]
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now binduno-pricelogger.timer
```

`sync.py` takes its own file lock (`/opt/binduno-pricelogger/sync.lock`) so
a slow run (heavy Scryfall bulk parsing on a memory-limited box) can never
overlap with the next scheduled one.

## Updating

`binduno.py` on the server needs to be kept in sync with the main repo by
hand (`scp` a fresh copy to `/opt/binduno-pricelogger/binduno.py`) whenever
`refresh_cards()`, `backfill_price_history()`, `log_price_history()` or
`downsample_price_history()` change.
