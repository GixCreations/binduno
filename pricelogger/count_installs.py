#!/usr/bin/env python3
"""Rough, privacy-respecting install-count tracker for the maintainer's own
curiosity. NOT exposed publicly, sends nothing anywhere, and adds no new
code or network call to binduno.py itself - it only reads nginx's own
access log, on the server, after the fact.

Counts hits to /pricedata/history.sqlite.gz: that URL is fetched exactly
once by a fresh Binduno install doing its one-time price-history backfill
(see backfill_price_history() in binduno.py), so counting requests to it
approximates cumulative installs over time. Not exact - a failed/retried
backfill could count twice, an install that never reaches this server at
all (e.g. no network path to it) counts zero - but a reasonable
order-of-magnitude signal, without adding any tracking to the app.

Handles log rotation robustly: nginx's log for this site rotates daily
(see /etc/logrotate.d/nginx), so a plain "count today's file" approach
would silently reset itself every day. This instead tracks how many *new*
matching lines have appeared since the last run (detecting a rotation
when the file's current match count drops below what was last seen, and
treating everything currently in the file as new in that case) and adds
only the increment to a persistent running total that survives rotation.
"""
import os
import re

LOG = "/var/log/nginx/binduno.access.log"
STATE_DIR = "/opt/binduno-pricelogger/install-count"
CHECKPOINT = os.path.join(STATE_DIR, "last_line_count.txt")
TOTAL = os.path.join(STATE_DIR, "total.txt")
HISTORY = os.path.join(STATE_DIR, "history.csv")   # date,running_total - so growth over time is visible, not just the latest number
PATTERN = "GET /pricedata/history.sqlite.gz"

# IPs known to be the maintainer/assistant's own testing against the real
# production server, not real users - excluded so repeated live testing
# doesn't inflate the count. Add to this (and re-deploy) after any session
# that does live testing here; a stale/rotated testing IP left in this list
# is harmless (just never matches anything real), but a NEW testing IP not
# yet added here WILL be counted as if it were a real install.
EXCLUDE_IPS = {
    "149.249.143.211",   # assistant's testing machine, seen 2026-09-14 - 13 hits in one day from live app testing, none real
}

_IP_RE = re.compile(r"^(\S+)")


def count_matches(path):
    n = 0
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                if PATTERN not in line or " 200 " not in line:
                    continue
                m = _IP_RE.match(line)
                if m and m.group(1) in EXCLUDE_IPS:
                    continue
                n += 1
    except FileNotFoundError:
        pass
    return n


def read_int(path, default=0):
    try:
        with open(path) as f:
            return int(f.read().strip() or default)
    except (FileNotFoundError, ValueError):
        return default


def main():
    from datetime import datetime
    os.makedirs(STATE_DIR, exist_ok=True)
    current = count_matches(LOG)
    last = read_int(CHECKPOINT)
    total = read_int(TOTAL)
    increment = current - last if current >= last else current  # negative delta = rotation happened since last run
    total += increment
    with open(CHECKPOINT, "w") as f:
        f.write(str(current))
    with open(TOTAL, "w") as f:
        f.write(str(total))
    with open(HISTORY, "a") as f:
        f.write(f"{datetime.now().isoformat(timespec='minutes')},{total}\n")
    print(f"matches in current log file (excluding known test IPs): {current}, "
          f"new since last check: +{increment}, running total: {total}")


if __name__ == "__main__":
    main()
