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

The same `/pricedata/` location also serves `sl-expansions.json` (see
below) - a second, unrelated file that happens to live in the same nginx
`alias` since both are small, publicly-fetchable, best-effort files the
app tries before falling back to something baked in.

## Server layout

- `/opt/binduno-pricelogger/` — `binduno.py` + `sync.py`, owned by a
  dedicated system user `binduno-price` (no login shell).
- `/opt/binduno-pricelogger/data/` — this instance's own SQLite DB
  (`BINDUNO_DATA`), entirely separate from anyone's real collection.
- `/var/www/binduno-pricedata/` — `history.sqlite.gz` (the published
  export, rebuilt daily by `sync.py`) and `sl-expansions.json` (hand-
  maintained, see below) — both served by nginx.

## Secret Lair Cardmarket-expansion names (`sl-expansions.json`)

Cardmarket splits Secret Lair into one expansion per (super)drop, but
never exposes the id -> name mapping machine-readably (see the comment on
`CM_SLD_EXPANSIONS` in `binduno.py`). That map used to be baked into
`binduno.py` only, so a newly announced drop stayed unresolved until every
install updated to a new release. `sl-expansions.json` here is the exact
same map, just also servable independently of an app release —
`refresh_cards()` merges it in (`_fetch_live_sl_expansions()`) on top of
the baked-in one whenever there's a Secret Lair card to resolve.

**Not auto-generated** - unlike `history.sqlite.gz`, `sync.py` never
touches this file. To add a newly discovered drop:

```bash
# edit a local copy, then:
scp sl-expansions.json admin@<vps>:/tmp/
ssh admin@<vps> "sudo cp /tmp/sl-expansions.json /var/www/binduno-pricedata/ \
  && sudo chown binduno-price:binduno-price /var/www/binduno-pricedata/sl-expansions.json \
  && rm /tmp/sl-expansions.json"
```

Live immediately for every install on their next card-data refresh — no
release needed. Still worth folding the same entry into `CM_SLD_EXPANSIONS`
in `binduno.py` on the next real release anyway, so a fresh install (or one
with no network path to binduno.com) isn't missing it either.

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
