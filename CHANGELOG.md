# Changelog

Notable changes to Binduno, newest first. Started 2026-10-08 — everything
before v6.108 only lives in the [commit history](https://github.com/GixCreations/binduno/commits/main).

## v6.108 — 2026-10-08

### Fixed
- A Replace-mode CSV reimport that changed a card's recorded storage
  binder (or added binder tracking for the first time) reset that card's
  purchase date to the import day, silently destroying the "Value over
  time" chart's history for the whole collection in a single import.
  Reconciliation now falls back to matching the same printing regardless
  of binder when nothing matches under the exact binder.
- The maintainer-only install-count tracker (`pricelogger/count_installs.py`)
  had been failing silently for three weeks (wrong Unix group on its
  service account, couldn't read nginx's log) - fixed, and switched from
  excluding one hard-coded test IP to excluding the whole cloud subnet it
  belongs to, since that address rotates between sessions.

No database schema change in this update - your card data isn't
re-downloaded.
