#!/usr/bin/env python3
"""Headless price-history sync for the central Binduno price server.

Reuses binduno.py's own catalog + price-history functions directly instead
of reimplementing them - binduno.py is safe to import (main() only runs
under the __main__ guard). Run once daily via systemd timer:

    1. refresh_cards()            - today's Scryfall card/price catalog
    2. backfill_price_history()   - one-time, ~90 days from MTGJSON
    3. log_price_history()        - log today's changed prices
    4. downsample_price_history() - keep the table bounded long-term
    5. export the price_history table as a small downloadable SQLite file

Fresh Binduno installs fetch that exported file (see binduno.py's
backfill_price_history) instead of - or in addition to - MTGJSON, getting
whatever depth this server has accumulated since it started (more than
MTGJSON's fixed 90-day window, the longer this has been running).
"""
import fcntl
import gzip
import os
import shutil
import sqlite3
import sys

os.environ.setdefault("BINDUNO_DATA", "/opt/binduno-pricelogger/data")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import binduno  # noqa: E402  (must follow the BINDUNO_DATA env var above)

EXPORT_DIR = os.environ.get("BINDUNO_PRICEDATA_DIR", "/var/www/binduno-pricedata")


def export_price_history(c):
    """Copy just the price_history table into its own small SQLite file,
    gzip it, and atomically replace the previously published one."""
    os.makedirs(EXPORT_DIR, exist_ok=True)
    tmp_db = os.path.join(EXPORT_DIR, "history.sqlite.tmp")
    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    out = sqlite3.connect(tmp_db)
    out.execute("""CREATE TABLE price_history(
        set_code TEXT, number TEXT, date TEXT,
        eur_cents INT, eur_foil_cents INT,
        PRIMARY KEY(set_code, number, date))""")
    rows = c.execute(
        "SELECT set_code, number, date, eur_cents, eur_foil_cents FROM price_history")
    out.executemany("INSERT INTO price_history VALUES (?,?,?,?,?)", rows)
    out.execute("CREATE INDEX ix_ph_date ON price_history(date)")
    out.commit()
    out.execute("VACUUM")
    out.close()

    final_db = os.path.join(EXPORT_DIR, "history.sqlite")
    os.replace(tmp_db, final_db)
    gz_final = final_db + ".gz"
    gz_tmp = gz_final + ".tmp"
    with open(final_db, "rb") as f_in, gzip.open(gz_tmp, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.replace(gz_tmp, gz_final)
    os.remove(final_db)  # only the .gz is served; no point keeping both
    size_mb = os.path.getsize(gz_final) / 1e6
    print(f"exported {size_mb:.1f} MB -> {gz_final}")


def main():
    c = binduno.connect()
    binduno.init(c)

    print("syncing card catalog from Scryfall (default_cards - smaller, no name lookup needed here)...")
    binduno.refresh_cards(bulk_type="default_cards")

    c = binduno.connect()  # refresh_cards() closed its own connection
    if not binduno.meta_get(c, "price_backfill_done"):
        print("first run - seeding ~90 days of price history from MTGJSON...")
        binduno.backfill_price_history(auto=True)
        c = binduno.connect()

    n = binduno.log_price_history(c)
    binduno.downsample_price_history(c)
    print(f"logged {n} changed price row(s) today")

    export_price_history(c)
    c.close()


if __name__ == "__main__":
    # A run can take a long time on a memory-constrained box (heavy swapping
    # while parsing Scryfall's bulk export) - a lock file keeps the daily
    # systemd timer from starting a second, overlapping instance against the
    # same SQLite file.
    lock_path = "/opt/binduno-pricelogger/sync.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another sync is already running - skipping this run")
        sys.exit(0)
    try:
        main()
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
