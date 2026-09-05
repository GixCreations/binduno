#!/usr/bin/env python3
"""
Binduno — local Magic: The Gathering collection tracker with persistent storage.

    python3 binduno.py

Opens http://127.0.0.1:8770 in your browser. Data lives in a SQLite file
in your user folder (or a "binduno_data" folder next to this script), so your
progress is kept between sessions. Python 3.9+ only, no third-party packages.
"""

import base64, csv, io, json, gzip, os, platform, re, shutil, socket, socketserver, sqlite3, sys, tarfile, tempfile, threading, time, webbrowser
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "5.74"
SCHEMA = 15


def _env(name, *legacy):
    """Read a BINDUNO_<name> environment variable, falling back to the old
    MTG_TRACKER_* names (the app was renamed from "MTG Tracker")."""
    v = os.environ.get("BINDUNO_" + name)
    if v is not None:
        return v
    for old in legacy:
        v = os.environ.get(old)
        if v is not None:
            return v
    return None


# Repo the in-app "Update from GitHub" button pulls new versions from. Baked in
# so testers don't have to type anything; still overridable in Settings or via
# BINDUNO_REPO (e.g. for a fork).
GITHUB_REPO = "GixCreations/binduno"
PORT = int(_env("PORT", "MTG_TRACKER_PORT") or 8770)


def _default_data_dir():
    """The branded per-user data folder a fresh install uses."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Binduno")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Binduno")
    return os.path.expanduser("~/.binduno")


def _legacy_data_dir():
    """Where data lived while the app was still called "MTG Tracker"."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/MTG Tracker")
    return os.path.expanduser("~/.mtg_tracker")       # Windows + Linux both used this


def _data_dir():
    """Keep data outside the app bundle so it survives updates and moves."""
    env = _env("DATA", "MTG_TRACKER_DATA")
    if env:
        return os.path.expanduser(env)
    # A "binduno_data" folder (or the old "mtg_tracker_data") next to the
    # script — or, in the frozen Windows .exe, next to the executable — turns
    # on portable mode.
    roots = [os.path.dirname(os.path.abspath(__file__))]
    if getattr(sys, "frozen", False):
        roots.insert(0, os.path.dirname(os.path.abspath(sys.executable)))
    for root in roots:
        for folder in ("binduno_data", "mtg_tracker_data"):
            here = os.path.join(root, folder)
            if os.path.isfile(os.path.join(here, "tracker.db")):
                return here                           # existing portable setup wins
    branded = _default_data_dir()
    if os.path.isfile(os.path.join(branded, "tracker.db")):
        return branded
    if os.path.isfile(os.path.join(_legacy_data_dir(), "tracker.db")):
        # data from before the "MTG Tracker" -> "Binduno" rename: move it once
        return branded if _migrate_legacy_data(branded) else _legacy_data_dir()
    return branded


def _migrate_legacy_data(target):
    """One-time: copy a pre-rename "MTG Tracker" data folder into the new
    "Binduno" one so the collection follows the rename. tracker.db holds the
    cards and every setting; it is copied with SQLite's backup API, which is
    WAL-safe. The old folder is left untouched as a backup. Returns True when
    `target` now has a usable tracker.db."""
    old_db = os.path.join(_legacy_data_dir(), "tracker.db")
    new_db = os.path.join(target, "tracker.db")
    if os.path.isfile(new_db):
        return True
    if not os.path.isfile(old_db):
        return False
    try:
        os.makedirs(target, exist_ok=True)
        src = sqlite3.connect("file:%s?mode=ro" % old_db, uri=True)
        dst = sqlite3.connect(new_db)
        try:
            with dst:
                src.backup(dst)
        finally:
            src.close()
            dst.close()
        print("Moved your collection from %s to %s (the old folder is kept "
              "as a backup)." % (_legacy_data_dir(), target))
        return True
    except Exception as e:                             # noqa: BLE001
        try:
            if os.path.exists(new_db) and os.path.getsize(new_db) == 0:
                os.remove(new_db)
        except Exception:                             # noqa: BLE001
            pass
        print("(couldn't move the old data folder automatically, using the "
              "existing folder: %s)" % e)
        return False


BASE = _data_dir()
DB = os.path.join(BASE, "tracker.db")
UA = {"User-Agent": "Binduno/1.0", "Accept": "application/json"}
ENDGAME_EUR = 300.0
CARDS_PER_SELLER = 10
# Domestic (same country -> same country) shipping estimate per Cardmarket
# country, in EUR: (cheapest untracked rate, cheapest tracked rate). Pulled
# directly from https://help.cardmarket.com/api/shippingCosts (one call per
# country, fromCountry == toCountry) on 2026-08-29 — not guessed, not
# derived from the German table. Tracking is mandatory Cardmarket-wide once
# an order exceeds 25 EUR (confirmed on the same help page, applies to every
# country checked), so that threshold stays a single constant rather than
# part of this table. Singapore and Japan returned no domestic shipping
# methods at all from Cardmarket's own API and are intentionally absent —
# not selectable rather than silently wrong.
SHIP_RATES = {
    "AT": ("Österreich", 1.30, 7.38), "BE": ("Belgien", 1.93, 9.65),
    "BG": ("Bulgarien", 1.10, 1.80), "CH": ("Schweiz", 1.84, 4.81),
    "CY": ("Zypern", 0.73, 2.96), "CZ": ("Tschechien", 1.64, 3.89),
    "DE": ("Deutschland", 1.25, 3.95), "DK": ("Dänemark", 3.48, 9.17),
    "EE": ("Estland", 2.10, 13.25), "ES": ("Spanien", 1.45, 7.06),
    "FI": ("Finnland", 3.30, 8.90), "FR": ("Frankreich", 1.82, 2.52),
    "GB": ("Großbritannien", 1.41, 4.79), "GR": ("Griechenland", 1.50, 2.40),
    "HR": ("Kroatien", 1.02, 2.75), "HU": ("Ungarn", 1.56, 4.32),
    "IE": ("Irland", 2.35, 10.50), "IS": ("Island", 2.44, 9.92),
    "IT": ("Italien", 1.60, 6.50), "LI": ("Liechtenstein", 1.73, 4.26),
    "LT": ("Litauen", 1.95, 4.10), "LU": ("Luxemburg", 1.50, 5.70),
    "LV": ("Lettland", 2.65, 4.29), "MT": ("Malta", 0.75, 4.35),
    "NL": ("Niederlande", 1.70, 6.20), "NO": ("Norwegen", 2.97, 8.24),
    "PL": ("Polen", 1.90, 2.85), "PT": ("Portugal", 1.30, 4.73),
    "RO": ("Rumänien", 1.98, 2.71), "SE": ("Schweden", 2.36, 10.14),
    "SI": ("Slowenien", 1.88, 3.84), "SK": ("Slowakei", 1.70, 3.40),
}
LANG_EXCLUDED = {"rin"}          # released in Italian only
# promo_types values that mark a real alternate treatment. Scryfall also uses
# promo_types for pure metadata ("universesbeyond", "boosterfun", "poster",
# "headliner"), so this has to stay a whitelist rather than a truthiness check —
# "universesbeyond" in particular is present on every UB card, base printing
# included, and made the classifier below mark plain #110 Smaug as special.
PROMO_LABEL = {
    "surgefoil": "Surge Foil", "galaxyfoil": "Galaxy Foil", "textured": "Textured Foil",
    "oilslick": "Oil Slick", "confettifoil": "Confetti Foil", "halofoil": "Halo Foil",
    "neonink": "Neon Ink", "gilded": "Gilded", "raisedfoil": "Raised Foil",
    "stepandcompleat": "Step-and-Compleat", "rainbowfoil": "Rainbow Foil",
    "doublerainbow": "Double Rainbow", "serialized": "Serialized",
    "ripplefoil": "Ripple Foil", "silverfoil": "Silver Foil",
    "manafoil": "Mana Foil", "invisibleink": "Invisible Ink",
    "fracturefoil": "Fracture Foil", "dossier": "Dossier", "embossed": "Embossed",
    "gleaminggold": "Gleaming Gold",
}
FRAME_LABEL = {"showcase": "Showcase", "extendedart": "Extended Art",
               "inverted": "Inverted", "etched": "Etched"}
# frame_effects values that mark a real alternate treatment. Scryfall also uses
# frame_effects for ordinary things ("legendary", "nyxtouched", "sunmoondfc"),
# so this has to stay a whitelist rather than a truthiness check.
SPECIAL_FRAMES = {"showcase", "extendedart", "etched", "inverted"}
FORMATS = ["standard", "pioneer", "modern", "legacy", "vintage", "commander",
           "pauper", "brawl", "duel", "oathbreaker", "premodern", "oldschool",
           "penny", "historic", "timeless"]
FMT_LABEL = {"standard": "Standard", "pioneer": "Pioneer", "modern": "Modern",
             "legacy": "Legacy", "vintage": "Vintage", "commander": "Commander",
             "pauper": "Pauper", "brawl": "Brawl", "duel": "Duel Commander",
             "oathbreaker": "Oathbreaker", "premodern": "Premodern",
             "oldschool": "Old School", "penny": "Penny Dreadful",
             "historic": "Historic", "timeless": "Timeless"}

# set_type -> (category, label).  Anything unlisted falls back to Special Set.
CATEGORY = {
    "core":            ("Normal Set",  "Core"),
    "expansion":       ("Normal Set",  "Expansion"),
    "masters":         ("Special Set", "Masters"),
    "masterpiece":     ("Special Set", "Masterpiece"),
    "draft_innovation":("Special Set", "Draft Innovation"),
    "funny":           ("Special Set", "Un-Set"),
    "token":           ("Special Set", "Token"),
    "memorabilia":     ("Special Set", "Memorabilia"),
    "promo":           ("Special Set", "Promo"),
    "alchemy":         ("Special Set", "Alchemy"),
    "spellbook":       ("Special Set", "Spellbook"),
    "minigame":        ("Special Set", "Minigame"),
    "arsenal":         ("Special Set", "Arsenal"),
    "commander":       ("Sealed Set",  "Commander"),
    "duel_deck":       ("Sealed Set",  "Duel Deck"),
    "planechase":      ("Sealed Set",  "Planechase"),
    "archenemy":       ("Sealed Set",  "Archenemy"),
    "premium_deck":    ("Sealed Set",  "Premium Deck"),
    "from_the_vault":  ("Sealed Set",  "From the Vault"),
    "box":             ("Sealed Set",  "Box Set"),
    "treasure_chest":  ("Sealed Set",  "Treasure Chest"),
    "starter":         ("Sealed Set",  "Starter"),
    "vanguard":        ("Special Set", "Vanguard"),
}
# these never count toward completion totals
NOT_COUNTED = {"token", "memorabilia", "promo", "funny", "alchemy", "minigame",
               "vanguard", "treasure_chest", "box", "from_the_vault",
               "premium_deck", "spellbook"}


# ----------------------------------------------------------------- database
def connect():
    os.makedirs(BASE, exist_ok=True)
    c = sqlite3.connect(DB, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    # These matter a lot on Windows, where every plain read goes through the
    # AV file-system filter: memory-mapped reads and a big page cache cut the
    # per-page syscalls that made a cold "set totals" recompute take 10-15 s.
    c.execute("PRAGMA synchronous=NORMAL")          # durable with WAL, far fewer fsyncs
    c.execute("PRAGMA temp_store=MEMORY")           # sort / materialize in RAM, no temp file
    c.execute("PRAGMA cache_size=-65536")           # 64 MB page cache
    try:
        c.execute("PRAGMA mmap_size=268435456")     # 256 MB memory-mapped I/O
    except sqlite3.OperationalError:
        pass
    return c


_local = threading.local()


def db():
    """One SQLite connection per thread. Sharing a single connection across the
    server's worker threads corrupts state and can crash the interpreter."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = connect()
    return conn


def init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS sets(
      code TEXT PRIMARY KEY, name TEXT, set_type TEXT, released TEXT,
      printed_size INT, icon TEXT, digital INT, parent TEXT, lang_only TEXT);
    CREATE TABLE IF NOT EXISTS cards(
      set_code TEXT, number TEXT, num_int INT, name TEXT, type_line TEXT,
      rarity TEXT, eur REAL, eur_foil REAL, booster INT, digital INT,
      extra INT DEFAULT 0, mana TEXT, cmc REAL, oracle TEXT, artist TEXT,
      colors TEXT, pt TEXT, img TEXT, cm_uri TEXT, scry_uri TEXT, legal TEXT,
      variant TEXT, finishes TEXT, ver INT DEFAULT 1, extras_idx INT DEFAULT 0,
      cm_suffix TEXT, cm_ver INT DEFAULT 1,
      PRIMARY KEY(set_code, number));
    CREATE TABLE IF NOT EXISTS set_pref(code TEXT PRIMARY KEY, mode TEXT,
      sealed_note TEXT, sealed_price REAL);
    CREATE TABLE IF NOT EXISTS cart(
      set_code TEXT, number TEXT, qty INT DEFAULT 1, added TEXT,
      PRIMARY KEY(set_code, number));
    CREATE TABLE IF NOT EXISTS collection(
      set_code TEXT, number TEXT, name TEXT, qty INT, lang TEXT, foil TEXT,
      PRIMARY KEY(set_code, number, lang, foil));
    CREATE TABLE IF NOT EXISTS history(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action TEXT, detail TEXT);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS price_history(
      set_code TEXT, number TEXT, date TEXT,
      eur_cents INTEGER, eur_foil_cents INTEGER,
      PRIMARY KEY(set_code, number, date)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS watchlist(
      set_code TEXT, number TEXT, added TEXT,
      PRIMARY KEY(set_code, number));
    CREATE INDEX IF NOT EXISTS ix_cards_set ON cards(set_code);
    CREATE INDEX IF NOT EXISTS ix_cards_name ON cards(name);
    CREATE INDEX IF NOT EXISTS ix_cards_rar ON cards(rarity);
    CREATE INDEX IF NOT EXISTS ix_coll_set ON collection(set_code);
    """)
    # add columns that later versions introduced (CREATE TABLE IF NOT EXISTS
    # silently leaves existing tables alone, so this has to be explicit)
    for table, cols in {
        "set_pref": {"sealed_note": "TEXT", "sealed_price": "REAL"},
        "sets": {"parent": "TEXT", "lang_only": "TEXT"},
        "cart": {"qty": "INT DEFAULT 1", "added": "TEXT"},
        "cards": {"ver": "INT DEFAULT 1", "extras_idx": "INT DEFAULT 0",
                  "cm_suffix": "TEXT", "cm_ver": "INT DEFAULT 1"},
        "collection": {"lang": "TEXT", "foil": "TEXT"},
    }.items():
        try:
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if not have:
            continue
        for col, decl in cols.items():
            if col not in have:
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                except sqlite3.Error:
                    pass
    c.commit()
    if meta_get(c, "schema") != str(SCHEMA):
        c.execute("DROP TABLE IF EXISTS cards")
        c.executescript("""
        CREATE TABLE cards(
          set_code TEXT, number TEXT, num_int INT, name TEXT, type_line TEXT,
          rarity TEXT, eur REAL, eur_foil REAL, booster INT, digital INT,
          extra INT DEFAULT 0, mana TEXT, cmc REAL, oracle TEXT, artist TEXT,
          colors TEXT, pt TEXT, img TEXT, cm_uri TEXT, scry_uri TEXT, legal TEXT,
          variant TEXT, finishes TEXT, ver INT DEFAULT 1, extras_idx INT DEFAULT 0,
          cm_suffix TEXT, cm_ver INT DEFAULT 1, name_de TEXT, type_de TEXT, oracle_de TEXT,
          PRIMARY KEY(set_code, number));
        CREATE INDEX ix_cards_set ON cards(set_code);
        CREATE INDEX ix_cards_name ON cards(name);
        CREATE INDEX ix_cards_rar ON cards(rarity);""")
        c.execute("DELETE FROM meta WHERE k='cards_updated'")
        c.commit()
        meta_set(c, "schema", SCHEMA)


def meta_get(c, k, d=None):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else d


def meta_set(c, k, v):
    c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (k, str(v)))
    c.commit()


def log(c, action, detail):
    c.execute("INSERT INTO history(ts,action,detail) VALUES(?,?,?)",
              (datetime.now().isoformat(timespec="seconds"), action, detail))
    c.execute("DELETE FROM history WHERE id NOT IN "
              "(SELECT id FROM history ORDER BY id DESC LIMIT 100)")
    c.commit()


# ------------------------------------------------------- GitHub self-update
def github_repo(c):
    """'owner/name' of the GitHub repo Binduno updates from. Defaults to the
    baked-in GITHUB_REPO; overridable in Settings or via BINDUNO_REPO."""
    r = (meta_get(c, "github_repo") or _env("REPO", "MTG_TRACKER_REPO")
         or GITHUB_REPO or "").strip()
    r = r.replace("https://github.com/", "").replace("http://github.com/", "").strip("/")
    return r if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", r) else ""


def _ver_tuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", s or ""))


def _http_text(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Binduno",
                                               "Accept": "application/vnd.github.raw"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _gh_json(path, timeout=10):
    return json.loads(_http_text("https://api.github.com" + path, timeout))


def github_latest(repo):
    """Newest binduno.py for `repo`: prefer the latest Release, fall back to
    the raw file on the default branch. Returns dict with srcUrl/latest/…"""
    info = {"repo": repo, "current": VERSION}
    try:
        try:
            rel = _gh_json(f"/repos/{repo}/releases/latest")
            tag = rel.get("tag_name") or ""
            src = next((a["browser_download_url"] for a in rel.get("assets", [])
                        if a.get("name") == "binduno.py"), None) \
                or f"https://raw.githubusercontent.com/{repo}/{tag}/binduno.py"
            info.update(tag=tag, title=rel.get("name") or tag, srcUrl=src,
                        notes=(rel.get("body") or "")[:4000],
                        htmlUrl=rel.get("html_url"), latest=re.sub(r"^v", "", tag))
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            br = _gh_json(f"/repos/{repo}").get("default_branch") or "main"
            src = f"https://raw.githubusercontent.com/{repo}/{br}/binduno.py"
            mv = re.search(r'VERSION\s*=\s*"([^"]+)"', _http_text(src))
            info.update(tag=br, title=f"{br} branch", srcUrl=src, notes="",
                        htmlUrl=f"https://github.com/{repo}",
                        latest=mv.group(1) if mv else "?")
    except urllib.error.HTTPError as e:
        raise RuntimeError("GitHub returned HTTP %s. %s" % (
            e.code, "Rate limit — try again in a few minutes."
            if e.code in (403, 429) else "Try again later."))
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError("Couldn't reach GitHub (%s). Check your internet "
                           "connection and try again." % (getattr(e, "reason", e),))
    info["newer"] = _ver_tuple(info.get("latest")) > _ver_tuple(VERSION)
    return info


def apply_new_source(c, src, note):
    """Shared by the file-upload and GitHub update paths: validate, back up
    the running file, overwrite it, and schedule a restart."""
    if "Binduno" not in src or "def main()" not in src:
        raise ValueError("This does not look like a Binduno script.")
    m = re.search(r'VERSION\s*=\s*"([^"]+)"', src)
    newver = m.group(1) if m else "?"
    compile(src, "binduno.py", "exec")                   # syntax check
    target = os.path.abspath(__file__)
    shutil.copy2(target, os.path.join(BASE, "binduno_previous.py"))
    with open(target, "w", encoding="utf-8") as f:
        f.write(src)
    log(c, "App", f"{note}: {VERSION} -> {newver}")
    threading.Timer(0.6, lambda: os.execv(sys.executable,
                                          [sys.executable, target])).start()
    return {"ok": True, "from": VERSION, "to": newver}


def _startup_update_check():
    """Runs once, a few seconds after launch. Records whether a newer version
    is on GitHub (Home shows a hint), and — only if the user opted in — installs
    it and restarts. Never installs without that opt-in."""
    time.sleep(5)
    try:
        c = connect()
    except Exception:                                          # noqa: BLE001
        return
    if meta_get(c, "auto_update_check", "1") != "1":
        return
    repo = github_repo(c)
    if not repo:
        return
    try:
        info = github_latest(repo)
    except Exception as e:                                      # noqa: BLE001
        UPDATE.update(checked=True, error=str(e))
        return
    UPDATE.update(checked=True, error="", current=VERSION,
                  available=bool(info.get("newer")), latest=info.get("latest", ""),
                  srcUrl=info.get("srcUrl", ""), htmlUrl=info.get("htmlUrl", ""),
                  notes=(info.get("notes") or "")[:2000])
    if UPDATE["available"] and meta_get(c, "auto_update_install") == "1":
        try:
            apply_new_source(c, _http_text(info["srcUrl"]), "Auto-updated from GitHub")
        except Exception as e:                                  # noqa: BLE001
            log(c, "App", f"Auto-update failed: {e}")


# ----------------------------------------------------------- scryfall import
REFRESH = {"running": False, "step": "", "pct": 0, "error": ""}

# Filled in once on startup by _startup_update_check() when the GitHub check is
# enabled; the Home page reads it to show an "update available" hint.
UPDATE = {"checked": False, "available": False, "latest": "", "current": VERSION,
          "srcUrl": "", "notes": "", "htmlUrl": "", "error": ""}

# The browser UI tells the server to stop when its tab goes away (beforeunload).
# A plain page reload — and the launch-time auto-open landing on a stale tab —
# fires that very same event, which used to kill the server right as the new
# page was loading ("site not reachable"). So /api/quit no longer exits on the
# spot: it arms a short timer, and any HTTP request that arrives before it
# fires (the reloaded page fetching itself) calls the shutdown off.
QUIT_TIMER = None
QUIT_GRACE = 2.5
TRAY_ACTIVE = False        # set once a menu-bar / tray icon is up
TRAY_ICON = None           # the pystray Icon, so an explicit Quit can remove it


def fetch(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=240)


def find_bulk_url(entry):
    hits = []

    def walk(o):
        if isinstance(o, str):
            if o.startswith("http") and (".json" in o or ".jsonl" in o):
                hits.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(entry)
    pref = [u for u in hits if "data.scryfall" in u or "bulk" in u]
    return (pref or hits or [None])[0]


def _norm_name(n):
    """Scryfall gives reversible cards (Inverted/Borderless shocklands, Art
    Series, Secret Lair reversibles…) a doubled "X // X" name. Collapse those
    to "X" so a printing groups with the plain card. Real double-faced cards
    (front != back) are left untouched."""
    n = (n or "").strip()
    if " // " in n:
        a, b = n.split(" // ", 1)
        if a.strip() and a.strip() == b.strip():
            return a.strip()
    return n


def refresh_cards():
    c = connect(); init(c)
    try:
        REFRESH.update(running=True, step="Loading set list", pct=3, error="")
        sets, url = {}, "https://api.scryfall.com/sets"
        while url:
            with fetch(url) as r:
                d = json.load(r)
            for s in d.get("data", []):
                sets[s["code"]] = [
                    s["code"], s.get("name", ""), s.get("set_type", ""),
                    s.get("released_at", "") or "",
                    s.get("printed_size") or s.get("card_count") or 0,
                    s.get("icon_svg_uri", "") or "",
                    1 if s.get("digital") else 0,
                    s.get("parent_set_code", "") or "", ""]
            url = d.get("next_page") if d.get("has_more") else None

        REFRESH.update(step="Finding bulk data", pct=8)
        with fetch("https://api.scryfall.com/bulk-data") as r:
            cat = json.load(r)
        # "all_cards" (not "default_cards") is required for the German
        # card-name-language setting: Scryfall's own bulk-data description says
        # default_cards only includes a foreign-language object for a card when
        # that card was NEVER printed in English — a bilingual card like most of
        # the German-language product line simply has no German object in it.
        entry = next((e for e in cat.get("data", []) if e.get("type") == "all_cards"), None)
        if not entry:
            raise RuntimeError("all_cards not available")
        durl = find_bulk_url(entry)
        if not durl:
            raise RuntimeError("no download link in bulk-data entry")

        tmp = os.path.join(BASE, "bulk.tmp")
        total = entry.get("compressed_size") or entry.get("size", 0) or 1
        REFRESH.update(step="Downloading card data", pct=10)
        with fetch(durl) as r, open(tmp, "wb") as f:
            got = 0
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b); got += len(b)
                REFRESH["pct"] = 10 + min(55, int(got / total * 55))
                REFRESH["step"] = f"Downloading card data — {got/1e6:.0f} MB"

        with open(tmp, "rb") as f:
            magic = f.read(2)
        opener = gzip.open if magic == b"\x1f\x8b" else open

        REFRESH.update(step="Reading cards", pct=68)
        rows = []
        alt_pick = {}
        with opener(tmp, "rt", encoding="utf-8") as f:
            first = f.read(1); f.seek(0)
            it = json.load(f) if first == "[" else None
            if it is None:
                it = []
                for line in f:
                    line = line.strip().rstrip(",")
                    if line[:1] == "{":
                        try:
                            it.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            # Some sets never had an English printing (Renaissance, FBB, ...).
            # Import those in their own language so they are at least visible.
            # Language exclusivity is decided per PRINTING (set + collector
            # number), not per set: masterpiece sheets like Mystical Archive
            # mix English base printings with Japanese-only alternate-art
            # collector numbers in the very same set, and a whole-set English
            # check would silently drop every one of those Japanese exclusives.
            en_sets = {x.get("set") for x in it if x.get("lang") == "en"}
            en_cn = {(x.get("set"), x.get("collector_number"))
                     for x in it if x.get("lang") == "en"}
            # German display name for the card-name-language setting (independent
            # of the page-language UI toggle). Scryfall's `name` is always the
            # canonical English Oracle name, even on foreign prints — the actual
            # localized text printed on a German card lives in `printed_name` on
            # that German print object. Collected separately from which printing
            # ends up "kept" above, since a card can be kept in English while a
            # German printing of the same collector number still exists to name it.
            de_names, de_types, de_oracle = {}, {}, {}
            for x in it:
                if x.get("lang") != "de":
                    continue
                key = (x.get("set"), x.get("collector_number"))
                if x.get("printed_name"):
                    de_names[key] = x["printed_name"]
                if x.get("printed_type_line"):
                    de_types[key] = x["printed_type_line"]
                dfaces = x.get("card_faces") or []
                if dfaces and not x.get("printed_text"):
                    txt = "\n\n//\n\n".join(f.get("printed_text", "") for f in dfaces)
                    if txt.strip():
                        de_oracle[key] = txt
                elif x.get("printed_text"):
                    de_oracle[key] = x["printed_text"]
            from collections import Counter as _C
            alt_lang = {}
            for x in it:
                code = x.get("set", "")
                cn = x.get("collector_number", "")
                if (code, cn) in en_cn or code not in sets:
                    continue
                alt_lang.setdefault((code, cn), _C())[x.get("lang", "")] += 1
            # prefer German when a printing never had an English version,
            # otherwise fall back to whichever language has the most cards
            alt_pick = {}
            for key, cnt in alt_lang.items():
                if not cnt:
                    continue
                alt_pick[key] = "de" if cnt.get("de") else cnt.most_common(1)[0][0]
            if alt_pick:
                REFRESH["step"] = f"Reading cards — {len(alt_pick)} non-English printings"
            # Sets that never had an English printing at all still get their
            # `lang_only` marker for the UI; mixed sets like Mystical Archive
            # stay unmarked since most of their printings are English.
            set_lang = {}
            for (code, _cn), lang in alt_pick.items():
                if code in en_sets:
                    continue
                set_lang.setdefault(code, _C())[lang] += 1
            set_lang_pick = {code: ("de" if cnt.get("de") else cnt.most_common(1)[0][0])
                              for code, cnt in set_lang.items()}

            seen_alt = set()
            for k in it:
                code = k.get("set", "")
                cn = k.get("collector_number", "")
                lang = k.get("lang")
                if lang != "en":
                    if alt_pick.get((code, cn)) != lang:
                        continue
                    key = (code, cn)
                    if key in seen_alt:
                        continue
                    seen_alt.add(key)
                s = sets.get(code)
                if not s:
                    continue
                p = k.get("prices") or {}
                eur = p.get("eur")
                eurf = p.get("eur_foil")
                cn = k.get("collector_number", "")
                m = re.match(r"(\d+)", cn)
                faces = k.get("card_faces") or []
                imgs = k.get("image_uris") or (faces[0].get("image_uris") if faces else None) or {}
                img = imgs.get("normal") or imgs.get("large") or imgs.get("small") or ""
                if faces and not k.get("oracle_text"):
                    oracle = "\n\n//\n\n".join(f.get("oracle_text", "") for f in faces)
                    mana = " // ".join(f.get("mana_cost", "") for f in faces if f.get("mana_cost"))
                else:
                    oracle = k.get("oracle_text", "") or ""
                    mana = k.get("mana_cost", "") or ""
                lg = k.get("legalities") or {}
                legal = "".join((lg.get(f) or "not_legal")[0] for f in FORMATS)
                pt = (f"{k.get('power')}/{k.get('toughness')}"
                      if k.get("power") is not None else (k.get("loyalty") or ""))
                fin = k.get("finishes") or []
                bits = [PROMO_LABEL[t] for t in (k.get("promo_types") or [])
                        if t in PROMO_LABEL]
                bits += [FRAME_LABEL[t] for t in (k.get("frame_effects") or [])
                         if t in FRAME_LABEL]
                if k.get("border_color") == "borderless":
                    bits.append("Borderless")
                if not bits and fin == ["foil"]:
                    bits.append("Foil only")
                variant = " · ".join(dict.fromkeys(bits))
                ni = int(m.group(1)) if m else 0
                psize = s[4] or 0
                # Cardmarket's ": Extras" expansion holds every printing that is
                # not the plain base-frame card. Booster-fun treatments are
                # numbered *inside* the printed range in modern sets — the
                # Dragon-Hoard Smaug is HOB #229 of 248 — so testing the
                # collector number against printed_size misses them entirely.
                # printed_size is also nullable on Scryfall; the card_count
                # fallback (321 for HOB) then makes the test never fire at all.
                # Classify by treatment instead and keep the number test only as
                # a backstop for printings Scryfall does not tag.
                special = (bool(set(k.get("promo_types") or []) & set(PROMO_LABEL))
                           or bool(set(k.get("frame_effects") or []) & SPECIAL_FRAMES)
                           or k.get("border_color") == "borderless")
                extra = 1 if (special or k.get("promo") or k.get("variation")
                              or k.get("oversized")
                              or (psize and ni > psize)) else 0
                rows.append((code, cn, ni,
                             _norm_name(k.get("name", "")), k.get("type_line") or "",
                             (k.get("rarity") or "r")[:1],
                             float(eur) if eur else None,
                             float(eurf) if eurf else None,
                             1 if k.get("booster") else 0,
                             1 if (k.get("digital") or s[6]) else 0,
                             extra, mana, k.get("cmc") or 0, oracle,
                             k.get("artist", "") or "",
                             "".join(k.get("colors") or []), pt, img,
                             (k.get("purchase_uris") or {}).get("cardmarket", "") or "",
                             k.get("scryfall_uri", "") or "", legal,
                             variant, ",".join(fin)))

        # Cardmarket numbers the printings of one card name inside a set as
        # V.1, V.2 ... in collector-number order. Mirror that so want lists
        # point at the right version.
        # Cardmarket keeps the regular printings inside the set itself and puts
        # special treatments into separate expansions called
        # "<Set>: Extras Version 1", "... Version 2" and so on, numbered per card
        # in collector-number order. Mirror both so want lists resolve.
        # Safety net: products where *every* printing is a special treatment
        # (Secret Lairs, Art Series, promo-only sets) have no separate ": Extras"
        # page on Cardmarket — everything lives in the one expansion. Without
        # this, such a set would emit "(V.1) (Secret Lair Drop: Extras)".
        by_set = {}
        for i, r in enumerate(rows):
            by_set.setdefault(r[0], []).append(i)
        for _code, idxs in by_set.items():
            if all(rows[i][10] for i in idxs):
                for i in idxs:
                    rows[i] = rows[i][:10] + (0,) + rows[i][11:]

        by_name = {}
        for i, r in enumerate(rows):
            by_name.setdefault((r[0], r[3]), []).append(i)
        vers = [1] * len(rows)
        extras = [0] * len(rows)
        for _key, idxs in by_name.items():
            base = sorted((i for i in idxs if not rows[i][10]), key=lambda i: rows[i][2])
            extra = sorted((i for i in idxs if rows[i][10]), key=lambda i: rows[i][2])
            for pos, i in enumerate(base, start=1):
                vers[i] = pos
            for pos, i in enumerate(extra, start=1):
                vers[i] = 1
                extras[i] = pos
        # Every set that has special treatments gets a ": Extras" expansion on
        # Cardmarket; the number shown there as "Version 1/2/3" is that card's
        # position among the extras, counted separately from the base printings.
        suffixes, cmvers = [""] * len(rows), [1] * len(rows)
        for _key, idxs in by_name.items():
            for i in idxs:
                if extras[i]:
                    suffixes[i] = ": Extras"
                    cmvers[i] = extras[i]
                else:
                    cmvers[i] = vers[i]
        rows = [r + (vers[i], extras[i], suffixes[i], cmvers[i],
                     _norm_name(de_names.get((r[0], r[1]), "")),
                     de_types.get((r[0], r[1]), ""), de_oracle.get((r[0], r[1]), ""))
                for i, r in enumerate(rows)]

        REFRESH.update(step="Saving to database", pct=88)
        for code, lg in set_lang_pick.items():
            if code in sets:
                sets[code][8] = lg
        c.execute("DELETE FROM sets"); c.execute("DELETE FROM cards")
        c.executemany("INSERT INTO sets VALUES(?,?,?,?,?,?,?,?,?)",
                      [tuple(v) for v in sets.values()])
        c.executemany("INSERT OR REPLACE INTO cards VALUES(" + ",".join("?"*30) + ")", rows)
        c.commit()
        meta_set(c, "cards_updated", datetime.now().isoformat(timespec="seconds"))
        log(c, "Card data", f"{len(rows):,} printings from {len(sets):,} sets downloaded")
        try:
            os.remove(tmp)
        except OSError:
            pass
        if price_logging_enabled(c):
            REFRESH.update(step="Logging price history", pct=95)
            n = log_price_history(c)
            downsample_price_history(c)
            log(c, "Price history", f"{n:,} price(s) changed and logged")
        REFRESH.update(running=False, step="Done", pct=100)
    except Exception as e:                                   # noqa: BLE001
        REFRESH.update(running=False, step="Failed", error=str(e))
    finally:
        c.close()


def price_logging_enabled(c):
    return meta_get(c, "price_logging") != "0"          # on by default


def log_price_history(c):
    """Append today's Cardmarket EUR price for every priced card, but only
    the cards whose price actually changed since the last logged point —
    most bulk cards sit flat for weeks, so this keeps the table far smaller
    than a full daily snapshot of all ~117k printings would."""
    today = datetime.now().strftime("%Y-%m-%d")
    latest = {}
    for r in c.execute("""
            SELECT h.set_code, h.number, h.eur_cents, h.eur_foil_cents
            FROM price_history h
            JOIN (SELECT set_code, number, MAX(date) d FROM price_history
                  GROUP BY set_code, number) m
              ON m.set_code = h.set_code AND m.number = h.number AND m.d = h.date"""):
        latest[(r["set_code"], r["number"])] = (r["eur_cents"], r["eur_foil_cents"])
    rows = []
    for r in c.execute("""SELECT set_code, number, eur, eur_foil FROM cards
                          WHERE eur IS NOT NULL OR eur_foil IS NOT NULL"""):
        ec = round(r["eur"] * 100) if r["eur"] is not None else None
        efc = round(r["eur_foil"] * 100) if r["eur_foil"] is not None else None
        if latest.get((r["set_code"], r["number"])) != (ec, efc):
            rows.append((r["set_code"], r["number"], today, ec, efc))
    if rows:
        c.executemany("""INSERT INTO price_history(set_code, number, date, eur_cents, eur_foil_cents)
                          VALUES(?,?,?,?,?)
                          ON CONFLICT(set_code, number, date) DO UPDATE SET
                            eur_cents=excluded.eur_cents, eur_foil_cents=excluded.eur_foil_cents""",
                      rows)
        c.commit()
    return len(rows)


def downsample_price_history(c):
    """Once a logged point is more than a year old, keep only one point per
    week instead of one per day — bounds long-term growth instead of
    letting the table grow forever at full daily resolution."""
    cutoff = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    c.execute("""
        DELETE FROM price_history
        WHERE date < ?
        AND date NOT IN (
            SELECT MIN(date) FROM price_history
            WHERE date < ?
            GROUP BY set_code, number, strftime('%Y-%W', date))""",
              (cutoff, cutoff))
    c.commit()


WATCHLIST_MAX = 100


def watchlist_rows(c):
    """One row per watched card: current price plus a 7-point daily series
    built by forward-filling price_history (which only has a row on days
    the price actually changed) — cards with no history yet just show a
    flat line at the current price."""
    watched = c.execute("SELECT set_code, number FROM watchlist ORDER BY added DESC").fetchall()
    if not watched:
        return []
    keys = [(w["set_code"], w["number"]) for w in watched]
    values_sql = ",".join("(?,?)" for _ in keys)
    args = [x for k in keys for x in k]
    cards = {(r["set_code"], r["number"]): r for r in c.execute(
        f"""SELECT k.set_code, k.number, k.name, k.name_de, k.eur, k.img, k.rarity, s.name set_name
            FROM cards k JOIN sets s ON s.code=k.set_code
            WHERE (k.set_code, k.number) IN (VALUES {values_sql})""", args)}
    hist = {}
    for r in c.execute(
            f"""SELECT set_code, number, date, eur_cents FROM price_history
                WHERE (set_code, number) IN (VALUES {values_sql}) ORDER BY date""", args):
        hist.setdefault((r["set_code"], r["number"]), []).append((r["date"], r["eur_cents"]))

    today = datetime.now().date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    out = []
    for k in keys:
        card = cards.get(k)
        if not card:
            continue
        points = hist.get(k, [])
        vals, cur, idx = [], None, 0
        for day in days:
            while idx < len(points) and points[idx][0] <= day:
                cur = points[idx][1]
                idx += 1
            vals.append(cur)
        if vals[0] is None:
            fallback = round((card["eur"] or 0) * 100)
            vals = [v if v is not None else fallback for v in vals]
        first, last = vals[0], vals[-1]
        change = round((last - first) / first * 100, 1) if first else None
        out.append({"set": k[0], "number": k[1], "name": card["name"], "nameDe": card["name_de"] or "",
                     "setName": card["set_name"], "img": card["img"], "rarity": card["rarity"],
                     "eur": round(card["eur"] or 0, 2),
                     "series": [v / 100 for v in vals], "changePct": change,
                     "changeEur": round((last - first) / 100, 2)})
    return out


# MTGJSON is used only for this one-time (or occasionally re-run) backfill —
# the app's own daily price sync (log_price_history above) needs no external
# ID mapping since it reads straight from its own `cards` table. MTGJSON
# keys its price data by its own UUIDs, not Scryfall IDs, so AllPrintings.sqlite
# is downloaded temporarily just to build a uuid -> (set_code, number)
# crosswalk (verified against real data: e.g. FRF #2 in MTGJSON is the same
# "Abzan Advantage" as in this app's own Scryfall-sourced cards table), then
# deleted — only the derived price rows are kept.
MTGJSON_PRINTINGS_URL = "https://mtgjson.com/api/v5/AllPrintings.sqlite.gz"
MTGJSON_PRICES_URL = "https://mtgjson.com/api/v5/AllPrices.json.gz"


def _download_to(url, dest):
    # urlretrieve sends no headers at all — mtgjson.com/GitHub 403s the
    # default urllib User-Agent, same as Scryfall's UA requirement elsewhere.
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=240) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def backfill_price_history():
    c = connect(); init(c)
    tmp_dir = tempfile.mkdtemp(prefix="binduno_price_backfill_")
    try:
        REFRESH.update(running=True, step="Downloading MTGJSON card index", pct=5, error="")
        printings_gz = os.path.join(tmp_dir, "AllPrintings.sqlite.gz")
        _download_to(MTGJSON_PRINTINGS_URL, printings_gz)

        REFRESH.update(step="Extracting card index", pct=30)
        printings_db = os.path.join(tmp_dir, "AllPrintings.sqlite")
        with gzip.open(printings_gz, "rb") as fin, open(printings_db, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        os.remove(printings_gz)

        REFRESH.update(step="Building card index", pct=40)
        idx_conn = sqlite3.connect(printings_db)
        crosswalk = {u: (sc.lower(), num)
                     for u, sc, num in idx_conn.execute("SELECT uuid, setCode, number FROM cards")}
        idx_conn.close()
        os.remove(printings_db)

        REFRESH.update(step="Downloading 90-day price history", pct=50)
        prices_gz = os.path.join(tmp_dir, "AllPrices.json.gz")
        _download_to(MTGJSON_PRICES_URL, prices_gz)

        REFRESH.update(step="Reading price history", pct=75)
        with gzip.open(prices_gz, "rt", encoding="utf-8") as f:
            prices = json.load(f)["data"]
        os.remove(prices_gz)

        REFRESH.update(step="Matching and logging prices", pct=90)
        rows = []
        for uuid, entry in prices.items():
            key = crosswalk.get(uuid)
            if not key:
                continue
            cm = entry.get("paper", {}).get("cardmarket")
            if not cm or cm.get("currency") != "EUR":
                continue
            retail = cm.get("retail", {})
            normal, foil = retail.get("normal", {}), retail.get("foil", {})
            last = None
            last_ec = last_efc = None
            for d in sorted(set(normal) | set(foil)):
                if d in normal:
                    last_ec = round(normal[d] * 100)
                if d in foil:
                    last_efc = round(foil[d] * 100)
                cur = (last_ec, last_efc)
                if cur != last:
                    rows.append((key[0], key[1], d, last_ec, last_efc))
                    last = cur
        if rows:
            # OR IGNORE: never clobber a day the app already logged itself
            # (its own observed Scryfall price) with MTGJSON's version.
            c.executemany("""INSERT OR IGNORE INTO price_history
                              (set_code, number, date, eur_cents, eur_foil_cents)
                              VALUES(?,?,?,?,?)""", rows)
            c.commit()
        downsample_price_history(c)
        meta_set(c, "price_backfill_done", "1")
        log(c, "Price history", f"Backfilled {len(rows):,} price point(s) from MTGJSON (90 days)")
        REFRESH.update(running=False, step="Done", pct=100)
    except Exception as e:                                   # noqa: BLE001
        REFRESH.update(running=False, step="Failed", error=str(e))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        c.close()


# ------------------------------------------------------------ collection I/O
def _lang_code(s):
    """Normalise a language cell to a 2-letter code. Different exporters write
    'en', 'English' or 'Englisch' — Binduno stores the short code."""
    s = (s or "").strip().lower()
    if not s:
        return "en"
    return {"english": "en", "englisch": "en", "german": "de", "deutsch": "de",
            "french": "fr", "französisch": "fr", "francais": "fr", "italian": "it",
            "italienisch": "it", "italiano": "it", "spanish": "es", "spanisch": "es",
            "espanol": "es", "portuguese": "pt", "portugiesisch": "pt",
            "japanese": "ja", "japanisch": "ja", "korean": "ko", "koreanisch": "ko",
            "russian": "ru", "russisch": "ru", "chinese": "zh",
            "chinese simplified": "zh", "chinese traditional": "zh"}.get(s, s[:2])


def _foil_norm(s):
    """normal / foil / etched from whatever an exporter puts in its foil column."""
    s = (s or "").strip().lower()
    if s in ("", "normal", "nonfoil", "non-foil", "none", "no", "false", "0"):
        return "normal"
    if "etch" in s:
        return "etched"
    return "foil"


# Each supported exporter maps its own column names onto Binduno's internal
# row shape: (set_code, collector_number, front_face_name, quantity, language,
# foil). Matching later happens on set_code + numeric collector number, so a
# format only works if its export carries both.
def _imp_manabox(rows):
    return [((r.get("Set code") or ""), (r.get("Collector number") or ""),
             (r.get("Name") or "").split(" // ")[0], r.get("Quantity"),
             r.get("Language"), _foil_norm(r.get("Foil"))) for r in rows]


def _imp_moxfield(rows):
    return [((r.get("Edition") or ""), (r.get("Collector Number") or ""),
             (r.get("Name") or "").split(" // ")[0], r.get("Count"),
             r.get("Language"), _foil_norm(r.get("Foil"))) for r in rows]


def _imp_archidekt(rows):
    return [((r.get("Edition Code") or r.get("Set Code") or ""),
             (r.get("Collector Number") or ""),
             (r.get("Name") or "").split(" // ")[0], r.get("Quantity"),
             r.get("Language"), _foil_norm(r.get("Finish") or r.get("Foil")))
            for r in rows]


IMPORT_FORMATS = {
    "manabox":   ("ManaBox",   {"Name", "Set code", "Collector number", "Quantity"},
                  _imp_manabox),
    "moxfield":  ("Moxfield",   {"Name", "Edition", "Collector Number", "Count"},
                  _imp_moxfield),
    "archidekt": ("Archidekt",  {"Name", "Edition Code", "Collector Number", "Quantity"},
                  _imp_archidekt),
}


def _detect_format(fields):
    fs = set(fields or [])
    for fmt, (_label, need, _fn) in IMPORT_FORMATS.items():
        if need.issubset(fs):
            return fmt
    return None


def _commit_collection_rows(c, raw_rows, mode, label):
    """raw_rows: iterable of (set_code, number, name, qty, lang, foil) — the
    shape every IMPORT_FORMATS parser (and the Cardmarket-purchase import)
    returns. Aggregates duplicates and writes them into `collection`."""
    agg, cards = {}, 0
    for setc, num, name, qraw, lang, foil in raw_rows:
        try:
            q = int(float(qraw or 1))
        except (ValueError, TypeError):
            q = 1
        if q <= 0:
            continue
        key = (setc.lower().strip(), num.strip(), _lang_code(lang), foil)
        if key in agg:
            agg[key][0] += q
        else:
            agg[key] = [q, name.strip()]
        cards += q
    rows = [(k[0], k[1], v[1], v[0], k[2], k[3]) for k, v in agg.items()]
    if not rows:
        return {"rows": 0, "cards": 0, "mode": mode}
    if mode == "replace":
        c.execute("DELETE FROM collection")
        c.executemany("INSERT OR REPLACE INTO collection VALUES(?,?,?,?,?,?)", rows)
    else:
        for row in rows:
            c.execute("""INSERT INTO collection VALUES(?,?,?,?,?,?)
                         ON CONFLICT(set_code,number,lang,foil)
                         DO UPDATE SET qty=qty+excluded.qty""", row)
    meta_set(c, "collection_updated", datetime.now().isoformat(timespec="seconds"))
    log(c, "Collection", f"{cards:,} cards imported ({label}, {mode} mode)")
    c.commit()
    # Nudge the WAL toward the main db so other worker connections see the new
    # collection promptly. PASSIVE never blocks (TRUNCATE could wait on a
    # reader for the full busy_timeout — that was the 10-20 s stall after an
    # import).
    try:
        c.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except sqlite3.Error:
        pass
    return {"rows": len(rows), "cards": cards, "mode": mode}


def import_collection(c, text, mode, fmt="auto"):
    text = (text or "").lstrip("\ufeff").lstrip()
    if text[:1] in ("{", "["):
        raise ValueError("This looks like a JSON file, not a CSV export. Export your "
                         "collection as CSV from ManaBox, Moxfield or Archidekt.")
    rdr = csv.DictReader(io.StringIO(text))
    fields = rdr.fieldnames or []
    if fmt in ("auto", "", None):
        fmt = _detect_format(fields)
        if not fmt:
            fs = set(fields)
            if "Count" in fs and ("Card Number" in fs or "Edition" in fs):
                raise ValueError("This looks like a Deckbox export. Deckbox lists set "
                                 "names instead of set codes, which Binduno can't match "
                                 "yet — please export from ManaBox, Moxfield or Archidekt.")
            raise ValueError("Unrecognised collection file. Supported: ManaBox, Moxfield, "
                             "Archidekt (CSV export). Columns found: "
                             + (", ".join(fields) if fields else "none"))
    if fmt not in IMPORT_FORMATS:
        raise ValueError(f"Unknown import format: {fmt}")
    label, need, parse = IMPORT_FORMATS[fmt]
    if not need.issubset(set(fields)):
        missing = ", ".join(sorted(need - set(fields)))
        raise ValueError(f"This does not look like a {label} export — missing "
                         f"column(s): {missing}")
    result = _commit_collection_rows(c, parse(list(rdr)), mode, label)
    if not result["rows"]:
        raise ValueError(f"The {label} file was read, but held no cards.")
    result["format"] = fmt
    result["formatLabel"] = label
    return result


def import_cm_purchase(c, items, mode="add"):
    """Add every article scraped from a Cardmarket purchase page straight into
    the collection. items: [{name, setSlug, setTitle, number, qty, foil, lang}]
    - the same shape the cm-helper userscript already builds for /api/cm-match.
    Cards whose set can't be resolved (promos, unusual products) are skipped
    and reported back so the user can add them by hand."""
    rows, skipped = [], []
    for it in items:
        name = (it.get("name") or "").strip()
        num = str(it.get("number") or "").strip()
        code, _ = resolve_cm_set(c, it.get("setSlug", ""), it.get("setTitle", ""))
        if not name or not num or not code:
            if name:
                skipped.append(name)
            continue
        rows.append((code, num, name, it.get("qty") or 1,
                     it.get("lang") or "en", "foil" if it.get("foil") else "normal"))
    result = _commit_collection_rows(c, rows, mode, "Cardmarket purchase")
    result["matched"] = len(rows)
    result["skipped"] = len(skipped)
    result["skippedNames"] = skipped[:20]
    return result


# --------------------------------------------------------------- computation
def shipping(n, value, tracked_only=False, country="DE"):
    if n <= 0:
        return 0.0
    orders = max(1, -(-n // CARDS_PER_SELLER))
    per_value = value / orders
    untracked, tracked = SHIP_RATES.get(country, SHIP_RATES["DE"])[1:]
    unit = tracked if (tracked_only or per_value > 25) else untracked
    return round(orders * unit, 2)


def tracked_shipping_only(c):
    return meta_get(c, "tracked_shipping_only") == "1"


def shipping_country(c):
    return meta_get(c, "shipping_country") or "DE"


# ---- Set-completion goal: what has to be owned for a set to read 100% ----
# scope      "names"     one printing of every card name is enough
#            "printings"  every eligible collector number counts on its own
# extras     "exclude"    only the plain base-frame printing of each card
#            "include"    Showcase / Borderless / Extended Art / special foils too
# serialized "exclude"    drop the numbered limited prints (only relevant when
#            "include"    extras are included — serialized is a subset of extras)
GOAL_DEFAULTS = {"scope": "names", "extras": "exclude", "serialized": "exclude"}


def goal_prefs(c):
    return {k: meta_get(c, "goal_" + k) or v for k, v in GOAL_DEFAULTS.items()}


def endgame_prefs(c):
    """{on, eur}. When off, eur is a number no card will ever reach, so the
    'defer expensive cards' logic simply never fires."""
    on = meta_get(c, "endgame_on") == "1"          # off unless explicitly enabled
    try:
        eur = float(meta_get(c, "endgame_eur") or ENDGAME_EUR)
    except ValueError:
        eur = ENDGAME_EUR
    return {"on": on, "eur": eur if on else 1e18}


def goal_eligible(extra, variant, gp):
    """Does this printing count toward the set's 100% mark?"""
    if not extra:
        return True
    if gp["extras"] != "include":
        return False
    if gp["serialized"] != "include" and "Serialized" in (variant or ""):
        return False
    return True


def set_progress(rows, gp, eg_eur=ENDGAME_EUR):
    """rows: list of dicts with name, extra, variant, eur, have(bool).
    Returns the goal-aware totals for one set. printingsTotal/Owned stay raw
    (every printing) so the Home 'prints' tile keeps its literal meaning."""
    for r in rows:
        r["inGoal"] = goal_eligible(r["extra"], r["variant"], gp)
    pr_total = len(rows)
    pr_owned = sum(1 for r in rows if r["have"])
    by_name = {}
    for r in rows:
        by_name.setdefault(r["name"], []).append(r)
    total = owned = missing = only_extra = eg_n = 0
    missing_val = eg_v = 0.0
    if gp["scope"] == "printings":
        elig = [r for r in rows if r["inGoal"]]
        total = len(elig)
        owned = sum(1 for r in elig if r["have"])
        for grp in by_name.values():                     # cost: one buy per missing name
            eg = [r for r in grp if r["inGoal"]]
            if not eg or any(r["have"] for r in grp):
                continue
            price = min((r["eur"] or 0) for r in eg)
            if price >= eg_eur:
                eg_n += 1; eg_v += price
            else:
                missing += 1; missing_val += price
    else:                                                # "names"
        for grp in by_name.values():
            eg = [r for r in grp if r["inGoal"]]
            if not eg:
                continue
            total += 1
            if any(r["have"] for r in grp):
                owned += 1
                if not any(r["have"] for r in eg):
                    only_extra += 1                      # owns it, but not a base print
                continue
            price = min((r["eur"] or 0) for r in eg)
            if price >= eg_eur:
                eg_n += 1; eg_v += price
            else:
                missing += 1; missing_val += price
    return {"total": total, "owned": owned, "missing": missing,
            "missingValue": missing_val, "endgameCount": eg_n, "endgameValue": eg_v,
            "onlyExtra": only_extra,
            "printingsTotal": pr_total, "printingsOwned": pr_owned}


def set_rows(c):
    """One aggregated row per set.

    Counting uses exactly the same rules as the buy list: a card is only
    "missing" if you own no printing of that name inside the set, alternate
    printings of a name are counted once, and anything at or above the
    endgame threshold is left out of the cost.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    tracked, ship_country = tracked_shipping_only(c), shipping_country(c)
    gp = goal_prefs(c)
    eg_eur = endgame_prefs(c)["eur"]
    prefs, sealed = {}, {}
    for r in c.execute("SELECT code,mode,sealed_note,sealed_price FROM set_pref"):
        prefs[r["code"]] = r["mode"]
        if r["sealed_note"] or r["sealed_price"]:
            sealed[r["code"]] = {"note": r["sealed_note"] or "",
                                 "price": r["sealed_price"] or 0}
    meta = {}
    for r in c.execute("SELECT code,name,set_type,released,icon,digital,printed_size,"
                       "parent,lang_only FROM sets"):
        meta[r["code"]] = dict(r)

    rows = c.execute("""
        SELECT k.set_code, k.num_int, k.name, k.rarity, k.eur, k.extra, k.variant,
               CASE WHEN o.qty IS NULL THEN 0 ELSE 1 END AS have
        FROM cards k
        JOIN sets s ON s.code = k.set_code
        LEFT JOIN (SELECT set_code, number, SUM(qty) qty FROM collection
                   GROUP BY set_code, number) o
               ON o.set_code = k.set_code AND o.number = k.number
        WHERE k.digital = 0 AND s.released <> '' AND s.released <= ?
        ORDER BY k.set_code, k.num_int
    """, (today,)).fetchall()

    per = {}
    for r in rows:
        per.setdefault(r["set_code"], []).append(dict(r))

    out = []
    for code, cards in per.items():
        m = meta.get(code)
        if not m:
            continue
        pg = set_progress(cards, gp, eg_eur)
        total, owned = pg["total"], pg["owned"]
        missing, missing_value = pg["missing"], pg["missingValue"]
        eg_n, eg_v = pg["endgameCount"], pg["endgameValue"]
        owned_value = sum((r["eur"] or 0) for r in cards if r["have"])
        st = m["set_type"] or ""
        cat, label = CATEGORY.get(st, ("Special Set",
                                       st.replace("_", " ").title() or "Other"))
        if m["name"].endswith(" Eternal") or ": Eternal" in m["name"]:
            cat, label = "Special Set", "Eternal"
        lang_only = m["lang_only"] or ""
        auto = ((st not in NOT_COUNTED) and (code not in LANG_EXCLUDED)
                and not lang_only)
        pref = prefs.get(code)
        counted = True if pref == "include" else (False if pref == "exclude" else auto)
        ship = shipping(missing, missing_value, tracked, ship_country)
        out.append({
            "code": code, "name": m["name"], "released": m["released"],
            "icon": m["icon"], "category": cat, "label": label,
            "kind": f"{cat} — {label}", "counted": counted,
            "setType": st, "parent": m["parent"] or "", "langOnly": lang_only,
            "sealed": sealed.get(code),
            "total": total, "owned": owned,
            "pct": owned / total if total else 0,
            "printingsTotal": pg["printingsTotal"], "printingsOwned": pg["printingsOwned"],
            "onlyExtra": pg["onlyExtra"],
            "ownedValue": round(owned_value, 2),
            "missing": missing, "missingValue": round(missing_value, 2),
            "shipping": ship, "totalCost": round(missing_value + ship, 2),
            "endgameCount": eg_n, "endgameValue": round(eg_v, 2),
            "auto": auto, "pref": pref or "",
        })
    out.sort(key=lambda x: (x["released"], x["name"]), reverse=True)
    return out


def home_stats(c, sets):
    counted = [s for s in sets if s["counted"]]
    # the "prints" tile stays a literal count of every distinct printing;
    # the set-completion figures (pct, missing, setsComplete) are goal-aware.
    printings = sum(s["printingsTotal"] for s in counted)
    owned_pr = sum(s["printingsOwned"] for s in counted)
    codes = tuple(s["code"] for s in counted) or ("",)
    marks = ",".join("?" * len(codes))
    today = datetime.now().strftime("%Y-%m-%d")
    names_total = c.execute(
        f"""SELECT COUNT(DISTINCT k.name) n FROM cards k JOIN sets s ON s.code=k.set_code
            WHERE k.set_code IN ({marks}) AND k.digital=0 AND k.extra=0""", codes).fetchone()["n"]
    names_owned = c.execute(
        f"""SELECT COUNT(DISTINCT k.name) n FROM cards k JOIN sets s ON s.code=k.set_code
            JOIN collection o ON o.set_code=k.set_code AND o.number=k.number
            WHERE k.set_code IN ({marks}) AND k.digital=0 AND k.extra=0""", codes).fetchone()["n"]
    physical = c.execute("SELECT COALESCE(SUM(qty),0) q FROM collection").fetchone()["q"]
    value = c.execute("""
        SELECT COALESCE(SUM(o.qty * CASE
                 WHEN o.foil <> 'normal' THEN COALESCE(k.eur_foil, k.eur, 0)
                 ELSE COALESCE(k.eur, k.eur_foil, 0) END), 0) v
        FROM collection o JOIN cards k
          ON k.set_code=o.set_code AND k.number=o.number""").fetchone()["v"]
    rar = {}
    for r in c.execute(
        f"""SELECT k.rarity r,
                   COUNT(*) t,
                   SUM(CASE WHEN o.number IS NOT NULL THEN 1 ELSE 0 END) o
            FROM cards k JOIN sets s ON s.code=k.set_code
            LEFT JOIN (SELECT DISTINCT set_code,number FROM collection) o
                   ON o.set_code=k.set_code AND o.number=k.number
            WHERE k.set_code IN ({marks}) AND k.digital=0
            GROUP BY k.rarity""", codes):
        rar[r["r"]] = {"total": r["t"], "owned": r["o"] or 0}
    rar_names = {}
    for r in c.execute(
        f"""SELECT k.rarity r,
                   COUNT(DISTINCT k.name) t,
                   COUNT(DISTINCT CASE WHEN o.name IS NOT NULL THEN k.name END) o
            FROM cards k
            LEFT JOIN (SELECT DISTINCT name FROM collection) o ON o.name=k.name
            WHERE k.set_code IN ({marks}) AND k.digital=0 AND k.extra=0
            GROUP BY k.rarity""", codes):
        rar_names[r["r"]] = {"total": r["t"], "owned": r["o"] or 0}
    defer = c.execute(
        f"""SELECT COUNT(*) n, COALESCE(SUM(mp),0) v FROM (
              SELECT k.name, MIN(k.eur) mp
              FROM cards k
              WHERE k.set_code IN ({marks}) AND k.digital=0 AND k.extra=0
                AND k.eur IS NOT NULL
                AND k.name NOT IN (SELECT DISTINCT name FROM collection)
              GROUP BY k.name HAVING MIN(k.eur) >= ?)""",
        codes + (endgame_prefs(c)["eur"],)).fetchone()
    open_sets = [s for s in counted if s["missing"] > 0]
    return {
        "names": {"owned": names_owned, "total": names_total},
        "printings": {"owned": owned_pr, "total": printings},
        "physical": physical, "value": round(value, 2),
        "remaining": round(sum(s["totalCost"] for s in open_sets), 2),
        "endgameCount": defer["n"],
        "endgameValue": round(defer["v"], 2),
        "setsExcluded": len(sets) - len(counted),
        "shipping": round(sum(s["shipping"] for s in open_sets), 2),
        "rarity": rar,
        "rarityNames": rar_names,
        "setsComplete": sum(1 for s in counted if s["missing"] == 0),
        "setsTotal": len(counted),
        "nearest": sorted([s for s in open_sets if s["pct"] < 1],
                          key=lambda s: -s["pct"])[:6],
        "cheapest": sorted(open_sets, key=lambda s: s["totalCost"])[:6],
        "cardsUpdated": meta_get(c, "cards_updated", ""),
        "collectionUpdated": meta_get(c, "collection_updated", ""),
    }


def set_detail(c, code):
    s = c.execute("SELECT * FROM sets WHERE code=?", (code,)).fetchone()
    if not s:
        return None
    tracked, ship_country = tracked_shipping_only(c), shipping_country(c)
    gp = goal_prefs(c)
    eg_eur = endgame_prefs(c)["eur"]
    ps = s["printed_size"] or 0
    q = """SELECT k.number, k.num_int, k.name, k.name_de, k.type_line, k.rarity, k.eur,
                  k.eur_foil, k.img, k.mana, k.artist, k.colors, k.variant, k.finishes, k.ver,
                  k.extras_idx, k.cm_suffix, k.cm_ver, k.extra,
                  COALESCE(o.qty,0) qty
           FROM cards k
           LEFT JOIN (SELECT set_code,number,SUM(qty) qty FROM collection
                      GROUP BY set_code,number) o
                  ON o.set_code=k.set_code AND o.number=k.number
           WHERE k.set_code=? AND k.digital=0
           ORDER BY k.num_int, k.number"""
    # The set list shows every distinct printing (collector number) — basic
    # lands have several arts of one name, older sets a nonfoil plus a
    # separately-numbered foil-only "star" of the same name. Which of those
    # printings actually have to be owned for the set to read 100% is decided
    # by the Set-goal settings (goal_prefs): "inGoal" marks the ones that do,
    # the rest stay visible (and buyable) but greyed and never "want".
    cards = []
    for r in c.execute(q, (code,)):
        eur = r["eur"] or 0
        have = r["qty"] > 0
        tl = r["type_line"] or ""
        cards.append({"number": r["number"], "name": r["name"], "nameDe": r["name_de"] or "",
                      "type": r["type_line"], "rarity": r["rarity"],
                      "basic": tl.startswith("Basic ") and "Land" in tl,
                      "extra": r["extra"], "inGoal": goal_eligible(r["extra"], r["variant"], gp),
                      "eur": round(eur, 2),
                      "foil": round(r["eur_foil"], 2) if r["eur_foil"] else 0,
                      "img": r["img"], "mana": r["mana"], "artist": r["artist"],
                      "variant": r["variant"] or "", "finishes": r["finishes"] or "",
                      "ver": r["ver"] or 1, "extras": r["extras_idx"] or 0, "cmSuffix": r["cm_suffix"] or "", "cmVer": r["cm_ver"] or 1,
                      "qty": r["qty"], "have": have, "note": "", "want": False})
    # Decide want / note per card. "names" scope: one owned printing of a name
    # settles it, and a still-missing name flags exactly one printing to buy —
    # eg[0], the lowest collector number (Cardmarket's V.1). Picking "the
    # cheapest printing" instead flip-flopped between V.1 and V.2 for basic
    # lands whose two arts are priced a cent apart. "printings" scope: every
    # in-goal printing stands on its own.
    by_name = {}
    for x in cards:
        by_name.setdefault(x["name"], []).append(x)
    for grp in by_name.values():
        eg = [x for x in grp if x["inGoal"]]     # already in collector-number order
        if not eg:
            continue
        rep = eg[0]
        name_have = any(x["have"] for x in grp)
        eg_have = any(x["have"] for x in eg)
        if gp["scope"] == "names":
            if name_have:
                if not eg_have:
                    rep["note"] = "BaseMissing"          # owned only as a non-goal print
            elif min(x["eur"] for x in eg) >= eg_eur:
                rep["note"] = "Endgame"
            else:
                rep["want"] = True
        else:
            for x in eg:
                if x["have"]:
                    continue
                if x["eur"] >= eg_eur:
                    x["note"] = "Endgame"
                else:
                    x["want"] = True
    prog = set_progress(
        [{"name": x["name"], "extra": x["extra"], "variant": x["variant"],
          "eur": x["eur"], "have": x["have"]} for x in cards], gp, eg_eur)
    # "Buy missing" groups: basic lands get their own bucket and are pulled out
    # of Commons — you rarely want every Wastes/Snow-Plains art on a want-list,
    # and when you do it is a deliberate separate pick.
    buckets = {}
    bucket_defs = [("c", lambda x: x["rarity"] == "c" and not x["basic"]),
                   ("u", lambda x: x["rarity"] == "u"),
                   ("r", lambda x: x["rarity"] == "r"),
                   ("m", lambda x: x["rarity"] == "m"),
                   ("s", lambda x: x["rarity"] == "s"),
                   ("b", lambda x: x["rarity"] == "b"),
                   ("land", lambda x: x["basic"])]
    for key, pred in bucket_defs:
        sel = [x for x in cards if x["want"] and pred(x)]
        if sel:
            val = sum(x["eur"] for x in sel)
            buckets[key] = {"count": len(sel),
                            "value": round(val, 2),
                            "shipping": shipping(len(sel), val, tracked, ship_country),
                            "total": round(val + shipping(len(sel), val, tracked, ship_country), 2)}
    allw = [x for x in cards if x["want"]]
    val = sum(x["eur"] for x in allw)
    return {"code": code, "name": s["name"],
            "released": s["released"], "icon": s["icon"],
            "cards": cards, "buckets": buckets, "goal": gp,
            "owned": prog["owned"], "total": prog["total"],
            "pct": prog["owned"] / prog["total"] if prog["total"] else 0,
            "onlyExtra": prog["onlyExtra"],
            "all": {"count": len(allw),
                    "value": round(val, 2), "shipping": shipping(len(allw), val, tracked, ship_country),
                    "total": round(val + shipping(len(allw), val, tracked, ship_country), 2)}}


def counted_codes(c):
    return [x["code"] for x in cached_sets(c) if x["counted"]]


def owned_set_codes(c):
    """Every non-digital set code, regardless of set-completion goal prefs.
    "Do I already own this card" (the Cardmarket helper) has to look
    everywhere you could have a physical copy — a Commander precon or
    Secret Lair card is just as owned as one from a counted expansion,
    even though it doesn't count toward set-completion totals."""
    return [r["code"] for r in c.execute("SELECT code FROM sets WHERE digital=0")]


# --------------------------------------------------- Cardmarket browser helper
# The user's own set names Cardmarket brands differently. Left = Cardmarket
# expansion name, right = the Scryfall/Binduno set name.
CM_HELPER_LABELS = {
    "en": {"exact": "in collection", "otherFinish": "other finish",
           "otherVersion": "other version", "otherSet": "other set", "missing": "missing",
           "on": "on", "off": "off",
           "addPurchase": "Add this purchase to Binduno", "adding": "Adding…",
           "addFailed": "Failed — is Binduno running?", "added": "Added",
           "notMatched": "not matched"},
    "de": {"exact": "in Sammlung", "otherFinish": "anderes Finish",
           "otherVersion": "andere Version", "otherSet": "anderes Set", "missing": "fehlt",
           "on": "an", "off": "aus",
           "addPurchase": "Kauf zu Binduno hinzufügen", "adding": "Wird hinzugefügt…",
           "addFailed": "Fehlgeschlagen — läuft Binduno?", "added": "Hinzugefügt",
           "notMatched": "nicht erkannt"},
}


# keyed by lowercased Cardmarket expansion name AND by its URL slug — the live
# page often hands us only the slug (Bootstrap clears the title attribute).
CM_SET_ALIAS = {
    "sixth edition": "Classic Sixth Edition", "sixth-edition": "Classic Sixth Edition",
    "fourth edition": "Fourth Edition", "fourth-edition": "Fourth Edition",
    "fifth edition": "Fifth Edition", "fifth-edition": "Fifth Edition",
    "revised edition": "Revised Edition", "revised-edition": "Revised Edition",
    "limited edition alpha": "Limited Edition Alpha", "limited edition beta": "Limited Edition Beta",
}

# Cardmarket slug -> Scryfall/Binduno set code, for the handful the heuristics
# below can't reach (checked against the full cardmarket.com/Magic/Expansions
# list). Slug is lowercased, ": Extras" already stripped.
CM_SLUG_TO_CODE = {
    "commander": "cmd",                             # the original 2011 product
    "commander-ikoria": "c20",
    "retro-frame-artifacts": "brr",
    "universes-beyond-warhammer-40000": "40k",
    "mystery-booster-playtest-cards": "cmb1",
    "ultimate-box-toppers": "puma",
    "introductory-twoplayer-set": "itp",
    "foreign-black-bordered": "fbb",
    "marvel-source-material-cards": "msh",
    "war-of-the-spark-mythic-edition": "med",
    "ravnica-allegiance-mythic-edition": "med",
    "guilds-of-ravnica-mythic-edition": "med",
}


def _cm_slug(s):
    s = re.sub(r"['’ʼ`]", "", (s or "").lower())   # Baldur's -> baldurs, not baldur-s
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _cm_unspecial(t):
    """Undo cmName()'s Cardmarket-name transforms to get back the set name."""
    if t.startswith("Commander: "):
        return t[len("Commander: "):] + " Commander"
    if t.startswith("Universes Beyond: "):
        return t[len("Universes Beyond: "):]
    if t.endswith(": Eternal"):
        return t[:-len(": Eternal")] + " Eternal"
    if t.endswith(": Source Material Cards"):
        return t[:-len(": Source Material Cards")] + " Source Material"
    if t.endswith(": Mystical Archive"):
        return t[:-len(": Mystical Archive")] + " Mystical Archive"
    if t == "Marvel Source Material Cards":
        return "Marvel Universe"
    return t


_STOP = {"the", "of", "a", "and", "to"}


def _toks(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower())) - _STOP


def resolve_cm_set(c, slug, title):
    """(Cardmarket expansion slug, title) -> (set_code|None, is_extras).
    Cardmarket's title attribute is often gone by the time the userscript
    reads a row (Bootstrap moves it), so the slug alone has to carry it."""
    title = (title or "").strip()
    slug = (slug or "").strip().lower()
    is_extras = title.endswith(": Extras") or slug.endswith("-extras")
    ptitle = re.sub(r":?\s*Extras$", "", title).strip()
    pslug = re.sub(r"-extras$", "", slug)
    pslug = re.sub(r"^magic-the-gathering-", "", pslug)        # UB/crossover sets carry this
    # "Core 2019" -> "Core Set 2019"
    ptitle = re.sub(r"^Core (\d{4})$", r"Core Set \1", ptitle)
    pslug = re.sub(r"^core-(\d{4})$", r"core-set-\1", pslug)
    # Cardmarket splits Secret Lair into hundreds of drops; Scryfall keeps one set
    if pslug.startswith("secret-lair"):
        return ("slu" if "ultimate" in pslug else "slc" if "countdown" in pslug else "sld"), is_extras
    if pslug in CM_SLUG_TO_CODE:
        return CM_SLUG_TO_CODE[pslug], is_extras
    rows = c.execute("SELECT code, name FROM sets WHERE digital=0").fetchall()
    byname = {r["name"].lower(): r["code"] for r in rows}
    byslug = {_cm_slug(r["name"]): r["code"] for r in rows}

    for key in (_cm_unspecial(ptitle).lower(), ptitle.lower(),
                CM_SET_ALIAS.get(ptitle.lower(), "").lower(),
                CM_SET_ALIAS.get(pslug, "").lower()):
        if key and key in byname:
            return byname[key], is_extras
    for cand in (pslug, re.sub(r"^universes-beyond-", "", pslug),
                 _cm_slug(CM_SET_ALIAS.get(pslug, ""))):
        if cand and cand in byslug:
            return byslug[cand], is_extras

    # token match: every word of the set name must occur in the Cardmarket
    # name. Among those, take the most specific — preferring a name that also
    # says "commander" when the Cardmarket product is a Commander deck (its
    # wording differs, e.g. "Commander: Streets of New Capenna" vs Scryfall's
    # "New Capenna Commander").
    qt = _toks(ptitle) or _toks(pslug.replace("-", " "))
    if qt:
        want_cmdr = "commander" in qt
        # set name is contained in the Cardmarket name (pick most specific)
        sub = sorted(
            ((("commander" in _toks(nm)) == want_cmdr, len(_toks(nm)), code)
             for nm, code in byname.items()
             if _toks(nm) and _toks(nm).issubset(qt)),
            reverse=True)
        if sub and (len(sub) == 1 or sub[0][:2] != sub[1][:2]):
            return sub[0][2], is_extras
        # Cardmarket name is a shortening of the set name (pick shortest)
        sup = sorted(
            (len(_toks(nm)), code) for nm, code in byname.items()
            if _toks(nm) and qt.issubset(_toks(nm)) and ("commander" in _toks(nm)) == want_cmdr)
        if sup and (len(sup) == 1 or sup[0][0] != sup[1][0]):
            return sup[0][1], is_extras
    return None, is_extras


def _owns_name(c, codes, marks, name):
    """Total copies of a card name across the given sets, front-face-tolerant."""
    if not codes:
        return 0
    front = name.split(" // ")[0]
    return c.execute(
        f"""SELECT COALESCE(SUM(o.qty),0) q FROM cards k JOIN collection o
              ON o.set_code=k.set_code AND o.number=k.number
            WHERE k.digital=0 AND k.set_code IN ({marks})
              AND (k.name=? COLLATE NOCASE OR k.name_de=? COLLATE NOCASE
                   OR k.name LIKE ? COLLATE NOCASE)""",
        codes + [name, name, front + " // %"]).fetchone()["q"]


def cm_match(c, items):
    """For each Cardmarket offer row decide whether it is already owned.
    status: exact | otherFinish | otherVersion | otherSet | missing
          | unknownSet | unknownCard"""
    counted = owned_set_codes(c)
    marks = ",".join("?" * len(counted)) or "''"
    out = []
    for it in items:
        r = {"i": it.get("i")}
        name = (it.get("name") or "").strip()
        name = re.sub(r"\s+/\s+", " // ", name)                 # DFC: "A / B" -> "A // B"
        name = re.sub(r"\s*\([WUBRGCA]+ [\d*]+/[\d*]+[^)]*\)", "", name)  # token P/T annotation
        m = re.match(r"^(.*?)\s*\(V\.(\d+)\)\s*$", name)
        ver = int(it["version"]) if it.get("version") else (int(m.group(2)) if m else None)
        if m:
            name = m.group(1).strip()
        want_foil = bool(it.get("foil"))
        code, is_extras = resolve_cm_set(c, it.get("setSlug", ""), it.get("setTitle", ""))
        if not name:
            r["status"] = "unknownCard"; out.append(r); continue
        if not code:
            # set not mapped (promos, sets Binduno doesn't track) — still answer
            # the "do I own this card at all" question by name
            r["qty"] = _owns_name(c, counted, marks, name)
            r["status"] = "otherSet" if r["qty"] else "missing"
            out.append(r); continue
        r["set"] = code
        front = name.split(" // ")[0]
        # total copies of this card name across all counted sets (for display)
        r["qty"] = _owns_name(c, counted, marks, name)
        rows = c.execute(
            """SELECT k.number, k.cm_ver, k.cm_suffix,
                      COALESCE(SUM(CASE WHEN o.foil='normal' THEN o.qty ELSE 0 END),0) nf,
                      COALESCE(SUM(CASE WHEN o.foil<>'normal' THEN o.qty ELSE 0 END),0) fo
               FROM cards k
               LEFT JOIN collection o ON o.set_code=k.set_code AND o.number=k.number
               WHERE k.set_code=? AND k.digital=0
                 AND (k.name=? COLLATE NOCASE OR k.name_de=? COLLATE NOCASE
                      OR k.name LIKE ? COLLATE NOCASE)
               GROUP BY k.number""", (code, name, name, front + " // %")).fetchall()
        here_qty = sum(x["nf"] + x["fo"] for x in rows)
        owned_elsewhere = r["qty"] > here_qty
        if not rows:
            r["status"] = "otherSet" if r["qty"] else "missing"
            out.append(r); continue

        def hit(row):
            ex = (row["cm_suffix"] or "") == ": Extras"
            if ex != is_extras:
                return False
            return ver is None or (row["cm_ver"] or 1) == ver
        cand = [x for x in rows if hit(x)] or rows
        own_nf = any(x["nf"] for x in cand)
        own_fo = any(x["fo"] for x in cand)
        r["exactQty"] = sum((x["fo"] if want_foil else x["nf"]) for x in cand)
        if (own_fo if want_foil else own_nf):
            r["status"] = "exact"
        elif own_nf or own_fo:
            r["status"] = "otherFinish"
        elif any(x["nf"] or x["fo"] for x in rows):
            r["status"] = "otherVersion"
        elif owned_elsewhere:
            r["status"] = "otherSet"
        else:
            r["status"] = "missing"
        out.append(r)
    return {"results": out}


def card_search(c, p):
    """Filtered, paginated card browser."""
    where = ["k.digital=0"]
    args = []
    if p.get("allsets") != "1":
        # Secret Lair is set_type "box" and so doesn't count toward completion,
        # but its cards are individually tracked and people do look them up — the
        # browser stays useful only if they show up here without "all sets".
        cc = sorted(set(counted_codes(c)) | secret_lair_codes(c))
        if cc:
            where.append("k.set_code IN (%s)" % ",".join("?" * len(cc))); args += cc
    if p.get("baseonly") == "1":
        where.append("k.extra=0")
    if p.get("noprice") == "1":
        where.append("k.eur IS NULL AND k.eur_foil IS NULL")
    price_min = float(p["minprice"]) if p.get("minprice") else None
    price_max = float(p["maxprice"]) if p.get("maxprice") else None
    if p.get("q"):
        where.append("k.name LIKE ?"); args.append(f"%{p['q']}%")
    if p.get("text"):
        where.append("k.oracle LIKE ?"); args.append(f"%{p['text']}%")
    if p.get("artist"):
        where.append("k.artist LIKE ?"); args.append(f"%{p['artist']}%")
    if p.get("type"):
        where.append("k.type_line LIKE ?"); args.append(f"%{p['type']}%")
    if p.get("rarity"):
        rs = [x for x in p["rarity"].split(",") if x]
        if rs:
            where.append("k.rarity IN (%s)" % ",".join("?" * len(rs))); args += rs
    if p.get("sets"):
        ss = [x for x in p["sets"].split(",") if x]
        if ss:
            where.append("k.set_code IN (%s)" % ",".join("?" * len(ss))); args += ss
    if p.get("colors"):
        cols = [x.upper() for x in p["colors"] if x.upper() in "WUBRGC"]
        mode = p.get("colormode", "atleast")
        if "C" in cols:
            where.append("k.colors=''")
        elif cols:
            if mode == "only":
                for ch in "WUBRG":
                    if ch not in cols:
                        where.append(f"k.colors NOT LIKE '%{ch}%'")
                where.append("k.colors<>''")
            elif mode == "exact":
                where.append("length(k.colors)=?"); args.append(len(cols))
                for ch in cols:
                    where.append(f"k.colors LIKE '%{ch}%'")
            else:
                for ch in cols:
                    where.append(f"k.colors LIKE '%{ch}%'")
    own = p.get("owned", "all")
    join = ("LEFT JOIN (SELECT set_code,number,SUM(qty) qty FROM collection "
            "GROUP BY set_code,number) o ON o.set_code=k.set_code AND o.number=k.number")
    if own == "owned":
        where.append("o.qty IS NOT NULL")
    elif own == "missing":
        where.append("o.qty IS NULL")
    elif own == "newname":
        where.append("k.name NOT IN (SELECT DISTINCT name FROM collection)")
    uniq = p.get("unique") == "1"
    # collapsed by name the price test has to hit the cheapest printing, so it
    # belongs in HAVING; otherwise it is a plain row filter
    having, hargs = [], []
    if price_min is not None:
        if uniq:
            having.append("MIN(k.eur) >= ?"); hargs.append(price_min)
        else:
            where.append("COALESCE(k.eur,0) >= ?"); args.append(price_min)
    if price_max is not None:
        if uniq:
            having.append("MIN(k.eur) <= ?"); hargs.append(price_max)
        else:
            where.append("COALESCE(k.eur,0) <= ?"); args.append(price_max)
    w = " AND ".join(where)
    grp = ("GROUP BY k.name" + (" HAVING " + " AND ".join(having) if having else "")) if uniq else ""
    args = args + hargs

    # when collapsing by name, MIN() makes SQLite return the row of the cheapest
    # printing for the bare columns, so set/number/price stay consistent
    eur_expr = "MIN(k.eur)" if uniq else "k.eur"
    sortmap = {"name": "k.name", "released": "s.released", "number": "k.num_int",
               "price": ("MIN(k.eur)" if uniq else "COALESCE(k.eur,0)"),
               "rarity": "k.rarity",
               "qty": "COALESCE(o.qty,0)", "cmc": "k.cmc", "set": "s.name"}
    sort = sortmap.get(p.get("sort", "released"), "s.released")
    d = "DESC" if p.get("dir") == "-1" else "ASC"
    per = max(1, min(120, int(p.get("per", 60))))
    page = max(1, int(p.get("page", 1)))
    base = f"FROM cards k JOIN sets s ON s.code=k.set_code {join} WHERE {w}"
    if uniq:
        total = c.execute(f"SELECT COUNT(*) n FROM (SELECT 1 {base} {grp})",
                          args).fetchone()["n"]
    else:
        total = c.execute(f"SELECT COUNT(*) n {base}", args).fetchone()["n"]
    rows = c.execute(
        f"""SELECT k.set_code, s.name set_name, s.released, k.number, k.name, k.name_de, k.type_line,
                   k.rarity, {eur_expr} eur, k.eur_foil, k.img, k.mana, k.artist, k.colors,
                   k.variant, k.finishes, k.ver, k.extras_idx, k.cm_suffix, k.cm_ver, COALESCE(o.qty,0) qty {base} {grp}
            ORDER BY {sort} {d}, k.name LIMIT ? OFFSET ?""",
        args + [per, (page - 1) * per]).fetchall()
    return {"total": total, "page": page, "per": per,
            "cards": [{"set": r["set_code"], "setName": r["set_name"],
                       "released": r["released"], "number": r["number"],
                       "name": r["name"], "nameDe": r["name_de"] or "",
                       "type": r["type_line"], "rarity": r["rarity"],
                       "eur": round(r["eur"] or 0, 2), "foil": round(r["eur_foil"] or 0, 2),
                       "img": r["img"], "mana": r["mana"], "artist": r["artist"],
                       "colors": r["colors"], "variant": r["variant"] or "",
                       "finishes": r["finishes"] or "", "ver": r["ver"] or 1, "extras": r["extras_idx"] or 0, "cmSuffix": r["cm_suffix"] or "", "cmVer": r["cm_ver"] or 1,
                       "qty": r["qty"]} for r in rows]}


def card_detail(c, code, number):
    r = c.execute("""SELECT k.*, s.name set_name, s.released, s.icon
                     FROM cards k JOIN sets s ON s.code=k.set_code
                     WHERE k.set_code=? AND k.number=?""", (code, number)).fetchone()
    if not r:
        return None
    qty = c.execute("SELECT COALESCE(SUM(qty),0) q FROM collection WHERE set_code=? AND number=?",
                    (code, number)).fetchone()["q"]
    qty_normal = qty_foil = 0
    for x in c.execute("""SELECT foil, COALESCE(SUM(qty),0) q FROM collection
                          WHERE set_code=? AND number=? GROUP BY foil""", (code, number)):
        if x["foil"] == "foil":
            qty_foil += x["q"]
        else:
            qty_normal += x["q"]
    prints = []
    for x in c.execute("""SELECT k.set_code, k.number, k.name_de, k.eur, k.eur_foil, s.name set_name,
                                 s.released, COALESCE(o.qty,0) qty
                          FROM cards k JOIN sets s ON s.code=k.set_code
                          LEFT JOIN (SELECT set_code,number,SUM(qty) qty FROM collection
                                     GROUP BY set_code,number) o
                                 ON o.set_code=k.set_code AND o.number=k.number
                          WHERE k.name=? AND k.digital=0
                          ORDER BY s.released DESC""", (r["name"],)):
        prints.append({"set": x["set_code"], "setName": x["set_name"], "number": x["number"],
                       "nameDe": x["name_de"] or "",
                       "released": x["released"], "eur": round(x["eur"] or 0, 2),
                       "foil": round(x["eur_foil"] or 0, 2), "qty": x["qty"]})
    legal = {}
    code_map = {"l": "legal", "n": "not_legal", "r": "restricted", "b": "banned"}
    for i, f in enumerate(FORMATS):
        ch = (r["legal"] or "")[i:i + 1]
        legal[FMT_LABEL[f]] = code_map.get(ch, "not_legal")
    in_watchlist = bool(c.execute("SELECT 1 FROM watchlist WHERE set_code=? AND number=?",
                                  (code, number)).fetchone())
    return {"set": r["set_code"], "setName": r["set_name"], "setIcon": r["icon"],
            "inWatchlist": in_watchlist,
            "released": r["released"], "number": r["number"], "name": r["name"],
            "nameDe": r["name_de"] or "", "typeDe": r["type_de"] or "", "oracleDe": r["oracle_de"] or "",
            "type": r["type_line"], "rarity": r["rarity"], "mana": r["mana"],
            "cmc": r["cmc"], "oracle": r["oracle"], "artist": r["artist"],
            "pt": r["pt"], "img": r["img"], "colors": r["colors"],
            "eur": round(r["eur"] or 0, 2), "foil": round(r["eur_foil"] or 0, 2),
            "variant": r["variant"] or "", "finishes": r["finishes"] or "",
            "ver": r["ver"] or 1, "extras": r["extras_idx"] or 0, "cmSuffix": r["cm_suffix"] or "", "cmVer": r["cm_ver"] or 1, "cardmarket": r["cm_uri"], "scryfall": r["scry_uri"],
            "qty": qty, "qtyNormal": qty_normal, "qtyFoil": qty_foil,
            "legal": legal, "printings": prints}


def missing_names(c, p):
    """One row per card name you own nowhere, priced at its cheapest printing."""
    cc = counted_codes(c)
    if not cc:
        return {"total": 0, "cards": [], "page": 1, "per": 150}
    marks = ",".join("?" * len(cc))
    args = list(cc)
    where = [f"k.set_code IN ({marks})", "k.digital=0", "k.extra=0",
             "k.eur IS NOT NULL",
             "k.name NOT IN (SELECT DISTINCT name FROM collection)"]
    if p.get("q"):
        where.append("k.name LIKE ?"); args.append(f"%{p['q']}%")
    if p.get("set"):
        where.append("k.set_code=?"); args.append(p["set"])
    if p.get("rarity"):
        rarities = [r for r in p["rarity"].split(",") if r]
        if rarities:
            where.append(f"k.rarity IN ({','.join('?' * len(rarities))})")
            args.extend(rarities)
    having, hargs = [], []
    if p.get("maxprice"):
        having.append("MIN(k.eur) <= ?"); hargs.append(float(p["maxprice"]))
    if p.get("minprice"):
        having.append("MIN(k.eur) >= ?"); hargs.append(float(p["minprice"]))
    if p.get("hideendgame") == "1":
        having.append("MIN(k.eur) < ?"); hargs.append(endgame_prefs(c)["eur"])
    w = " AND ".join(where)
    h = (" HAVING " + " AND ".join(having)) if having else ""
    base = (f"FROM cards k JOIN sets s ON s.code=k.set_code WHERE {w} "
            f"GROUP BY k.name{h}")
    args = args + hargs
    total = c.execute(f"SELECT COUNT(*) n, COALESCE(SUM(mp),0) v FROM "
                      f"(SELECT MIN(k.eur) mp {base})", args).fetchone()
    sortmap = {"name": "k.name", "price": "MIN(k.eur)", "rarity": "k.rarity",
               "released": "s.released", "set": "s.name"}
    sort = sortmap.get(p.get("sort", "price"), "MIN(k.eur)")
    d = "DESC" if p.get("dir") == "-1" else "ASC"
    per = max(1, min(300, int(p.get("per", 150))))
    page = max(1, int(p.get("page", 1)))
    rows = c.execute(
        f"""SELECT k.name, MIN(k.name_de) name_de, k.set_code, s.name set_name, k.number, k.rarity,
                   MIN(k.eur) eur, k.img, k.type_line, k.variant, k.ver, k.extras_idx, k.cm_suffix, k.cm_ver {base}
            ORDER BY {sort} {d}, k.name LIMIT ? OFFSET ?""",
        args + [per, (page - 1) * per]).fetchall()
    return {"total": total["n"], "value": round(total["v"], 2), "page": page, "per": per,
            "cards": [{"name": r["name"], "nameDe": r["name_de"] or "",
                       "set": r["set_code"], "setName": r["set_name"],
                       "number": r["number"], "rarity": r["rarity"],
                       "eur": round(r["eur"], 2), "img": r["img"],
                       "type": r["type_line"], "variant": r["variant"] or "",
                       "ver": r["ver"] or 1, "extras": r["extras_idx"] or 0, "cmSuffix": r["cm_suffix"] or "", "cmVer": r["cm_ver"] or 1} for r in rows]}


def secret_lair_codes(c):
    """Set codes whose name is a Secret Lair drop. Generating a working
    Cardmarket want list for these isn't solved yet (CM splits Secret Lair
    into hundreds of separate expansions), so they're kept out of the cart."""
    return {r["code"] for r in c.execute(
        "SELECT code FROM sets WHERE lower(name) LIKE 'secret lair%'")}


def cart_rows(c):
    rows = c.execute("""SELECT t.set_code, t.number, t.qty, k.name, k.name_de, k.eur, k.eur_foil,
                               k.img, k.rarity, k.variant, k.ver, k.extras_idx, k.cm_suffix, k.cm_ver, s.name set_name
                        FROM cart t
                        JOIN cards k ON k.set_code=t.set_code AND k.number=t.number
                        JOIN sets s ON s.code=t.set_code
                        ORDER BY s.name, k.num_int""").fetchall()
    items = [{"set": r["set_code"], "setName": r["set_name"], "number": r["number"],
              "name": r["name"], "nameDe": r["name_de"] or "", "qty": r["qty"], "rarity": r["rarity"],
              "eur": round(r["eur"] or 0, 2), "foil": round(r["eur_foil"] or 0, 2),
              "img": r["img"], "variant": r["variant"] or "",
              "ver": r["ver"] or 1, "extras": r["extras_idx"] or 0, "cmSuffix": r["cm_suffix"] or "", "cmVer": r["cm_ver"] or 1} for r in rows]
    goods = sum(i["eur"] * i["qty"] for i in items)
    n = sum(i["qty"] for i in items)
    ship = shipping(n, goods, tracked_shipping_only(c), shipping_country(c))
    by_set = {}
    for i in items:
        by_set.setdefault(i["setName"], []).append(i)
    return {"items": items, "count": n, "goods": round(goods, 2),
            "shipping": ship, "total": round(goods + ship, 2),
            "sets": len(by_set)}


def export_collection(c):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Name", "Set code", "Set name", "Collector number", "Foil", "Rarity",
                "Quantity", "Language", "Purchase price", "Purchase price currency"])
    for r in c.execute("""SELECT o.name, o.set_code, s.name set_name, o.number, o.foil,
                                 k.rarity, o.qty, o.lang, k.eur, k.eur_foil
                          FROM collection o
                          LEFT JOIN cards k ON k.set_code=o.set_code AND k.number=o.number
                          LEFT JOIN sets s ON s.code=o.set_code
                          ORDER BY o.set_code, o.number"""):
        price = (r["eur_foil"] if (r["foil"] or "normal") != "normal" and r["eur_foil"]
                 else r["eur"]) or ""
        w.writerow([r["name"], (r["set_code"] or "").upper(), r["set_name"] or "",
                    r["number"], r["foil"] or "normal",
                    {"c": "common", "u": "uncommon", "r": "rare", "m": "mythic"}.get(
                        r["rarity"], r["rarity"] or ""),
                    r["qty"], r["lang"] or "en", price, "EUR" if price else ""])
    return out.getvalue()


# --------------------------------------------------------------------- server
CACHE = {"stamp": None, "sets": None, "home": None}
SETS_CACHE_FILE = os.path.join(BASE, "sets_cache.pkl")


def _sets_stamp(c):
    """Everything the set/home aggregates depend on besides raw card and
    collection rows. Cheap to compute (a handful of meta reads + the tiny
    set_pref table); when it is unchanged the expensive recompute — and the
    on-disk copy from the last run — can be reused."""
    return (
        meta_get(c, "collection_updated", ""),
        meta_get(c, "cards_updated", ""),
        datetime.now().strftime("%Y-%m-%d"),
        SCHEMA,
        json.dumps(goal_prefs(c), sort_keys=True),
        json.dumps(endgame_prefs(c), sort_keys=True),
        shipping_country(c), tracked_shipping_only(c),
        tuple(sorted(
            (r["code"], r["mode"] or "", r["sealed_note"] or "", r["sealed_price"] or 0)
            for r in c.execute("SELECT code,mode,sealed_note,sealed_price FROM set_pref"))),
    )


def _cache_save():
    try:
        import pickle
        tmp = SETS_CACHE_FILE + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump({k: CACHE[k] for k in ("stamp", "sets", "home")}, f,
                        pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, SETS_CACHE_FILE)
    except Exception:                               # noqa: BLE001
        pass


def _cache_load(stamp):
    try:
        import pickle
        with open(SETS_CACHE_FILE, "rb") as f:
            d = pickle.load(f)
        return d if isinstance(d, dict) and d.get("stamp") == stamp else None
    except Exception:                               # noqa: BLE001
        return None


def cached_sets(c):
    stamp = _sets_stamp(c)
    if CACHE["sets"] is not None and CACHE["stamp"] == stamp:
        return CACHE["sets"]
    disk = _cache_load(stamp)
    if disk and disk.get("sets") is not None:
        CACHE.update(stamp=stamp, sets=disk["sets"], home=disk.get("home"))
        return CACHE["sets"]
    CACHE.update(stamp=stamp, sets=set_rows(c), home=None)
    _cache_save()
    return CACHE["sets"]


def cached_home(c):
    """home_stats() runs ~7 GROUP BY / DISTINCT queries over the whole cards
    table — seconds on a slow (Windows + AV) box. Cache it on the same stamp as
    the set list, in memory and on disk, so a launch with unchanged data is
    instant."""
    stamp = _sets_stamp(c)
    if CACHE["home"] is not None and CACHE["stamp"] == stamp:
        return CACHE["home"]
    sets = cached_sets(c)                            # also brings CACHE["stamp"] to `stamp`
    CACHE["home"] = home_stats(c, sets)
    _cache_save()
    return CACHE["home"]


def bust():
    CACHE.update(stamp=None, sets=None, home=None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200, cors=False):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cors:                                  # only the Cardmarket-helper endpoints
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _keepalive(self):
        """Any incoming request means the UI is still there — cancel a pending
        self-shutdown that a reload's beforeunload beacon may have armed."""
        global QUIT_TIMER
        if QUIT_TIMER is not None:
            QUIT_TIMER.cancel()
            QUIT_TIMER = None

    def _dispatch(self, fn):
        self._keepalive()
        try:
            fn()
        except (ConnectionError, TimeoutError):
            pass                                 # browser hung up mid-request — normal
        except Exception as e:                   # noqa: BLE001
            import traceback
            traceback.print_exc()
            try:
                self.send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:                    # noqa: BLE001
                pass

    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

    def _get(self):
        p = self.path.split("?")[0]
        qs = {}
        if "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    qs[k] = urllib.parse.unquote_plus(v)
        c = db()
        if p == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/api/stats":
            has_cards = bool(c.execute("SELECT 1 FROM cards LIMIT 1").fetchone())
            has_collection = bool(c.execute("SELECT 1 FROM collection LIMIT 1").fetchone())
            onboarding_flag = meta_get(c, "onboarding_done")
            # Never explicitly set (upgrade from before the wizard existed) —
            # infer from existing data instead of forcing the wizard onto an
            # installation that's obviously already set up.
            onboarding_done = (has_cards and has_collection) if onboarding_flag is None \
                else onboarding_flag == "1"
            self.send_json({"version": VERSION,
                            "dbPath": DB,
                            "homeDir": os.path.expanduser("~"),
                            "stats": cached_home(c),
                            "hasCards": has_cards,
                            "hasCollection": has_collection,
                            "trackedShipping": tracked_shipping_only(c),
                            "shippingCountry": shipping_country(c),
                            "shipRates": {code: {"name": n, "untracked": u, "tracked": t}
                                          for code, (n, u, t) in SHIP_RATES.items()},
                            "autoSync": auto_sync_enabled(c),
                            "priceLogging": price_logging_enabled(c),
                            "goal": goal_prefs(c),
                            "endgame": {"on": meta_get(c, "endgame_on") == "1",
                                        "eur": float(meta_get(c, "endgame_eur") or ENDGAME_EUR)},
                            "cmHelper": {"on": meta_get(c, "cm_helper_on", "1") == "1",
                                         "lastSeen": meta_get(c, "cm_helper_last_seen", "")},
                            "showCosts": meta_get(c, "show_costs") == "1",
                            "githubRepo": github_repo(c),
                            "update": UPDATE,
                            "autoUpdateCheck": meta_get(c, "auto_update_check", "1") == "1",
                            "autoUpdateInstall": meta_get(c, "auto_update_install") == "1",
                            "lan": {"url": _lan_url(), "port": PORT},
                            "onboardingDone": onboarding_done})
        elif p in ("/cm-helper.user.js", "/cm-helper.bookmarklet.js"):
            src = (CM_BOOKMARKLET if p.endswith("bookmarklet.js") else CM_USERSCRIPT)
            body = src.replace("__PORT__", str(PORT)).replace("__VERSION__", VERSION).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/api/cm-helper-pref":
            lang = meta_get(c, "ui_lang") or "en"
            meta_set(c, "cm_helper_last_seen",
                     datetime.now().isoformat(timespec="seconds"))
            self.send_json({"on": meta_get(c, "cm_helper_on", "1") == "1",
                            "lang": lang,
                            "labels": CM_HELPER_LABELS.get(lang, CM_HELPER_LABELS["en"])},
                           cors=True)
        elif p == "/api/lan-qr.png":
            lu = _lan_url()
            if not lu:
                self.send_json({"error": "no LAN address"}, 404)
                return
            try:
                img = _qr_png(lu)
            except Exception as e:                            # noqa: BLE001
                self.send_json({"error": str(e)}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(img)))
            self.end_headers()
            self.wfile.write(img)
        elif p == "/api/sets":
            self.send_json(cached_sets(c))
        elif p.startswith("/api/set/"):
            d = set_detail(c, p.rsplit("/", 1)[-1])
            self.send_json(d or {"error": "unknown set"}, 200 if d else 404)
        elif p == "/api/cards":
            self.send_json(card_search(c, qs))
        elif p.startswith("/api/card/"):
            parts = p.split("/")
            d = card_detail(c, parts[3], urllib.parse.unquote(parts[4])) if len(parts) > 4 else None
            self.send_json(d or {"error": "unknown card"}, 200 if d else 404)
        elif p == "/api/missing":
            self.send_json(missing_names(c, qs))
        elif p == "/api/cart":
            self.send_json(cart_rows(c))
        elif p == "/api/watchlist":
            self.send_json({"items": watchlist_rows(c), "max": WATCHLIST_MAX})
        elif p == "/api/export":
            body = export_collection(c).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="mtg_collection_export.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/api/history":
            self.send_json([dict(r) for r in c.execute(
                "SELECT ts,action,detail FROM history ORDER BY id DESC LIMIT 100")])
        elif p == "/api/refresh-status":
            self.send_json(REFRESH)
        elif p == "/api/github-latest":
            repo = github_repo(c)
            if not repo:
                self.send_json({"error": "no GitHub repo configured"}, 400)
                return
            try:
                self.send_json(github_latest(repo))
            except Exception as e:                            # noqa: BLE001
                self.send_json({"error": str(e)}, 502)
        else:
            self.send_json({"error": "not found"}, 404)

    def _post(self):
        c = db()
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        if self.path == "/api/cm-match":
            meta_set(c, "cm_helper_last_seen",
                     datetime.now().isoformat(timespec="seconds"))
            self.send_json(cm_match(c, json.loads(raw).get("items", [])), cors=True)
        elif self.path == "/api/cm-helper-pref":
            d = json.loads(raw)
            meta_set(c, "cm_helper_on", "1" if d.get("on") else "0")
            self.send_json({"ok": True, "on": meta_get(c, "cm_helper_on", "1") == "1"}, cors=True)
        elif self.path == "/api/cm-purchase-import":
            try:
                payload = json.loads(raw)
                res = import_cm_purchase(c, payload.get("items", []), payload.get("mode", "add"))
                bust()
                cached_home(c)     # rebuild sets + home now, on the connection that committed
                self.send_json({"ok": True, **res}, cors=True)
            except Exception as e:                            # noqa: BLE001
                self.send_json({"ok": False, "error": str(e)}, 400, cors=True)
        elif self.path == "/api/import":
            try:
                payload = json.loads(raw)
                res = import_collection(c, payload["csv"], payload.get("mode", "replace"),
                                        payload.get("format", "auto"))
                bust()
                cached_home(c)     # rebuild sets + home now, on the connection that committed
                self.send_json({"ok": True, **res})
            except Exception as e:                            # noqa: BLE001
                self.send_json({"ok": False, "error": str(e)}, 400)
        elif self.path == "/api/refresh-cards":
            if REFRESH["running"]:
                self.send_json({"ok": False, "error": "already running"}, 409)
                return
            threading.Thread(target=lambda: (refresh_cards(), bust()), daemon=True).start()
            self.send_json({"ok": True})
        elif self.path == "/api/backfill-price-history":
            if REFRESH["running"]:
                self.send_json({"ok": False, "error": "already running"}, 409)
                return
            threading.Thread(target=backfill_price_history, daemon=True).start()
            self.send_json({"ok": True})
        elif self.path == "/api/set-pref":
            try:
                d = json.loads(raw)
                code, mode = d["code"], d.get("mode", "")
                if mode in ("include", "exclude"):
                    c.execute("INSERT INTO set_pref(code,mode) VALUES(?,?) "
                              "ON CONFLICT(code) DO UPDATE SET mode=excluded.mode", (code, mode))
                    log(c, "Sets", f"{code.upper()} set to {mode}")
                else:
                    c.execute("DELETE FROM set_pref WHERE code=?", (code,))
                    log(c, "Sets", f"{code.upper()} reset to default")
                c.commit(); bust()
                self.send_json({"ok": True})
            except Exception as e:                            # noqa: BLE001
                self.send_json({"ok": False, "error": str(e)}, 400)
        elif self.path == "/api/set-pref-bulk":
            d = json.loads(raw)
            for code in d.get("codes", []):
                m = d.get("mode", "")
                if m in ("include", "exclude"):
                    c.execute("INSERT INTO set_pref(code,mode) VALUES(?,?) "
                              "ON CONFLICT(code) DO UPDATE SET mode=excluded.mode", (code, m))
                else:
                    c.execute("DELETE FROM set_pref WHERE code=?", (code,))
            c.commit(); bust()
            log(c, "Sets", f"{len(d.get('codes', []))} sets set to {d.get('mode') or 'default'}")
            self.send_json({"ok": True})
        elif self.path == "/api/update-app":
            try:
                src = json.loads(raw).get("script", "")
                self.send_json(apply_new_source(c, src, "Updated from file"))
            except Exception as e:                            # noqa: BLE001
                self.send_json({"ok": False, "error": str(e)}, 400)
        elif self.path == "/api/github-repo":
            repo = (json.loads(raw or "{}").get("repo") or "").strip()
            meta_set(c, "github_repo", repo)
            self.send_json({"ok": True, "githubRepo": github_repo(c)})
        elif self.path == "/api/update-prefs":
            d = json.loads(raw or "{}")
            if "check" in d:
                meta_set(c, "auto_update_check", "1" if d["check"] else "0")
            if "install" in d:
                meta_set(c, "auto_update_install", "1" if d["install"] else "0")
            self.send_json({"ok": True,
                            "autoUpdateCheck": meta_get(c, "auto_update_check", "1") == "1",
                            "autoUpdateInstall": meta_get(c, "auto_update_install") == "1"})
        elif self.path == "/api/update-from-github":
            try:
                repo = github_repo(c)
                if not repo:
                    raise ValueError("No GitHub repo configured.")
                url = json.loads(raw or "{}").get("srcUrl") or github_latest(repo)["srcUrl"]
                if "githubusercontent.com" not in url and "github.com" not in url:
                    raise ValueError("Refusing to fetch source from a non-GitHub URL.")
                self.send_json(apply_new_source(c, _http_text(url), "Updated from GitHub"))
            except Exception as e:                            # noqa: BLE001
                self.send_json({"ok": False, "error": str(e)}, 400)
        elif self.path == "/api/quit":
            explicit = False
            try:
                explicit = bool(json.loads(raw or "{}").get("explicit"))
            except Exception:                                # noqa: BLE001
                pass
            self.send_json({"ok": True})
            if explicit:
                # The user pressed the Quit button and confirmed — really stop,
                # and take the menu-bar / tray icon down with us.
                def _die():
                    if TRAY_ICON is not None:
                        try:
                            TRAY_ICON.stop()
                        except Exception:                    # noqa: BLE001
                            pass
                    os._exit(0)
                t = threading.Timer(0.25, _die)
                t.daemon = True
                t.start()
                return
            # An implicit quit — the beforeunload beacon on a tab close / reload.
            # With a tray icon running, keep serving; you reopen from the tray.
            if TRAY_ACTIVE:
                return
            global QUIT_TIMER
            if QUIT_TIMER is not None:
                QUIT_TIMER.cancel()
            QUIT_TIMER = threading.Timer(QUIT_GRACE, lambda: os._exit(0))
            QUIT_TIMER.daemon = True
            QUIT_TIMER.start()
        elif self.path == "/api/cart":
            d = json.loads(raw)
            act = d.get("action")
            sl = secret_lair_codes(c) if act in ("add", "addmany") else set()
            sl_skipped = 0
            if act == "add":
                if d["set"] in sl:
                    sl_skipped = 1
                else:
                    c.execute("""INSERT INTO cart(set_code,number,qty,added) VALUES(?,?,?,?)
                                 ON CONFLICT(set_code,number)
                                 DO UPDATE SET qty=qty+excluded.qty""",
                              (d["set"], d["number"], int(d.get("qty", 1)),
                               datetime.now().isoformat(timespec="seconds")))
            elif act == "set":
                q = int(d.get("qty", 0))
                if q <= 0:
                    c.execute("DELETE FROM cart WHERE set_code=? AND number=?",
                              (d["set"], d["number"]))
                else:
                    c.execute("UPDATE cart SET qty=? WHERE set_code=? AND number=?",
                              (q, d["set"], d["number"]))
            elif act == "addmany":
                now = datetime.now().isoformat(timespec="seconds")
                for it in d.get("items", []):
                    if it["set"] in sl:
                        sl_skipped += 1
                        continue
                    c.execute("""INSERT INTO cart(set_code,number,qty,added) VALUES(?,?,1,?)
                                 ON CONFLICT(set_code,number) DO NOTHING""",
                              (it["set"], it["number"], now))
            elif act == "clear":
                c.execute("DELETE FROM cart")
            elif act == "toCollection":
                items = cart_rows(c)["items"]
                for it in items:
                    row = c.execute("""SELECT qty FROM collection
                                       WHERE set_code=? AND number=? AND lang='en' AND foil='normal'""",
                                    (it["set"], it["number"])).fetchone()
                    cur = row["qty"] if row else 0
                    c.execute("""INSERT INTO collection(set_code,number,name,qty,lang,foil)
                                 VALUES(?,?,?,?,'en','normal')
                                 ON CONFLICT(set_code,number,lang,foil)
                                 DO UPDATE SET qty=excluded.qty""",
                              (it["set"], it["number"], it["name"], cur + it["qty"]))
                if items:
                    log(c, "Collection", f"Added {len(items)} cart line(s) to collection")
                    bust()
            c.commit()
            res = cart_rows(c)
            if sl_skipped:
                res["secretLairSkipped"] = sl_skipped
            self.send_json(res)
        elif self.path == "/api/shipping-pref":
            d = json.loads(raw)
            if "trackedOnly" in d:
                meta_set(c, "tracked_shipping_only", "1" if d.get("trackedOnly") else "0")
            if d.get("country") in SHIP_RATES:
                meta_set(c, "shipping_country", d["country"])
            bust()
            self.send_json({"ok": True, "trackedShipping": tracked_shipping_only(c),
                            "shippingCountry": shipping_country(c)})
        elif self.path == "/api/auto-sync-pref":
            d = json.loads(raw)
            meta_set(c, "auto_sync", "1" if d.get("enabled") else "0")
            self.send_json({"ok": True, "autoSync": auto_sync_enabled(c)})
        elif self.path == "/api/price-logging-pref":
            d = json.loads(raw)
            meta_set(c, "price_logging", "1" if d.get("enabled") else "0")
            self.send_json({"ok": True, "priceLogging": price_logging_enabled(c)})
        elif self.path == "/api/ui-lang":
            # the UI language is a per-device localStorage setting; mirror it
            # here too so the Cardmarket userscript can label in the same language
            lang = (json.loads(raw).get("lang") or "en")
            meta_set(c, "ui_lang", lang if lang in CM_HELPER_LABELS else "en")
            self.send_json({"ok": True})
        elif self.path == "/api/costs-pref":
            d = json.loads(raw)
            meta_set(c, "show_costs", "1" if d.get("show") else "0")
            self.send_json({"ok": True, "showCosts": meta_get(c, "show_costs") == "1"})
        elif self.path == "/api/goal-pref":
            d = json.loads(raw)
            for k in GOAL_DEFAULTS:
                if d.get(k) is not None:
                    meta_set(c, "goal_" + k, str(d[k]))
            bust()                                   # cached_sets totals depend on this
            self.send_json({"ok": True, "goal": goal_prefs(c)})
        elif self.path == "/api/endgame-pref":
            d = json.loads(raw)
            if "on" in d:
                meta_set(c, "endgame_on", "1" if d["on"] else "0")
            if d.get("eur") is not None:
                try:
                    meta_set(c, "endgame_eur", str(int(float(d["eur"]))))
                except ValueError:
                    pass
            bust()
            self.send_json({"ok": True, "endgame": {
                "on": meta_get(c, "endgame_on") == "1",
                "eur": float(meta_get(c, "endgame_eur") or ENDGAME_EUR)}})
        elif self.path == "/api/onboarding":
            d = json.loads(raw)
            meta_set(c, "onboarding_done", "1" if d.get("done") else "0")
            self.send_json({"ok": True, "onboardingDone": meta_get(c, "onboarding_done") == "1"})
        elif self.path == "/api/watchlist":
            d = json.loads(raw)
            act = d.get("action")
            if act == "add":
                cnt = c.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
                exists = c.execute("SELECT 1 FROM watchlist WHERE set_code=? AND number=?",
                                   (d["set"], d["number"])).fetchone()
                if not exists and cnt >= WATCHLIST_MAX:
                    self.send_json({"ok": False,
                                    "error": f"Watchlist is full ({WATCHLIST_MAX} cards max)."}, 400)
                    return
                c.execute("""INSERT INTO watchlist(set_code, number, added) VALUES(?,?,?)
                             ON CONFLICT(set_code, number) DO NOTHING""",
                          (d["set"], d["number"], datetime.now().isoformat(timespec="seconds")))
                c.commit()
            elif act == "remove":
                c.execute("DELETE FROM watchlist WHERE set_code=? AND number=?",
                          (d["set"], d["number"]))
                c.commit()
            self.send_json({"ok": True, "items": watchlist_rows(c), "max": WATCHLIST_MAX})
        elif self.path == "/api/sealed":
            d = json.loads(raw)
            c.execute("""INSERT INTO set_pref(code,sealed_note,sealed_price) VALUES(?,?,?)
                         ON CONFLICT(code) DO UPDATE SET sealed_note=excluded.sealed_note,
                                                         sealed_price=excluded.sealed_price""",
                      (d["code"], d.get("note", ""), float(d.get("price") or 0)))
            c.commit(); bust()
            self.send_json({"ok": True})
        elif self.path == "/api/collection-adjust":
            d = json.loads(raw)
            set_code, number = d["set"], d["number"]
            foil = "foil" if d.get("foil") else "normal"
            name = d.get("name", "")
            row = c.execute("""SELECT qty FROM collection
                               WHERE set_code=? AND number=? AND lang='en' AND foil=?""",
                            (set_code, number, foil)).fetchone()
            cur = row["qty"] if row else 0
            if d.get("action") == "set":
                new_qty = max(0, int(d.get("qty", 0)))
            else:
                new_qty = max(0, cur + int(d.get("delta", 0)))
            if new_qty != cur:
                if new_qty == 0:
                    c.execute("""DELETE FROM collection
                                WHERE set_code=? AND number=? AND lang='en' AND foil=?""",
                             (set_code, number, foil))
                else:
                    c.execute("""INSERT INTO collection(set_code,number,name,qty,lang,foil)
                                 VALUES(?,?,?,?,'en',?)
                                 ON CONFLICT(set_code,number,lang,foil)
                                 DO UPDATE SET qty=excluded.qty""",
                              (set_code, number, name, new_qty, foil))
                c.commit(); bust()
                log(c, "Collection",
                    f'{name} ({"Foil" if foil == "foil" else "Nonfoil"}) '
                    f'{set_code.upper()} #{number} -> {new_qty}x')
            self.send_json(card_detail(c, set_code, number))
        elif self.path == "/api/reset":
            c.execute("DELETE FROM collection")
            log(c, "Collection", "Collection cleared")
            c.commit()
            bust()
            cached_home(c)     # rebuild sets + home now, on the connection that committed
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, 404)


PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Binduno</title>
<!--FAVICON-->
<style>
:root{
 --bg:#0f1319;--panel:#171d26;--panel2:#131920;--line:#28313d;--text:#e8ebef;
 --muted:#8d98a7;--dim:#5f6a78;--track:#3b4756;
 --w:#e8dcb5;--u:#4a90c4;--b:#8b7fa8;--r:#c8503c;--g:#4f9d69;
 --gold:#d4a629;--mythic:#e0692c;--ok:#4f9d69;--good-bg:#183024;
 --bad:#d98a8a;--bad-bg:#33191b;
 --kind-normal-bd:#33506b;--kind-normal-fg:#8fb6d8;
 --kind-special-bd:#5b4a72;--kind-special-fg:#b49ed0;--kind-sealed-bd:#6b5324;
 --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
 --sans:"Avenir Next",Avenir,"Segoe UI",system-ui,sans-serif;
 --mono:"SF Mono",Menlo,Consolas,monospace;
 --nav-bg:rgba(15,19,25,.94)}
:root[data-theme="light"]{
 --bg:#f5f3ee;--panel:#ffffff;--panel2:#f0ede6;--line:#d9d3c5;--text:#211d16;
 --muted:#6b6558;--dim:#948d7d;--track:#cdc6b4;
 --w:#8a7a3a;--u:#2b6ea8;--b:#6b5a92;--r:#a83f2e;--g:#3d7a4f;
 --gold:#93690f;--mythic:#b8460f;--ok:#3d7a4f;--good-bg:#e3efe4;
 --bad:#a83f2e;--bad-bg:#f7e2df;
 --kind-normal-bd:#a9c3db;--kind-normal-fg:#2b6ea8;
 --kind-special-bd:#cdb9e0;--kind-special-fg:#6b4a92;--kind-sealed-bd:#e0c98a;
 --nav-bg:rgba(245,243,238,.92)}
/* Colorblind-safe: dark base unchanged, but "good/legal" vs "bad/banned" moves
   from green-vs-red (hard to tell apart with deuteranopia/protanopia) to the
   blue-vs-orange pairing that stays distinguishable across all common types. */
:root[data-theme="colorblind"]{
 --ok:#4a90c4;--good-bg:#16283a;--bad:#e0a458;--bad-bg:#3a2712}
*{box-sizing:border-box}
/* always reserve the scrollbar gutter so switching between a short and a tall
   page doesn't nudge the whole centred layout sideways */
html{scrollbar-gutter:stable}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:15px;line-height:1.5}
a{color:inherit}
/* nav follows the active theme like the rest of the page, via --nav-bg
   (a themed, slightly transparent tint for the blur) plus the same
   --text/--muted/--gold/--line tokens everything else uses. */
nav{position:sticky;top:0;z-index:30;background:var(--nav-bg);backdrop-filter:blur(9px);
    border-bottom:1px solid var(--line);color:var(--text)}
.navin{max-width:1240px;margin:0 auto;padding:0 22px;display:flex;align-items:center;gap:28px;height:60px}
.brand{font-family:var(--mono);font-size:19px;color:var(--text);
  display:inline-flex;align-items:center;gap:9px}
.brandicon{width:30px;height:30px;display:block}
.bi-l{display:none}
:root[data-theme="light"] .bi-d{display:none}
:root[data-theme="light"] .bi-l{display:block}
nav a.tab{color:var(--muted);text-decoration:none;font-size:14px;padding:19px 2px;
  border-bottom:2px solid transparent;cursor:pointer}
nav a.tab:hover{color:var(--text)}
nav a.tab.on{color:var(--gold);border-color:var(--gold)}
.wrap{max-width:1240px;margin:0 auto;padding:26px 22px 80px}
footer{max-width:1240px;margin:0 auto;padding:18px 22px 26px;color:var(--dim);
  font-size:12px;line-height:1.6;border-top:1px solid var(--line)}
h1{font-family:var(--serif);font-weight:400;font-size:30px;margin:0 0 4px}
h2{font-family:var(--serif);font-weight:400;font-size:20px;margin:34px 0 12px}
.sub{color:var(--muted);font-size:14px;margin:0 0 20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:16px}
.card .k{font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
  min-height:13px;line-height:13px}
.card .v{font-family:var(--serif);font-size:28px;margin-top:6px}
.card .n{font-family:var(--mono);font-size:11.5px;color:var(--dim);margin-top:3px}
.donuts{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin-top:12px}
.donut{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:18px;
  display:flex;gap:16px;align-items:center}
.donut svg{flex:0 0 96px}
.donut .t{font-size:11px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted)}
.donut .p{font-family:var(--serif);font-size:26px;margin:3px 0}
.donut .s{font-family:var(--mono);font-size:11.5px;color:var(--dim)}
.rarbars{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:16px}
.rarrow{display:grid;grid-template-columns:92px 1fr 128px;gap:12px;align-items:center;margin:9px 0}
.rarrow .lb{font-size:13px;color:var(--muted)}
.rarrow .track{height:9px;background:var(--track);border-radius:5px;overflow:hidden}
.rarrow .fill{height:100%;border-radius:5px}
.rarrow .nm{font-family:var(--mono);font-size:11.5px;color:var(--dim);text-align:right}
.list{background:var(--panel);border:1px solid var(--line);border-radius:6px;overflow:hidden}
.li{display:flex;align-items:center;gap:11px;padding:11px 15px;border-bottom:1px solid #1e2530;cursor:pointer}
.li:last-child{border-bottom:0}
.li:hover{background:var(--panel2)}
.seticon{filter:invert(80%) sepia(10%) saturate(250%);vertical-align:-3px}
.li img{width:19px;height:19px}
.li .nm{flex:1;font-size:14px}
.li .mt{font-family:var(--mono);font-size:12px;color:var(--muted)}
.bar{flex:0 0 92px;height:6px;background:var(--track);border-radius:4px;overflow:hidden}
.bar span{display:block;height:100%;background:var(--gold)}
/* inside a set card (.set is a flex column) flex-basis controls height, so the
   shared .bar would be 92px tall — pin it back to a thin full-width bar */
.set .bar{flex:0 0 6px;width:100%}
.tools{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin:14px 0}
input,select,button{font-family:var(--sans);font-size:13.5px}
input[type=search],select{background:var(--panel2);border:1px solid var(--line);color:var(--text);
  padding:8px 10px;border-radius:4px}
input[type=search]{flex:1 1 220px;min-width:160px}
button{background:var(--panel);border:1px solid var(--line);color:var(--text);padding:8px 13px;
  border-radius:4px;cursor:pointer}
button:hover{border-color:var(--gold);color:var(--gold)}
button.pri{background:var(--gold);border-color:var(--gold);color:#181206;font-weight:600}
button.pri:hover{filter:brightness(1.1);color:#181206}
#cardWatch.on{border-color:var(--gold);color:var(--gold)}
button:disabled{opacity:.45;cursor:not-allowed}
button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:4px;overflow:hidden}
.seg button{border:0;border-radius:0;padding:8px 12px}
.seg button.on{background:var(--gold);color:#181206}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:12px}
.set{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:15px;
  display:flex;flex-direction:column;gap:9px}
.set.done{border-color:var(--ok);background:linear-gradient(180deg,var(--good-bg),var(--panel))}
.set .hd{display:flex;gap:10px;align-items:flex-start;min-height:56px}
.set .kindrow{display:flex;gap:6px;align-items:center;flex-wrap:wrap;min-height:24px}
.set .spacer{flex:1}
.set img{width:26px;height:26px;flex:0 0 26px}
.set .nm{font-family:var(--serif);font-size:16px;line-height:1.25;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden;min-height:2.5em}
.set .cd{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:2px}
.kind{display:inline-block;font-size:10.5px;letter-spacing:.06em;padding:2px 7px;border-radius:3px;
  border:1px solid var(--line);color:var(--muted)}
.kind.normal{border-color:var(--kind-normal-bd);color:var(--kind-normal-fg)}
.kind.special{border-color:var(--kind-special-bd);color:var(--kind-special-fg)}
.kind.sealed{border-color:var(--kind-sealed-bd);color:var(--gold)}
.set .st{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;
  font-family:var(--mono);font-size:11.5px;color:var(--muted)}
.set .st span{white-space:nowrap}
.set .acts{display:flex;gap:7px;margin-top:2px}
.set .acts button{flex:1 1 auto;padding:7px 8px;font-size:12.5px;white-space:nowrap}
.tscroll{max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
/* a horizontal-scroll wrapper is its own scroll container, so sticky headers
   inside it stick to the wrapper (60px down, over the first row) instead of
   the page — pin them normally instead */
.tscroll th{position:static;top:auto}
table{width:100%;border-collapse:collapse;font-size:13.5px;font-family:var(--sans)}
table td,table td span,table td a{font-family:var(--sans);font-size:13.5px;font-weight:400}
table td.num,table td.num span{font-family:var(--mono);font-size:12.5px}
th{position:sticky;top:60px;background:var(--panel);text-align:left;font-weight:500;font-size:10.5px;
  letter-spacing:.1em;text-transform:uppercase;color:var(--muted);padding:10px 9px;
  border-bottom:1px solid var(--line);white-space:nowrap;z-index:4}
table th,table thead th,table th.num{font-family:var(--sans);font-size:10.5px;
  letter-spacing:.1em;font-weight:500}
td{padding:8px 9px;border-bottom:1px solid #1c222b}
td:last-child{white-space:nowrap}
td.nowrap,th.nowrap{white-space:nowrap}
td button{padding:4px 9px;font-size:12px}
tr.done td{background:var(--good-bg)}
.num{text-align:right;white-space:nowrap}
td.num,table td.num{text-align:right;font-family:var(--mono);font-size:12.5px;white-space:nowrap}
th.num{text-align:right}
.pager{display:flex;gap:7px;align-items:center;justify-content:center;margin:18px 0}
.pager span{font-family:var(--mono);font-size:12px;color:var(--muted)}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:10px;
  width:96vw;max-width:1680px;height:94vh;max-height:94vh;padding:0}
dialog:not([open]){display:none}
dialog[open]{display:flex;flex-direction:column}
dialog::backdrop{background:rgba(6,9,13,.8)}
.dh{padding:18px 26px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;
  justify-content:space-between;flex-wrap:wrap;flex:0 0 auto}
.dh h3{font-family:var(--serif);font-weight:400;font-size:21px;margin:0}
.dbody{flex:1;overflow:auto;padding:0 26px 26px}
.dbody table{margin-top:0}
.dbody th{top:0;cursor:pointer;user-select:none}
.dbody th:hover{color:var(--gold)}
.dbody th .ar{opacity:.45;font-size:9px;margin-left:3px}
.set.off,tr.off td{opacity:.5}
.set.off{border-style:dashed}
.badge{font-size:10px;letter-spacing:.06em;padding:2px 6px;border-radius:3px;
  border:1px solid #6b3a3a;color:#d98a8a;white-space:nowrap}
.opt{display:grid;grid-template-columns:minmax(120px,1fr) 74px 88px 88px 100px 168px;gap:10px;align-items:center;
  padding:10px 12px;border:1px solid var(--line);border-radius:5px;margin-bottom:7px;background:var(--panel2)}
.opt .lb{font-size:14px}
.opt .n{font-family:var(--mono);font-size:12px;color:var(--muted);text-align:right}
.have td{color:var(--dim)}
tr.notgoal td{opacity:.5}
tr.notgoal td .badge{opacity:1}
.note{color:var(--r);font-size:12px}
textarea{width:100%;height:130px;background:var(--panel2);color:var(--text);border:1px solid var(--line);
  font-family:var(--mono);font-size:12px;padding:9px;border-radius:4px;margin-top:10px}
.drop{border:1px dashed var(--line);border-radius:6px;padding:16px;background:var(--panel2)}
.drop.ok{border-style:solid;border-color:var(--gold)}
.radio{display:flex;gap:16px;margin:10px 0}
.radio label{display:flex;gap:7px;align-items:flex-start;cursor:pointer;font-size:14px;
  border:1px solid var(--line);border-radius:5px;padding:11px 13px;flex:1;background:var(--panel2)}
.radio label.on{border-color:var(--gold)}
.radio .d{display:block;color:var(--muted);font-size:12.5px;margin-top:2px}
.prog{height:8px;background:#1d242e;border-radius:5px;overflow:hidden;margin:10px 0}
.prog span{display:block;height:100%;background:var(--gold);transition:width .3s}
#busy{position:fixed;inset:0;z-index:500;display:none;align-items:center;justify-content:center;
  background:rgba(8,10,14,.72);backdrop-filter:blur(2px)}
#busy .busybox{background:var(--panel,#12161d);border:1px solid var(--line,#232a34);
  border-radius:12px;padding:22px 26px;width:min(420px,86vw);box-shadow:0 20px 60px rgba(0,0,0,.55)}
#busy .busymsg{font-size:14px;margin-bottom:12px;color:var(--text)}
#busy .prog{margin:0}
#busy .prog span{transition:width .4s ease}
.hist{font-family:var(--mono);font-size:12.5px}
.hist .row{display:flex;gap:14px;padding:9px 0;border-bottom:1px solid #1c222b}
.hist .ts{color:var(--gold);flex:0 0 200px}
.hist .ac{color:var(--muted);flex:0 0 90px}
.empty{text-align:center;padding:60px 20px;color:var(--muted)}
.empty h2{margin-top:0}
.msg{padding:11px 14px;border-radius:5px;margin:12px 0;font-size:13.5px}
.msg.ok{background:#152a1e;border:1px solid #2c5a3e;color:#8fd6a8}
.msg.err{background:#2a1616;border:1px solid #5c2c2c;color:#e0a0a0}
.cgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}
.cc{background:var(--panel);border:1px solid var(--line);border-radius:7px;overflow:hidden;
  display:flex;flex-direction:column;cursor:pointer;transition:border-color .13s,transform .13s}
.cc:hover{border-color:var(--gold);transform:translateY(-2px)}
.cc .imgwrap{aspect-ratio:488/680;background:#0c1016;position:relative}
.cc img.face{width:100%;height:100%;object-fit:cover;display:block}
.cc .noimg{display:flex;align-items:center;justify-content:center;height:100%;
  color:var(--dim);font-size:12px;text-align:center;padding:10px}
.cc .tilecart{position:absolute;bottom:7px;right:7px;background:rgba(15,19,25,.92);
  border:1px solid var(--line);color:var(--text);border-radius:5px;padding:2px 9px;
  font-size:14px;line-height:1.3;opacity:0;transition:opacity .13s}
.cc:hover .tilecart{opacity:1}
.cc .tilecart:hover{border-color:var(--gold);color:var(--gold)}
.cc .owned{position:absolute;top:7px;right:7px;background:rgba(15,19,25,.9);
  border:1px solid var(--ok);color:var(--ok);border-radius:11px;padding:1px 8px;
  font-family:var(--mono);font-size:11px}
.cc .miss{position:absolute;top:7px;right:7px;background:rgba(15,19,25,.9);
  border:1px solid var(--line);color:var(--muted);border-radius:11px;padding:1px 8px;
  font-family:var(--mono);font-size:11px}
.cc .meta{padding:8px 10px;display:flex;flex-direction:column;gap:3px}
.cc .cn{font-size:13px;line-height:1.25;overflow-wrap:anywhere}
.cc .cset{font-family:var(--mono);font-size:10.5px;color:var(--dim);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cc .vrow{display:flex;flex-wrap:wrap;gap:3px}
.cc .vrow .varlbl{margin-left:0}
.cc .cp{font-family:var(--mono);font-size:11.5px;color:var(--gold)}
.cc .cp em{color:var(--muted);font-style:normal}
.filters{display:grid;grid-template-columns:250px 1fr;gap:20px;align-items:start}
.fbox{background:var(--panel);border:1px solid var(--line);border-radius:7px;padding:14px;
  position:sticky;top:76px;max-height:calc(100vh - 96px);overflow:auto}
.fbox label{display:block;font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--muted);margin:13px 0 5px}
.fbox label:first-child{margin-top:0}
.fbox input,.fbox select{width:100%}
.pips{display:flex;gap:4px;flex-wrap:nowrap;justify-content:space-between}
.pip{width:30px;height:30px;flex:0 0 30px;border-radius:50%;cursor:pointer;display:flex;
  align-items:center;justify-content:center;user-select:none;position:relative;
  border:2px solid transparent;opacity:.42;transition:opacity .13s,border-color .13s;
  font-weight:700;font-size:14px;color:#2b2417}
.pip img{width:22px;height:22px;display:block;pointer-events:none}
.pip:hover{opacity:.75}
.pip.on{opacity:1;border-color:var(--gold)}
.pip[data-c=W]{background:#f4ecd4}.pip[data-c=U]{background:#9fd2ef}
.pip[data-c=B]{background:#b6aeb0}.pip[data-c=R]{background:#f0a08a}
.pip[data-c=G]{background:#a2ceab}.pip[data-c=C]{background:#cdc7c1}
.rarrow2{display:flex;gap:6px;flex-wrap:wrap}
.rchip{padding:5px 11px;border:1px solid var(--line);border-radius:14px;font-size:12.5px;
  cursor:pointer;color:var(--muted);background:var(--panel2);user-select:none}
.rchip.on{border-color:var(--gold);color:var(--gold);background:#231d0d}
.prange{display:flex;gap:7px}
.prange input{width:100%}
.chk{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--text);
  text-transform:none;letter-spacing:0;margin:5px 0}
.chk input{width:auto}
.catfilter{position:relative}
.catfilter summary{background:var(--panel2);border:1px solid var(--line);color:var(--text);
  padding:8px 10px;border-radius:4px;cursor:pointer;font-size:13.5px;list-style:none;user-select:none}
.catfilter summary::-webkit-details-marker{display:none}
.catfilter summary:after{content:"▾";margin-left:6px;color:var(--muted)}
.catfilter[open] summary{border-color:var(--gold)}
.catpanel{position:absolute;top:calc(100% + 4px);left:0;z-index:20;background:var(--panel);
  border:1px solid var(--line);border-radius:6px;padding:10px 12px;min-width:230px;
  max-height:360px;overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.4)}
.catgroup{margin-bottom:10px}
.catgroup:last-child{margin-bottom:0}
.catgrouplbl{display:flex;align-items:center;gap:7px;font-size:13px;font-weight:600;
  color:var(--text);margin-bottom:4px;cursor:pointer}
.catlbl{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--muted);
  margin:3px 0 3px 20px;cursor:pointer}
.catlbl:hover,.catgrouplbl:hover{color:var(--gold)}
.catgroup input,.catlbl input{width:auto}
.cardpage{display:grid;grid-template-columns:340px 1fr;gap:28px;align-items:start}
.cardpage .art{width:100%;border-radius:14px;border:1px solid var(--line);display:block}
.cardpage h1{font-size:27px;margin:0}
.mana{font-family:var(--mono);color:var(--muted);font-size:15px;
  display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.cartrow .nmline{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
img.ms{width:16px;height:16px;vertical-align:-3px;margin:0 1px}
.rules{background:var(--panel2);border:1px solid var(--line);border-radius:6px;
  padding:14px 16px;white-space:pre-wrap;line-height:1.65;margin:14px 0}
.legal{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:6px;overflow:hidden}
.legal div{background:var(--panel);padding:8px 12px;display:flex;justify-content:space-between;
  align-items:center;font-size:13px}
.tag{font-family:var(--mono);font-size:10px;letter-spacing:.07em;padding:2px 7px;border-radius:3px}
.tag.l{background:var(--good-bg);color:var(--ok)}.tag.n{background:var(--panel2);color:var(--dim)}
.tag.b{background:var(--bad-bg);color:var(--bad)}.tag.r{background:var(--panel2);color:var(--gold)}
.buybtn{display:inline-flex;align-items:center;gap:9px;background:var(--gold);color:#181206;
  border:0;border-radius:5px;padding:11px 17px;font-weight:600;font-size:14px;
  text-decoration:none;margin:6px 8px 0 0}
.buybtn:hover{filter:brightness(1.08)}
.ver{font-family:var(--mono);font-size:11px;color:var(--dim);margin-left:auto}
#tipbox{position:fixed;z-index:60;display:none;max-width:330px;background:#0b0e13;
  border:1px solid var(--gold);border-radius:7px;padding:11px 13px;font-size:12.5px;
  line-height:1.55;color:var(--text);box-shadow:0 10px 34px rgba(0,0,0,.6);pointer-events:none}
#tipbox b{display:block;font-family:var(--serif);font-weight:400;font-size:14px;
  color:var(--gold);margin-bottom:5px}
[data-tip]{cursor:help;border-bottom:1px dotted var(--dim)}
#cardpop{position:fixed;z-index:70;display:none;pointer-events:none;line-height:0;
  border:1px solid var(--gold);border-radius:12px;overflow:hidden;background:#0b0e13;
  box-shadow:0 12px 40px rgba(0,0,0,.7)}
#cardpop img{display:block;width:360px;max-width:44vw;height:auto}
.cartn{display:none;background:var(--gold);color:#181206;border-radius:9px;
  padding:0 6px;font-family:var(--mono);font-size:10.5px;margin-left:3px}
.cartn.on{display:inline-block}
.crumbs{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-bottom:14px;font-size:13px}
.crumbs a{color:var(--muted);cursor:pointer;text-decoration:none;border-bottom:1px dotted var(--dim)}
.crumbs a:hover{color:var(--gold);border-color:var(--gold)}
.crumbs span.sep{color:var(--dim)}
.crumbs b{font-weight:400;color:var(--text)}
tr.child td:first-child,tr.child2 td:first-child{position:relative}
tr.child td:first-child{padding-left:34px}
tr.child2 td:first-child{padding-left:56px}
tr.child td:first-child::before,tr.child2 td:first-child::before{
  content:"\21B3";position:absolute;top:8px;color:var(--dim);font-family:var(--mono)}
tr.child td:first-child::before{left:14px}
tr.child2 td:first-child::before{left:36px}
.verlbl{font-family:var(--mono);font-size:10px;color:#8fc0e0;border:1px solid #2b4d66;
  border-radius:3px;padding:2px 5px;margin-left:5px;vertical-align:middle;white-space:nowrap;display:inline-block}
.varlbl{font-family:var(--mono);font-size:10px;letter-spacing:.04em;color:var(--mythic);
  border:1px solid #5a3520;border-radius:3px;padding:2px 5px;margin-left:5px;vertical-align:middle;
  white-space:nowrap;display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis}
.chunk{background:var(--panel);border:1px solid var(--line);border-radius:6px;
  padding:13px 15px;margin-bottom:9px}
.chunk h4{margin:0 0 7px;font-family:var(--serif);font-weight:400;font-size:16px}
.cartrow{display:grid;grid-template-columns:44px 1fr 120px 92px 108px 40px;gap:11px;
  align-items:center;padding:9px 14px;border-bottom:1px solid #1e2530}
.cartrow img{width:44px;border-radius:3px}
.qbtn{display:inline-flex;align-items:center;gap:5px}
.qbtn button{padding:3px 9px;font-size:13px;line-height:1.2}
.psep{border:0;border-top:1px solid var(--line);margin:30px 0}
#sub>div>h2:first-child{margin-top:0}
.helpnav{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:16px}
.helpnav button.on{background:var(--gold);color:#181206;border-color:var(--gold)}
.help h3{font-family:var(--serif);font-weight:400;font-size:18px;margin:22px 0 7px}
.help p,.help li{color:var(--muted);font-size:14px;line-height:1.65}
.help code{font-family:var(--mono);font-size:12.5px;color:var(--gold)}
.help table{margin:10px 0}
@media(max-width:900px){.filters{grid-template-columns:1fr}.fbox{position:static;max-height:none}
  .cardpage{grid-template-columns:1fr}}
.setlink,[data-card],[data-code],[data-view],[data-buy],[data-cart],[data-q]{cursor:pointer}
.setlink{color:var(--text);border-bottom:1px solid transparent}
.setlink:hover{color:var(--gold);border-color:var(--gold)}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
/* --- phone / narrow-viewport layout --- */
@media(max-width:680px){
  body{font-size:14px}
  /* one compact nav row: icon + tabs + a power button pinned right */
  .navin{gap:11px;padding:9px 12px;height:auto;flex-wrap:wrap;align-items:center}
  .brand{font-size:0}                 /* keep the icon, drop the wordmark */
  .brandicon{width:26px;height:26px}
  nav a.tab{font-size:13.5px;padding:6px 0;border-bottom-width:2px}
  #navCart{font-size:0}
  #navCart::after{content:"Cart";font-size:13.5px}
  #ver{display:none}
  #navQuit{margin-left:auto!important}
  .li{flex-wrap:wrap;padding:10px 13px}
  .li .nm{flex:1 1 100%}
  .li .bar{flex:1 1 60px}
  .li .mt{flex:0 0 auto!important}
  .wrap{padding:16px 13px 64px}
  footer{padding:14px 13px 22px}
  h1{font-size:24px}
  h2{font-size:18px;margin:24px 0 10px}
  .sub{margin-bottom:14px}
  th{top:54px}
  /* let wide tables scroll inside their own box instead of the whole page */
  #out,#setBody,#watchlistOut,.dbody{overflow-x:auto;-webkit-overflow-scrolling:touch}
  #out th,#setBody th,#watchlistOut th{position:static;top:auto}
  #out>table,#setBody>table{min-width:560px}
  /* watchlist: drop the sparkline column and let the rest wrap so it fits */
  #watchlistOut tr>*:nth-child(4){display:none}
  #watchlistOut .tscroll>table{min-width:0}
  #watchlistOut td,#watchlistOut th{padding:7px 6px}
  #watchlistOut td:nth-child(5),#watchlistOut td:nth-child(5) span{white-space:normal}
  .tscroll>table{min-width:480px}
  #view{overflow-x:hidden}
  table{font-size:13px}
  td,th{padding:7px 7px}
  .tools{gap:7px;margin:12px 0}
  input[type=search]{flex-basis:100%}
  .cards{grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:9px}
  .card{padding:13px}
  .card .v{font-size:23px}
  .donuts{gap:9px}
  .donut{padding:14px;gap:12px}
  .donut svg{flex:0 0 74px;width:74px;height:74px}
  .donut .p{font-size:22px}
  .grid{grid-template-columns:1fr;gap:9px}
  .set{padding:13px}
  .set .hd{min-height:0}
  .rarrow{grid-template-columns:66px 1fr 66px;gap:9px}
  .rarrow .nm{font-size:10.5px}
  .cartrow{grid-template-columns:38px 1fr auto;gap:8px 10px;padding:10px}
  .cartrow img{width:38px}
  .cartrow .qbtn,.cartrow>:nth-child(4),.cartrow>:nth-child(5){grid-column:2/-1}
  .opt{grid-template-columns:1fr 92px;gap:8px}
  .radio{flex-direction:column;gap:8px}
  .helpnav{gap:6px}
  .helpnav button,.seg button{padding:7px 10px}
  /* segmented tab strips wrap instead of squashing their labels */
  .seg{flex-wrap:wrap}
  .seg button{white-space:nowrap}
  /* the 5-way Settings tab strip: one scrollable row beats an ugly wrap */
  .segtabs{flex-wrap:nowrap;overflow-x:auto;max-width:100%;scrollbar-width:none}
  .segtabs::-webkit-scrollbar{display:none}
  .segtabs button{flex:0 0 auto}
  #cardpop img{width:280px;max-width:86vw}
  #tipbox{max-width:82vw}
  .psep{margin:22px 0}
  dialog{width:100vw;height:100vh;max-height:100vh;border-radius:0;border:0}
  .dh{padding:14px 15px}.dbody{padding:0 15px 18px}
}
</style></head><body>
<nav><div class="navin">
  <div class="brand" id="brand" style="cursor:pointer" title="Home"><!--BRANDICON-->binduno</div>
  <a class="tab" data-p="home" id="navHome">Home</a>
  <a class="tab" data-p="collection" id="navCollection">Collection</a>
  <a class="tab" data-p="cart"><span id="navCart">Wantlist-Cart</span> <span id="cartN" class="cartn"></span></a>
  <a class="tab" data-p="manage" id="navManage">Manage</a>
  <span class="ver" id="ver"></span>
  <button id="navQuit" aria-label="Quit" style="margin-left:12px;padding:6px 9px;
    display:inline-flex;align-items:center;justify-content:center">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
      <line x1="12" y1="3" x2="12" y2="12"></line>
      <path d="M7.1 7.1a7 7 0 1 0 9.8 0"></path></svg></button>
</div></nav>
<div class="wrap" id="view"></div>
<footer>This is an unofficial fan-made project and is not affiliated with, endorsed, sponsored,
or approved by Wizards of the Coast. Magic: The Gathering, all card names, images and
related assets are trademarks and/or copyrights of Wizards of the Coast LLC and Hasbro,
Inc. All prices are sourced from Scryfall and Cardmarket and shown for personal,
non-commercial reference only.
<div style="margin-top:8px">Made with &#10084;&#65039; in Odenwald</div></footer>
<div id="tipbox" role="tooltip"></div>
<div id="cardpop"><img alt=""></div>


<script>
"use strict";
let THEME="dark";
try{THEME=localStorage.getItem("mtgTheme")||"dark";}catch(e){}
document.documentElement.setAttribute("data-theme",THEME);
function setTheme(mode){
  THEME=mode;
  try{localStorage.setItem("mtgTheme",mode);}catch(e){}
  document.documentElement.setAttribute("data-theme",mode);
}
let LANG="en";
try{LANG=localStorage.getItem("mtgLang")||"en";}catch(e){}
function setLang(l){
  LANG=l;
  try{localStorage.setItem("mtgLang",l);}catch(e){}
  fetch("/api/ui-lang",{method:"POST",body:JSON.stringify({lang:l})}).catch(()=>{});
  paintNav();
  if(typeof route==="function")route();
}
function t(key,vars){
  let s=(T[LANG]&&T[LANG][key])||T.en[key]||key;
  if(vars)for(const k in vars)s=s.split("{"+k+"}").join(vars[k]);
  return s;
}
// Card/set name language — independent of LANG (the page/UI language) above.
// Only affects how a card's name is DISPLAYED; wantLine() must keep using
// c.name (the canonical English Oracle name) regardless, since that is what
// Cardmarket's want-list parser actually matches against.
let CARDLANG="en";
try{CARDLANG=localStorage.getItem("mtgCardLang")||"en";}catch(e){}
function setCardLang(l){
  CARDLANG=l;
  try{localStorage.setItem("mtgCardLang",l);}catch(e){}
  if(typeof route==="function")route();
}
const cardName=c=>(CARDLANG==="de"&&c.nameDe)?c.nameDe:(c.name||"");
const cardType=c=>(CARDLANG==="de"&&c.typeDe)?c.typeDe:(c.type||"");
const cardOracle=c=>(CARDLANG==="de"&&c.oracleDe)?c.oracleDe:(c.oracle||"");
// Translation dictionary. English is the fallback for any key missing from
// another language, so `en` must always be complete; other languages only
// need the keys that differ. Grown page by page — see git history for which
// pages are covered so far.
const T={
en:{
  "nav.home":"Home","nav.collection":"Collection","nav.missing":"Missing Names",
  "nav.cart":"Wantlist-Cart","nav.manage":"Settings","nav.quit":"Quit",
  "nav.quitTitle":"Stop the tracker","nav.homeTitle":"Home",
  "nav.confirmQuit":"Stop the tracker? The page stops working until you start it again.",
  "nav.stopped":"Tracker stopped","nav.stoppedDesc":"Open Binduno.app again to continue.",
  "home.title":"Collection Statistics",
  "home.updated":"Cards updated {cards} · collection updated {collection}",
  "home.never":"never","home.cardNames":"Card names","home.printings":"Printings",
  "home.setsCompleted":"Sets completed","home.ofCountedSets":"of {n} counted sets",
  "home.xOfY":"{a} of {b}","home.physicalCards":"Physical cards",
  "home.duplicatesIncluded":"duplicates included","home.collectionValue":"Collection value",
  "home.cardmarketTrend":"Cardmarket trend","home.remainingCost":"Remaining cost",
  "home.inclShipping":"incl. {n} shipping","home.cardsOver300":"Cards over 300 €","home.cardsOver":"Cards over {eur}",
  "home.showTheseCards":"Show these cards","home.leftOutOfRemaining":"{n} — left out of remaining cost",
  "home.namesStillMissing":"Names still missing","home.acrossCountedSets":"across all counted sets",
  "home.byRarity":"By rarity","home.closestToCompletion":"Closest to completion",
  "home.nothingOpen":"Nothing open.","home.cheapestToFinish":"Cheapest to finish",
  "home.cheapestDesc":"Sets you could close out for the least money, shipping included.",
  "home.shopByName":"Or shop by card name instead →","home.nothingLoaded":"Nothing loaded yet",
  "home.watchlist":"Watchlist","home.watchlistDesc":"Cards you're keeping an eye on, with "+
    "their Cardmarket price trend over the last 7 days. Add cards from any card page.",
  "home.watchlistEmpty":"No cards on the watchlist yet — open a card and click "+
    "\"Add to Watchlist\".",
  "home.watchlist7d":"Last 7 days","home.watchlistChange":"Change",
  "home.watchlistRemove":"Remove from watchlist",
  "home.watchlistCount":"{n} of {max} cards",
  "home.nothingLoadedDesc":"Download the card data and import your ManaBox export to get started.",
  "home.goToManage":"Go to Settings",
  "wizard.back":"Back","wizard.skipAll":"Skip setup","wizard.next":"Next","wizard.start":"Get started",
  "wizard.skipStep":"Skip this step",
  "wizard.finish":"Go to Binduno",
  "wizard.welcomeTitle":"Welcome to Binduno",
  "wizard.welcomeBody1":"Binduno is a local app for tracking your Magic: The Gathering "+
    "collection and generating Cardmarket want lists. Everything runs on this computer — "+
    "your collection data never leaves it, there's no account and no cloud.",
  "wizard.welcomeBody2":"This short setup picks a language, your country (for shipping cost "+
    "estimates), imports your collection if you already have a ManaBox export, and downloads "+
    "the Scryfall card database Binduno needs to know what exists. Every step can be skipped "+
    "and revisited later under Settings.",
  "wizard.langTitle":"Language",
  "wizard.countryTitle":"Country",
  "wizard.importTitle":"Import your collection",
  "wizard.importDesc":"If you already have a CSV export from ManaBox, Moxfield or "+
    "Archidekt, upload it now. Otherwise skip this — you can import any time under Settings.",
  "wizard.cardDataTitle":"Card database",
  "wizard.cardDataDesc":"Binduno needs Scryfall's card database (names, sets, Cardmarket "+
    "prices) to know what exists at all — around 100–150 MB, once. This can take a minute "+
    "or two depending on your connection.",
  "wizard.cardDataBtn":"Download card database",
  "wizard.cardDataConfirm":"Download the Scryfall card database now (~100–150 MB)? This runs "+
    "in the background and can take a minute or two.",
  "wizard.doneTitle":"All set",
  "wizard.doneBody":"Binduno is ready. You can change any of these settings again any time "+
    "under Settings, and re-run this setup from there too.",
  "rarity.c":"Common","rarity.u":"Uncommon","rarity.r":"Rare","rarity.m":"Mythic",
  "rarity.s":"Special","rarity.b":"Basic land","rarity.land":"Basic lands",
  "collection.title":"Collection","collection.viewSets":"View Sets","collection.viewCards":"View Cards",
  "collection.desc":"Every set with release date, progress and what it would cost to complete it. "+
    "“To finish” is the price of the cards you still need plus estimated shipping.",
  "collection.searchPlaceholder":"Search set name or code…",
  "collection.allCategories":"All categories","collection.noCategories":"No categories",
  "collection.nOfTotalCategories":"{n} of {total} categories",
  "collection.allSets":"All sets","collection.incompleteOnly":"Incomplete only",
  "collection.completedOnly":"Completed only","collection.startedOnly":"Started only",
  "collection.notStarted":"Not started","collection.countedInTotals":"Counted in totals",
  "collection.excludedFromTotals":"Excluded from totals",
  "collection.sortReleased":"Release date","collection.sortName":"Name",
  "collection.sortCompletion":"Completion","collection.sortCostToFinish":"Cost to finish",
  "collection.sortSetSize":"Set size","collection.sortOwnedValue":"Owned value",
  "collection.resetFilters":"Reset filters",
  "collection.resetFiltersTitle":"Back to default: started sets, cheapest to finish first",
  "collection.hideExcluded":"hide excluded","collection.groupSubsets":"group subsets",
  "collection.groupSubsetsTitleOn":"Show subsets indented under their parent set",
  "collection.groupSubsetsTitleOff":"Table view only",
  "collection.grid":"Grid","collection.table":"Table",
  "setCard.currentValue":"Current value","setCard.complete":"complete",
  "setCard.cardsToBuy":"{n} cards to buy {v}","setCard.shipPrefix":"+ ship {v}",
  "setCard.sealedNoted":"Sealed noted","setCard.cheaper":" — cheaper",
  "setCard.viewSet":"View Set","setCard.view":"View",
  "setCard.buyMissing":"Buy missing","setCard.buy":"Buy",
  "setCard.excludeFromTotals":"Exclude from totals","setCard.includeInTotals":"Include in totals",
  "setCard.neverPrintedEnglish":"Never printed in English","setCard.onlyBadge":"{lang} only",
  "setCard.excludedBadge":"excluded",
  "collection.thSet":"Set","collection.thCode":"Code","collection.thReleased":"Released",
  "collection.thKind":"Kind","collection.thOwned":"Owned","collection.thProgress":"Progress",
  "collection.thCurrentValue":"Current Value",
  "collection.thCurrentValueTip":"Cardmarket value of the cards you already own",
  "collection.thCardsToBuy":"Cards to buy",
  "collection.thCardsToBuyTip":"Cards you still need, at Cardmarket trend",
  "collection.thShip":"+ Ship","collection.thToFinish":"To finish",
  "collection.thToFinishTip":"Cards to buy plus estimated shipping",
  "collection.previous":"Previous","collection.next":"Next",
  "collection.pageOfN":"Page {p} of {n} · {count} sets","collection.setsCount":"{count} sets",
  "missing.title":"Missing card names",
  "missing.desc":"Every card name you own in no set at all, priced at its cheapest printing "+
    "across the sets that count. This is the shopping list for “one of everything”.",
  "missing.searchPlaceholder":"Search name…","missing.anyRarity":"Any rarity",
  "missing.anySet":"All sets","missing.minPricePlaceholder":"min €",
  "missing.maxPricePlaceholder":"max €","missing.sortNameAZ":"Name A–Z",
  "missing.sortCheapestFirst":"Cheapest first","missing.sortRarity":"Rarity",
  "missing.hide300":"hide endgame","missing.namesMissing":"Names missing",
  "missing.cheapestTotal":"Cheapest printings total",
  "missing.cardsOnlyNoShipping":"cards only, no shipping","missing.thisPage":"This page",
  "missing.pageOfN2":"page {p} of {n}","missing.cheapestFirstSuffix":" · cheapest first",
  "missing.addPageToCart":"Add page to Wantlist-Cart","missing.addedCount":"{n} added",
  "missing.thCard":"Card","missing.thCheapestIn":"Cheapest in","missing.thNo":"No.",
  "missing.thRarity":"Rarity","missing.thPrice":"Price","missing.thCart":"Cart",
  "missing.loading":"Loading…","missing.pagerPageOfN":"Page {p} of {n}",
  "cart.desc":"Cards you collected across sets, ready to turn into Cardmarket want lists. "+
    "Prices are Cardmarket trend prices; shipping is an estimate.",
  "cart.emptyTitle":"The Wantlist-Cart is empty",
  "cart.emptyDesc":"Add cards with the + button in any set, card list or missing-names view.",
  "cart.cards":"Cards","cart.fromNSets":"from {n} sets","cart.cardsTotal":"Cards total",
  "cart.shippingEst":"Shipping (est.)","cart.total":"Total",
  "cart.cardsPlusShipping":"cards + estimated shipping",
  "cart.filterPlaceholder":"Filter cards or sets…","cart.sortSet":"Set",
  "cart.sortCardName":"Card name","cart.sortPrice":"Price","cart.sortLineTotal":"Line total",
  "cart.sortQuantity":"Quantity","cart.empty":"Empty Wantlist-Cart",
  "cart.buildWantlist":"Build wantlist","cart.remove":"Remove",
  "cart.secretLairWhy":"Secret Lair want lists aren't supported yet",
  "cart.secretLairSkipped":"{n} Secret Lair card(s) were skipped — see the note on the set page.",
  "cart.secretLairNote":"Secret Lair cards can't be added to the Wantlist-Cart yet. Cardmarket splits Secret Lair into hundreds of separate expansions with no reliable mapping, so a generated want list wouldn't match. Buy these directly from the card's Cardmarket page.",
  "cart.addAllToCollection":"Add all to collection","cart.addAllToCollectionConfirm":
    "Add all {n} cards in the Wantlist-Cart to your collection as nonfoil? "+
    "The cart itself stays as it is.",
  "cart.addedAllToCollection":"Added to collection",
  "cart.confirmClear":"Remove everything from the Wantlist-Cart?",
  "wantlist.nothingToCopy":"Nothing to copy.",
  "wantlist.secretLairNote":"Secret Lair lines carry no expansion or version — Cardmarket splits Secret Lair into hundreds of separate expansions and there is no reliable mapping. On import, expect some of these not to match; add those by hand from the card's Cardmarket page.",
  "wantlist.limitInfo":"Cardmarket allows {limit} entries per want list, so this is split "+
    "into {n} {lists}. Paste each block into its own want list.",
  "wantlist.list":"list","wantlist.listsPlural":"lists",
  "wantlist.entryHeader":"Want list {i} of {n} — {count} entries",
  "wantlist.copyList":"Copy list {i}","wantlist.copied":"Copied",
  "manage.title":"Settings",
  "manage.tabCollection":"Collection","manage.tabCompletion":"Completion","manage.tabCm":"Cardmarket",
  "manage.tabAppearance":"Appearance","manage.tabAbout":"Update & Help",
  "manage.tabUpdate":"Update Collection",
  "manage.tabSets":"Excluded Sets","manage.tabDesign":"Design","manage.tabShipping":"Shipping",
  "manage.tabLanguage":"Language","manage.tabGoals":"Set goals",
  "manage.tabHistory":"History","manage.tabApp":"Update App","manage.tabHelp":"Help",
  "cm.title":"Cardmarket helper",
  "cm.desc":"A userscript that runs on cardmarket.com and marks each single offer by whether the card is already in your collection — handy for topping up a seller's order with cheap missing cards at no extra shipping.",
  "cm.step1":"Install a free, open-source userscript manager: <a href='https://violentmonkey.github.io/' target='_blank' rel='noopener'>Violentmonkey</a> (Chrome / Firefox / Edge) or <a href='https://apps.apple.com/app/userscripts/id1463298887' target='_blank' rel='noopener'>Userscripts</a> by Quoid (Safari, from the Mac App Store).",
  "cm.step2":"Open this URL — the manager offers to install the script:",
  "cm.stepAllow":"Chrome and Brave (since v120) also need user scripts switched on: open the extension's details page (chrome://extensions or brave://extensions -> Violentmonkey -> Details) and enable 'Allow User Scripts', then reload the page.",
  "cm.stepRunning":"Keep Binduno running while you use it — the script reads your collection from this app, so nothing gets marked if it is closed.",
  "cm.step3":"Open a seller's singles list on Cardmarket. A 'Binduno: on/off' button appears bottom-right and each offer row gets a coloured bar and a badge.",
  "cm.stepPermit":"The first time it runs, your userscript manager asks whether the script may contact localhost — allow it (once).",
  "cm.legend":"Green = this exact printing is in your collection · yellow = you own the card from another set/version/finish · red = you don't own it. The count shows how many copies you own.",
  "cm.toggleNote":"On/off lives on the button on the Cardmarket page and syncs back here. The script only reads the pages you open and talks to this local app — it makes no requests to Cardmarket.",
  "cm.updateNote":"The script does not auto-update. After a Binduno update, open the URL above again and reinstall to get the matching helper version.",
  "goal.title":"What counts as a complete set",
  "goal.desc":"These rules decide when a set reads 100%. They apply everywhere — set pages, the home dashboard and the buy lists.",
  "goal.presetTitle":"Quick pick",
  "goal.presetDesc":"Sets all three options at once. Fine-tune below afterwards if you like.",
  "goal.preset.oneEach":"One of every card","goal.preset.oneEachDesc":"Any single printing of each card name finishes the set.",
  "goal.preset.baseSet":"Base set","goal.preset.baseSetDesc":"Every plain base-frame printing on its own — extra arts of basics and foil-only stars still count.",
  "goal.preset.everything":"Everything","goal.preset.everythingDesc":"Every collector number: showcase, borderless, extended art, special foils. Serialized still excluded.",
  "goal.scope":"Counting","goal.scopeNames":"One printing per card name is enough","goal.scopePrintings":"Every collector number counts on its own",
  "goal.extras":"Special printings (Showcase, Borderless, Extended Art, special foils)","goal.extrasInclude":"Count toward 100%","goal.extrasExclude":"Don't count — base printing only",
  "goal.serialized":"Serialized cards (numbered limited prints)","goal.serializedInclude":"Count toward 100%","goal.serializedExclude":"Don't count","goal.serializedNote":"Only relevant while special printings count.",
  "endgame.title":"Very expensive cards",
  "endgame.desc":"Cards whose cheapest printing is at or above the threshold are set aside: they don't count toward a set's missing cards or its cost, and are shown on their own on the home page instead. Turn this off to treat them like any other missing card.",
  "endgame.enable":"Set aside cards above a price threshold",
  "endgame.threshold":"Threshold",
  "wizard.endgameTitle":"How to handle very expensive cards","wizard.endgameDesc":"Some singles cost hundreds of euros. Binduno can set those aside so one Reserved-List card doesn't make a whole set look unaffordable. Change this any time under Settings → Completion.",
  "wizard.egSetAside":"Set aside cards from {eur}","wizard.egSetAsideDesc":"They're listed separately on the home page and left out of set cost/missing counts.",
  "wizard.egCountAll":"Count every card","wizard.egCountAllDesc":"No price cutoff — expensive cards are normal missing cards.",
  "wizard.cmTitle":"Cardmarket browser helper","wizard.cmDesc":"Optional: a userscript that marks single offers on cardmarket.com by whether the card is already in your collection. Set it up any time under Settings → Cardmarket.",
  "wizard.cmOpen":"Open setup instructions",
  "wizard.collectorTitle":"What kind of collector are you?",
  "wizard.collectorDesc":"This sets how Binduno measures set completion. You can change it any time under Settings → Set goals.",
  "lang.appLanguage":"App language",
  "lang.appLanguageDesc":"Menus, buttons and labels. This is saved on this device only.",
  "lang.cardSetNames":"Card and set names",
  "lang.cardSetNamesDesc":"What language card and set names are shown in — independent of the app language above.",
  "lang.current":"Current",
  "lang.cardLangEnDesc":"Card and set names as printed in English.",
  "lang.cardLangDeDesc":"Card names as printed on the German cards, where a German printing "+
    "exists. Set names stay English — Scryfall does not provide a localized set name, "+
    "even on German cards.",
  "design.theme":"Theme","design.themeDesc":"Choose how the app looks. This is saved on this device only.",
  "design.dark":"Dark","design.darkDesc":"The default look.",
  "design.light":"Light","design.lightDesc":"A bright theme for daylight use.",
  "design.colorblind":"Colorblind-friendly",
  "design.colorblindDesc":"Keeps the dark theme, but swaps the legal/banned and "+
    "completed/open colors from green-vs-red to blue-vs-orange, which stays distinguishable "+
    "across the common forms of color blindness.",
  "shipPref.countryTitle":"Country","shipPref.countryDesc":"Domestic (same country to same "+
    "country) shipping rates, pulled from Cardmarket's own shipping cost calculator.",
  "shipPref.countryRates":"Currently: {untracked} untracked, {tracked} tracked per order.",
  "shipPref.title":"Shipping option","shipPref.desc":"How shipping is estimated for cost "+
    "previews (set list, Buy missing, Wantlist-Cart). This only changes the estimate shown "+
    "in the app, not what you actually pick at Cardmarket checkout.",
  "shipPref.standard":"Standard","shipPref.standardDesc":"Cardmarket's cheapest option for "+
    "the order's value — untracked letter for orders up to 25 €, tracked once an order "+
    "exceeds that (Cardmarket requires tracking above 25 € everywhere).",
  "shipPref.tracked":"Tracked only","shipPref.trackedDesc":"Always estimate the tracked "+
    "shipping rate for the selected country, for when you only ever want tracked shipping.",
  "manageUpdate.importTitle":"Import collection",
  "manageUpdate.replace":"Replace",
  "manageUpdate.replaceDesc":"Wipe the stored collection and use this file as the new truth.",
  "manageUpdate.add":"Add",
  "manageUpdate.addDesc":"Keep what is stored and add these quantities on top.",
  "manageUpdate.importBtn":"Import collection","manageUpdate.importing":"Importing…",
  "busy.importing":"Importing your collection…","busy.clearing":"Clearing your collection…",
  "busy.recount":"Recalculating set totals — almost there…",
  "manageUpdate.importedMsg":"{n} cards imported with {mode} mode.",
  "manageUpdate.modeReplace":"replace","manageUpdate.modeAdd":"add",
  "manageUpdate.cardDataTitle":"Card data",
  "manageUpdate.cardDataDesc":"Names, rarities, set structure and Cardmarket prices come "+
    "from Scryfall. Refresh every few weeks, or after a new set is released.",
  "manageUpdate.downloadBtn":"Download latest card data",
  "manageUpdate.autoSync":"Sync automatically once a day",
  "manageUpdate.autoSyncDesc":"Checks every hour in the background whether the card data "+
    "is more than 24 hours old, and downloads a fresh copy (same download as the button "+
    "above) if so — mainly to keep Cardmarket prices current. Runs only while Binduno is "+
    "open; nothing happens while it's closed.",
  "manageUpdate.priceHistoryTitle":"Price history",
  "manageUpdate.priceHistoryDesc":"Every time card data is refreshed (manually or "+
    "automatically), any Cardmarket price that changed since the last check is logged with "+
    "today's date. Points older than a year are thinned to one per week to keep the file "+
    "from growing forever.",
  "manageUpdate.priceLogging":"Log price history",
  "manageUpdate.priceLoggingDesc":"Turn off to stop recording price changes entirely. "+
    "Existing history is kept, just nothing new gets added.",
  "manageUpdate.backfillBtn":"Load 90-day price history from MTGJSON",
  "manageUpdate.backfillDesc":"Catch-up download (~180 MB, deleted afterward — only the "+
    "resulting prices are kept) that fills in real past Cardmarket prices for the last 90 "+
    "days, instead of waiting weeks for the app's own logging to build up history. Binduno "+
    "already runs this on its own — once after a fresh install, and again automatically "+
    "whenever it notices the log has a gap of 2+ days (i.e. it wasn't running for a "+
    "while) — this button is only for forcing it right now instead of waiting for that "+
    "hourly check. Existing logged days are never overwritten either way.",
  "manageUpdate.backfillDone":"Price history backfilled.",
  "manageUpdate.backupTitle":"Backup",
  "manageUpdate.backupDesc":"Export everything you own as CSV in ManaBox column layout, "+
    "so it can be re-imported here or loaded into ManaBox.",
  "manageUpdate.exportBtn":"Export collection as CSV",
  "manageUpdate.dangerZone":"Danger zone","manageUpdate.clearBtn":"Clear stored collection",
  "manageUpdate.confirmClear":"Remove every card from the stored collection?",
  "manageUpdate.cardDataUpToDate":"Card data up to date.",
  "manageUpdate.failed":"Failed: {err}",
  "manageSets.title":"Which sets count toward your totals",
  "manageSets.desc":"Excluded sets stay visible in the Collection but are left out of every "+
    "percentage, cost and chart. Defaults leave out promos, tokens, memorabilia, Un-sets and "+
    "anything digital.",
  "manageSets.searchPlaceholder":"Search set…",
  "manageSets.includeEverything":"Include everything","manageSets.restoreDefaults":"Restore defaults",
  "manageSets.ofCounted":"{on} of {total} counted",
  "manageSets.excludeAll":"Exclude all","manageSets.includeAll":"Include all",
  "manageSets.counted":"Counted ✓","manageSets.excluded":"Excluded ✕",
  "manageApp.wizardTitle":"Setup wizard","manageApp.wizardDesc":"Re-run the initial setup "+
    "(language, country, collection import, card database) any time.",
  "manageApp.wizardBtn":"Restart setup wizard",
  "manageApp.title":"Update the app",
  "manageApp.desc":"Pick a newer binduno.py. It is checked for syntax errors, the "+
    "running file is backed up as <code>binduno_previous.py</code>, then the app "+
    "replaces itself and restarts. No Terminal, no reinstalling.",
  "manageApp.filePickerNote":"Choose binduno.py — the file picker shows all files "+
    "because macOS has no file type registered for .py",
  "manageApp.installBtn":"Install update","manageApp.installing":"Installing…",
  "manageApp.rebuildTitle":"Rebuild the macOS app",
  "manageApp.rebuildDesc":"If the app icon or launcher ever breaks, run "+
    "<code>python3 binduno.py --install-app</code> once in Terminal.",
  "manageApp.whereThingsLive":"Where things live",
  "manageApp.database":"Database","manageApp.logFile":"Log file",
  "manageApp.updatedMsg":"Updated {from} → {to}. Restarting…",
  "manageApp.restartSlow":"Restart is taking long. Open Binduno.app again.",
  "help.basics":"Basics","help.prices":"Prices","help.shipping":"Shipping",
  "help.rules":"Counting rules","help.data":"Your data",
  "history.title":"Change history","history.desc":"The last 100 changes to your stored data.",
  "history.none":"No changes recorded yet.",
  "tip.default":"How this is calculated",
  "common.addToCart":"Add to Wantlist-Cart","common.addedCount":"{n} added",
  "common.shipNote":"Shipping is estimated: about {cps} cards per seller, then Cardmarket "+
    "letter rates (up to 17 cards 1.40 €, up to 40 cards 2.10 €) or 5.00 € tracked once an "+
    "order passes 25 €. Hover any shipping figure for the full calculation.",
  "setPage.ownedOfTotal":"{owned} of {total} owned",
  "setPage.addAllMissing":"Add all missing to Wantlist-Cart",
  "setPage.buyMissingDots":"Buy missing…",
  "setPage.thType":"Type","setPage.thFoil":"Foil","setPage.thCopies":"Copies",
  "setPage.thOwned":"Owned","setPage.thNote":"Note","setPage.yes":"yes","setPage.no":"no",
  "buyPage.title":"Buy missing cards",
  "buyPage.desc":"{setName} — {n} {cards} whose name you own in no printing of this set. "+
    "Prices are Cardmarket trend prices, not the cheapest offer.",
  "buyPage.cardSingular":"card","buyPage.cardPlural":"cards",
  "buyPage.pickGroup":"Pick a group to add it straight to the Wantlist-Cart.",
  "buyPage.allMissing":"All missing cards",
  "buyPage.cardsCount":"{n} cards","buyPage.cardsOnlyTip":"Cardmarket trend prices, no shipping.",
  "buyPage.cardsOnlyTipTitle":"Cards only","buyPage.breadcrumbBuyMissing":"Buy missing",
  "cardPage.back":"← Back","cardPage.noImage":"No image available",
  "cardPage.buyOnCardmarket":"Buy on Cardmarket · {price}","cardPage.buyFoil":"Buy foil · {price}",
  "cardPage.viewOnScryfall":"View on Scryfall","cardPage.regular":"Regular",
  "cardPage.copiesOwned":"Copies owned","cardPage.yourCollection":"Your collection",
  "cardPage.nonfoil":"Nonfoil","cardPage.setTo4":"Set to 4 copies",
  "cardPage.wantListEntry":"Want list entry","cardPage.set":"Set",
  "cardPage.illustratedBy":"Illustrated by {artist}","cardPage.formatLegality":"Format legality",
  "cardPage.allPrintings":"All printings",
  "setPage.noteEndgame":"Endgame","setPage.noteOtherPrinting":"Other printing",
  "setPage.notInGoal":"off-goal","setPage.baseMissing":"base missing",
  "setPage.onlyExtra":"{n} card(s) owned only as a special printing — base printing still missing",
  "cardPage.added":"Added",
  "cardPage.addToWatchlist":"Add to Watchlist","cardPage.inWatchlist":"★ In Watchlist",
  "browse.searching":"Searching…","browse.nothingMatches":"Nothing matches",
  "browse.loosenFilter":"Loosen a filter or reset the search.",
  "browse.resetSearch":"Reset search","browse.cardName":"Card name",
  "browse.searchByName":"Search by name","browse.ownership":"Ownership",
  "browse.all":"All","browse.owned":"Owned","browse.missing":"Missing",
  "browse.missingEverySet":"Missing in every set (new card names)",
  "browse.rulesText":"Rules text","browse.searchCardText":"Search card text",
  "browse.artist":"Artist","browse.searchByArtist":"Search by artist",
  "browse.typeLine":"Type line","browse.typeLinePlaceholder":"e.g. Creature, Instant",
  "browse.colors":"Colors","browse.colorWhite":"White","browse.colorBlue":"Blue",
  "browse.colorBlack":"Black","browse.colorRed":"Red","browse.colorGreen":"Green",
  "browse.colorless":"Colorless","browse.includesColors":"Includes these colors",
  "browse.onlyColors":"Only these colors","browse.exactColors":"Exactly these colors",
  "browse.any":"Any","browse.priceRange":"Price range (€)","browse.from":"from","browse.to":"to",
  "browse.options":"Options","browse.oneRowPerName":"One row per card name",
  "browse.baseSetOnly":"Base-set printings only",
  "browse.onlyNoPrice":"Only cards without a price",
  "browse.includeExcludedSets":"Include cards from excluded sets",
  "browse.sortCollectorNumber":"Collector number","browse.sortManaValue":"Mana value",
  "browse.sortCopiesOwned":"Copies owned",
  "wizard.recoBtn":"Use recommended settings",
  "wizard.recoApplying":"Applying…",
  "wizard.manualBtn":"Set everything up myself",
  "wizard.recoNote":"Recommended: app language from your browser, English card names, "+
    "shipping country from your browser region, cheapest shipping, daily card-data "+
    "sync and price history on, one printing per card name, no price cap, dark theme. "+
    "Every one of these stays changeable in Settings.",
  "manageUpdate.formatLabel":"File format",
  "manageUpdate.formatAuto":"Detect automatically",
  "manageUpdate.detected":"Detected: {fmt}",
  "phone.tab":"Phone",
  "phone.title":"Open on your phone",
  "phone.desc":"Binduno also listens on your local network, so any device on the same "+
    "Wi-Fi can open it. Scan this code with your phone camera, or type the address.",
  "phone.hint":"Only works while this computer runs Binduno and both devices are on the "+
    "same network. The address can change when the computer reconnects to Wi-Fi.",
  "phone.noLan":"No local network address found — this computer may be offline or only "+
    "reachable via localhost.",
  "cm.statusSeen":"Helper last seen {ago}.",
  "cm.statusNever":"Helper not detected yet — open a Cardmarket seller page with the "+
    "userscript installed.",
  "cm.seenNow":"just now","cm.seenMin":"{n} min ago","cm.seenHour":"{n} h ago",
  "cm.seenDay":"{n} d ago",
  "cm.testBtn":"Refresh status",
  "costs.title":"Cost estimates",
  "costs.desc":"Off by default: the running total of what a full collection would still "+
    "cost can be discouraging when you are just starting out. Turn it on to show the "+
    "\"remaining cost\" and \"cheapest to finish\" cards on the Home page. Prices on the "+
    "Collection and Missing pages (and the \"cost to finish\" sort) are shown either way.",
  "costs.enable":"Show what is left to buy in euros",
  "setCard.nMissing":"{n} missing",
  "gh.title":"Update from GitHub",
  "gh.desc":"Binduno pulls new versions straight from its GitHub repository — the "+
    "newest Release, or the file on the default branch if there are no Releases yet. "+
    "The repo is preset; only change it if you run your own fork.",
  "gh.save":"Save","gh.check":"Check for updates","gh.checking":"Checking GitHub…",
  "gh.checkFailed":"Couldn't complete the check — the local app didn't answer in time (usually a slow or offline network). Try again, or use the file updater below.",
  "gh.upToDate":"You are on the newest version ({v}).",
  "gh.available":"Version {next} is available (you have {cur}). {name}",
  "gh.install":"Download and install {v}","gh.viewRelease":"Release notes on GitHub",
  "gh.autoCheck":"Check for updates on every start",
  "gh.autoInstall":"Install updates automatically",
  "gh.autoNote":"The check runs quietly a few seconds after launch. With auto‑install on, "+
    "a new version is downloaded and the app restarts itself — otherwise you just get a "+
    "note on the home page.",
  "home.updateAvailable":"Binduno {v} is available.",
  "home.updateOpen":"Update","home.updateDismiss":"Later",
  "tip.shipping":"Estimated shipping",
  "tip.cmExpTitle":"Cardmarket expansion",
  "tip.cmExp":"Cardmarket sells this printing in a separate expansion, listed as version {v}.",
  "tip.cmVerTitle":"Cardmarket version",
  "tip.cmVer":"Cardmarket lists this printing as version {v} of this card in this set.",
  "start.title":"Start here","start.dismiss":"Dismiss","start.hide":"Don't show this again",
  "start.body":"Three things Binduno is for — pick one, or read how it thinks.",
  "start.a1":"See where my collection stands","start.a2":"Build a want list",
  "start.a3":"Mark cards while shopping on Cardmarket","start.a4":"How Binduno thinks",
  "explain.link":"How Binduno thinks",
  "explain.title":"How Binduno thinks","explain.back":"Back to the home page",
  "explain.intro":"The whole model in five short points.",
  "explain.h1":"Two goals at once",
  "explain.b1":"Card names counts owning any one printing of a card — your “one of everything” project. Printings counts every set-and-number on its own — full set completion. Both are shown side by side.",
  "explain.h2":"You decide what counts",
  "explain.b2":"By default a set is complete when you own one plain printing of each card name. Under Settings → Completion you can instead require every collector number, and choose whether Showcase / borderless / serialized printings count. Promos, tokens and Un-sets are left out.",
  "explain.h3":"Prices are Cardmarket trend",
  "explain.b3":"Values and “cost to finish” use Cardmarket's trend price via Scryfall — not the cheapest current offer, and with no German-seller premium. Real cost is usually a bit lower. The Home page's totals are off by default (Settings → Completion); euro figures elsewhere are always shown.",
  "explain.h4":"Want lists are built to Cardmarket's rules",
  "explain.b4":"The Wantlist-Cart is the only place want-list text is made. It uses Cardmarket's exact names and bracket order, quantity prefixes, and splits into 150-entry blocks you paste one after another.",
  "explain.h5":"Everything stays on your computer",
  "explain.b5":"Your collection lives in a local SQLite file. No account, no cloud. The only thing downloaded is Scryfall's card database; the optional Cardmarket helper only reads pages you already opened.",
  "cm.browserLabel":"Your browser",
  "cm.optBookmarklet":"Bookmarklet (no extension)",
  "cm.bmHeading":"Bookmarklet — no extension (Chrome &amp; Firefox)",
  "cm.step1b.chrome":"Install <a href='https://violentmonkey.github.io/' target='_blank' rel='noopener'>Violentmonkey</a> (free, open source) from your browser's extension store.",
  "cm.step1b.firefox":"Install <a href='https://addons.mozilla.org/firefox/addon/violentmonkey/' target='_blank' rel='noopener'>Violentmonkey</a> (free, open source) from Firefox Add-ons.",
  "cm.step1b.safari":"Install <a href='https://apps.apple.com/app/userscripts/id1463298887' target='_blank' rel='noopener'>Userscripts</a> by Quoid (free) from the Mac App Store.",
  "cm.stepSafariEnable":"In Safari → Settings → Extensions, switch Userscripts on and allow it on cardmarket.com.",
  "cm.bmIntro":"No extension to install — the catch is you must click it once on every page.",
  "cm.bmDrag":"Drag this link onto your bookmarks bar:",
  "cm.bmClick":"On a Cardmarket seller's singles page, click the bookmark. It marks the visible rows and keeps refreshing for about 90 seconds; click it again whenever you change page.",
  "cm.bmNote":"Does not work in Safari — Safari blocks the bookmarklet's connection to the local app. On Safari, use the extension method above instead.",
},
de:{
  "nav.home":"Start","nav.collection":"Sammlung","nav.missing":"Fehlende Namen",
  "nav.cart":"Wantlist-Cart","nav.manage":"Einstellungen","nav.quit":"Beenden",
  "nav.quitTitle":"Tracker beenden","nav.homeTitle":"Start",
  "nav.confirmQuit":"Tracker beenden? Die Seite funktioniert erst wieder nach einem Neustart.",
  "nav.stopped":"Tracker beendet","nav.stoppedDesc":"Öffne Binduno.app erneut, um weiterzumachen.",
  "home.title":"Sammlungsstatistik",
  "home.updated":"Karten aktualisiert {cards} · Sammlung aktualisiert {collection}",
  "home.never":"nie","home.cardNames":"Kartennamen","home.printings":"Drucke",
  "home.setsCompleted":"Sets vollständig","home.ofCountedSets":"von {n} gezählten Sets",
  "home.xOfY":"{a} von {b}","home.physicalCards":"Physische Karten",
  "home.duplicatesIncluded":"inkl. Duplikate","home.collectionValue":"Sammlungswert",
  "home.cardmarketTrend":"Cardmarket-Trend","home.remainingCost":"Restkosten",
  "home.inclShipping":"inkl. {n} Versand","home.cardsOver300":"Karten über 300 €","home.cardsOver":"Karten über {eur}",
  "home.showTheseCards":"Diese Karten anzeigen","home.leftOutOfRemaining":"{n} — nicht in Restkosten",
  "home.namesStillMissing":"Noch fehlende Namen","home.acrossCountedSets":"über alle gezählten Sets",
  "home.byRarity":"Nach Seltenheit","home.closestToCompletion":"Kurz vor Fertigstellung",
  "home.nothingOpen":"Nichts offen.","home.cheapestToFinish":"Günstigste Fertigstellung",
  "home.cheapestDesc":"Sets, die du mit dem geringsten Geldeinsatz abschließen könntest, Versand inklusive.",
  "home.shopByName":"Oder nach Kartennamen einkaufen →","home.nothingLoaded":"Noch nichts geladen",
  "home.watchlist":"Watchlist","home.watchlistDesc":"Karten, die du im Blick behältst, mit "+
    "ihrem Cardmarket-Preisverlauf der letzten 7 Tage. Karten über eine beliebige "+
    "Kartenseite hinzufügen.",
  "home.watchlistEmpty":"Noch keine Karten auf der Watchlist — auf einer Kartenseite auf "+
    "„Zur Watchlist hinzufügen“ klicken.",
  "home.watchlist7d":"Letzte 7 Tage","home.watchlistChange":"Änderung",
  "home.watchlistRemove":"Von Watchlist entfernen",
  "home.watchlistCount":"{n} von {max} Karten",
  "home.nothingLoadedDesc":"Lade zuerst die Kartendaten herunter und importiere deinen ManaBox-Export.",
  "home.goToManage":"Zu den Einstellungen",
  "wizard.back":"Zurück","wizard.skipAll":"Einrichtung überspringen","wizard.next":"Weiter",
  "wizard.start":"Los geht's","wizard.finish":"Zu Binduno",
  "wizard.skipStep":"Diesen Schritt überspringen",
  "wizard.welcomeTitle":"Willkommen bei Binduno",
  "wizard.welcomeBody1":"Binduno ist eine lokale App zum Tracken deiner Magic: The "+
    "Gathering-Sammlung und zum Erzeugen von Cardmarket-Wantlisten. Alles läuft auf diesem "+
    "Rechner — deine Sammlungsdaten verlassen ihn nie, es gibt kein Konto und keine Cloud.",
  "wizard.welcomeBody2":"Diese kurze Einrichtung wählt eine Sprache, dein Land (für "+
    "Versandkosten-Schätzungen), importiert deine Sammlung falls du schon einen "+
    "ManaBox-Export hast, und lädt die Scryfall-Kartendatenbank, die Binduno braucht, um zu "+
    "wissen was es überhaupt gibt. Jeder Schritt kann übersprungen und später unter "+
    "Einstellungen nachgeholt werden.",
  "wizard.langTitle":"Sprache",
  "wizard.countryTitle":"Land",
  "wizard.importTitle":"Sammlung importieren",
  "wizard.importDesc":"Falls du schon einen CSV-Export aus ManaBox, Moxfield oder Archidekt "+
    "hast, lade ihn jetzt hoch. Sonst diesen Schritt überspringen — Import geht jederzeit "+
    "unter Einstellungen nach.",
  "wizard.cardDataTitle":"Kartendatenbank",
  "wizard.cardDataDesc":"Binduno braucht Scryfalls Kartendatenbank (Namen, Sets, "+
    "Cardmarket-Preise), um überhaupt zu wissen was existiert — einmalig etwa 100–150 MB. "+
    "Je nach Verbindung dauert das ein bis zwei Minuten.",
  "wizard.cardDataBtn":"Kartendatenbank herunterladen",
  "wizard.cardDataConfirm":"Jetzt die Scryfall-Kartendatenbank herunterladen (~100–150 MB)? "+
    "Läuft im Hintergrund und kann ein bis zwei Minuten dauern.",
  "wizard.doneTitle":"Fertig eingerichtet",
  "wizard.doneBody":"Binduno ist startklar. Alle diese Einstellungen kannst du jederzeit "+
    "unter Einstellungen ändern, und diese Einrichtung von dort auch erneut starten.",
  "rarity.c":"Gewöhnlich","rarity.u":"Ungewöhnlich","rarity.r":"Selten","rarity.m":"Mythisch",
  "rarity.s":"Spezial","rarity.b":"Standardland","rarity.land":"Standardländer",
  "collection.title":"Sammlung","collection.viewSets":"Sets anzeigen","collection.viewCards":"Karten anzeigen",
  "collection.desc":"Jedes Set mit Erscheinungsdatum, Fortschritt und Kosten zur Fertigstellung. "+
    "„Restkosten“ sind der Preis der noch fehlenden Karten plus geschätzter Versand.",
  "collection.searchPlaceholder":"Set-Name oder -Code suchen…",
  "collection.allCategories":"Alle Kategorien","collection.noCategories":"Keine Kategorien",
  "collection.nOfTotalCategories":"{n} von {total} Kategorien",
  "collection.allSets":"Alle Sets","collection.incompleteOnly":"Nur unvollständige",
  "collection.completedOnly":"Nur vollständige","collection.startedOnly":"Nur begonnene",
  "collection.notStarted":"Nicht begonnen","collection.countedInTotals":"In Gesamtwerten gezählt",
  "collection.excludedFromTotals":"Aus Gesamtwerten ausgeschlossen",
  "collection.sortReleased":"Erscheinungsdatum","collection.sortName":"Name",
  "collection.sortCompletion":"Fortschritt","collection.sortCostToFinish":"Restkosten",
  "collection.sortSetSize":"Set-Größe","collection.sortOwnedValue":"Sammlungswert",
  "collection.resetFilters":"Filter zurücksetzen",
  "collection.resetFiltersTitle":"Zurück zum Standard: begonnene Sets, günstigste zuerst",
  "collection.hideExcluded":"Ausgeschlossene ausblenden","collection.groupSubsets":"Subsets gruppieren",
  "collection.groupSubsetsTitleOn":"Subsets eingerückt unter ihrem Hauptset anzeigen",
  "collection.groupSubsetsTitleOff":"Nur in der Tabellenansicht",
  "collection.grid":"Kacheln","collection.table":"Tabelle",
  "setCard.currentValue":"Aktueller Wert","setCard.complete":"vollständig",
  "setCard.cardsToBuy":"{n} Karten zu kaufen {v}","setCard.shipPrefix":"+ Versand {v}",
  "setCard.sealedNoted":"Sealed notiert","setCard.cheaper":" — günstiger",
  "setCard.viewSet":"Set anzeigen","setCard.view":"Anzeigen",
  "setCard.buyMissing":"Fehlende kaufen","setCard.buy":"Kaufen",
  "setCard.excludeFromTotals":"Aus Gesamtwerten ausschließen","setCard.includeInTotals":"In Gesamtwerte einschließen",
  "setCard.neverPrintedEnglish":"Nie auf Englisch erschienen","setCard.onlyBadge":"nur {lang}",
  "setCard.excludedBadge":"ausgeschlossen",
  "collection.thSet":"Set","collection.thCode":"Code","collection.thReleased":"Erschienen",
  "collection.thKind":"Art","collection.thOwned":"Besitz","collection.thProgress":"Fortschritt",
  "collection.thCurrentValue":"Aktueller Wert",
  "collection.thCurrentValueTip":"Cardmarket-Wert der bereits besessenen Karten",
  "collection.thCardsToBuy":"Zu kaufende Karten",
  "collection.thCardsToBuyTip":"Noch benötigte Karten, zum Cardmarket-Trendpreis",
  "collection.thShip":"+ Versand","collection.thToFinish":"Restkosten",
  "collection.thToFinishTip":"Zu kaufende Karten plus geschätzter Versand",
  "collection.previous":"Zurück","collection.next":"Weiter",
  "collection.pageOfN":"Seite {p} von {n} · {count} Sets","collection.setsCount":"{count} Sets",
  "missing.title":"Fehlende Kartennamen",
  "missing.desc":"Jeder Kartenname, den du in keinem einzigen Set besitzt, zum Preis des "+
    "günstigsten Drucks in den gezählten Sets. Die Einkaufsliste für „von jedem eine“.",
  "missing.searchPlaceholder":"Name suchen…","missing.anyRarity":"Beliebige Seltenheit",
  "missing.anySet":"Alle Sets","missing.minPricePlaceholder":"min €",
  "missing.maxPricePlaceholder":"max €","missing.sortNameAZ":"Name A–Z",
  "missing.sortCheapestFirst":"Günstigste zuerst","missing.sortRarity":"Seltenheit",
  "missing.hide300":"Endgame ausblenden","missing.namesMissing":"Fehlende Namen",
  "missing.cheapestTotal":"Günstigste Drucke gesamt",
  "missing.cardsOnlyNoShipping":"nur Karten, kein Versand","missing.thisPage":"Diese Seite",
  "missing.pageOfN2":"Seite {p} von {n}","missing.cheapestFirstSuffix":" · günstigste zuerst",
  "missing.addPageToCart":"Seite zum Wantlist-Cart hinzufügen","missing.addedCount":"{n} hinzugefügt",
  "missing.thCard":"Karte","missing.thCheapestIn":"Günstigste in","missing.thNo":"Nr.",
  "missing.thRarity":"Seltenheit","missing.thPrice":"Preis","missing.thCart":"Cart",
  "missing.loading":"Lädt…","missing.pagerPageOfN":"Seite {p} von {n}",
  "cart.desc":"Karten aus allen Sets, bereit für Cardmarket-Wantlisten. "+
    "Preise sind Cardmarket-Trendpreise; der Versand ist geschätzt.",
  "cart.emptyTitle":"Der Wantlist-Cart ist leer",
  "cart.emptyDesc":"Füge Karten über den +-Button in Set-, Karten- oder Fehlende-Namen-Ansicht hinzu.",
  "cart.cards":"Karten","cart.fromNSets":"aus {n} Sets","cart.cardsTotal":"Karten gesamt",
  "cart.shippingEst":"Versand (geschätzt)","cart.total":"Gesamt",
  "cart.cardsPlusShipping":"Karten plus geschätzter Versand",
  "cart.filterPlaceholder":"Karten oder Sets filtern…","cart.sortSet":"Set",
  "cart.sortCardName":"Kartenname","cart.sortPrice":"Preis","cart.sortLineTotal":"Zeilensumme",
  "cart.sortQuantity":"Menge","cart.empty":"Wantlist-Cart leeren",
  "cart.buildWantlist":"Wantlist erzeugen","cart.remove":"Entfernen",
  "cart.secretLairWhy":"Wantlisten für Secret Lair werden noch nicht unterstützt",
  "cart.secretLairSkipped":"{n} Secret-Lair-Karte(n) übersprungen — siehe Hinweis auf der Set-Seite.",
  "cart.secretLairNote":"Secret-Lair-Karten können noch nicht in den Wantlist-Cart. Cardmarket teilt Secret Lair in hunderte einzelne Erweiterungen ohne verlässliche Zuordnung auf, eine erzeugte Wantlist würde also nicht treffen. Diese Karten direkt über die Cardmarket-Seite der Karte kaufen.",
  "cart.addAllToCollection":"Alle zur Sammlung hinzufügen","cart.addAllToCollectionConfirm":
    "Alle {n} Karten aus dem Wantlist-Cart als Nonfoil zur Sammlung hinzufügen? "+
    "Der Cart selbst bleibt dabei unverändert.",
  "cart.addedAllToCollection":"Zur Sammlung hinzugefügt",
  "cart.confirmClear":"Wirklich alles aus dem Wantlist-Cart entfernen?",
  "wantlist.nothingToCopy":"Nichts zu kopieren.",
  "wantlist.secretLairNote":"Secret-Lair-Zeilen haben keine Erweiterung und keine Version — Cardmarket teilt Secret Lair in hunderte einzelne Erweiterungen auf, eine verlässliche Zuordnung gibt es nicht. Beim Import treffen manche davon nicht; die dann von Hand über die Cardmarket-Seite der Karte hinzufügen.",
  "wantlist.limitInfo":"Cardmarket erlaubt {limit} Einträge pro Wantlist, daher aufgeteilt "+
    "in {n} {lists}. Jeden Block einzeln einfügen.",
  "wantlist.list":"Liste","wantlist.listsPlural":"Listen",
  "wantlist.entryHeader":"Wantlist {i} von {n} — {count} Einträge",
  "wantlist.copyList":"Liste {i} kopieren","wantlist.copied":"Kopiert",
  "manage.title":"Einstellungen",
  "manage.tabCollection":"Sammlung","manage.tabCompletion":"Vervollständigung","manage.tabCm":"Cardmarket",
  "manage.tabAppearance":"Darstellung","manage.tabAbout":"Update & Hilfe",
  "manage.tabUpdate":"Sammlung aktualisieren",
  "manage.tabSets":"Ausgeschlossene Sets","manage.tabDesign":"Design","manage.tabShipping":"Versand",
  "manage.tabLanguage":"Sprache","manage.tabGoals":"Set-Ziele",
  "manage.tabHistory":"Verlauf","manage.tabApp":"App aktualisieren","manage.tabHelp":"Hilfe",
  "cm.title":"Cardmarket-Helfer",
  "cm.desc":"Ein Userscript, das auf cardmarket.com läuft und jedes Single-Angebot danach markiert, ob die Karte schon in deiner Sammlung ist — praktisch, um eine Händler-Bestellung mit günstigen fehlenden Karten ohne Zusatzversand aufzufüllen.",
  "cm.step1":"Einen kostenlosen, quelloffenen Userscript-Manager installieren: <a href='https://violentmonkey.github.io/' target='_blank' rel='noopener'>Violentmonkey</a> (Chrome / Firefox / Edge) oder <a href='https://apps.apple.com/app/userscripts/id1463298887' target='_blank' rel='noopener'>Userscripts</a> von Quoid (Safari, aus dem Mac App Store).",
  "cm.step2":"Diese URL öffnen — der Manager bietet die Installation des Scripts an:",
  "cm.stepAllow":"Chrome und Brave (ab v120) brauchen zusätzlich aktivierte User-Skripte: auf der Detailseite der Erweiterung (chrome://extensions bzw. brave://extensions -> Violentmonkey -> Details) 'User-Skripte zulassen' einschalten, dann Seite neu laden.",
  "cm.stepRunning":"Binduno währenddessen laufen lassen — das Script liest deine Sammlung aus dieser App; ist sie zu, wird nichts markiert.",
  "cm.step3":"Auf Cardmarket eine Händler-Singles-Liste öffnen. Unten rechts erscheint ein 'Binduno: an/aus'-Knopf, und jede Angebotszeile bekommt einen farbigen Balken plus Badge.",
  "cm.stepPermit":"Beim ersten Lauf fragt der Userscript-Manager, ob das Script localhost kontaktieren darf — einmal erlauben.",
  "cm.legend":"Grün = genau dieser Druck ist in der Sammlung · gelb = Karte aus einem anderen Set/Version/Finish vorhanden · rot = nicht vorhanden. Die Zahl zeigt, wie viele Exemplare du besitzt.",
  "cm.toggleNote":"An/Aus sitzt auf dem Knopf auf der Cardmarket-Seite und wird hierher zurücksynchronisiert. Das Script liest nur die Seiten, die du öffnest, und spricht mit dieser lokalen App — es sendet nichts an Cardmarket.",
  "cm.updateNote":"Das Script aktualisiert sich nicht selbst. Nach einem Binduno-Update die URL oben erneut öffnen und neu installieren, damit die Helfer-Version passt.",
  "goal.title":"Wann gilt ein Set als vollständig",
  "goal.desc":"Diese Regeln bestimmen, wann ein Set 100 % erreicht. Sie gelten überall — Set-Seiten, Startseite und Kauflisten.",
  "goal.presetTitle":"Schnellauswahl",
  "goal.presetDesc":"Setzt alle drei Optionen auf einmal. Danach unten bei Bedarf feinjustieren.",
  "goal.preset.oneEach":"Ein Exemplar pro Karte","goal.preset.oneEachDesc":"Irgendein Druck jedes Kartennamens vervollständigt das Set.",
  "goal.preset.baseSet":"Basis-Set","goal.preset.baseSetDesc":"Jeder normale Basis-Frame-Druck einzeln — mehrere Arten von Basics und Foil-only-Star-Karten zählen weiter.",
  "goal.preset.everything":"Alles","goal.preset.everythingDesc":"Jede Sammlernummer: Showcase, Borderless, Extended Art, Spezial-Foils. Serialisierte weiterhin ausgeschlossen.",
  "goal.scope":"Zählweise","goal.scopeNames":"Ein Druck pro Kartenname reicht","goal.scopePrintings":"Jede Sammlernummer zählt einzeln",
  "goal.extras":"Sonderdrucke (Showcase, Borderless, Extended Art, Spezial-Foils)","goal.extrasInclude":"Zählen zur 100 %","goal.extrasExclude":"Zählen nicht — nur Basis-Druck",
  "goal.serialized":"Serialisierte Karten (nummerierte limitierte Prints)","goal.serializedInclude":"Zählen zur 100 %","goal.serializedExclude":"Zählen nicht","goal.serializedNote":"Nur relevant, solange Sonderdrucke mitzählen.",
  "endgame.title":"Sehr teure Karten",
  "endgame.desc":"Karten, deren günstigster Druck den Schwellwert erreicht oder überschreitet, werden zurückgestellt: sie zählen nicht zu den fehlenden Karten eines Sets und nicht zu dessen Kosten, sondern werden separat auf der Startseite gezeigt. Aus = sie zählen wie jede andere fehlende Karte.",
  "endgame.enable":"Karten über einem Preis-Schwellwert zurückstellen",
  "endgame.threshold":"Schwellwert",
  "wizard.endgameTitle":"Wie mit sehr teuren Karten umgehen","wizard.endgameDesc":"Manche Singles kosten hunderte Euro. Binduno kann sie zurückstellen, damit eine Reserved-List-Karte nicht ein ganzes Set unbezahlbar aussehen lässt. Jederzeit änderbar unter Einstellungen → Vervollständigung.",
  "wizard.egSetAside":"Karten ab {eur} zurückstellen","wizard.egSetAsideDesc":"Sie stehen separat auf der Startseite und fließen nicht in Set-Kosten/Fehlmengen ein.",
  "wizard.egCountAll":"Jede Karte zählen","wizard.egCountAllDesc":"Keine Preisgrenze — teure Karten sind normale fehlende Karten.",
  "wizard.cmTitle":"Cardmarket-Browser-Helfer","wizard.cmDesc":"Optional: ein Userscript, das Single-Angebote auf cardmarket.com danach markiert, ob die Karte schon in deiner Sammlung ist. Jederzeit einrichtbar unter Einstellungen → Cardmarket.",
  "wizard.cmOpen":"Einrichtung öffnen",
  "wizard.collectorTitle":"Welche Art von Sammler bist du?",
  "wizard.collectorDesc":"Das legt fest, wie Binduno die Set-Vervollständigung misst. Jederzeit änderbar unter Einstellungen → Set-Ziele.",
  "lang.appLanguage":"App-Sprache",
  "lang.appLanguageDesc":"Menüs, Buttons und Beschriftungen. Nur auf diesem Gerät gespeichert.",
  "lang.cardSetNames":"Karten- und Set-Namen",
  "lang.cardSetNamesDesc":"In welcher Sprache Karten- und Set-Namen angezeigt werden — unabhängig von der App-Sprache oben.",
  "lang.current":"Aktuell",
  "lang.cardLangEnDesc":"Karten- und Set-Namen wie auf Englisch gedruckt.",
  "lang.cardLangDeDesc":"Kartennamen wie auf den deutschen Karten gedruckt, sofern eine "+
    "deutsche Druckversion existiert. Set-Namen bleiben englisch — Scryfall liefert keinen "+
    "lokalisierten Set-Namen, auch nicht auf deutschen Karten.",
  "design.theme":"Design","design.themeDesc":"Wie die App aussieht. Nur auf diesem Gerät gespeichert.",
  "design.dark":"Dunkel","design.darkDesc":"Der Standard-Look.",
  "design.light":"Hell","design.lightDesc":"Ein helles Design für Tageslicht.",
  "design.colorblind":"Farbenblind-freundlich",
  "design.colorblindDesc":"Bleibt beim dunklen Design, tauscht aber Legal/Banned und "+
    "Fertig/Offen von Grün-vs-Rot auf Blau-vs-Orange — das bleibt bei den gängigen "+
    "Rot-Grün-Sehschwächen unterscheidbar.",
  "shipPref.countryTitle":"Land","shipPref.countryDesc":"Inlandsversandtarife (gleiches Land "+
    "zu gleichem Land), direkt aus Cardmarkets eigenem Versandkostenrechner übernommen.",
  "shipPref.countryRates":"Aktuell: {untracked} ungetrackt, {tracked} getrackt pro Bestellung.",
  "shipPref.title":"Versandoption","shipPref.desc":"Wie der Versand für Preisschätzungen "+
    "berechnet wird (Set-Liste, Fehlende kaufen, Wantlist-Cart). Ändert nur die in der App "+
    "angezeigte Schätzung, nicht was du beim Cardmarket-Checkout tatsächlich wählst.",
  "shipPref.standard":"Standard","shipPref.standardDesc":"Cardmarkets günstigste Option je "+
    "nach Bestellwert — ungetrackter Brief bis 25 € Bestellwert, getrackt darüber "+
    "(Cardmarket verlangt überall Tracking ab 25 €).",
  "shipPref.tracked":"Nur getrackt","shipPref.trackedDesc":"Schätzt immer den getrackten "+
    "Versandtarif des gewählten Landes — für alle, die grundsätzlich nur getrackten "+
    "Versand wollen.",
  "manageUpdate.importTitle":"Sammlung importieren",
  "manageUpdate.replace":"Ersetzen",
  "manageUpdate.replaceDesc":"Gespeicherte Sammlung löschen und diese Datei als neue Wahrheit verwenden.",
  "manageUpdate.add":"Hinzufügen",
  "manageUpdate.addDesc":"Gespeichertes behalten und diese Mengen obendrauf addieren.",
  "manageUpdate.importBtn":"Sammlung importieren","manageUpdate.importing":"Importiert…",
  "busy.importing":"Sammlung wird importiert…","busy.clearing":"Sammlung wird gelöscht…",
  "busy.recount":"Set-Summen werden neu berechnet — gleich fertig…",
  "manageUpdate.importedMsg":"{n} Karten importiert, Modus {mode}.",
  "manageUpdate.modeReplace":"Ersetzen","manageUpdate.modeAdd":"Hinzufügen",
  "manageUpdate.cardDataTitle":"Kartendaten",
  "manageUpdate.cardDataDesc":"Namen, Seltenheiten, Set-Struktur und Cardmarket-Preise "+
    "kommen von Scryfall. Alle paar Wochen aktualisieren, oder nach einem neuen Set.",
  "manageUpdate.downloadBtn":"Neueste Kartendaten herunterladen",
  "manageUpdate.autoSync":"Automatisch einmal täglich synchronisieren",
  "manageUpdate.autoSyncDesc":"Prüft im Hintergrund stündlich, ob die Kartendaten älter als "+
    "24 Stunden sind, und lädt dann eine frische Kopie herunter (derselbe Download wie der "+
    "Button oben) — vor allem um Cardmarket-Preise aktuell zu halten. Läuft nur, solange "+
    "Binduno geöffnet ist; bei geschlossener App passiert nichts.",
  "manageUpdate.priceHistoryTitle":"Preishistorie",
  "manageUpdate.priceHistoryDesc":"Bei jeder Aktualisierung der Kartendaten (manuell oder "+
    "automatisch) wird jeder seit der letzten Prüfung geänderte Cardmarket-Preis mit "+
    "heutigem Datum geloggt. Einträge, die älter als ein Jahr sind, werden auf einen "+
    "Punkt pro Woche ausgedünnt, damit die Datei nicht unbegrenzt wächst.",
  "manageUpdate.priceLogging":"Preishistorie mitloggen",
  "manageUpdate.priceLoggingDesc":"Deaktivieren, um das Aufzeichnen von Preisänderungen "+
    "komplett zu stoppen. Bisherige Historie bleibt erhalten, es kommt nur nichts Neues dazu.",
  "manageUpdate.backfillBtn":"90 Tage Preishistorie von MTGJSON laden",
  "manageUpdate.backfillDesc":"Nachhol-Download (~180 MB, wird danach wieder gelöscht — "+
    "nur die daraus erzeugten Preise bleiben), der echte vergangene Cardmarket-Preise der "+
    "letzten 90 Tage einträgt, statt wochenlang auf die eigene Protokollierung zu warten. "+
    "Binduno macht das bereits von selbst — einmal nach einer frischen Installation, und "+
    "danach automatisch immer dann, wenn es eine Lücke von 2+ Tagen im Log bemerkt (d.h. "+
    "die App lief eine Weile nicht) — dieser Button erzwingt es nur sofort, statt auf die "+
    "stündliche Prüfung zu warten. Bereits geloggte Tage bleiben dabei immer unberührt.",
  "manageUpdate.backfillDone":"Preishistorie nachgeladen.",
  "manageUpdate.backupTitle":"Backup",
  "manageUpdate.backupDesc":"Alles Besessene als CSV im ManaBox-Spaltenformat exportieren, "+
    "zum Re-Import hier oder in ManaBox.",
  "manageUpdate.exportBtn":"Sammlung als CSV exportieren",
  "manageUpdate.dangerZone":"Gefahrenzone","manageUpdate.clearBtn":"Gespeicherte Sammlung löschen",
  "manageUpdate.confirmClear":"Wirklich jede Karte aus der gespeicherten Sammlung entfernen?",
  "manageUpdate.cardDataUpToDate":"Kartendaten aktuell.",
  "manageUpdate.failed":"Fehlgeschlagen: {err}",
  "manageSets.title":"Welche Sets in die Gesamtwerte einfließen",
  "manageSets.desc":"Ausgeschlossene Sets bleiben in der Sammlung sichtbar, fließen aber in "+
    "keinen Prozentwert, keine Kosten und kein Diagramm ein. Standardmäßig ausgeschlossen: "+
    "Promos, Token, Memorabilia, Un-Sets und alles Digitale.",
  "manageSets.searchPlaceholder":"Set suchen…",
  "manageSets.includeEverything":"Alles einschließen","manageSets.restoreDefaults":"Standard wiederherstellen",
  "manageSets.ofCounted":"{on} von {total} gezählt",
  "manageSets.excludeAll":"Alle ausschließen","manageSets.includeAll":"Alle einschließen",
  "manageSets.counted":"Gezählt ✓","manageSets.excluded":"Ausgeschlossen ✕",
  "manageApp.wizardTitle":"Setup-Assistent","manageApp.wizardDesc":"Die anfängliche "+
    "Einrichtung (Sprache, Land, Sammlungsimport, Kartendatenbank) jederzeit erneut "+
    "durchlaufen.",
  "manageApp.wizardBtn":"Setup-Assistent erneut starten",
  "manageApp.title":"App aktualisieren",
  "manageApp.desc":"Wähle eine neuere binduno.py. Sie wird auf Syntaxfehler geprüft, "+
    "die laufende Datei als <code>binduno_previous.py</code> gesichert, dann ersetzt "+
    "sich die App selbst und startet neu. Kein Terminal, keine Neuinstallation.",
  "manageApp.filePickerNote":"binduno.py auswählen — der Dateiauswahl-Dialog zeigt alle "+
    "Dateien, da macOS keinen Dateityp für .py registriert hat",
  "manageApp.installBtn":"Update installieren","manageApp.installing":"Installiert…",
  "manageApp.rebuildTitle":"Die macOS-App neu bauen",
  "manageApp.rebuildDesc":"Falls Icon oder Starter je kaputtgehen, einmal "+
    "<code>python3 binduno.py --install-app</code> im Terminal ausführen.",
  "manageApp.whereThingsLive":"Wo alles liegt",
  "manageApp.database":"Datenbank","manageApp.logFile":"Logdatei",
  "manageApp.updatedMsg":"Aktualisiert {from} → {to}. Startet neu…",
  "manageApp.restartSlow":"Neustart dauert lange. Öffne Binduno.app erneut.",
  "help.basics":"Grundlagen","help.prices":"Preise","help.shipping":"Versand",
  "help.rules":"Zählregeln","help.data":"Deine Daten",
  "history.title":"Änderungsverlauf","history.desc":"Die letzten 100 Änderungen an deinen gespeicherten Daten.",
  "history.none":"Noch keine Änderungen aufgezeichnet.",
  "tip.default":"So wird das berechnet",
  "common.addToCart":"Zum Wantlist-Cart hinzufügen","common.addedCount":"{n} hinzugefügt",
  "common.shipNote":"Versand geschätzt: ca. {cps} Karten pro Verkäufer, dann Cardmarket-"+
    "Brieftarife (bis 17 Karten 1,40 €, bis 40 Karten 2,10 €) oder 5,00 € versichert ab "+
    "25 € Bestellwert. Für die genaue Rechnung über eine Versandangabe hovern.",
  "setPage.ownedOfTotal":"{owned} von {total} besessen",
  "setPage.addAllMissing":"Alle fehlenden zum Wantlist-Cart hinzufügen",
  "setPage.buyMissingDots":"Fehlende kaufen…",
  "setPage.thType":"Typ","setPage.thFoil":"Foil","setPage.thCopies":"Kopien",
  "setPage.thOwned":"In Besitz","setPage.thNote":"Notiz","setPage.yes":"ja","setPage.no":"nein",
  "buyPage.title":"Fehlende Karten kaufen",
  "buyPage.desc":"{setName} — {n} {cards}, die du in keinem Druck dieses Sets besitzt. "+
    "Preise sind Cardmarket-Trendpreise, nicht das günstigste Angebot.",
  "buyPage.cardSingular":"Kartenname","buyPage.cardPlural":"Kartennamen",
  "buyPage.pickGroup":"Gruppe wählen, um sie direkt in den Wantlist-Cart zu legen.",
  "buyPage.allMissing":"Alle fehlenden Karten",
  "buyPage.cardsCount":"{n} Karten","buyPage.cardsOnlyTip":"Cardmarket-Trendpreise, kein Versand.",
  "buyPage.cardsOnlyTipTitle":"Nur Karten","buyPage.breadcrumbBuyMissing":"Fehlende kaufen",
  "cardPage.back":"← Zurück","cardPage.noImage":"Kein Bild verfügbar",
  "cardPage.buyOnCardmarket":"Auf Cardmarket kaufen · {price}","cardPage.buyFoil":"Foil kaufen · {price}",
  "cardPage.viewOnScryfall":"Auf Scryfall ansehen","cardPage.regular":"Normal",
  "cardPage.copiesOwned":"Kopien in Besitz","cardPage.yourCollection":"Deine Sammlung",
  "cardPage.nonfoil":"Nonfoil","cardPage.setTo4":"Auf 4 Kopien setzen",
  "cardPage.wantListEntry":"Wantlist-Eintrag","cardPage.set":"Set",
  "cardPage.illustratedBy":"Illustriert von {artist}","cardPage.formatLegality":"Format-Legalität",
  "cardPage.allPrintings":"Alle Drucke",
  "setPage.noteEndgame":"Endgame","setPage.noteOtherPrinting":"Anderer Druck",
  "setPage.notInGoal":"zählt nicht","setPage.baseMissing":"Basis fehlt",
  "setPage.onlyExtra":"{n} Karte(n) nur als Sonderdruck vorhanden — Basis-Druck fehlt noch",
  "cardPage.added":"Hinzugefügt",
  "cardPage.addToWatchlist":"Zur Watchlist hinzufügen","cardPage.inWatchlist":"★ In Watchlist",
  "browse.searching":"Suche…","browse.nothingMatches":"Nichts gefunden",
  "browse.loosenFilter":"Filter lockern oder Suche zurücksetzen.",
  "browse.resetSearch":"Suche zurücksetzen","browse.cardName":"Kartenname",
  "browse.searchByName":"Nach Name suchen","browse.ownership":"Besitzstatus",
  "browse.all":"Alle","browse.owned":"In Besitz","browse.missing":"Fehlend",
  "browse.missingEverySet":"In keinem Set vorhanden (neue Kartennamen)",
  "browse.rulesText":"Regeltext","browse.searchCardText":"Kartentext durchsuchen",
  "browse.artist":"Künstler","browse.searchByArtist":"Nach Künstler suchen",
  "browse.typeLine":"Typzeile","browse.typeLinePlaceholder":"z.B. Creature, Instant",
  "browse.colors":"Farben","browse.colorWhite":"Weiß","browse.colorBlue":"Blau",
  "browse.colorBlack":"Schwarz","browse.colorRed":"Rot","browse.colorGreen":"Grün",
  "browse.colorless":"Farblos","browse.includesColors":"Enthält diese Farben",
  "browse.onlyColors":"Nur diese Farben","browse.exactColors":"Exakt diese Farben",
  "browse.any":"Beliebig","browse.priceRange":"Preisbereich (€)","browse.from":"von","browse.to":"bis",
  "browse.options":"Optionen","browse.oneRowPerName":"Eine Zeile pro Kartenname",
  "browse.baseSetOnly":"Nur Basisset-Drucke",
  "browse.onlyNoPrice":"Nur Karten ohne Preis",
  "browse.includeExcludedSets":"Karten aus ausgeschlossenen Sets einschließen",
  "browse.sortCollectorNumber":"Sammler-Nummer","browse.sortManaValue":"Manawert",
  "browse.sortCopiesOwned":"Kopien in Besitz",
  "wizard.recoBtn":"Empfohlene Einstellungen übernehmen",
  "wizard.recoApplying":"Wird übernommen…",
  "wizard.manualBtn":"Alles selbst einrichten",
  "wizard.recoNote":"Empfohlen: App-Sprache aus dem Browser, englische Kartennamen, "+
    "Versandland aus der Browser-Region, günstigster Versand, tägliche Kartendaten-"+
    "Synchronisierung und Preishistorie an, ein Druck pro Kartenname, keine Preisgrenze, "+
    "dunkles Design. Alles davon bleibt in den Einstellungen änderbar.",
  "manageUpdate.formatLabel":"Dateiformat",
  "manageUpdate.formatAuto":"Automatisch erkennen",
  "manageUpdate.detected":"Erkannt: {fmt}",
  "phone.tab":"Handy",
  "phone.title":"Auf dem Handy öffnen",
  "phone.desc":"Binduno lauscht auch im lokalen Netzwerk — jedes Gerät im selben WLAN "+
    "kann es öffnen. Scanne den Code mit der Handy-Kamera oder tippe die Adresse ein.",
  "phone.hint":"Funktioniert nur, solange dieser Rechner Binduno ausführt und beide "+
    "Geräte im selben Netz sind. Die Adresse kann sich ändern, wenn sich der Rechner "+
    "neu ins WLAN einbucht.",
  "phone.noLan":"Keine lokale Netzwerkadresse gefunden — dieser Rechner ist evtl. "+
    "offline oder nur über localhost erreichbar.",
  "cm.statusSeen":"Helfer zuletzt gesehen {ago}.",
  "cm.statusNever":"Helfer noch nicht erkannt — öffne eine Cardmarket-Händlerseite mit "+
    "installiertem Userscript.",
  "cm.seenNow":"gerade eben","cm.seenMin":"vor {n} Min","cm.seenHour":"vor {n} Std",
  "cm.seenDay":"vor {n} Tg",
  "cm.testBtn":"Status aktualisieren",
  "costs.title":"Kostenschätzungen",
  "costs.desc":"Standardmäßig aus: Die laufende Summe, was eine vollständige Sammlung noch "+
    "kosten würde, kann am Anfang abschreckend wirken. Einschalten, um „Restkosten“ und "+
    "„Günstigste Fertigstellung“ auf der Startseite zu zeigen. Preise auf der Sammlungs- "+
    "und Fehlende-Karten-Seite (und die Sortierung „Restkosten“) sind so oder so sichtbar.",
  "costs.enable":"Anzeigen, was noch zu kaufen ist (in Euro)",
  "setCard.nMissing":"{n} fehlen",
  "gh.title":"Update von GitHub",
  "gh.desc":"Binduno holt neue Versionen direkt aus seinem GitHub-Repository — das "+
    "neueste Release, oder die Datei im Default-Branch, solange es keine Releases gibt. "+
    "Das Repo ist voreingestellt; nur ändern, wenn du einen eigenen Fork betreibst.",
  "gh.save":"Speichern","gh.check":"Nach Updates suchen","gh.checking":"Prüfe GitHub…",
  "gh.checkFailed":"Prüfung nicht abgeschlossen — die lokale App hat nicht rechtzeitig geantwortet (meist langsames oder fehlendes Netz). Nochmal versuchen, oder unten den Datei-Updater nutzen.",
  "gh.upToDate":"Du hast die neueste Version ({v}).",
  "gh.available":"Version {next} ist verfügbar (du hast {cur}). {name}",
  "gh.install":"{v} herunterladen und installieren","gh.viewRelease":"Release-Notizen auf GitHub",
  "gh.autoCheck":"Bei jedem Start nach Updates suchen",
  "gh.autoInstall":"Updates automatisch installieren",
  "gh.autoNote":"Die Prüfung läuft ein paar Sekunden nach dem Start still im Hintergrund. "+
    "Mit automatischer Installation wird eine neue Version geladen und die App startet sich "+
    "neu — sonst gibt es nur einen Hinweis auf der Startseite.",
  "home.updateAvailable":"Binduno {v} ist verfügbar.",
  "home.updateOpen":"Aktualisieren","home.updateDismiss":"Später",
  "tip.shipping":"Geschätzter Versand",
  "tip.cmExpTitle":"Cardmarket-Erweiterung",
  "tip.cmExp":"Cardmarket führt diesen Druck in einer eigenen Erweiterung, als Version {v}.",
  "tip.cmVerTitle":"Cardmarket-Version",
  "tip.cmVer":"Cardmarket führt diesen Druck als Version {v} dieser Karte in diesem Set.",
  "start.title":"Hier starten","start.dismiss":"Ausblenden","start.hide":"Nicht mehr anzeigen",
  "start.body":"Drei Dinge, wofür Binduno da ist — such dir eins aus, oder lies, wie es denkt.",
  "start.a1":"Sehen, wo meine Sammlung steht","start.a2":"Eine Wantlist bauen",
  "start.a3":"Karten beim Kauf auf Cardmarket markieren","start.a4":"So denkt Binduno",
  "explain.link":"So denkt Binduno",
  "explain.title":"So denkt Binduno","explain.back":"Zurück zur Startseite",
  "explain.intro":"Das ganze Modell in fünf kurzen Punkten.",
  "explain.h1":"Zwei Ziele gleichzeitig",
  "explain.b1":"Kartennamen zählt, ob du irgendeinen Druck einer Karte besitzt — dein „von jedem eins“-Projekt. Drucke zählt jede Set-und-Nummer einzeln — vollständige Set-Vervollständigung. Beides wird nebeneinander gezeigt.",
  "explain.h2":"Du legst fest, was zählt",
  "explain.b2":"Standardmäßig ist ein Set vollständig, wenn du von jedem Kartennamen einen normalen Druck hast. Unter Einstellungen → Vervollständigung kannst du stattdessen jede Sammlernummer verlangen und wählen, ob Showcase / Borderless / serialisierte Drucke mitzählen. Promos, Token und Un-Sets bleiben außen vor.",
  "explain.h3":"Preise sind Cardmarket-Trend",
  "explain.b3":"Werte und „Restkosten“ nutzen Cardmarkets Trendpreis über Scryfall — nicht das günstigste aktuelle Angebot, und ohne Aufschlag für deutsche Verkäufer. Real zahlst du meist etwas weniger. Die Summen auf der Startseite sind standardmäßig aus (Einstellungen → Vervollständigung), Euro-Zahlen anderswo sind immer sichtbar.",
  "explain.h4":"Wantlisten folgen Cardmarkets Regeln",
  "explain.b4":"Der Wantlist-Cart ist der einzige Ort, an dem Wantlist-Text entsteht. Er nutzt Cardmarkets exakte Namen und Klammerreihenfolge, Mengen-Präfixe und teilt in 150er-Blöcke, die du nacheinander einfügst.",
  "explain.h5":"Alles bleibt auf deinem Rechner",
  "explain.b5":"Deine Sammlung liegt in einer lokalen SQLite-Datei. Kein Konto, keine Cloud. Heruntergeladen wird nur Scryfalls Kartendatenbank; der optionale Cardmarket-Helfer liest nur Seiten, die du ohnehin geöffnet hast.",
  "cm.browserLabel":"Dein Browser",
  "cm.optBookmarklet":"Bookmarklet (ohne Erweiterung)",
  "cm.bmHeading":"Bookmarklet — ohne Erweiterung (Chrome &amp; Firefox)",
  "cm.step1b.chrome":"<a href='https://violentmonkey.github.io/' target='_blank' rel='noopener'>Violentmonkey</a> (kostenlos, quelloffen) aus dem Erweiterungs-Store deines Browsers installieren.",
  "cm.step1b.firefox":"<a href='https://addons.mozilla.org/firefox/addon/violentmonkey/' target='_blank' rel='noopener'>Violentmonkey</a> (kostenlos, quelloffen) aus den Firefox-Add-ons installieren.",
  "cm.step1b.safari":"<a href='https://apps.apple.com/app/userscripts/id1463298887' target='_blank' rel='noopener'>Userscripts</a> von Quoid (kostenlos) aus dem Mac App Store installieren.",
  "cm.stepSafariEnable":"In Safari → Einstellungen → Erweiterungen „Userscripts“ einschalten und für cardmarket.com erlauben.",
  "cm.bmIntro":"Nichts zu installieren — dafür musst du es auf jeder Seite einmal anklicken.",
  "cm.bmDrag":"Zieh diesen Link in deine Lesezeichenleiste:",
  "cm.bmClick":"Auf der Singles-Seite eines Cardmarket-Händlers das Lesezeichen anklicken. Es markiert die sichtbaren Zeilen und aktualisiert ~90 Sekunden lang; nach einem Seitenwechsel erneut klicken.",
  "cm.bmNote":"Funktioniert nicht in Safari — Safari blockiert die Verbindung des Bookmarklets zur lokalen App. Unter Safari stattdessen die Erweiterungs-Methode oben nutzen.",
},
};
const $=s=>document.querySelector(s);
const money=n=>(n||0).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2})+" €";
const num=n=>(n||0).toLocaleString("en-US");
const pct=n=>(n*100).toFixed(1)+" %";
const RAR={c:["Common","#7d8896"],u:["Uncommon","#a8b4c2"],r:["Rare","#d4a629"],
           m:["Mythic","#e0692c"],s:["Special","#b49ed0"],b:["Basic land","#6f7a88"]};
const rarLabel=k=>t("rarity."+k);
function isSecretLair(code){const s=SETS.find(x=>x.code===code);return !!s&&/^secret lair/i.test(s.name||"");}
function toast(msg){
  let b=$("#toast");
  if(!b){b=document.createElement("div");b.id="toast";
    b.style.cssText="position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:200;"
      +"background:#0b0e13;color:var(--text);border:1px solid var(--line);border-radius:8px;"
      +"padding:10px 16px;font-size:13.5px;max-width:min(92vw,520px);box-shadow:0 8px 30px rgba(0,0,0,.5)";
    document.body.appendChild(b);}
  b.textContent=msg;b.style.opacity="1";
  clearTimeout(toast._t);toast._t=setTimeout(()=>{b.style.transition="opacity .5s";b.style.opacity="0";},4200);
}
// Full-screen blocking bar for import / clear. Covers the nav too, so clicks
// during the (10-20 s) rebuild are visibly held, not silently dropped. Only
// call busyDone() once load() has finished and the data is ready to render.
function busyStart(msg){
  let b=$("#busy");
  if(!b){b=document.createElement("div");b.id="busy";
    b.innerHTML=`<div class="busybox"><div class="busymsg"></div>
      <div class="prog"><span style="width:6%"></span></div></div>`;
    document.body.appendChild(b);}
  b.querySelector(".busymsg").textContent=msg;
  b.querySelector(".prog span").style.width="6%";
  b.style.display="flex";
  clearInterval(busyStart._t);
  // creep the bar forward while we wait, so it never looks frozen; it caps at
  // 90 % until busyDone() takes it to 100 %.
  let p=6;
  busyStart._t=setInterval(()=>{p=Math.min(90,p+Math.max(1,(90-p)*0.08));
    const s=$("#busy .prog span");if(s)s.style.width=p+"%";},600);
  return b;
}
function busyStep(msg){const m=$("#busy .busymsg");if(m&&msg)m.textContent=msg;}
async function busyDone(){
  clearInterval(busyStart._t);
  const b=$("#busy");if(!b)return;
  const s=b.querySelector(".prog span");if(s)s.style.width="100%";
  await new Promise(r=>setTimeout(r,400));
  b.style.display="none";
}
function updateDismissed(v){try{return localStorage.getItem("bnd_upd_dismiss")===v;}catch(e){return false;}}
function dismissUpdate(v){try{localStorage.setItem("bnd_upd_dismiss",v);}catch(e){}}
let SHOW_COSTS=false;
let RARMODE="names";   // "By rarity" on Home: card-name counts vs. every printing
let FORCE_RELOAD=false;   // set after an import so the next route re-fetches
let SETS=[],STATS=null,PAGE=1,PER=24,VIEW="grid",SORT="totalCost",DIR=1,
    Q="",FLABELS=null,FSTAT="started";

function donut(p,color,size=96){
  const R=40,C=2*Math.PI*R,off=C*(1-Math.min(p,1));
  return `<svg viewBox="0 0 100 100" width="${size}" height="${size}">
    <circle cx="50" cy="50" r="${R}" fill="none" stroke="var(--track)" stroke-width="12"/>
    <circle cx="50" cy="50" r="${R}" fill="none" stroke="${color}" stroke-width="12"
      stroke-dasharray="${C}" stroke-dashoffset="${off}" stroke-linecap="round"
      transform="rotate(-90 50 50)"/>
    <text x="50" y="55" text-anchor="middle" fill="#e8ebef"
      style="font:600 19px var(--sans)">${(p*100).toFixed(0)}%</text></svg>`;
}
const icon=(s,sz=26)=>s.icon?`<img class="seticon" src="${s.icon}" alt="" width="${sz}" height="${sz}" loading="lazy">`
  :`<span style="width:${sz}px;display:inline-block"></span>`;
const kindClass=c=>c.startsWith("Normal")?"normal":c.startsWith("Sealed")?"sealed":"special";

async function getJSON(url){
  const res=await fetch(url,{cache:"no-store"});
  const d=await res.json().catch(()=>({error:"Bad response from the server"}));
  if(!res.ok||d.error)throw new Error(d.error||("HTTP "+res.status));
  return d;
}
async function load(){
  const r=await getJSON("/api/stats");
  STATS=r.stats; window.HAS=r; TRACKED_SHIP=!!r.trackedShipping;
  SHIP_COUNTRY=r.shippingCountry||"DE"; SHIP_RATES=r.shipRates||{};
  AUTO_SYNC=r.autoSync!==false; PRICE_LOGGING=r.priceLogging!==false;
  SHOW_COSTS=!!r.showCosts;
  ONBOARDING_DONE=!!r.onboardingDone;
  SETS=await getJSON("/api/sets");
  await cartLoad();   // so the nav badge shows a pending cart right after a restart
}
function offline(e){
  const msg=(e&&e.message)||"";
  const dead=/Load failed|NetworkError|Failed to fetch/i.test(msg);
  $("#view").innerHTML=dead
   ? `<div class="empty"><h2>No connection to the tracker</h2>
      <p>The local server is not answering. It probably stopped running.</p>
      <p class="sub">Start it again with <code>python3 binduno.py</code>
         or by opening Binduno.app, then reload this page.</p>
      <button class="pri" onclick="location.reload()">Reload</button></div>`
   : `<div class="empty"><h2>Something went wrong</h2>
      <p>The tracker is running, but this request failed.</p>
      <div class="msg err" style="max-width:720px;margin:14px auto;text-align:left">${msg}</div>
      <p class="sub">Full details are in ~/Library/Logs/MTG&nbsp;Tracker.log.</p>
      <button class="pri" onclick="location.reload()">Reload</button></div>`;
}

/* ---------------- Home ---------------- */
function home(){
  if(!window.HAS.hasCards||!window.HAS.hasCollection) return setupPrompt();
  const s=STATS;
  const nm=s.names.owned/Math.max(1,s.names.total), pr=s.printings.owned/Math.max(1,s.printings.total);
  const upd=(window.HAS&&window.HAS.update)||{};
  const showUpd=upd.available&&!updateDismissed(upd.latest);
  let startSeen=true; try{startSeen=localStorage.getItem("bnd_start_seen")==="1";}catch(e){}
  $("#view").innerHTML=`
  <h1>${t("home.title")}</h1>
  <p class="sub">${t("home.updated",{
     cards:s.cardsUpdated?s.cardsUpdated.replace("T"," "):t("home.never"),
     collection:s.collectionUpdated?s.collectionUpdated.replace("T"," "):t("home.never")})}</p>
  ${showUpd?`<div class="msg ok" id="updBanner" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;max-width:760px">
    <span>${t("home.updateAvailable",{v:upd.latest})}</span>
    <button class="pri" id="updGo">${t("home.updateOpen")}</button>
    <button id="updHide">${t("home.updateDismiss")}</button></div>`:""}
  ${startSeen?"":`<div class="card" id="startCard" style="max-width:820px;padding:18px;position:relative">
    <button id="startX" title="${t("start.dismiss")}" style="position:absolute;top:8px;right:8px;padding:2px 8px">✕</button>
    <div class="v" style="font-size:18px;font-family:var(--serif)">${t("start.title")}</div>
    <p class="sub" style="margin:6px 0 12px">${t("start.body")}</p>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button data-sc="collection">${t("start.a1")}</button>
      <button data-sc="cart">${t("start.a2")}</button>
      <button data-sc="cm">${t("start.a3")}</button>
      <button data-sc="explain">${t("start.a4")}</button>
    </div>
    <label class="chk" style="margin:14px 0 0;font-size:12.5px;color:var(--muted)">
      <input type="checkbox" id="startNever"> ${t("start.hide")}</label></div>`}
  <div class="donuts">
    <div class="donut">${donut(nm,"#d4a629")}
      <div><div class="t">${t("home.cardNames")}</div><div class="p">${pct(nm)}</div>
      <div class="s">${t("home.xOfY",{a:num(s.names.owned),b:num(s.names.total)})}</div></div></div>
    <div class="donut">${donut(pr,"#4a90c4")}
      <div><div class="t">${t("home.printings")}</div><div class="p">${pct(pr)}</div>
      <div class="s">${t("home.xOfY",{a:num(s.printings.owned),b:num(s.printings.total)})}</div></div></div>
    <div class="donut">${donut(s.setsComplete/Math.max(1,s.setsTotal),"#4f9d69")}
      <div><div class="t">${t("home.setsCompleted")}</div><div class="p">${s.setsComplete}</div>
      <div class="s">${t("home.ofCountedSets",{n:s.setsTotal})}</div></div></div>
  </div>
  <div class="cards" style="margin-top:12px">
    <div class="card"><div class="k">${t("home.physicalCards")}</div><div class="v">${num(s.physical)}</div>
      <div class="n">${t("home.duplicatesIncluded")}</div></div>
    <div class="card"><div class="k">${t("home.collectionValue")}</div>
      <div class="v" style="color:var(--gold)">${money(s.value)}</div>
      <div class="n">${t("home.cardmarketTrend")}</div></div>
    ${SHOW_COSTS?`<div class="card"><div class="k">${t("home.remainingCost")}</div><div class="v">${money(s.remaining)}</div>
      <div class="n">${t("home.inclShipping",{n:money(s.shipping)})}</div></div>`:""}
    ${(SHOW_COSTS&&window.HAS.endgame&&window.HAS.endgame.on!==false)?`<div class="card" id="bigTicket" style="cursor:pointer"
         title="${t("home.showTheseCards")}">
      <div class="k">${t("home.cardsOver",{eur:Math.round((window.HAS.endgame&&window.HAS.endgame.eur)||300)+" €"})}</div>
      <div class="v" style="color:var(--mythic)">${num(s.endgameCount)}</div>
      <div class="n">${t("home.leftOutOfRemaining",{n:money(s.endgameValue)})}</div></div>`:""}
    <div class="card"><div class="k">${t("home.namesStillMissing")}</div>
      <div class="v">${num(s.names.total-s.names.owned)}</div>
      <div class="n">${t("home.acrossCountedSets")}</div></div>
  </div>

  <h2 style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">${t("home.byRarity")}
    <span class="seg" id="rarMode" style="font-family:var(--sans)">
      <button data-rm="names" class="${RARMODE==="names"?"on":""}">${t("home.cardNames")}</button>
      <button data-rm="printings" class="${RARMODE==="printings"?"on":""}">${t("home.printings")}</button>
    </span></h2>
  <div class="rarbars">${(()=>{const RD=(RARMODE==="names"?s.rarityNames:s.rarity)||{};
     return Object.entries(RAR).filter(([k])=>RD[k]).map(([k,[,col]])=>{
     const d=RD[k],p=d.owned/Math.max(1,d.total);
     return `<div class="rarrow"><div class="lb">${rarLabel(k)}</div>
       <div class="track"><div class="fill" style="width:${p*100}%;background:${col}"></div></div>
       <div class="nm">${num(d.owned)} / ${num(d.total)}</div></div>`;}).join("");})()}</div>

  <h2>${t("home.closestToCompletion")}</h2>
  <div class="list">${s.nearest.map(x=>row(x)).join("")||`<div class="li">${t("home.nothingOpen")}</div>`}</div>

  ${SHOW_COSTS?`<h2>${t("home.cheapestToFinish")}</h2>
  <p class="sub">${t("home.cheapestDesc")}</p>
  <div class="list">${s.cheapest.map(x=>row(x,true)).join("")}</div>`:""}
  <p class="sub" style="margin-top:10px"><a data-go="missing" style="cursor:pointer;
    color:var(--gold);border-bottom:1px dotted">${t("home.shopByName")}</a></p>

  <h2>${t("home.watchlist")}</h2>
  <p class="sub">${t("home.watchlistDesc")}</p>
  <div id="watchlistOut"><p class="sub">${t("missing.loading")}</p></div>`;
  document.querySelectorAll("[data-rm]").forEach(b=>b.onclick=()=>{RARMODE=b.dataset.rm;home();});
  if($("#updGo"))$("#updGo").onclick=()=>{SUB="about";ABOUT_SUB="app";go("manage");};
  if($("#updHide"))$("#updHide").onclick=()=>{dismissUpdate(upd.latest);const b=$("#updBanner");if(b)b.remove();};
  const seeStart=()=>{try{localStorage.setItem("bnd_start_seen","1");}catch(e){}};
  if($("#startX"))$("#startX").onclick=()=>{seeStart();const c=$("#startCard");if(c)c.remove();};
  if($("#startNever"))$("#startNever").onchange=e=>{
    if(e.target.checked){seeStart();const c=$("#startCard");if(c)c.remove();}};
  document.querySelectorAll("[data-sc]").forEach(b=>b.onclick=()=>{seeStart();
    const d=b.dataset.sc;
    if(d==="cm"){SUB="cm";go("manage");}
    else go(d);});
  bindRows();
  if($("#bigTicket"))$("#bigTicket").onclick=()=>{
    CMODE="cards";
    CF={...CF,q:"",text:"",artist:"",type:"",rarity:"",colors:[],owned:"newname",
        unique:"1",baseonly:"1",allsets:"0",minprice:"300",maxprice:"",
        sort:"price",dir:-1,page:1};
    go("collection");
  };
  drawWatchlist();
}
function sparkline(vals){
  const w=100,h=28,pad=3;
  const nums=vals.filter(v=>v!=null);
  if(nums.length<2)return "";
  const min=Math.min(...nums),max=Math.max(...nums),range=(max-min)||1;
  const step=(w-2*pad)/(vals.length-1);
  const color=nums[nums.length-1]>nums[0]?"var(--ok)"
    :nums[nums.length-1]<nums[0]?"var(--bad)":"var(--muted)";
  const pts=vals.map((v,i)=>`${(pad+i*step).toFixed(1)},${
    (h-pad-((v-min)/range)*(h-2*pad)).toFixed(1)}`).join(" ");
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6"
      stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}
async function drawWatchlist(){
  const out=$("#watchlistOut");
  if(!out)return;
  const r=await getJSON("/api/watchlist");
  if(!r.items.length){
    out.innerHTML=`<div class="empty"><p>${t("home.watchlistEmpty")}</p></div>`;
    return;
  }
  out.innerHTML=`<div class="tscroll"><table><thead><tr><th>${t("missing.thCard")}</th><th>${t("cardPage.set")}</th>
      <th class="num">${t("missing.thPrice")}</th><th>${t("home.watchlist7d")}</th>
      <th class="num">${t("home.watchlistChange")}</th><th class="num"></th></tr></thead>
    <tbody>${r.items.map(c=>`<tr>
      <td><span class="setlink" data-card="${c.set}|${c.number}">${cardName(c)}</span></td>
      <td><span class="setlink" data-set="${c.set}">${c.setName}</span></td>
      <td class="num" style="color:var(--gold)">${c.eur?money(c.eur):"—"}</td>
      <td>${sparkline(c.series)}</td>
      <td class="num">${c.changePct==null?"—":
        `${c.changeEur>0?"+":""}${money(c.changeEur)} <span class="mt">(${
          c.changePct>0?"+":""}${c.changePct.toFixed(1)}%)</span>`}</td>
      <td class="num"><button data-unwatch="${c.set}|${c.number}"
        title="${t("home.watchlistRemove")}" style="padding:3px 9px;font-size:12px">✕</button></td>
    </tr>`).join("")}</tbody></table></div>
    <p class="sub" style="margin-top:8px">${t("home.watchlistCount",{n:r.items.length,max:r.max})}</p>`;
  bindSetLinks();bindTiles();
  document.querySelectorAll("[data-unwatch]").forEach(b=>b.onclick=async()=>{
    const [sc,nr]=b.dataset.unwatch.split("|");
    await fetch("/api/watchlist",{method:"POST",
      body:JSON.stringify({action:"remove",set:sc,number:nr})});
    drawWatchlist();
  });
}
const row=(x,cost)=>`<div class="li" data-code="${x.code}">${icon(x,19)}
  <span class="nm">${x.name}</span>
  <span class="bar"><span style="width:${x.pct*100}%"></span></span>
  <span class="mt" style="flex:0 0 86px;text-align:right">${x.owned}/${x.total}</span>
  <span class="mt" style="color:var(--gold);flex:0 0 84px;text-align:right">${
    cost?money(x.totalCost):pct(x.pct)}</span></div>`;
function bindRows(){document.querySelectorAll(".li[data-code]").forEach(e=>
  e.onclick=()=>openSet(e.dataset.code));bindCrumbs();}

function setupPrompt(){
  $("#view").innerHTML=`<div class="empty"><h2>${t("home.nothingLoaded")}</h2>
  <p>${t("home.nothingLoadedDesc")}</p>
  <button class="pri" onclick="go('manage')">${t("home.goToManage")}</button>
  <p class="sub" style="margin-top:18px"><a data-go="explain" style="cursor:pointer;color:var(--gold);border-bottom:1px dotted">${t("explain.link")}</a></p></div>`;
  document.querySelectorAll("[data-go]").forEach(a=>a.onclick=()=>go(a.dataset.go));
}
function explainPage(){
  CRUMBS=[];
  const P=[1,2,3,4,5].map(n=>`<div class="card" style="padding:18px">
    <div class="v" style="font-size:17px;font-family:var(--serif)">${t("explain.h"+n)}</div>
    <p class="sub" style="margin:8px 0 0">${t("explain.b"+n)}</p></div>`).join("");
  $("#view").innerHTML=`${crumbs([{label:t("nav.home"),hash:"home"},{label:t("explain.title")}])}
    <h1>${t("explain.title")}</h1>
    <p class="sub" style="max-width:640px">${t("explain.intro")}</p>
    <div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr));max-width:900px">${P}</div>
    <p class="sub" style="margin-top:20px"><button class="pri" onclick="go('home')">${t("explain.back")}</button></p>`;
  bindCrumbs();
}

/* ---------------- Setup wizard ---------------- */
const WIZ_STEPS=["welcome","lang","country","import","carddata","collector","endgame","cmhelper","done"];
let WIZ_STEP=0, WIZ_IMPORT_TEXT=null;
function wizardNav(showBack,showSkipStep){
  return `<div style="display:flex;gap:8px;margin-top:18px;align-items:center">
    ${showBack?`<button data-wiz-back>${t("wizard.back")}</button>`:""}
    ${showSkipStep?`<button data-wiz-next>${t("wizard.skipStep")}</button>`:""}
    <span style="flex:1"></span>
    <a data-wiz-skip style="cursor:pointer;color:var(--muted);
       border-bottom:1px dotted;font-size:13px">${t("wizard.skipAll")}</a></div>`;
}
function wizardPage(){
  CRUMBS=[];
  const step=WIZ_STEPS[WIZ_STEP];
  let body="";
  if(step==="welcome"){
    body=`<h1>${t("wizard.welcomeTitle")}</h1>
      <p class="sub">${t("wizard.welcomeBody1")}</p>
      <p class="sub">${t("wizard.welcomeBody2")}</p>
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:8px">
        <button class="pri" data-wiz-reco>${t("wizard.recoBtn")}</button>
        <button data-wiz-next>${t("wizard.manualBtn")}</button></div>
      <p class="sub" style="margin-top:12px;font-size:12px;max-width:560px">${t("wizard.recoNote")}</p>`;
  }else if(step==="lang"){
    const UI_LANGS=[["en","English"],["de","Deutsch"]];
    const CARD_LANGS=[["en","English",t("lang.cardLangEnDesc")],
      ["de","Deutsch",t("lang.cardLangDeDesc")]];
    body=`<h1>${t("wizard.langTitle")}</h1>
      <h2 style="margin-top:0">${t("lang.appLanguage")}</h2>
      <p class="sub">${t("lang.appLanguageDesc")}</p>
      <div class="seg" id="uiLangSeg">${UI_LANGS.map(([id,label])=>
        `<button data-ui="${id}" class="${LANG===id?"on":""}">${label}</button>`).join("")}</div>
      <h2>${t("lang.cardSetNames")}</h2>
      <p class="sub">${t("lang.cardSetNamesDesc")}</p>
      <div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">${
        CARD_LANGS.map(([id,label,desc])=>`<div class="card" data-cl="${id}"
          style="cursor:pointer;${CARDLANG===id?"border-color:var(--gold)":""}">
          <div class="k">${CARDLANG===id?t("lang.current"):""}</div>
          <div class="v" style="font-size:16px">${label}</div>
          <div class="n">${desc}</div></div>`).join("")}</div>
      <button class="pri" data-wiz-next style="margin-top:18px">${t("wizard.next")}</button>${wizardNav(true)}`;
  }else if(step==="country"){
    const countries=Object.entries(SHIP_RATES).sort((a,b)=>a[1].name.localeCompare(b[1].name));
    const cur=SHIP_RATES[SHIP_COUNTRY];
    body=`<h1>${t("wizard.countryTitle")}</h1>
      <p class="sub">${t("shipPref.countryDesc")}</p>
      <select id="shipCountry">${countries.map(([code,r])=>
        `<option value="${code}" ${code===SHIP_COUNTRY?"selected":""}>${r.name}</option>`).join("")}</select>
      ${cur?`<p class="sub">${t("shipPref.countryRates",
        {untracked:money(cur.untracked),tracked:money(cur.tracked)})}</p>`:""}
      <button class="pri" data-wiz-next style="margin-top:12px">${t("wizard.next")}</button>${wizardNav(true)}`;
  }else if(step==="import"){
    body=`<h1>${t("wizard.importTitle")}</h1>
      <p class="sub">${t("wizard.importDesc")}</p>
      <div class="drop" id="drop"><input type="file" id="file" accept=".csv"></div>
      <label class="sub" style="display:block;margin:10px 0 4px">${t("manageUpdate.formatLabel")}</label>
      <select id="fmt">
        <option value="auto">${t("manageUpdate.formatAuto")}</option>
        <option value="manabox">ManaBox</option>
        <option value="moxfield">Moxfield</option>
        <option value="archidekt">Archidekt</option></select>
      <button id="imp" class="pri" disabled style="margin-top:10px">${t("manageUpdate.importBtn")}</button>
      <div id="impMsg"></div>${wizardNav(true,true)}`;
  }else if(step==="carddata"){
    body=`<h1>${t("wizard.cardDataTitle")}</h1>
      <p class="sub">${t("wizard.cardDataDesc")}</p>
      <button id="wizRef" class="pri">${t("wizard.cardDataBtn")}</button>
      <div class="prog" style="display:none" id="pw"><span id="pb" style="width:0%"></span></div>
      <div id="refMsg" class="sub"></div>${wizardNav(true,true)}`;
  }else if(step==="collector"){
    body=`<h1>${t("wizard.collectorTitle")}</h1>
      <p class="sub">${t("wizard.collectorDesc")}</p>
      ${goalPresetCards()}
      <button class="pri" data-wiz-next style="margin-top:18px">${t("wizard.next")}</button>${wizardNav(true)}`;
  }else if(step==="endgame"){
    const e=(window.HAS&&window.HAS.endgame)||{on:false,eur:300};
    const eur=Math.round(e.eur)||300;
    body=`<h1>${t("wizard.endgameTitle")}</h1>
      <p class="sub">${t("wizard.endgameDesc")}</p>
      <div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
        <div class="card" data-eg="on" style="cursor:pointer;${e.on!==false?"border-color:var(--gold)":""}">
          <div class="k">${e.on!==false?t("lang.current"):""}</div>
          <div class="v" style="font-size:15px">${t("wizard.egSetAside",{eur:money(eur)})}</div>
          <div class="n">${t("wizard.egSetAsideDesc")}</div></div>
        <div class="card" data-eg="off" style="cursor:pointer;${e.on===false?"border-color:var(--gold)":""}">
          <div class="k">${e.on===false?t("lang.current"):""}</div>
          <div class="v" style="font-size:15px">${t("wizard.egCountAll")}</div>
          <div class="n">${t("wizard.egCountAllDesc")}</div></div>
      </div>
      <div id="egRow" style="margin-top:12px;${e.on!==false?"":"opacity:.4;pointer-events:none"}">
        <label class="sub" style="display:block;margin-bottom:4px">${t("endgame.threshold")}</label>
        <select id="egEur">${EG_STEPS.map(v=>`<option value="${v}" ${eur===v?"selected":""}>${money(v)}</option>`).join("")}</select>
      </div>
      <button class="pri" data-wiz-next style="margin-top:18px">${t("wizard.next")}</button>${wizardNav(true)}`;
  }else if(step==="cmhelper"){
    body=`<h1>${t("wizard.cmTitle")}</h1>
      <p class="sub">${t("wizard.cmDesc")}</p>
      <button class="pri" data-wiz-next style="margin-top:8px">${t("wizard.next")}</button>${wizardNav(true)}`;
  }else if(step==="done"){
    body=`<h1>${t("wizard.doneTitle")}</h1>
      <p class="sub">${t("wizard.doneBody")}</p>
      <button class="pri" data-wiz-finish>${t("wizard.finish")}</button>`;
  }
  $("#view").innerHTML=`<div class="wizard" style="max-width:640px">${body}</div>`;
  wizardBind(step);
}
function wizardGoto(i){WIZ_STEP=Math.max(0,Math.min(WIZ_STEPS.length-1,i));wizardPage();}
async function wizardFinish(){
  await fetch("/api/onboarding",{method:"POST",body:JSON.stringify({done:true})});
  ONBOARDING_DONE=true;
  go("home");
}
function wizardBind(step){
  document.querySelectorAll("[data-wiz-back]").forEach(b=>b.onclick=()=>wizardGoto(WIZ_STEP-1));
  document.querySelectorAll("[data-wiz-skip]").forEach(b=>b.onclick=wizardFinish);
  document.querySelectorAll("[data-wiz-finish]").forEach(b=>b.onclick=wizardFinish);
  document.querySelectorAll("[data-wiz-next]").forEach(b=>b.onclick=()=>wizardGoto(WIZ_STEP+1));
  if(step==="welcome"){
    const rb=document.querySelector("[data-wiz-reco]");
    if(rb) rb.onclick=async()=>{
      rb.disabled=true; rb.textContent=t("wizard.recoApplying");
      const nav=navigator.language||"en";
      const lng=nav.toLowerCase().slice(0,2)==="de"?"de":"en";
      const reg=(nav.split("-")[1]||"").toUpperCase();
      const rates=(window.HAS&&window.HAS.shipRates)||{};
      const country=rates[reg]?reg:"DE";
      setLang(lng); setCardLang("en"); setTheme("dark");
      await fetch("/api/shipping-pref",{method:"POST",body:JSON.stringify({country,trackedOnly:false})});
      await fetch("/api/auto-sync-pref",{method:"POST",body:JSON.stringify({enabled:true})});
      await fetch("/api/price-logging-pref",{method:"POST",body:JSON.stringify({enabled:true})});
      await fetch("/api/goal-pref",{method:"POST",body:JSON.stringify({scope:"names",extras:"exclude",serialized:"exclude"})});
      await fetch("/api/endgame-pref",{method:"POST",body:JSON.stringify({on:false})});
      await load();
      wizardGoto(WIZ_STEPS.indexOf("import"));
    };
  }
  if(step==="lang"){
    document.querySelectorAll("[data-ui]").forEach(b=>b.onclick=()=>{setLang(b.dataset.ui);wizardPage();});
    document.querySelectorAll("[data-cl]").forEach(b=>b.onclick=()=>{setCardLang(b.dataset.cl);wizardPage();});
  }else if(step==="collector"){
    document.querySelectorAll("[data-goalp]").forEach(el=>el.onclick=async()=>{
      const p=GOAL_PRESETS.find(x=>x[0]===el.dataset.goalp)[1];
      await saveGoal(p);wizardPage();});
  }else if(step==="endgame"){
    const put=async body=>{
      const r=await fetch("/api/endgame-pref",{method:"POST",body:JSON.stringify(body)}).then(r=>r.json());
      if(r.endgame&&window.HAS)window.HAS.endgame=r.endgame;
      await load();wizardPage();};
    document.querySelectorAll("[data-eg]").forEach(el=>el.onclick=()=>
      put({on:el.dataset.eg==="on", eur:+$("#egEur").value}));
    if($("#egEur")) $("#egEur").onchange=()=>put({on:true, eur:+$("#egEur").value});
  }else if(step==="country"){
    $("#shipCountry").onchange=async()=>{
      await fetch("/api/shipping-pref",{method:"POST",
        body:JSON.stringify({country:$("#shipCountry").value})});
      await load();wizardPage();};
  }else if(step==="import"){
    $("#file").onchange=e=>{
      const f=e.target.files[0];if(!f)return;
      const rd=new FileReader();
      rd.onload=()=>{WIZ_IMPORT_TEXT=rd.result;$("#imp").disabled=false;
        $("#drop").classList.add("ok");};
      rd.readAsText(f);
    };
    $("#imp").onclick=async()=>{
      $("#imp").disabled=true;$("#imp").textContent=t("manageUpdate.importing");
      busyStart(t("busy.importing"));
      let r;
      try{
        r=await fetch("/api/import",{method:"POST",
          body:JSON.stringify({csv:WIZ_IMPORT_TEXT,mode:"replace",
            format:($("#fmt")&&$("#fmt").value)||"auto"})}).then(r=>r.json());
        if(r.ok){busyStep(t("busy.recount"));FORCE_RELOAD=true;await load();}
      }catch(e){ r={ok:false,error:String(e)}; }
      await busyDone();
      $("#imp").textContent=t("manageUpdate.importBtn");
      $("#impMsg").innerHTML=r.ok
        ? `<div class="msg ok">${t("manageUpdate.importedMsg",{n:num(r.cards),
            mode:t("manageUpdate.modeReplace")})} · ${t("manageUpdate.detected",{fmt:r.formatLabel})}</div>`
        : `<div class="msg err">${r.error}</div>`;
      if(r.ok){setTimeout(()=>wizardGoto(WIZ_STEP+1),700);}
      else $("#imp").disabled=false;
    };
  }else if(step==="carddata"){
    $("#wizRef").onclick=async()=>{
      if(!confirm(t("wizard.cardDataConfirm")))return;
      $("#wizRef").disabled=true;
      await fetch("/api/refresh-cards",{method:"POST"});
      $("#pw").style.display="block";
      (async function poll(){
        const s=await fetch("/api/refresh-status").then(r=>r.json());
        $("#pb").style.width=s.pct+"%";
        $("#refMsg").textContent=s.error?t("manageUpdate.failed",{err:s.error}):s.step;
        if(s.running){setTimeout(poll,700);return;}
        $("#wizRef").disabled=false;
        if(!s.error){await load();setTimeout(()=>wizardGoto(WIZ_STEP+1),700);}
      })();
    };
  }
}

/* ---------------- Collection ---------------- */
let CMODE="sets",FAMILY=false,HIDEEXC=true;
const modeSegHTML=()=>`<div class="seg" id="modeSeg" style="margin:6px 0 14px">
    <button data-cm="sets" class="${CMODE==="sets"?"on":""}">${t("collection.viewSets")}</button>
    <button data-cm="cards" class="${CMODE==="cards"?"on":""}">${t("collection.viewCards")}</button>
    <button data-cm="missing" class="${CMODE==="missing"?"on":""}">${t("nav.missing")}</button></div>`;
function bindModeSeg(){document.querySelectorAll("[data-cm]").forEach(b=>b.onclick=()=>{
  CMODE=b.dataset.cm;
  ({sets:collection,cards:cardsView,missing:missingView})[CMODE]();});}
function collection(){
  if(!window.HAS.hasCards) return setupPrompt();
  if(CMODE==="cards") return cardsView();
  if(CMODE==="missing") return missingView();
  $("#view").innerHTML=`<h1>${t("collection.title")}</h1>
  ${modeSegHTML()}
  <p class="sub">${t("collection.desc")}</p>
  <div class="tools">
    <input type="search" id="q" placeholder="${t("collection.searchPlaceholder")}" value="${Q}">
    <details class="catfilter" id="catFilter"><summary id="catSummary"></summary>
      <div class="catpanel" id="catPanel"></div></details>
    <select id="fstat"><option value="all">${t("collection.allSets")}</option>
      <option value="open">${t("collection.incompleteOnly")}</option><option value="done">${t("collection.completedOnly")}</option>
      <option value="started">${t("collection.startedOnly")}</option><option value="empty">${t("collection.notStarted")}</option>
      <option value="counted">${t("collection.countedInTotals")}</option><option value="excluded">${t("collection.excludedFromTotals")}</option></select>
    <select id="sort">
      <option value="released">${t("collection.sortReleased")}</option><option value="name">${t("collection.sortName")}</option>
      <option value="pct">${t("collection.sortCompletion")}</option><option value="totalCost">${t("collection.sortCostToFinish")}</option>
      <option value="total">${t("collection.sortSetSize")}</option><option value="ownedValue">${t("collection.sortOwnedValue")}</option></select>
    <button id="dir">${DIR<0?"▼":"▲"}</button>
    <button id="resetFilters" title="${t("collection.resetFiltersTitle")}">${t("collection.resetFilters")}</button>
    <label class="chk" style="margin:0"><input type="checkbox" id="hideExc" ${
      HIDEEXC?"checked":""}> ${t("collection.hideExcluded")}</label>
    <label class="chk" style="margin:0;${VIEW==="table"?"":"opacity:.4"}"
      title="${VIEW==="table"?t("collection.groupSubsetsTitleOn"):t("collection.groupSubsetsTitleOff")}">
      <input type="checkbox" id="famChk" ${FAMILY?"checked":""} ${
        VIEW==="table"?"":"disabled"}> ${t("collection.groupSubsets")}</label>
    <div class="seg" id="viewSeg"><button data-v="grid" class="${VIEW==="grid"?"on":""}">${t("collection.grid")}</button>
      <button data-v="table" class="${VIEW==="table"?"on":""}">${t("collection.table")}</button></div>
  </div>
  <div id="out"></div><div class="pager" id="pg"></div>`;
  bindModeSeg();
  $("#famChk").onchange=e=>{FAMILY=e.target.checked;PAGE=1;render();};
  $("#hideExc").onchange=e=>{HIDEEXC=e.target.checked;PAGE=1;render();};
  $("#q").oninput=e=>{Q=e.target.value;PAGE=1;render();};
  $("#fstat").value=FSTAT; $("#sort").value=SORT;
  $("#fstat").onchange=e=>{FSTAT=e.target.value;PAGE=1;render();};
  $("#sort").onchange=e=>{SORT=e.target.value;render();};
  $("#dir").onclick=()=>{DIR=-DIR;$("#dir").textContent=DIR<0?"▼":"▲";render();};
  $("#resetFilters").onclick=()=>{
    Q="";FLABELS=null;FSTAT="started";SORT="totalCost";DIR=1;HIDEEXC=true;PAGE=1;collection();};
  document.querySelectorAll("#viewSeg button").forEach(b=>b.onclick=()=>{
    VIEW=b.dataset.v;collection();});
  bindCatFilter();
  render();
}
function allLabels(){return new Set(SETS.map(s=>s.label));}
function labelGroups(){
  const order=["Normal Set","Sealed Set","Special Set"];
  const counts={};
  SETS.forEach(s=>{(counts[s.category]=counts[s.category]||new Map());
    counts[s.category].set(s.label,(counts[s.category].get(s.label)||0)+1);});
  return order.filter(c=>counts[c]).map(c=>({name:c,
    labels:[...counts[c].entries()].sort((a,b)=>b[1]-a[1]).map(e=>e[0])}));
}
function catSummary(){
  if(FLABELS===null)return t("collection.allCategories");
  const n=FLABELS.size,total=allLabels().size;
  if(n===0)return t("collection.noCategories");
  if(n>=total)return t("collection.allCategories");
  return t("collection.nOfTotalCategories",{n,total});
}
function bindCatFilter(){
  const groups=labelGroups();
  const cur=FLABELS===null?allLabels():FLABELS;
  $("#catSummary").textContent=catSummary();
  $("#catPanel").innerHTML=groups.map(g=>{
    const checkedN=g.labels.filter(l=>cur.has(l)).length;
    return `<div class="catgroup">
      <label class="catgrouplbl"><input type="checkbox" data-group="${g.name}"
        ${checkedN===g.labels.length?"checked":""}> ${g.name}</label>
      ${g.labels.map(l=>`<label class="catlbl"><input type="checkbox" data-label="${l}"
        ${cur.has(l)?"checked":""}> ${l}</label>`).join("")}
    </div>`;}).join("");
  document.querySelectorAll("#catPanel [data-group]").forEach(cb=>{
    const g=groups.find(x=>x.name===cb.dataset.group);
    const n=g.labels.filter(l=>cur.has(l)).length;
    cb.indeterminate=n>0&&n<g.labels.length;
    cb.onchange=()=>{
      const next=new Set(cur);
      if(cb.checked)g.labels.forEach(l=>next.add(l)); else g.labels.forEach(l=>next.delete(l));
      FLABELS=next.size>=allLabels().size?null:next;
      PAGE=1;bindCatFilter();render();};
  });
  document.querySelectorAll("#catPanel [data-label]").forEach(cb=>cb.onchange=()=>{
    const next=new Set(cur);
    if(cb.checked)next.add(cb.dataset.label); else next.delete(cb.dataset.label);
    FLABELS=next.size>=allLabels().size?null:next;
    PAGE=1;bindCatFilter();render();});
  document.onclick=e=>{
    const d=$("#catFilter");
    if(d&&d.open&&!d.contains(e.target))d.removeAttribute("open");};
}
function filtered(){
  let r=SETS.slice();
  if(Q){const q=Q.toLowerCase();r=r.filter(s=>s.name.toLowerCase().includes(q)||s.code.includes(q));}
  if(FLABELS!==null)r=r.filter(s=>FLABELS.has(s.label));
  if(FSTAT==="open")r=r.filter(s=>s.missing>0);
  if(FSTAT==="done")r=r.filter(s=>s.missing===0);
  if(FSTAT==="started")r=r.filter(s=>s.owned>0&&s.missing>0);
  if(FSTAT==="empty")r=r.filter(s=>s.owned===0);
  if(HIDEEXC)r=r.filter(s=>s.counted);
  if(FSTAT==="counted")r=r.filter(s=>s.counted);
  if(FSTAT==="excluded")r=r.filter(s=>!s.counted);
  r.sort((a,b)=>{const x=a[SORT],y=b[SORT];
    return (typeof x==="string"?x.localeCompare(y):x-y)*DIR;});
  return r;
}
function familyOrder(rows){
  // parents first, each followed by its subsets (Scryfall-style indentation)
  const byCode=Object.fromEntries(rows.map(s=>[s.code,s]));
  const kids={};rows.forEach(s=>{if(s.parent&&byCode[s.parent])
    (kids[s.parent]=kids[s.parent]||[]).push(s);});
  const out=[];
  rows.filter(s=>!s.parent||!byCode[s.parent]).forEach(pnt=>{
    out.push({...pnt,depth:0});
    (kids[pnt.code]||[]).forEach(k=>{
      out.push({...k,depth:1,parentName:pnt.name});
      (kids[k.code]||[]).forEach(g=>out.push({...g,depth:2,parentName:k.name}));});
  });
  return out;
}
function render(){
  let all=filtered();
  if(FAMILY&&VIEW==="table")all=familyOrder(all);
  const pages=Math.max(1,Math.ceil(all.length/PER));
  PAGE=Math.min(PAGE,pages);
  const page=all.slice((PAGE-1)*PER,PAGE*PER);
  $("#out").innerHTML = VIEW==="grid"
   ? `<div class="grid">${page.map(card).join("")}</div>`
   : `<table><thead><tr><th>${t("collection.thSet")}</th><th class="num nowrap">${t("collection.thCode")}</th><th class="num nowrap">${t("collection.thReleased")}</th><th>${t("collection.thKind")}</th><th class="num">${t("collection.thOwned")}</th><th class="num">${t("collection.thProgress")}</th><th class="num" title="${t("collection.thCurrentValueTip")}">${t("collection.thCurrentValue")}</th><th class="num" title="${t("collection.thCardsToBuyTip")}">${t("collection.thCardsToBuy")}</th><th class="num">${t("collection.thShip")}</th><th class="num" title="${t("collection.thToFinishTip")}">${t("collection.thToFinish")}</th><th></th></tr></thead><tbody>${page.map(trow).join("")}</tbody></table>`;
  $("#pg").innerHTML = pages>1 ? `<button ${PAGE<=1?"disabled":""} id="pv">${t("collection.previous")}</button>
    <span>${t("collection.pageOfN",{p:PAGE,n:pages,count:all.length})}</span>
    <button ${PAGE>=pages?"disabled":""} id="nx">${t("collection.next")}</button>` :
    `<span>${t("collection.setsCount",{count:all.length})}</span>`;
  if($("#pv"))$("#pv").onclick=()=>{PAGE--;render();scrollTo(0,0);};
  if($("#nx"))$("#nx").onclick=()=>{PAGE++;render();scrollTo(0,0);};
  document.querySelectorAll("[data-view]").forEach(b=>b.onclick=()=>openSet(b.dataset.view));
  bindSetLinks();
  document.querySelectorAll("[data-buy]").forEach(b=>b.onclick=()=>openBuy(b.dataset.buy));
  document.querySelectorAll("[data-tog]").forEach(b=>b.onclick=async()=>{
    const s=SETS.find(x=>x.code===b.dataset.tog);
    await fetch("/api/set-pref",{method:"POST",
      body:JSON.stringify({code:s.code,mode:s.counted?"exclude":"include"})});
    await load();render();});
}
const card=s=>`<div class="set ${s.missing===0&&s.counted?"done":""} ${s.counted?"":"off"}">
  <div class="hd" data-set="${s.code}" style="cursor:pointer">${icon(s)}
    <div><div class="nm">${s.name}</div>
    <div class="cd">${s.code.toUpperCase()} · ${s.released}</div></div></div>
  <div class="kindrow"><span class="kind ${kindClass(s.kind)}">${s.kind}</span>${
    s.langOnly?`<span class="badge" title="${t("setCard.neverPrintedEnglish")}">${
      t("setCard.onlyBadge",{lang:s.langOnly.toUpperCase()})}</span>`:""}${
    s.counted?"":`<span class="badge">${t("setCard.excludedBadge")}</span>`}</div>
  <div class="st"><span>${s.owned} / ${s.total}</span><span>${pct(s.pct)}</span></div>
  <div class="bar"><span style="width:${s.pct*100}%"></span></div>
  <div class="st"><span>${t("setCard.currentValue")+" "+money(s.ownedValue)}</span>
    <span style="color:var(--gold)">${s.missing?money(s.totalCost):t("setCard.complete")}</span></div>
  ${s.missing?`<div class="st" style="font-size:11px;color:var(--dim)"
    data-tip-title="${t('tip.shipping')}" data-tip="${SHIPCALC(s.missing,s.missingValue)}">
    <span>${t("setCard.cardsToBuy",{n:s.missing,v:money(s.missingValue)})}</span>
    <span>${t("setCard.shipPrefix",{v:money(s.shipping)})}</span></div>`:""}
  ${(s.sealed&&s.sealed.price)?`<div class="st"><span>${t("setCard.sealedNoted")}</span>
    <span style="color:${s.sealed.price<s.totalCost?"var(--ok)":"var(--muted)"}">${
      money(s.sealed.price)}${s.sealed.price<s.totalCost?t("setCard.cheaper"):""}</span></div>`:""}
  <div class="spacer"></div>
  <div class="acts"><button data-view="${s.code}">${t("setCard.viewSet")}</button>
    <button data-buy="${s.code}" ${s.missing?"":"disabled"}>${t("setCard.buyMissing")}</button>
    <button data-tog="${s.code}" title="${s.counted?t("setCard.excludeFromTotals"):t("setCard.includeInTotals")}"
      style="flex:0 0 40px">${s.counted?"✕":"✓"}</button></div></div>`;
const trow=s=>`<tr class="${s.missing===0&&s.counted?"done":""} ${s.counted?"":"off"} ${
  s.depth===1?"child":s.depth===2?"child2":""}">
  <td><span data-set="${s.code}" style="cursor:pointer;display:inline-flex;align-items:center;gap:6px">${icon(s,17)}<span class="setlink">${s.name}</span></span></td><td class="num nowrap">${s.code.toUpperCase()}</td>
  <td class="num nowrap">${s.released}</td><td><span class="kind ${kindClass(s.kind)}">${s.kind}</span></td>
  <td class="num">${s.owned}/${s.total}</td><td class="num">${pct(s.pct)}</td>
  <td class="num">${money(s.ownedValue)}</td>
  <td class="num">${s.missing?money(s.missingValue):"—"}</td>
  <td class="num" style="color:var(--dim)" data-tip-title="${t('tip.shipping')}"
      data-tip="${SHIPCALC(s.missing,s.missingValue)}">${s.missing?money(s.shipping):"—"}</td>
  <td class="num" style="color:var(--gold)">${s.missing?money(s.totalCost):"—"}</td>
  <td class="num"><button data-view="${s.code}">${t("setCard.view")}</button>
    <button data-buy="${s.code}" ${s.missing?"":"disabled"}>${t("setCard.buy")}</button>
    <button data-tog="${s.code}">${s.counted?"✕":"✓"}</button></td></tr>`;

/* ---------------- Set dialogs ---------------- */
let DETAIL=null,CS="number",CD=1,DV="table",CRUMBS=[],LAST_SET_CODE=null;
function crumbs(items){
  if(items.length<2)return "";
  return `<div class="crumbs">${items.map((it,i)=>i===items.length-1
    ? `<b>${it.label}</b>`
    : `<a data-go="${it.hash}">${it.label}</a><span class="sep">›</span>`).join("")}</div>`;
}
function bindSetLinks(){document.querySelectorAll("[data-set]").forEach(e=>
  e.onclick=ev=>{ev.stopPropagation();go("set/"+e.dataset.set);});}
function bindCrumbs(){document.querySelectorAll("[data-go]").forEach(a=>
  a.onclick=()=>{SUPPRESS_RESTORE=true;location.hash=a.dataset.go;});}
const MANASYM=t=>(t||"").replace(/\{([^}]+)\}/g,(m,sym)=>{
  const f=sym.replace(/\//g,"").toUpperCase();
  return `<img class="ms" src="https://svgs.scryfall.io/card-symbols/${encodeURIComponent(f)}.svg" alt="{${sym}}">`;});
// Set-name rewrites below are each confirmed against an actual Cardmarket
// product page (breadcrumb/title), not guessed from a general pattern —
// Cardmarket's naming is inconsistent across product lines (e.g. the Marvel
// bonus sheet gets no colon at all, unlike its TMNT equivalent), so a new
// mismatch needs its own verified rule rather than a broadened regex.
function cmName(n){
  if(/ Commander$/.test(n))return "Commander: "+n.replace(/ Commander$/,"");
  if(/ Eternal$/.test(n))return n.replace(/ Eternal$/,"")+": Eternal";
  // Masterpiece/bonus-sheet name: Scryfall "X Source Material" ->
  // Cardmarket "X: Source Material Cards".
  if(/ Source Material$/.test(n))return n.replace(/ Source Material$/,"")+": Source Material Cards";
  // Mystical Archive sheets: Scryfall "X Mystical Archive" ->
  // Cardmarket "X: Mystical Archive".
  if(/ Mystical Archive$/.test(n))return n.replace(/ Mystical Archive$/,"")+": Mystical Archive";
  // Marvel's bonus sheet keeps Scryfall's set name "Marvel Universe" but
  // Cardmarket lists it under a different name entirely, with no colon.
  if(n==="Marvel Universe")return "Marvel Source Material Cards";
  // These two are sold as a single Commander-only product with no separate
  // draft/collector set (unlike e.g. "Marvel Super Heroes Commander", which
  // already gets folded to "Commander: Marvel Super Heroes" above) — Cardmarket
  // brands the whole line "Universes Beyond: X" instead.
  if(n==="Doctor Who"||n==="Fallout")return "Universes Beyond: "+n;
  return n;
}
// Cardmarket keeps special treatments in their own expansions, e.g.
// "The Hobbit: Extras Version 2". Regular printings stay in the set itself.
/* ---------------- shipping ---------------- */
const esc=t=>String(t).replace(/&/g,"&amp;").replace(/"/g,"&quot;")
  .replace(/</g,"&lt;").replace(/>/g,"&gt;");
const CPS=10;
let TRACKED_SHIP=false, SHIP_COUNTRY="DE", SHIP_RATES={}, AUTO_SYNC=true, PRICE_LOGGING=true,
    ONBOARDING_DONE=true;
const shipNote=()=>t("common.shipNote",{cps:CPS});
function SHIPCALC(n,value){
  if(!n)return "No shipping";
  const orders=Math.max(1,Math.ceil(n/CPS));
  const per=n/orders, pv=value/orders;
  const rates=SHIP_RATES[SHIP_COUNTRY]||{untracked:1.25,tracked:3.95};
  const needTracked=TRACKED_SHIP||pv>25;
  const unit=needTracked?rates.tracked:rates.untracked;
  const why = TRACKED_SHIP ? "tracked shipping preferred, so every order uses it"
    : (pv>25 ? "over 25 € per order, so tracked shipping is required"
    : "cheapest untracked rate for this country");
  return esc(`${n} cards ÷ ${CPS} per seller ≈ ${orders} order${orders>1?"s":""}`)+
         `<br>`+esc(`each about ${per.toFixed(1)} cards worth ${pv.toFixed(2)} €`)+
         `<br>`+esc(why)+
         `<br><b>`+esc(`${orders} × ${unit.toFixed(2)} € = ${(orders*unit).toFixed(2)} €`)+`</b>`;
}

/* ---------------- shared bits ---------------- */
const VAR=c=>(c.variant?`<span class="varlbl">${c.variant}</span>`:"")+
  (c.extras?`<span class="verlbl" data-tip-title="${t('tip.cmExpTitle')}"
    data-tip="${t('tip.cmExp',{v:c.extras})}"
    >Extras ${c.extras}</span>`:"")+
  ((c.ver&&c.ver>1&&!c.extras)?`<span class="verlbl" data-tip-title="${t('tip.cmVerTitle')}"
    data-tip="${t('tip.cmVer',{v:c.ver})}"
    >V.${c.ver}</span>`:"");
const cardTile=c=>`<div class="cc" data-card="${c.set}|${c.number}">
  <div class="imgwrap">${c.img?`<img class="face" src="${c.img}" alt="${cardName(c)}" loading="lazy">`
    :`<div class="noimg">${cardName(c)}</div>`}
    <span class="${c.qty?"owned":"miss"}">${c.qty?c.qty+"×":"0"}</span>
    <button class="tilecart" data-cart="${c.set}|${c.number}"
      title="${t('common.addToCart')}">+</button></div>
  <div class="meta"><div class="cn">${cardName(c)}</div>
    ${c.variant||c.extras?`<div class="vrow">${VAR(c)}</div>`:""}
    ${c.setName?`<div class="cset">${c.setName} · #${c.number}</div>`:""}
    <div class="cp">${c.eur?money(c.eur):(c.foil?"<em>foil</em> "+money(c.foil):"—")}${
      c.eur&&c.foil?` <em>· foil ${money(c.foil)}</em>`:""}</div>
  </div></div>`;
function bindTiles(root){
  (root||document).querySelectorAll("[data-card]").forEach(e=>e.onclick=ev=>{
    ev.stopPropagation();
    const [sc,nr]=e.dataset.card.split("|");
    if(e.dataset.swap==="1"){
      const t="#card/"+sc+"/"+encodeURIComponent(nr);
      if(location.hash===t)route(); else{SUPPRESS_RESTORE=true;location.replace(t);}
    } else go("card/"+sc+"/"+encodeURIComponent(nr));
  });
}

/* ---------------- Set page ---------------- */
async function openSet(code){go("set/"+code);}
async function setPage(code){
  DETAIL=await getJSON("/api/set/"+code);
  CRUMBS=[{label:t("nav.collection"),hash:"collection"},{label:DETAIL.name,hash:"set/"+code}];
  if(code!==LAST_SET_CODE){CS="number";CD=1;}
  LAST_SET_CODE=code;
  const pct=DETAIL.total?Math.round(DETAIL.pct*100):0;
  $("#view").innerHTML=crumbs(CRUMBS.slice(0,1).concat([{label:DETAIL.name}]))+
    `<h1>${DETAIL.name}</h1>
     <p class="sub">${code.toUpperCase()} · ${DETAIL.released} · ${
        t("setPage.ownedOfTotal",{owned:DETAIL.owned,total:DETAIL.total})} · ${pct} %</p>
     ${DETAIL.onlyExtra?`<p class="sub">${t("setPage.onlyExtra",{n:DETAIL.onlyExtra})}</p>`:""}
     <div id="setBody"></div>`;
  bindCrumbs(); drawCards();
}
function drawCards(){
  const OUT=$("#setBody");
  if(!OUT)return;
  const RORDER={c:1,u:2,r:3,m:4,s:5,b:0};
  const SORTCOLS=[["number",t("missing.thNo")],["name",t("missing.thCard")],["type",t("setPage.thType")],
    ["rarity",t("missing.thRarity")],["eur",t("missing.thPrice")],["foil",t("setPage.thFoil")],
    ["qty",t("setPage.thCopies")],["have",t("setPage.thOwned")]];
  const rows=DETAIL.cards.slice().sort((a,b)=>{
    let x=a[CS],y=b[CS];
    if(CS==="number"){x=parseInt(a.number,10)||0;y=parseInt(b.number,10)||0;}
    else if(CS==="rarity"){x=RORDER[a.rarity]??9;y=RORDER[b.rarity]??9;}
    else if(CS==="have"){x=a.have?1:0;y=b.have?1:0;}
    else if(CS==="eur"){x=a.eur||0;y=b.eur||0;}
    else if(CS==="foil"){x=a.foil||0;y=b.foil||0;}
    else if(CS==="qty"){x=a.qty||0;y=b.qty||0;}
    if(typeof x==="string")return x.localeCompare(y)*CD;
    return (x-y)*CD;});
  const sl=isSecretLair(DETAIL.code);
  const head=`${sl?`<div class="msg" style="max-width:720px">${t("cart.secretLairNote")}</div>`:""}
    <div class="tools" style="margin:14px 0 10px">
    <div class="seg"><button data-dv="table" class="${DV==="table"?"on":""}">${t("collection.table")}</button>
    <button data-dv="grid" class="${DV==="grid"?"on":""}">${t("collection.grid")}</button></div>
    <select id="gsort">${SORTCOLS.map(([k,l])=>
      `<option value="${k}" ${CS===k?"selected":""}>${l}</option>`).join("")}</select>
    <button id="gdir">${CD<0?"▼":"▲"}</button>
    <button id="cartAllMissing" ${sl?"disabled title=\""+t("cart.secretLairWhy")+"\"":""}>${t("setPage.addAllMissing")}</button>
    <button id="buyFromSet">${t("setPage.buyMissingDots")}</button></div>`;
  if(DV==="grid"){
    OUT.innerHTML=head+`<div class="cgrid">${rows.map(c=>cardTile(
      {...c,set:DETAIL.code,setName:DETAIL.name})).join("")}</div>`;
    bindTiles(); bindSetTools(); bindCartButtons();
    document.querySelectorAll("[data-dv]").forEach(b=>b.onclick=()=>{DV=b.dataset.dv;drawCards();});
    $("#gsort").onchange=e=>{CS=e.target.value;drawCards();};
    $("#gdir").onclick=()=>{CD=-CD;drawCards();};
    return;
  }
  const NUMCOLS=new Set(["number","eur","foil","qty"]);
  const cols=[...SORTCOLS.map(([k,l])=>[k,l,NUMCOLS.has(k)?"num":""]),["note",t("setPage.thNote"),""],["",t("missing.thCart"),"num"]];
  OUT.innerHTML=head+`<table><thead><tr>${cols.map(([k,l,c])=>
    `<th class="${c}" data-c="${k}">${l}${CS===k?`<span class="ar">${CD>0?"▲":"▼"}</span>`:""}</th>`).join("")}
    </tr></thead><tbody>${rows.map(c=>`<tr class="${c.have?"have":""} ${c.inGoal?"":"notgoal"}">
      <td class="num">${c.number}</td>
      <td><span class="nmline"><span class="setlink"
        data-card="${DETAIL.code}|${c.number}" data-pop="${c.img||""}">${cardName(c)}</span>${VAR(c)}</span></td>
      <td>${c.type||""}</td>
      <td>${RAR[c.rarity]?rarLabel(c.rarity):"?"}</td><td class="num">${c.eur?money(c.eur):"—"}</td>
      <td class="num">${c.foil?money(c.foil):"—"}</td>
      <td class="num">${c.qty||""}</td>
      <td>${c.have?t("setPage.yes"):t("setPage.no")}</td><td class="note">${
        c.inGoal?"":`<span class="badge">${t("setPage.notInGoal")}</span> `}${
        {Endgame:t("setPage.noteEndgame"),BaseMissing:t("setPage.baseMissing"),
         "Other printing":t("setPage.noteOtherPrinting")}[c.note]||c.note||""}</td>
      <td class="num"><button data-cart="${DETAIL.code}|${c.number}" ${c.have?"disabled":""}
        >+</button></td></tr>`).join("")}
    </tbody></table>`;
  OUT.querySelectorAll("th").forEach(th=>th.onclick=()=>{
    const k=th.dataset.c; if(!k)return;
    CD = CS===k ? -CD : 1; CS=k; drawCards();});
  document.querySelectorAll("[data-dv]").forEach(b=>b.onclick=()=>{DV=b.dataset.dv;drawCards();});
  $("#gsort").onchange=e=>{CS=e.target.value;drawCards();};
  $("#gdir").onclick=()=>{CD=-CD;drawCards();};
  bindTiles(); bindSetTools(); bindCartButtons();
}
function bindSetTools(){
  const a=$("#cartAllMissing"),b=$("#buyFromSet");
  if(a)a.onclick=async()=>{
    const items=DETAIL.cards.filter(c=>c.want).map(c=>({set:DETAIL.code,number:c.number}));
    if(!items.length)return;
    await cartPost({action:"addmany",items});
    a.textContent=t("common.addedCount",{n:items.length});
    setTimeout(()=>a.textContent=t("setPage.addAllMissing"),1600);
  };
  if(b)b.onclick=()=>openBuy(DETAIL.code);
}

/* ---------------- Buy page ---------------- */
async function openBuy(code){go("buy/"+code);}
async function buyPage(code){
  DETAIL=await getJSON("/api/set/"+code);
  const opts=[["all",DETAIL.all],...Object.entries(DETAIL.buckets)];
  const n=DETAIL.cards.filter(c=>c.want).length;
  $("#view").innerHTML=crumbs([{label:t("nav.collection"),hash:"collection"},
      {label:DETAIL.name,hash:"set/"+code},{label:t("buyPage.breadcrumbBuyMissing")}])+
    `<h1>${t("buyPage.title")}</h1>
     <p class="sub">${t("buyPage.desc",{setName:DETAIL.name,n,
       cards:n===1?t("buyPage.cardSingular"):t("buyPage.cardPlural")})}
       ${shipNote()}</p>
     <p class="sub">${t("buyPage.pickGroup")}</p>
     <div id="buyOpts"></div>`;
  bindCrumbs();
  $("#buyOpts").innerHTML=opts.map(([k,b])=>`<div class="opt">
      <div class="lb">${k==="all"?t("buyPage.allMissing"):rarLabel(k)}</div>
      <div class="n">${t("buyPage.cardsCount",{n:b.count})}</div>
      <div class="n" data-tip-title="${t("buyPage.cardsOnlyTipTitle")}" data-tip="${t("buyPage.cardsOnlyTip")}"
        >${money(b.value)}</div>
      <div class="n" data-tip-title="${t('tip.shipping')}"
        data-tip="${SHIPCALC(b.count,b.value)}">+${money(b.shipping)}</div>
      <div class="n" style="color:var(--gold);font-weight:600">${money(b.total)}</div>
      <button data-bc="${k}" class="pri">${t("common.addToCart")}</button></div>`).join("");
  document.querySelectorAll("[data-bc]").forEach(b=>b.onclick=async()=>{
    const k=b.dataset.bc;
    const sel=DETAIL.cards.filter(c=>c.want&&(
      k==="all" ? true : k==="land" ? c.basic : (c.rarity===k&&!c.basic)));
    if(!sel.length)return;
    await cartPost({action:"addmany",items:sel.map(c=>({set:DETAIL.code,number:c.number}))});
    b.textContent=t("common.addedCount",{n:sel.length});setTimeout(()=>b.textContent=t("common.addToCart"),1700);
  });
}

/* ---------------- Card page ---------------- */
async function cardPage(sc,nr){
  const d=await getJSON(`/api/card/${sc}/${encodeURIComponent(nr)}`);
  $("#view").innerHTML=`
  ${crumbs(trailFor().concat([{label:cardName(d)}]))}
  <div style="display:flex;gap:8px;margin-bottom:16px">
    <button onclick="history.back()">${t("cardPage.back")}</button>
    <button id="cardCart" class="pri">${t("common.addToCart")}</button>
    <button id="cardWatch" class="${d.inWatchlist?"on":""}">${
      d.inWatchlist?t("cardPage.inWatchlist"):t("cardPage.addToWatchlist")}</button></div>
  <div class="cardpage">
    <div>${d.img?`<img class="art" src="${d.img}" alt="${cardName(d)}">`
      :`<div class="rules" style="text-align:center">${t("cardPage.noImage")}</div>`}
      ${d.cardmarket&&d.eur?`<a class="buybtn" href="${d.cardmarket}" target="_blank"
         rel="noopener">${t("cardPage.buyOnCardmarket",{price:money(d.eur)})}</a>`:""}
      ${d.cardmarket&&d.foil&&(d.finishes||"").includes("foil")?`<a class="buybtn"
         href="${d.cardmarket+(d.cardmarket.includes("?")?"&":"?")+"isFoil=Y"}"
         target="_blank" rel="noopener"
         style="background:linear-gradient(96deg,#c9a227,#e0692c,#4a90c4);color:#12161c">
         ${t("cardPage.buyFoil",{price:money(d.foil)})}</a>`:""}
      ${d.scryfall?`<a class="buybtn" href="${d.scryfall}" target="_blank" rel="noopener"
         style="background:var(--panel);color:var(--text);border:1px solid var(--line)">
         ${t("cardPage.viewOnScryfall")}</a>`:""}
    </div>
    <div>
      <h1>${cardName(d)}</h1>
      <div class="mana" style="margin:6px 0 10px">${MANASYM(d.mana)}${
        d.variant?`<span class="varlbl">${d.variant}</span>`:""}</div>
      <div class="sub" style="margin:0 0 4px">${cardType(d)}${d.pt?" · "+d.pt:""}</div>
      ${d.oracle?`<div class="rules">${MANASYM(cardOracle(d))}</div>`:""}
      <div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">
        <div class="card"><div class="k">${t("cardPage.regular")}</div>
          <div class="v" style="font-size:22px;color:var(--gold)">${d.eur?money(d.eur):"—"}</div></div>
        <div class="card"><div class="k">${t("setPage.thFoil")}</div>
          <div class="v" style="font-size:22px">${d.foil?money(d.foil):"—"}</div>
          <div class="n">${(d.finishes||"").split(",").filter(Boolean).join(", ")||"—"}</div></div>
        <div class="card"><div class="k">${t("cardPage.copiesOwned")}</div>
          <div class="v" id="qtyTotal" style="font-size:22px;color:${d.qty?"var(--ok)":"var(--muted)"}">${d.qty}</div></div>
        <div class="card"><div class="k">${t("missing.thRarity")}</div>
          <div class="v" style="font-size:22px">${RAR[d.rarity]?rarLabel(d.rarity):"?"}</div></div>
      </div>
      <h2>${t("cardPage.yourCollection")}</h2>
      <div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
        <div class="card"><div class="k">${t("cardPage.nonfoil")}</div>
          <div class="qbtn" style="margin-top:6px">
            <button data-adj="normal|-1">−</button>
            <span class="v" id="qtyNormal" style="font-size:20px;min-width:2ch;
              text-align:center;display:inline-block">${d.qtyNormal}</span>
            <button data-adj="normal|1">+</button>
            <button data-adj="normal|set4" title="${t("cardPage.setTo4")}" style="margin-left:8px">×4</button>
          </div></div>
        <div class="card"><div class="k">${t("setPage.thFoil")}</div>
          <div class="qbtn" style="margin-top:6px">
            <button data-adj="foil|-1">−</button>
            <span class="v" id="qtyFoil" style="font-size:20px;min-width:2ch;
              text-align:center;display:inline-block">${d.qtyFoil}</span>
            <button data-adj="foil|1">+</button>
            <button data-adj="foil|set4" title="${t("cardPage.setTo4")}" style="margin-left:8px">×4</button>
          </div></div>
      </div>
      <h2>${t("cardPage.wantListEntry")}</h2>
      <div class="rules" style="font-family:var(--mono);font-size:13px">${
        esc(wantLine(d,d.setName,1))}</div>
      <h2>${t("cardPage.set")}</h2>
      <div class="list"><div class="li" data-set="${d.set}">
        ${d.setIcon?`<img class="seticon" src="${d.setIcon}" width="19" height="19">`:""}
        <span class="nm">${d.setName}</span>
        <span class="mt">${d.set.toUpperCase()} · #${d.number} · ${d.released}</span></div></div>
      ${d.artist?`<p class="sub" style="margin-top:10px">${t("cardPage.illustratedBy",{artist:d.artist})}</p>`:""}
      <h2>${t("cardPage.formatLegality")}</h2>
      <div class="legal">${Object.entries(d.legal).map(([f,v])=>
        `<div><span>${f}</span><span class="tag ${v[0]==="l"?"l":v[0]==="b"?"b":v[0]==="r"?"r":"n"}">${
          v.replace("_"," ").toUpperCase()}</span></div>`).join("")}</div>
      <h2>${t("cardPage.allPrintings")} <span class="sub" style="margin:0">${d.printings.length}</span></h2>
      <div class="list">${d.printings.map(p=>`<div class="li" data-card="${p.set}|${p.number}" data-swap="1">
        <span class="nm">${p.setName}</span>
        <span class="mt">#${p.number} · ${p.released}</span>
        <span class="mt" style="flex:0 0 78px;text-align:right;color:var(--gold)">${
          p.eur?money(p.eur):"—"}</span>
        <span class="mt" style="flex:0 0 62px;text-align:right">${p.qty?p.qty+"×":"—"}</span>
      </div>`).join("")}</div>
    </div></div>`;
  bindCrumbs();bindTiles();bindSetLinks();
  $("#cardCart").onclick=async()=>{
    await cartPost({action:"add",set:d.set,number:d.number,qty:1});
    $("#cardCart").textContent=t("cardPage.added");
    setTimeout(()=>$("#cardCart").textContent=t("common.addToCart"),1500);};
  $("#cardWatch").onclick=async()=>{
    const action=d.inWatchlist?"remove":"add";
    const r=await fetch("/api/watchlist",{method:"POST",
      body:JSON.stringify({action,set:d.set,number:d.number})}).then(r=>r.json());
    if(!r.ok){alert(r.error);return;}
    d.inWatchlist=!d.inWatchlist;
    $("#cardWatch").textContent=d.inWatchlist?t("cardPage.inWatchlist"):t("cardPage.addToWatchlist");
    $("#cardWatch").classList.toggle("on",d.inWatchlist);};
  document.querySelectorAll("[data-adj]").forEach(b=>b.onclick=async()=>{
    const [foilKey,op]=b.dataset.adj.split("|");
    const body={set:d.set,number:d.number,name:d.name,foil:foilKey==="foil"};
    if(op==="set4")Object.assign(body,{action:"set",qty:4});
    else Object.assign(body,{action:"delta",delta:parseInt(op,10)});
    const nd=await fetch("/api/collection-adjust",{method:"POST",
      body:JSON.stringify(body)}).then(r=>r.json());
    d.qty=nd.qty;d.qtyNormal=nd.qtyNormal;d.qtyFoil=nd.qtyFoil;
    $("#qtyNormal").textContent=d.qtyNormal;
    $("#qtyFoil").textContent=d.qtyFoil;
    $("#qtyTotal").textContent=d.qty;
    $("#qtyTotal").style.color=d.qty?"var(--ok)":"var(--muted)";
    await load();
  });
}

/* ---------------- Card browser ---------------- */
let CF={q:"",text:"",artist:"",type:"",rarity:"",colors:[],colormode:"atleast",
        owned:"all",unique:"0",baseonly:"0",allsets:"0",noprice:"0",minprice:"",maxprice:"",
        sort:"released",dir:-1,page:1,per:60,view:"grid"};
async function cardsPane(){
  const box=$("#cardsOut");
  if(!box)return;
  const p=new URLSearchParams({q:CF.q,text:CF.text,artist:CF.artist,type:CF.type,
    rarity:CF.rarity,colors:CF.colors.join(""),colormode:CF.colormode,
    owned:CF.owned,unique:CF.unique,baseonly:CF.baseonly,allsets:CF.allsets,noprice:CF.noprice,
    minprice:CF.minprice,maxprice:CF.maxprice,
    sort:CF.sort,dir:CF.dir,page:CF.page,per:CF.per});
  box.innerHTML=`<p class="sub">${t("browse.searching")}</p>`;
  const r=await getJSON("/api/cards?"+p);
  const pages=Math.max(1,Math.ceil(r.total/r.per));
  box.innerHTML=`<div class="tools">
      <div class="seg"><button data-cv="grid" class="${CF.view==="grid"?"on":""}">${t("collection.grid")}</button>
      <button data-cv="table" class="${CF.view==="table"?"on":""}">${t("collection.table")}</button></div>
      <select id="csort2">${[["released",t("collection.sortReleased")],["name",t("collection.sortName")],["set",t("cardPage.set")],
        ["number",t("browse.sortCollectorNumber")],["price",t("missing.thPrice")],["rarity",t("missing.thRarity")],
        ["cmc",t("browse.sortManaValue")],["qty",t("browse.sortCopiesOwned")]].map(([v,l])=>
        `<option value="${v}" ${CF.sort===v?"selected":""}>${l}</option>`).join("")}</select>
      <button id="cdir2">${CF.dir<0?"▼":"▲"}</button>
      <span class="pill">${t("buyPage.cardsCount",{n:num(r.total)})}</span></div>
    ${CF.view==="grid"
      ? `<div class="cgrid">${r.cards.map(cardTile).join("")}</div>`
      : `<table><thead><tr><th>${t("missing.thCard")}</th><th>${t("cardPage.set")}</th><th class="num">${t("missing.thNo")}</th><th>${t("setPage.thType")}</th>
         <th>${t("missing.thRarity")}</th><th class="num">${t("missing.thPrice")}</th><th class="num">${t("setPage.thFoil")}</th>
         <th class="num">${t("setPage.thCopies")}</th><th class="num">${t("missing.thCart")}</th></tr></thead><tbody>${
         r.cards.map(c=>`<tr>
         <td><span class="nmline"><span class="setlink"
           data-card="${c.set}|${c.number}">${cardName(c)}</span>${VAR(c)}</span></td>
         <td><span class="setlink" data-set="${c.set}">${c.setName}</span></td>
         <td class="num">${c.number}</td><td>${c.type||""}</td>
         <td>${RAR[c.rarity]?rarLabel(c.rarity):"?"}</td>
         <td class="num">${c.eur?money(c.eur):"—"}</td>
         <td class="num">${c.foil?money(c.foil):"—"}</td>
         <td class="num">${c.qty||""}</td>
         <td class="num"><button data-cart="${c.set}|${c.number}">+</button></td></tr>`).join("")}
         </tbody></table>`}
    ${r.cards.length?"":`<div class="empty"><h2>${t("browse.nothingMatches")}</h2>
      <p>${t("browse.loosenFilter")}</p></div>`}
    <div class="pager">${pages>1?`<button ${CF.page<=1?"disabled":""} id="cpv">${t("collection.previous")}</button>
      <span>${t("missing.pagerPageOfN",{p:CF.page,n:num(pages)})}</span>
      <button ${CF.page>=pages?"disabled":""} id="cnx">${t("collection.next")}</button>`:""}</div>`;
  bindTiles();bindCartButtons();bindSetLinks();
  document.querySelectorAll("[data-cv]").forEach(b=>b.onclick=()=>{CF.view=b.dataset.cv;cardsPane();});
  $("#csort2").onchange=e=>{CF.sort=e.target.value;CF.page=1;cardsPane();};
  $("#cdir2").onclick=()=>{CF.dir=-CF.dir;CF.page=1;cardsPane();};
  if($("#cpv"))$("#cpv").onclick=()=>{CF.page--;cardsPane();scrollTo(0,0);};
  if($("#cnx"))$("#cnx").onclick=()=>{CF.page++;cardsPane();scrollTo(0,0);};
}
function cardsView(){
  $("#view").innerHTML=`<h1>${t("collection.title")}</h1>
  ${modeSegHTML()}
  <div class="filters">
    <div class="fbox">
      <button id="freset" style="width:100%">${t("browse.resetSearch")}</button>
      <label>${t("browse.cardName")}</label><input type="search" id="fq" value="${CF.q}" placeholder="${t("browse.searchByName")}">
      <label>${t("browse.ownership")}</label>
      <div class="seg" style="width:100%">${[["all",t("browse.all")],["owned",t("browse.owned")],["missing",t("browse.missing")]]
        .map(([v,l])=>`<button data-own="${v}" class="${CF.owned===v?"on":""}"
          style="flex:1 1 auto;white-space:nowrap">${l}</button>`).join("")}</div>
      <div class="chk" style="margin-top:7px"><input type="checkbox" id="fnew" ${
        CF.owned==="newname"?"checked":""}>
        <label for="fnew" style="margin:0;text-transform:none;letter-spacing:0;font-size:13px;color:var(--text)">${t("browse.missingEverySet")}</label></div>
      <label>${t("browse.rulesText")}</label><input type="search" id="ftext" value="${CF.text}" placeholder="${t("browse.searchCardText")}">
      <label>${t("browse.artist")}</label><input type="search" id="fart" value="${CF.artist}" placeholder="${t("browse.searchByArtist")}">
      <label>${t("browse.typeLine")}</label><input type="search" id="ftype" value="${CF.type}" placeholder="${t("browse.typeLinePlaceholder")}">
      <label>${t("browse.colors")}</label>
      <div class="pips">${["W","U","B","R","G","C"].map(cc=>
        `<div class="pip ${CF.colors.includes(cc)?"on":""}" data-c="${cc}" title="${
          {W:t("browse.colorWhite"),U:t("browse.colorBlue"),B:t("browse.colorBlack"),
           R:t("browse.colorRed"),G:t("browse.colorGreen"),C:t("browse.colorless")}[cc]}">
        <img src="https://svgs.scryfall.io/card-symbols/${cc}.svg" alt="${cc}"
          onerror="this.replaceWith(document.createTextNode('${cc}'))"></div>`).join("")}</div>
      <select id="fcmode" style="margin-top:7px">
        ${[["atleast",t("browse.includesColors")],["only",t("browse.onlyColors")],
           ["exact",t("browse.exactColors")]].map(([v,l])=>
        `<option value="${v}" ${CF.colormode===v?"selected":""}>${l}</option>`).join("")}</select>
      <label>${t("missing.thRarity")}</label>
      <div class="rarrow2">${[["",t("browse.any")],["c",rarLabel("c")],["u",rarLabel("u")],["r",rarLabel("r")],
        ["m",rarLabel("m")],["s",rarLabel("s")],["b",rarLabel("b")]].map(([v,l])=>
        `<span class="rchip ${CF.rarity===v?"on":""}" data-r="${v}">${l}</span>`).join("")}</div>
      <label>${t("browse.priceRange")}</label>
      <div class="prange"><input type="number" id="fmin" min="0" step="1" value="${CF.minprice}"
        placeholder="${t("browse.from")}"><input type="number" id="fmax" min="0" step="1" value="${CF.maxprice}"
        placeholder="${t("browse.to")}"></div>
      <label>${t("browse.options")}</label>
      <div class="chk"><input type="checkbox" id="funique" ${CF.unique==="1"?"checked":""}>
        <label for="funique" style="margin:0;text-transform:none;letter-spacing:0;font-size:13px;color:var(--text)">${t("browse.oneRowPerName")}</label></div>
      <div class="chk"><input type="checkbox" id="fbase" ${CF.baseonly==="1"?"checked":""}>
        <label for="fbase" style="margin:0;text-transform:none;letter-spacing:0;font-size:13px;color:var(--text)">${t("browse.baseSetOnly")}</label></div>
      <div class="chk"><input type="checkbox" id="fnop" ${CF.noprice==="1"?"checked":""}>
        <label for="fnop" style="margin:0;text-transform:none;letter-spacing:0;font-size:13px;color:var(--text)">${t("browse.onlyNoPrice")}</label></div>
      <div class="chk"><input type="checkbox" id="fall" ${CF.allsets==="1"?"checked":""}>
        <label for="fall" style="margin:0;text-transform:none;letter-spacing:0;font-size:13px;color:var(--text)">${t("browse.includeExcludedSets")}</label></div>
    </div>
    <div id="cardsOut"></div>
  </div>`;
  bindModeSeg();
  const upd=debounce(()=>{CF.page=1;cardsPane();});
  $("#fq").oninput=e=>{CF.q=e.target.value;upd();};
  $("#ftext").oninput=e=>{CF.text=e.target.value;upd();};
  $("#fart").oninput=e=>{CF.artist=e.target.value;upd();};
  $("#ftype").oninput=e=>{CF.type=e.target.value;upd();};
  $("#fcmode").onchange=e=>{CF.colormode=e.target.value;CF.page=1;cardsPane();};
  $("#funique").onchange=e=>{CF.unique=e.target.checked?"1":"0";CF.page=1;cardsPane();};
  $("#fbase").onchange=e=>{CF.baseonly=e.target.checked?"1":"0";CF.page=1;cardsPane();};
  $("#fnop").onchange=e=>{CF.noprice=e.target.checked?"1":"0";CF.page=1;cardsPane();};
  $("#fall").onchange=e=>{CF.allsets=e.target.checked?"1":"0";CF.page=1;cardsPane();};
  $("#fnew").onchange=e=>{CF.owned=e.target.checked?"newname":"all";CF.page=1;cardsView();};
  $("#fmin").oninput=debounce(()=>{CF.minprice=$("#fmin").value;CF.page=1;cardsPane();});
  $("#fmax").oninput=debounce(()=>{CF.maxprice=$("#fmax").value;CF.page=1;cardsPane();});
  document.querySelectorAll("[data-own]").forEach(b=>b.onclick=()=>{
    CF.owned=b.dataset.own;CF.page=1;cardsView();});
  document.querySelectorAll(".pip").forEach(pp=>pp.onclick=()=>{
    const cc=pp.dataset.c;
    CF.colors=CF.colors.includes(cc)?CF.colors.filter(x=>x!==cc):[...CF.colors,cc];
    pp.classList.toggle("on");CF.page=1;cardsPane();});
  document.querySelectorAll(".rchip").forEach(ch=>ch.onclick=()=>{
    CF.rarity=ch.dataset.r;CF.page=1;
    document.querySelectorAll(".rchip").forEach(x=>x.classList.toggle("on",x.dataset.r===CF.rarity));
    cardsPane();});
  $("#freset").onclick=()=>{CF={...CF,q:"",text:"",artist:"",type:"",rarity:"",colors:[],
    owned:"all",unique:"0",baseonly:"0",allsets:"0",noprice:"0",minprice:"",maxprice:"",
    page:1};cardsView();};
  cardsPane();
}

/* ---------------- Wantlist-Cart ---------------- */
let CART={items:[],count:0};
let CQ="",CSORT="set",CDIR=1;
async function cartPost(body){
  CART=await fetch("/api/cart",{method:"POST",body:JSON.stringify(body)}).then(r=>r.json());
  if(CART.secretLairSkipped)toast(t("cart.secretLairSkipped",{n:CART.secretLairSkipped}));
  paintCartBadge();return CART;
}
async function cartLoad(){
  try{CART=await fetch("/api/cart",{cache:"no-store"}).then(r=>r.json());}catch(e){}
  paintCartBadge();
}
function paintCartBadge(){
  const b=$("#cartN");if(!b)return;
  b.textContent=CART.count||"";b.classList.toggle("on",!!CART.count);
}
function bindCartButtons(){
  document.querySelectorAll("[data-cart]").forEach(b=>{
    const [sc]=b.dataset.cart.split("|");
    if(isSecretLair(sc)){b.disabled=true;b.title=t("cart.secretLairWhy");return;}
    b.onclick=async ev=>{
      ev.stopPropagation();
      const [s,nr]=b.dataset.cart.split("|");
      await cartPost({action:"add",set:s,number:nr,qty:1});
      const old=b.textContent;
      b.textContent="\u2713";setTimeout(()=>b.textContent=old,1100);
    };
  });
}
function cartView(){
  let r=CART.items.slice();
  if(CQ){const q=CQ.toLowerCase();
    r=r.filter(i=>i.name.toLowerCase().includes(q)||i.setName.toLowerCase().includes(q));}
  const key={set:i=>i.setName+" "+i.name,name:i=>i.name,price:i=>i.eur,
             line:i=>i.eur*i.qty,qty:i=>i.qty}[CSORT];
  r.sort((a,b)=>{const x=key(a),y=key(b);
    return (typeof x==="string"?x.localeCompare(y):x-y)*CDIR;});
  return r;
}
function cartPage(){
  $("#view").innerHTML=crumbs([{label:t("nav.cart")}])+`<h1>${t("nav.cart")}</h1>
  <p class="sub">${t("cart.desc")}</p>
  <div id="cartBody"></div>`;
  bindCrumbs();drawCart();
}
async function drawCart(){
  await cartLoad();
  const el=$("#cartBody");
  if(!CART.items.length){el.innerHTML=`<div class="empty"><h2>${t("cart.emptyTitle")}</h2>
    <p>${t("cart.emptyDesc")}</p></div>`;return;}
  el.innerHTML=`<div class="cards" style="margin-top:0">
      <div class="card"><div class="k">${t("cart.cards")}</div><div class="v">${num(CART.count)}</div>
        <div class="n">${t("cart.fromNSets",{n:CART.sets})}</div></div>
      <div class="card"><div class="k">${t("cart.cardsTotal")}</div>
        <div class="v" style="font-size:24px">${money(CART.goods)}</div></div>
      <div class="card" data-tip-title="${t('tip.shipping')}"
        data-tip="${SHIPCALC(CART.count,CART.goods)}">
        <div class="k">${t("cart.shippingEst")}</div>
        <div class="v" style="font-size:24px">${money(CART.shipping)}</div></div>
      <div class="card"><div class="k">${t("cart.total")}</div>
        <div class="v" style="font-size:24px;color:var(--gold)">${money(CART.total)}</div>
        <div class="n">${t("cart.cardsPlusShipping")}</div></div>
    </div>
    <div class="tools">
      <input type="search" id="cq" placeholder="${t("cart.filterPlaceholder")}" value="${CQ}">
      <select id="csort">${[["set",t("cart.sortSet")],["name",t("cart.sortCardName")],["price",t("cart.sortPrice")],
        ["line",t("cart.sortLineTotal")],["qty",t("cart.sortQuantity")]].map(([v,l])=>
        `<option value="${v}" ${CSORT===v?"selected":""}>${l}</option>`).join("")}</select>
      <button id="cdir">${CDIR<0?"\u25bc":"\u25b2"}</button>
      <button id="cartClear">${t("cart.empty")}</button>
      <button id="cartToColl">${t("cart.addAllToCollection")}</button>
      <button id="cartWant" class="pri">${t("cart.buildWantlist")}</button></div>
    <div id="cartWL"></div>
    <div class="list" style="margin-top:14px">${cartView().map(i=>`<div class="cartrow">
      ${i.img?`<img src="${i.img}" alt="${cardName(i)}" loading="lazy"
        data-card="${i.set}|${i.number}" style="cursor:pointer">`:"<span></span>"}
      <div><span class="nmline"><span class="setlink"
        data-card="${i.set}|${i.number}">${cardName(i)}</span>${VAR(i)}</span>
        <div class="mt" style="color:var(--muted);font-size:12px">
          <span class="setlink" data-set="${i.set}">${i.setName}</span> \u00b7 #${i.number}</div></div>
      <div class="qbtn"><button data-q="${i.set}|${i.number}|${i.qty-1}">\u2212</button>
        <span class="mt">${i.qty}</span>
        <button data-q="${i.set}|${i.number}|${i.qty+1}">+</button></div>
      <div class="num" style="font-family:var(--mono);font-size:12.5px">${money(i.eur)}</div>
      <div class="num" style="font-family:var(--mono);font-size:12.5px;color:var(--gold)">${money(i.eur*i.qty)}</div>
      <button data-q="${i.set}|${i.number}|0" title="${t("cart.remove")}">\u2715</button>
    </div>`).join("")}</div>`;
  bindTiles();bindSetLinks();
  document.querySelectorAll("[data-q]").forEach(b=>b.onclick=async()=>{
    const [sc,nr,q]=b.dataset.q.split("|");
    const y=window.scrollY;                 // keep the viewport put across the rebuild
    await cartPost({action:"set",set:sc,number:nr,qty:+q});
    await drawCart();
    window.scrollTo(0,y);});
  $("#cq").oninput=debounce(()=>{CQ=$("#cq").value;drawCart();});
  $("#csort").onchange=()=>{CSORT=$("#csort").value;drawCart();};
  $("#cdir").onclick=()=>{CDIR=-CDIR;drawCart();};
  $("#cartClear").onclick=async()=>{
    if(!confirm(t("cart.confirmClear")))return;
    await cartPost({action:"clear"});drawCart();};
  $("#cartToColl").onclick=async()=>{
    if(!CART.count)return;
    if(!confirm(t("cart.addAllToCollectionConfirm",{n:CART.count})))return;
    await cartPost({action:"toCollection"});
    await load();
    const b=$("#cartToColl");
    b.textContent=t("cart.addedAllToCollection");setTimeout(()=>b.textContent=t("cart.addAllToCollection"),1700);};
  $("#cartWant").onclick=()=>{
    $("#cartWL").innerHTML=wantChunks(cartView().map(i=>wantLine(i,i.setName,i.qty)));
    bindChunks();};
}

/* ---------------- want list ---------------- */
const CM_LIMIT=150;
// Cardmarket want-list syntax, confirmed by testing:
//   Card Name (V.n) (Expansion)
// The version comes first, in its own brackets, and the expansion follows.
// Special treatments live in a separate expansion called "<Set>: Extras";
// the "Version 1/2/3" shown in Cardmarket's dropdown is the V.n inside it.
//   Smaug the Magnificent (V.1) (The Hobbit: Extras)
// Cardmarket's bracket order differs between the two cases, confirmed by testing:
//   base printing : Smaug the Magnificent (The Hobbit) (V.1)
//   extra printing: Smaug the Magnificent (V.1) (The Hobbit: Extras)
function wantLine(c, setName, qty){
  const n = qty && qty > 1 ? `${qty}x ` : "";
  // Cardmarket splits Secret Lair into hundreds of separate expansions and
  // Scryfall/Binduno keeps one "Secret Lair Drop" set, so there is no reliable
  // expansion or V.n to emit — fall back to the generic Cardmarket catch-all
  // and no version.
  if((setName || "").trim() === "Secret Lair Drop")
    return `${n}${c.name} (Secret Lair Drop Series)`;
  const base = cmName(setName);
  const v = c.cmVer || 1;
  return c.cmSuffix
    ? `${n}${c.name} (V.${v}) (${base}${c.cmSuffix})`
    : `${n}${c.name} (${base}) (V.${v})`;
}

function wantChunks(lines){
  if(!lines.length)return `<p class="sub">${t("wantlist.nothingToCopy")}</p>`;
  const parts=[];
  for(let i=0;i<lines.length;i+=CM_LIMIT)parts.push(lines.slice(i,i+CM_LIMIT));
  const slNote=lines.some(l=>/\(Secret Lair Drop Series\)$/.test(l))
    ? `<div class="msg" style="max-width:720px">${t("wantlist.secretLairNote")}</div>` : "";
  return `<p class="sub">${t("wantlist.limitInfo",{limit:CM_LIMIT,n:parts.length,
      lists:parts.length>1?t("wantlist.listsPlural"):t("wantlist.list")})}</p>${slNote}
    ${parts.map((chunk,i)=>`<div class="chunk">
      <h4>${t("wantlist.entryHeader",{i:i+1,n:parts.length,count:chunk.length})}</h4>
      <textarea readonly id="wl${i}">${chunk.join("\n")}</textarea>
      <button class="pri" data-copy="${i}" style="margin-top:8px">${t("wantlist.copyList",{i:i+1})}</button>
    </div>`).join("")}`;
}
function bindChunks(){
  document.querySelectorAll("[data-copy]").forEach(b=>b.onclick=async()=>{
    const ta=$("#wl"+b.dataset.copy);
    try{await navigator.clipboard.writeText(ta.value);}
    catch(e){ta.select();document.execCommand("copy");}
    const o=b.textContent;b.textContent=t("wantlist.copied");setTimeout(()=>b.textContent=o,1400);});
}

/* ---------------- Missing names ---------------- */
let MF={q:"",rarity:"",set:"",maxprice:"",minprice:"",hideendgame:"1",sort:"set",dir:1,
        page:1,per:150,view:"table"};
function missingView(){
  const rarSet=new Set(MF.rarity?MF.rarity.split(","):[]);
  const setOpts=SETS.filter(s=>s.counted).slice().sort((a,b)=>a.name.localeCompare(b.name));
  $("#view").innerHTML=`<h1>${t("collection.title")}</h1>
  ${modeSegHTML()}
   <p class="sub">${t("missing.desc")}</p>
   <div class="tools">
     <input type="search" id="mq" placeholder="${t("missing.searchPlaceholder")}" value="${MF.q}">
     <select id="mset"><option value="">${t("missing.anySet")}</option>${setOpts.map(s=>
       `<option value="${s.code}" ${MF.set===s.code?"selected":""}>${s.name}</option>`).join("")}</select>
     <div class="seg" id="mrar">${["c","u","r","m","s","b"].map(k=>
       `<button data-r="${k}" class="${rarSet.has(k)?"on":""}">${rarLabel(k)}</button>`).join("")}</div>
     <input type="number" id="mmin" placeholder="${t("missing.minPricePlaceholder")}" style="width:80px" value="${MF.minprice}">
     <input type="number" id="mmax" placeholder="${t("missing.maxPricePlaceholder")}" style="width:80px" value="${MF.maxprice}">
     <select id="msort">${[["set",t("cart.sortSet")],["name",t("missing.sortNameAZ")],["price",t("missing.sortCheapestFirst")],
       ["rarity",t("missing.sortRarity")],["released",t("collection.sortReleased")]].map(([v,l])=>
       `<option value="${v}" ${MF.sort===v?"selected":""}>${l}</option>`).join("")}</select>
     <button id="mdir">${MF.dir<0?"▼":"▲"}</button>
     <div class="seg"><button data-mv="table" class="${MF.view==="table"?"on":""}">${t("collection.table")}</button>
       <button data-mv="grid" class="${MF.view==="grid"?"on":""}">${t("collection.grid")}</button></div>
     <label class="chk" style="margin:0"><input type="checkbox" id="mhide" ${
       MF.hideendgame==="1"?"checked":""}> ${t("missing.hide300")}</label>
   </div>
   <div id="mOut"></div>`;
  bindModeSeg();
  $("#mq").oninput=debounce(()=>{MF.q=$("#mq").value;MF.page=1;drawMissing();});
  $("#mset").onchange=()=>{MF.set=$("#mset").value;MF.page=1;drawMissing();};
  $("#mrar").querySelectorAll("[data-r]").forEach(b=>b.onclick=()=>{
    const k=b.dataset.r,cur=new Set(MF.rarity?MF.rarity.split(","):[]);
    cur.has(k)?cur.delete(k):cur.add(k);
    MF.rarity=[...cur].join(",");
    b.classList.toggle("on",cur.has(k));
    MF.page=1;drawMissing();});
  $("#mmin").oninput=debounce(()=>{MF.minprice=$("#mmin").value;MF.page=1;drawMissing();});
  $("#mmax").oninput=debounce(()=>{MF.maxprice=$("#mmax").value;MF.page=1;drawMissing();});
  $("#msort").onchange=()=>{MF.sort=$("#msort").value;MF.page=1;drawMissing();};
  $("#mdir").onclick=()=>{MF.dir=-MF.dir;$("#mdir").textContent=MF.dir<0?"▼":"▲";drawMissing();};
  $("#mhide").onchange=()=>{MF.hideendgame=$("#mhide").checked?"1":"0";MF.page=1;drawMissing();};
  document.querySelectorAll("[data-mv]").forEach(b=>b.onclick=()=>{
    MF.view=b.dataset.mv;
    document.querySelectorAll("[data-mv]").forEach(x=>x.classList.toggle("on",x.dataset.mv===MF.view));
    drawMissing();});
  drawMissing();
}
function debounce(fn,ms=340){let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms);};}
async function drawMissing(){
  const el=$("#mOut");el.innerHTML=`<p class="sub">${t("missing.loading")}</p>`;
  const p=new URLSearchParams({q:MF.q,rarity:MF.rarity,set:MF.set,maxprice:MF.maxprice,
    minprice:MF.minprice,hideendgame:MF.hideendgame,sort:MF.sort,dir:MF.dir,
    page:MF.page,per:MF.per});
  const r=await fetch("/api/missing?"+p,{cache:"no-store"}).then(x=>x.json());
  const pages=Math.max(1,Math.ceil(r.total/r.per));
  el.innerHTML=`<div class="cards" style="margin:0 0 14px">
      <div class="card"><div class="k">${t("missing.namesMissing")}</div><div class="v">${num(r.total)}</div></div>
      <div class="card"><div class="k">${t("missing.cheapestTotal")}</div>
        <div class="v" style="font-size:24px;color:var(--gold)">${money(r.value)}</div>
        <div class="n">${t("missing.cardsOnlyNoShipping")}</div></div>
      <div class="card"><div class="k">${t("missing.thisPage")}</div><div class="v">${r.cards.length}</div>
        <div class="n">${t("missing.pageOfN2",{p:r.page,n:num(pages)})}${
          MF.sort==="price"?t("missing.cheapestFirstSuffix") : ""}</div></div>
    </div>
    <div class="tools"><button id="mCart" class="pri">${t("missing.addPageToCart")}</button></div>
    ${MF.view==="grid"
      ? `<div class="cgrid">${r.cards.map(c=>cardTile({...c,foil:0,qty:0,setName:c.setName})).join("")}</div>`
      : `<table><thead><tr><th>${t("missing.thCard")}</th><th>${t("missing.thCheapestIn")}</th><th class="num">${t("missing.thNo")}</th>
         <th>${t("missing.thRarity")}</th><th class="num">${t("missing.thPrice")}</th><th class="num">${t("missing.thCart")}</th></tr></thead><tbody>
         ${r.cards.map(c=>`<tr>
           <td><span class="setlink" data-card="${c.set}|${c.number}">${cardName(c)}</span>${VAR(c)}</td>
           <td><span class="setlink" data-set="${c.set}">${c.setName}</span></td>
         <td class="num">${c.number}</td>
           <td>${RAR[c.rarity]?rarLabel(c.rarity):"?"}</td>
           <td class="num" style="color:var(--gold)">${money(c.eur)}</td>
           <td class="num"><button data-cart="${c.set}|${c.number}"
             style="padding:3px 9px;font-size:12px">+</button></td></tr>`).join("")}
         </tbody></table>`}
    <div class="pager">${pages>1?`<button ${MF.page<=1?"disabled":""} id="mpv">${t("collection.previous")}</button>
      <span>${t("missing.pagerPageOfN",{p:MF.page,n:num(pages)})}</span>
      <button ${MF.page>=pages?"disabled":""} id="mnx">${t("collection.next")}</button>`:""}</div>`;
  bindTiles();bindCartButtons();bindSetLinks();
  $("#mCart").onclick=async()=>{
    await cartPost({action:"addmany",items:r.cards.map(c=>({set:c.set,number:c.number}))});
    $("#mCart").textContent=t("missing.addedCount",{n:r.cards.length});
    setTimeout(()=>$("#mCart").textContent=t("missing.addPageToCart"),1600);};
  if($("#mpv"))$("#mpv").onclick=()=>{MF.page--;drawMissing();scrollTo(0,0);};
  if($("#mnx"))$("#mnx").onclick=()=>{MF.page++;drawMissing();scrollTo(0,0);};
}

/* ---------------- Manage ---------------- */
let SUB="collection";
const MANAGE_TABS=[
  ["collection","manage.tabCollection",function(){ updatePane(); }],
  ["completion","manage.tabCompletion",function(){ completionPane(); }],
  ["cm","manage.tabCm",function(){ cardmarketPane(); }],
  ["appearance","manage.tabAppearance",function(){ appearancePane(); }],
  ["about","manage.tabAbout",function(){ aboutPane(); }],
];
function manage(){
  $("#view").innerHTML=`<h1>${t("manage.title")}</h1>
  <div class="seg segtabs" style="margin:8px 0 18px">${MANAGE_TABS.map(([id,key])=>
    `<button data-s="${id}" class="${SUB===id?"on":""}">${t(key)}</button>`).join("")}</div>
  <div id="sub"></div>`;
  document.querySelectorAll("[data-s]").forEach(b=>b.onclick=()=>{
    if(b.dataset.s==="about"&&SUB!=="about")ABOUT_SUB="app";   // land on Update App
    SUB=b.dataset.s;manage(); });
  const on=document.querySelector(".segtabs button.on");
  if(on&&on.scrollIntoView)on.scrollIntoView({inline:"center",block:"nearest"});
  ((MANAGE_TABS.find(x=>x[0]===SUB)||MANAGE_TABS[0])[2])();
}
function sectionSep(){ return '<hr class="psep">'; }
function completionPane(){
  $("#sub").innerHTML='<div id="mSecA"></div>'+sectionSep()+'<div id="mSecCost"></div>'
    +sectionSep()+'<div id="mSecEg"></div>'+sectionSep()+'<div id="mSecB"></div>';
  goalsPane("#mSecA"); costsPane("#mSecCost"); endgamePane("#mSecEg"); setsPane("#mSecB");
}
function costsPane(sel){
  $(sel||"#sub").innerHTML=`<h2 style="margin-top:0">${t("costs.title")}</h2>
  <p class="sub">${t("costs.desc")}</p>
  <label class="chk"><input type="checkbox" id="showCosts" ${SHOW_COSTS?"checked":""}> ${t("costs.enable")}</label>`;
  $("#showCosts").onchange=async e=>{
    await fetch("/api/costs-pref",{method:"POST",body:JSON.stringify({show:e.target.checked})});
    await load(); manage();
  };
}
const EG_STEPS=[100,200,300,500,1000,2000];
function endgamePane(sel){
  sel=sel||"#sub";
  const e=(window.HAS&&window.HAS.endgame)||{on:false,eur:300};
  const opts=EG_STEPS.map(v=>`<option value="${v}" ${Math.round(e.eur)===v?"selected":""}>${money(v)}</option>`).join("");
  $(sel).innerHTML=`<h2 style="margin-top:0">${t("endgame.title")}</h2>
  <p class="sub">${t("endgame.desc")}</p>
  <label class="chk"><input type="checkbox" id="egOn" ${e.on!==false?"checked":""}> ${t("endgame.enable")}</label>
  <div id="egRow" style="margin-top:10px;${e.on!==false?"":"opacity:.4;pointer-events:none"}">
    <label class="sub" style="display:block;margin-bottom:4px">${t("endgame.threshold")}</label>
    <select id="egEur">${opts}</select>
  </div>`;
  async function save(){
    const r=await fetch("/api/endgame-pref",{method:"POST",body:JSON.stringify(
      {on:$("#egOn").checked, eur:+$("#egEur").value})}).then(r=>r.json());
    if(r.endgame&&window.HAS)window.HAS.endgame=r.endgame;
    await load(); endgamePane(sel);
  }
  $("#egOn").onchange=save;
  $("#egEur").onchange=save;
}
function cardmarketPane(){
  $("#sub").innerHTML='<div id="mSecA"></div>'+sectionSep()+'<div id="mSecB"></div>';
  shippingPane("#mSecA"); cmPane("#mSecB");
}
function appearancePane(){
  $("#sub").innerHTML='<div id="mSecA"></div>'+sectionSep()+'<div id="mSecB"></div>';
  designPane("#mSecA"); languagePane("#mSecB");
}
let ABOUT_SUB="app";
function aboutPane(){
  const TABS=[["app","manage.tabApp",appPane],["help","manage.tabHelp",helpPane],
              ["phone","phone.tab",phonePane],["history","manage.tabHistory",historyPane]];
  $("#sub").innerHTML=`<div class="helpnav">${TABS.map(([id,k])=>
    `<button data-as="${id}" class="${ABOUT_SUB===id?"on":""}">${t(k)}</button>`).join("")}</div>
    <div id="asub"></div>`;
  document.querySelectorAll("[data-as]").forEach(b=>b.onclick=()=>{ABOUT_SUB=b.dataset.as;aboutPane();});
  ((TABS.find(x=>x[0]===ABOUT_SUB)||TABS[0])[2])("#asub");
}
function cmSeenAgo(iso){
  if(!iso) return null;
  const ms=Date.now()-new Date(iso.replace(" ","T")).getTime();
  const mn=Math.max(0,Math.round(ms/60000));
  if(mn<1) return t("cm.seenNow");
  if(mn<60) return t("cm.seenMin",{n:mn});
  const h=Math.round(mn/60);
  if(h<24) return t("cm.seenHour",{n:h});
  return t("cm.seenDay",{n:Math.round(h/24)});
}
function cmPane(sel){
  const url=location.origin+"/cm-helper.user.js";
  const cm=(window.HAS&&window.HAS.cmHelper)||{};
  const ago=cmSeenAgo(cm.lastSeen);
  let br="chrome"; try{br=localStorage.getItem("bnd_cm_browser")||"chrome";}catch(e){}
  $(sel||"#sub").innerHTML=`<h2 style="margin-top:0">${t("cm.title")}</h2>
  <p class="sub">${t("cm.desc")}</p>
  <div class="msg ${ago?"ok":""}" style="max-width:700px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <span>${ago?t("cm.statusSeen",{ago}):t("cm.statusNever")}</span>
    <button id="cmTest" style="flex:0 0 auto">${t("cm.testBtn")}</button></div>
  <label class="sub" style="display:block;margin:14px 0 4px">${t("cm.browserLabel")}</label>
  <select id="cmBrowser">
    <option value="chrome">Chrome / Brave / Edge</option>
    <option value="firefox">Firefox</option>
    <option value="safari">Safari</option>
  </select>
  <div id="cmSteps" style="margin-top:12px"></div>
  <div style="margin-top:18px;max-width:700px;border:1px solid var(--line);border-radius:8px;padding:12px 14px">
    <div style="font-weight:600;margin-bottom:2px">${t("cm.bmHeading")}</div>
    <p class="sub" style="margin:4px 0">${t("cm.bmIntro")}</p>
    <ol class="sub" style="line-height:1.9;margin:6px 0 0;padding-left:20px">
      <li>${t("cm.bmDrag")}<br>
        <a id="bmLink" draggable="true" style="display:inline-block;margin-top:6px;background:var(--gold);
           color:#181206;padding:5px 14px;border-radius:4px;text-decoration:none;font-weight:600;cursor:grab">
           Binduno CM</a></li>
      <li>${t("cm.bmClick")}</li>
    </ol>
    <p class="sub" style="margin:8px 0 0">${t("cm.bmNote")}</p>
  </div>
  <p class="sub">${t("cm.legend")}</p>
  <div class="msg" style="max-width:700px">${t("cm.toggleNote")}</div>
  <p class="sub" style="max-width:700px;margin-top:12px">${t("cm.updateNote")}</p>`;
  $("#cmBrowser").value=(br==="bookmarklet"?"chrome":br);
  const renderSteps=()=>{
    const v=$("#cmBrowser").value;
    try{localStorage.setItem("bnd_cm_browser",v);}catch(e){}
    const steps=[t("cm.step1b."+v), `${t("cm.step2")} <a href="${url}" target="_blank"><code>${url}</code></a>`];
    if(v==="chrome") steps.push(t("cm.stepAllow"));
    if(v==="safari") steps.push(t("cm.stepSafariEnable"));
    steps.push(t("cm.stepRunning"),t("cm.step3"),t("cm.stepPermit"));
    $("#cmSteps").innerHTML=`<ol class="sub" style="line-height:1.9;max-width:700px">${
      steps.map(x=>`<li>${x}</li>`).join("")}</ol>`;
  };
  $("#cmBrowser").onchange=renderSteps; renderSteps();
  fetch("/cm-helper.bookmarklet.js").then(r=>r.text()).then(code=>{
    if($("#bmLink")) $("#bmLink").href="javascript:"+code.replace(/\s*\n\s*/g," ");
  });
  const tb=$("#cmTest");
  if(tb) tb.onclick=async()=>{ tb.disabled=true; await load(); cmPane(sel); };
}
function phonePane(sel){
  const lan=(window.HAS&&window.HAS.lan)||{};
  $(sel||"#sub").innerHTML=`<h2 style="margin-top:0">${t("phone.title")}</h2>
  <p class="sub" style="max-width:640px">${t("phone.desc")}</p>`+(lan.url
   ? `<div style="display:flex;gap:22px;align-items:center;flex-wrap:wrap;margin-top:8px">
        <img src="/api/lan-qr.png?t=${Date.now()}" width="184" height="184" alt="QR"
          style="background:#fff;padding:8px;border-radius:10px;image-rendering:pixelated">
        <div><p style="margin:0 0 8px"><code style="font-size:16px">${lan.url}</code></p>
        <p class="sub" style="max-width:340px">${t("phone.hint")}</p></div></div>`
   : `<div class="msg" style="max-width:640px">${t("phone.noLan")}</div>`);
}
// The three set-goal options and the presets that set them in one go. Kept in
// one place so the Settings pane and the setup wizard stay in sync.
const GOAL_PRESETS=[
  ["oneEach",   {scope:"names",     extras:"exclude", serialized:"exclude"}],
  ["baseSet",   {scope:"printings", extras:"exclude", serialized:"exclude"}],
  ["everything",{scope:"printings", extras:"include", serialized:"exclude"}],
];
function goalMatchesPreset(g,p){return g.scope===p.scope&&g.extras===p.extras&&g.serialized===p.serialized;}
async function saveGoal(patch){
  const r=await fetch("/api/goal-pref",{method:"POST",body:JSON.stringify(patch)}).then(r=>r.json());
  if(r.goal&&window.HAS)window.HAS.goal=r.goal;
  await load();
}
function goalPresetCards(onPick){
  const g=(window.HAS&&window.HAS.goal)||{scope:"names",extras:"exclude",serialized:"exclude"};
  return `<div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(184px,1fr))">${
    GOAL_PRESETS.map(([id,p])=>`<div class="card" data-goalp="${id}" style="cursor:pointer;${
      goalMatchesPreset(g,p)?"border-color:var(--gold)":""}">
      <div class="k">${goalMatchesPreset(g,p)?t("lang.current"):""}</div>
      <div class="v" style="font-size:15px">${t("goal.preset."+id)}</div>
      <div class="n">${t("goal.preset."+id+"Desc")}</div></div>`).join("")}</div>`;
}
function goalsPane(sel){
  const g=(window.HAS&&window.HAS.goal)||{scope:"names",extras:"exclude",serialized:"exclude"};
  const seg=(key,val,opts)=>`<div class="seg" data-goalseg="${key}">${opts.map(([v,l])=>
    `<button data-gv="${v}" class="${val===v?"on":""}">${l}</button>`).join("")}</div>`;
  $(sel||"#sub").innerHTML=`<h2 style="margin-top:0">${t("goal.title")}</h2>
  <p class="sub">${t("goal.desc")}</p>
  <h3>${t("goal.presetTitle")}</h3>
  <p class="sub">${t("goal.presetDesc")}</p>
  ${goalPresetCards()}
  <h3 style="margin-top:22px">${t("goal.scope")}</h3>
  ${seg("scope",g.scope,[["names",t("goal.scopeNames")],["printings",t("goal.scopePrintings")]])}
  <h3 style="margin-top:18px">${t("goal.extras")}</h3>
  ${seg("extras",g.extras,[["exclude",t("goal.extrasExclude")],["include",t("goal.extrasInclude")]])}
  <h3 style="margin-top:18px">${t("goal.serialized")}</h3>
  ${seg("serialized",g.serialized,[["exclude",t("goal.serializedExclude")],["include",t("goal.serializedInclude")]])}
  <p class="sub" style="${g.extras==="include"?"":"opacity:.5"}">${t("goal.serializedNote")}</p>`;
  document.querySelectorAll("[data-goalp]").forEach(el=>el.onclick=async()=>{
    const p=GOAL_PRESETS.find(x=>x[0]===el.dataset.goalp)[1];
    await saveGoal(p);goalsPane(sel);});
  document.querySelectorAll("[data-goalseg]").forEach(sg=>sg.querySelectorAll("[data-gv]").forEach(b=>
    b.onclick=async()=>{await saveGoal({[sg.dataset.goalseg]:b.dataset.gv});goalsPane(sel);}));
}
function languagePane(sel){
  const UI_LANGS=[["en","English"],["de","Deutsch"]];
  const CARD_LANGS=[["en","English",t("lang.cardLangEnDesc")],
    ["de","Deutsch",t("lang.cardLangDeDesc")]];
  $(sel||"#sub").innerHTML=`<h2 style="margin-top:0">${t("lang.appLanguage")}</h2>
  <p class="sub">${t("lang.appLanguageDesc")}</p>
  <div class="seg" id="uiLangSeg">${UI_LANGS.map(([id,label])=>
    `<button data-ui="${id}" class="${LANG===id?"on":""}">${label}</button>`).join("")}</div>
  <h2>${t("lang.cardSetNames")}</h2>
  <p class="sub">${t("lang.cardSetNamesDesc")}</p>
  <div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">${
    CARD_LANGS.map(([id,label,desc])=>`<div class="card" data-cl="${id}"
      style="cursor:pointer;${CARDLANG===id?"border-color:var(--gold)":""}">
      <div class="k">${CARDLANG===id?t("lang.current"):""}</div>
      <div class="v" style="font-size:16px">${label}</div>
      <div class="n">${desc}</div></div>`).join("")}</div>`;
  document.querySelectorAll("[data-ui]").forEach(b=>b.onclick=()=>{
    setLang(b.dataset.ui);manage();});
  document.querySelectorAll("[data-cl]").forEach(b=>b.onclick=()=>{
    setCardLang(b.dataset.cl);languagePane(sel);});
}
function designPane(sel){
  const THEMES=[["dark",t("design.dark"),t("design.darkDesc")],
    ["light",t("design.light"),t("design.lightDesc")],
    ["colorblind",t("design.colorblind"),t("design.colorblindDesc")]];
  $(sel||"#sub").innerHTML=`<h2 style="margin-top:0">${t("design.theme")}</h2>
  <p class="sub">${t("design.themeDesc")}</p>
  <div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">${
    THEMES.map(([id,label,desc])=>`<div class="card" data-th="${id}"
      style="cursor:pointer;${THEME===id?"border-color:var(--gold)":""}">
      <div class="k">${THEME===id?t("lang.current"):""}</div>
      <div class="v" style="font-size:16px">${label}</div>
      <div class="n">${desc}</div></div>`).join("")}</div>`;
  document.querySelectorAll("[data-th]").forEach(b=>b.onclick=()=>{
    setTheme(b.dataset.th);designPane(sel);});
}
function shippingPane(sel){
  const OPTS=[["0",t("shipPref.standard"),t("shipPref.standardDesc")],
    ["1",t("shipPref.tracked"),t("shipPref.trackedDesc")]];
  const countries=Object.entries(SHIP_RATES).sort((a,b)=>a[1].name.localeCompare(b[1].name));
  const cur=SHIP_RATES[SHIP_COUNTRY];
  $(sel||"#sub").innerHTML=`<h2 style="margin-top:0">${t("shipPref.countryTitle")}</h2>
  <p class="sub">${t("shipPref.countryDesc")}</p>
  <select id="shipCountry">${countries.map(([code,r])=>
    `<option value="${code}" ${code===SHIP_COUNTRY?"selected":""}>${r.name}</option>`).join("")}</select>
  ${cur?`<p class="sub">${t("shipPref.countryRates",{untracked:money(cur.untracked),tracked:money(cur.tracked)})}</p>`:""}
  <h2>${t("shipPref.title")}</h2>
  <p class="sub">${t("shipPref.desc")}</p>
  <div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">${
    OPTS.map(([id,label,desc])=>`<div class="card" data-sp="${id}"
      style="cursor:pointer;${(TRACKED_SHIP?"1":"0")===id?"border-color:var(--gold)":""}">
      <div class="k">${(TRACKED_SHIP?"1":"0")===id?t("lang.current"):""}</div>
      <div class="v" style="font-size:16px">${label}</div>
      <div class="n">${desc}</div></div>`).join("")}</div>`;
  $("#shipCountry").onchange=async()=>{
    await fetch("/api/shipping-pref",{method:"POST",
      body:JSON.stringify({country:$("#shipCountry").value})});
    await load();
    shippingPane(sel);};
  document.querySelectorAll("[data-sp]").forEach(b=>b.onclick=async()=>{
    await fetch("/api/shipping-pref",{method:"POST",
      body:JSON.stringify({trackedOnly:b.dataset.sp==="1"})});
    await load();
    shippingPane(sel);});
}
function updatePane(){
  $("#sub").innerHTML=`
  <h2 style="margin-top:0">${t("manageUpdate.importTitle")}</h2>
  <div class="drop" id="drop"><input type="file" id="file" accept=".csv"></div>
  <label class="sub" style="display:block;margin:10px 0 4px">${t("manageUpdate.formatLabel")}</label>
  <select id="fmt" style="margin-bottom:10px">
    <option value="auto">${t("manageUpdate.formatAuto")}</option>
    <option value="manabox">ManaBox</option>
    <option value="moxfield">Moxfield</option>
    <option value="archidekt">Archidekt</option></select>
  <div class="radio" id="mode">
    <label class="on"><input type="radio" name="m" value="replace" checked>
      <span>${t("manageUpdate.replace")}<span class="d">${t("manageUpdate.replaceDesc")}</span></span></label>
    <label><input type="radio" name="m" value="add">
      <span>${t("manageUpdate.add")}<span class="d">${t("manageUpdate.addDesc")}</span></span></label>
  </div>
  <button id="imp" class="pri" disabled>${t("manageUpdate.importBtn")}</button>
  <div id="impMsg"></div>
  <h2>${t("manageUpdate.cardDataTitle")}</h2>
  <p class="sub">${t("manageUpdate.cardDataDesc")}</p>
  <button id="ref">${t("manageUpdate.downloadBtn")}</button>
  <div class="prog" style="display:none" id="pw"><span id="pb" style="width:0%"></span></div>
  <div id="refMsg" class="sub"></div>
  <label class="chk" style="margin-top:10px"><input type="checkbox" id="autoSync" ${
    AUTO_SYNC?"checked":""}> ${t("manageUpdate.autoSync")}</label>
  <p class="sub">${t("manageUpdate.autoSyncDesc")}</p>
  <h2>${t("manageUpdate.priceHistoryTitle")}</h2>
  <p class="sub">${t("manageUpdate.priceHistoryDesc")}</p>
  <label class="chk"><input type="checkbox" id="priceLogging" ${
    PRICE_LOGGING?"checked":""}> ${t("manageUpdate.priceLogging")}</label>
  <p class="sub">${t("manageUpdate.priceLoggingDesc")}</p>
  <button id="backfill">${t("manageUpdate.backfillBtn")}</button>
  <p class="sub">${t("manageUpdate.backfillDesc")}</p>
  <h2>${t("manageUpdate.backupTitle")}</h2>
  <p class="sub">${t("manageUpdate.backupDesc")}</p>
  <a class="buybtn" href="/api/export" download>${t("manageUpdate.exportBtn")}</a>
  <h2>${t("manageUpdate.dangerZone")}</h2>
  <button id="clr">${t("manageUpdate.clearBtn")}</button>`;
  let text=null;
  $("#file").onchange=e=>{
    const f=e.target.files[0];if(!f)return;
    const rd=new FileReader();
    rd.onload=()=>{text=rd.result;$("#imp").disabled=false;
      $("#drop").classList.add("ok");};
    rd.readAsText(f);
  };
  document.querySelectorAll("#mode label").forEach(l=>l.onclick=()=>{
    document.querySelectorAll("#mode label").forEach(x=>x.classList.remove("on"));
    l.classList.add("on");});
  $("#imp").onclick=async()=>{
    const mode=document.querySelector("input[name=m]:checked").value;
    $("#imp").disabled=true;$("#imp").textContent=t("manageUpdate.importing");
    busyStart(t("busy.importing"));
    let r;
    try{
      r=await fetch("/api/import",{method:"POST",
        body:JSON.stringify({csv:text,mode,format:($("#fmt")&&$("#fmt").value)||"auto"})}).then(r=>r.json());
      if(r.ok){busyStep(t("busy.recount"));FORCE_RELOAD=true;await load();}
    }catch(e){ r={ok:false,error:String(e)}; }
    await busyDone();
    $("#imp").disabled=false;$("#imp").textContent=t("manageUpdate.importBtn");
    $("#impMsg").innerHTML=r.ok
      ? `<div class="msg ok">${t("manageUpdate.importedMsg",{n:num(r.cards),
          mode:t(r.mode==="add"?"manageUpdate.modeAdd":"manageUpdate.modeReplace")})} · ${t("manageUpdate.detected",{fmt:r.formatLabel})}</div>`
      : `<div class="msg err">${r.error}</div>`;
  };
  $("#ref").onclick=async()=>{
    await fetch("/api/refresh-cards",{method:"POST"});
    $("#pw").style.display="block";poll(t("manageUpdate.cardDataUpToDate"));
  };
  $("#autoSync").onchange=async e=>{
    await fetch("/api/auto-sync-pref",{method:"POST",
      body:JSON.stringify({enabled:e.target.checked})});
    AUTO_SYNC=e.target.checked;
  };
  $("#priceLogging").onchange=async e=>{
    await fetch("/api/price-logging-pref",{method:"POST",
      body:JSON.stringify({enabled:e.target.checked})});
    PRICE_LOGGING=e.target.checked;
  };
  $("#backfill").onclick=async()=>{
    await fetch("/api/backfill-price-history",{method:"POST"});
    $("#pw").style.display="block";poll(t("manageUpdate.backfillDone"));
  };
  $("#clr").onclick=async()=>{
    if(!confirm(t("manageUpdate.confirmClear")))return;
    busyStart(t("busy.clearing"));
    try{
      await fetch("/api/reset",{method:"POST"});
      busyStep(t("busy.recount"));FORCE_RELOAD=true;await load();
    }catch(e){}
    await busyDone();
    manage();
  };
  async function poll(doneMsg){
    const s=await fetch("/api/refresh-status").then(r=>r.json());
    $("#pb").style.width=s.pct+"%";
    $("#refMsg").textContent=s.error?t("manageUpdate.failed",{err:s.error}):s.step;
    if(s.running)setTimeout(()=>poll(doneMsg),700);
    else if(!s.error){await load();$("#refMsg").textContent=doneMsg;}
  }
}
function setsPane(sel){
  const groups={};
  SETS.forEach(s=>{(groups[s.kind]=groups[s.kind]||[]).push(s);});
  const keys=Object.keys(groups).sort();
  $(sel||"#sub").innerHTML=`<h2 style="margin-top:0">${t("manageSets.title")}</h2>
  <p class="sub">${t("manageSets.desc")}</p>
  <div class="tools"><input type="search" id="sq" placeholder="${t("manageSets.searchPlaceholder")}">
    <button id="allOn">${t("manageSets.includeEverything")}</button>
    <button id="allDef">${t("manageSets.restoreDefaults")}</button></div>
  ${keys.map(k=>{
    const g=groups[k],on=g.filter(s=>s.counted).length;
    return `<div class="list" style="margin-bottom:10px"><div class="li" style="background:var(--panel2)">
      <span class="nm"><b>${k}</b></span><span class="mt">${t("manageSets.ofCounted",{on,total:g.length})}</span>
      <button data-grp="${k}" data-m="exclude">${t("manageSets.excludeAll")}</button>
      <button data-grp="${k}" data-m="include">${t("manageSets.includeAll")}</button></div>
      ${g.map(s=>`<div class="li srow" data-n="${s.name.toLowerCase()} ${s.code}">
        ${icon(s,17)}<span class="nm">${s.name}</span>
        <span class="mt">${s.code.toUpperCase()} · ${s.released}</span>
        <button data-tog2="${s.code}" style="flex:0 0 108px">${
          s.counted?t("manageSets.counted"):t("manageSets.excluded")}</button></div>`).join("")}</div>`;}).join("")}`;
  $("#sq").oninput=e=>{const q=e.target.value.toLowerCase();
    document.querySelectorAll(".srow").forEach(r=>
      r.style.display=r.dataset.n.includes(q)?"":"none");};
  document.querySelectorAll("[data-tog2]").forEach(b=>b.onclick=async()=>{
    const s=SETS.find(x=>x.code===b.dataset.tog2);
    await fetch("/api/set-pref",{method:"POST",
      body:JSON.stringify({code:s.code,mode:s.counted?"exclude":"include"})});
    await load();setsPane(sel);});
  document.querySelectorAll("[data-grp]").forEach(b=>b.onclick=async()=>{
    const codes=groups[b.dataset.grp].map(s=>s.code);
    await fetch("/api/set-pref-bulk",{method:"POST",
      body:JSON.stringify({codes,mode:b.dataset.m})});
    await load();setsPane(sel);});
  $("#allOn").onclick=async()=>{
    await fetch("/api/set-pref-bulk",{method:"POST",
      body:JSON.stringify({codes:SETS.map(s=>s.code),mode:"include"})});
    await load();setsPane(sel);};
  $("#allDef").onclick=async()=>{
    await fetch("/api/set-pref-bulk",{method:"POST",
      body:JSON.stringify({codes:SETS.map(s=>s.code),mode:""})});
    await load();setsPane(sel);};
}
async function installFromGithub(srcUrl,ver,btn,box){
  if(btn){btn.disabled=true;btn.textContent=t("manageApp.installing");}
  let x;
  try{ x=await fetch("/api/update-from-github",{method:"POST",
    body:JSON.stringify({srcUrl})}).then(y=>y.json()); }
  catch(e){ x={ok:false,error:String(e)}; }
  if(x.ok){
    box.innerHTML=`<div class="msg ok">${t("manageApp.updatedMsg",{from:x.from,to:x.to})}</div>`;
    let n=0;const w=async()=>{n++;try{await fetch("/api/refresh-status",{cache:"no-store"});location.reload();}
      catch(e){n<40?setTimeout(w,500):box.innerHTML=`<div class="msg err">${t("manageApp.restartSlow")}</div>`;}};
    setTimeout(w,1200);
  }else if(btn){
    btn.disabled=false;btn.textContent=t("gh.install",{v:ver});
    box.insertAdjacentHTML("beforeend",`<div class="msg err" style="margin-top:8px">${x.error}</div>`);
  }
}
function appPane(sel){
  const ghRepo=(window.HAS&&window.HAS.githubRepo)||"";
  const upd=(window.HAS&&window.HAS.update)||{};
  const acheck=window.HAS.autoUpdateCheck!==false;
  const ainst=!!window.HAS.autoUpdateInstall;
  $(sel||"#sub").innerHTML=`
  ${upd.available?`<div class="msg ok" style="max-width:640px">
      <b>${t("gh.available",{cur:upd.current||"",next:upd.latest,name:""})}</b>
      <div style="margin-top:8px"><button class="pri" id="updNow">${t("gh.install",{v:upd.latest})}</button>
      ${upd.htmlUrl?`<a href="${upd.htmlUrl}" target="_blank" style="margin-left:10px;color:var(--gold)">${t("gh.viewRelease")}</a>`:""}</div></div>`:""}
  <h2 style="margin-top:${upd.available?"22px":"0"}">${t("gh.title")}</h2>
  <p class="sub">${t("gh.desc")}</p>
  <label class="chk"><input type="checkbox" id="auCheck" ${acheck?"checked":""}> ${t("gh.autoCheck")}</label>
  <label class="chk"><input type="checkbox" id="auInstall" ${ainst?"checked":""}> ${t("gh.autoInstall")}</label>
  <p class="sub" style="font-size:12px;max-width:560px">${t("gh.autoNote")}</p>
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;max-width:520px;margin-top:6px">
    <input type="text" id="ghRepo" placeholder="owner/repo" value="${ghRepo}"
      style="flex:1 1 200px;background:var(--panel2);border:1px solid var(--line);color:var(--text);padding:8px 10px;border-radius:4px">
    <button id="ghSave">${t("gh.save")}</button>
    <button id="ghCheck" class="pri" ${ghRepo?"":"disabled"}>${t("gh.check")}</button>
  </div>
  <div id="ghMsg" style="margin-top:10px"></div>
  <h2>${t("manageApp.title")}</h2>
  <p class="sub">${t("manageApp.desc")}</p>
  <div class="drop" id="updrop"><input type="file" id="upfile">
    <div class="datei" style="margin-top:6px;font-family:var(--mono);font-size:11.5px;color:var(--dim)">
      ${t("manageApp.filePickerNote")}</div></div>
  <button id="upbtn" class="pri" disabled style="margin-top:10px">${t("manageApp.installBtn")}</button>
  <div id="upMsg"></div>
  <h2>${t("manageApp.wizardTitle")}</h2>
  <p class="sub">${t("manageApp.wizardDesc")}</p>
  <button id="restartWiz">${t("manageApp.wizardBtn")}</button>
  <h2>${t("manageApp.rebuildTitle")}</h2>
  <p class="sub">${t("manageApp.rebuildDesc")}</p>
  <h2>${t("manageApp.whereThingsLive")}</h2>
  <div class="list"><div class="li"><span class="nm">${t("manageApp.database")}</span>
      <span class="mt" id="dbpath"></span></div>
    <div class="li"><span class="nm">${t("manageApp.logFile")}</span>
      <span class="mt">~/Library/Logs/Binduno.log</span></div></div>`;
  const home=window.HAS.homeDir;
  $("#dbpath").textContent=home&&window.HAS.dbPath&&window.HAS.dbPath.startsWith(home)
    ?"~"+window.HAS.dbPath.slice(home.length):(window.HAS.dbPath||"");
  $("#restartWiz").onclick=()=>{WIZ_STEP=0;go("wizard");};
  $("#auCheck").onchange=e=>fetch("/api/update-prefs",{method:"POST",body:JSON.stringify({check:e.target.checked})});
  $("#auInstall").onchange=e=>fetch("/api/update-prefs",{method:"POST",body:JSON.stringify({install:e.target.checked})});
  if($("#updNow"))$("#updNow").onclick=()=>installFromGithub(upd.srcUrl,upd.latest,$("#updNow"),$("#ghMsg"));
  $("#ghSave").onclick=async()=>{
    const r=await fetch("/api/github-repo",{method:"POST",
      body:JSON.stringify({repo:$("#ghRepo").value.trim()})}).then(x=>x.json());
    await load(); appPane(sel);
  };
  $("#ghCheck").onclick=async()=>{
    const box=$("#ghMsg");
    box.innerHTML=`<p class="sub">${t("gh.checking")}</p>`;
    let r;
    try{ r=await fetch("/api/github-latest").then(x=>x.json()); }
    catch(e){
      r={error:/load failed|fetch|network/i.test(String(e))
        ? t("gh.checkFailed") : String(e)};
    }
    if(r.error){ box.innerHTML=`<div class="msg err">${r.error}</div>`; return; }
    if(!r.newer){
      box.innerHTML=`<div class="msg ok">${t("gh.upToDate",{v:r.current})}</div>`;
      return;
    }
    box.innerHTML=`<div class="msg">${t("gh.available",{cur:r.current,next:r.latest,name:r.title||""})}
      ${r.notes?`<pre style="white-space:pre-wrap;font-family:var(--mono);font-size:11.5px;margin:8px 0 0;max-height:180px;overflow:auto">${
        r.notes.replace(/[<>&]/g,x=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[x]))}</pre>`:""}
      <div style="margin-top:10px"><button class="pri" id="ghInstall">${t("gh.install",{v:r.latest})}</button>
      ${r.htmlUrl?`<a href="${r.htmlUrl}" target="_blank" style="margin-left:10px;color:var(--gold)">${t("gh.viewRelease")}</a>`:""}</div></div>`;
    $("#ghInstall").onclick=()=>installFromGithub(r.srcUrl,r.latest,$("#ghInstall"),box);
  };
  let newsrc=null;
  $("#upfile").onchange=e=>{
    const f=e.target.files[0];if(!f)return;
    const rd=new FileReader();
    rd.onload=()=>{newsrc=rd.result;$("#upbtn").disabled=false;
      $("#updrop").classList.add("ok");};
    rd.readAsText(f);
  };
  $("#upbtn").onclick=async()=>{
    $("#upbtn").disabled=true;$("#upbtn").textContent=t("manageApp.installing");
    let r;
    try{ r=await fetch("/api/update-app",{method:"POST",
      body:JSON.stringify({script:newsrc})}).then(x=>x.json()); }
    catch(e){ r={ok:false,error:String(e)}; }
    if(r.ok){
      $("#upMsg").innerHTML=`<div class="msg ok">${t("manageApp.updatedMsg",{from:r.from,to:r.to})}</div>`;
      let tries=0;
      const wait=async()=>{
        tries++;
        try{ await fetch("/api/refresh-status",{cache:"no-store"}); location.reload(); }
        catch(e){ if(tries<40) setTimeout(wait,500);
                  else $("#upMsg").innerHTML=`<div class="msg err">${t("manageApp.restartSlow")}</div>`; }
      };
      setTimeout(wait,1200);
    }else{
      $("#upbtn").disabled=false;$("#upbtn").textContent=t("manageApp.installBtn");
      $("#upMsg").innerHTML=`<div class="msg err">${r.error}</div>`;
    }
  };
}

let HSUB="basics";
function helpPane(sel){
  const cur=SHIP_RATES[SHIP_COUNTRY];
  const T_EN={
   basics:`<h3>What this app is for</h3>
     <p>It tracks two different goals side by side. <b>Card names</b> counts every card you own
        at least once anywhere — that is your “one of everything” project. <b>Printings</b>
        counts each set-and-number combination separately, which is full set completion.
        The same collection gives very different percentages under the two rules.</p>
     <h3>Pages</h3>
     <ul><li><b>Home</b> — the two headline percentages, collection value, what is left to buy,
         and the price <b>Watchlist</b> (up to 100 cards with a 7-day trend).</li>
     <li><b>Collection</b> — three views via the toggle at the top: <b>Sets</b> (every set, or
         every printing, with filters — sets can be grouped so subsets sit under their
         parent), <b>Cards</b> (search across every printing), and <b>Missing Names</b> (one
         row per card name you own nowhere, priced at its cheapest printing — the shopping
         list for the name goal).</li>
     <li><b>Wantlist-Cart</b> — collect cards across sets, then generate want lists. The only
         place want-list text is generated.</li>
     <li><b>Settings</b> — imports, shipping, language, exclusions, history, updates, this
         page. The first-run setup can be re-run any time from
         <i>Settings → App → Setup-Assistent</i>.</li></ul>`,
   prices:`<h3>Where prices come from</h3>
     <p>Scryfall publishes a daily bulk file that carries Cardmarket's EUR figures. The app
        downloads it when you press <i>Download latest card data</i>. Nothing is fetched while
        you browse, so the app works offline apart from card images and set icons.</p>
     <h3>What the numbers mean</h3>
     <ul><li>Prices are Cardmarket's <b>trend price</b>, not the cheapest current offer.
         What you actually pay is usually lower.</li>
     <li>There is <b>no German-seller premium</b> in these figures. Buying only from German
         sellers costs noticeably more, sometimes a lot more on scarce cards.</li>
     <li>Foil and non-foil are stored separately. A card owned as foil is valued with the
         foil price; some cards only ever have a foil price.</li>
     <li>Cards with no Cardmarket price at all count as zero. Filter for them under
         Collection → View Cards.</li></ul>`,
   shipping:`<h3>How shipping is estimated</h3>
     <p>Cardmarket requires tracked shipping once an order exceeds 25 €; below that, sellers
        can use a cheaper untracked letter. Rates depend heavily on the seller's country —
        Binduno uses whichever country is set under <i>Settings → Versand</i>, currently
        <b>${cur?cur.name:SHIP_COUNTRY}</b>:</p>
     <table><thead><tr><th></th><th class="num">Untracked</th><th class="num">Tracked</th></tr></thead>
     <tbody><tr><td>Cheapest rate for ${cur?cur.name:SHIP_COUNTRY}</td>
       <td class="num">${cur?money(cur.untracked):"—"}</td>
       <td class="num">${cur?money(cur.tracked):"—"}</td></tr></tbody></table>
     <p>The model assumes roughly <b>${CPS} cards per seller</b>, because no single seller stocks
        every card you need. That assumption is the weakest part of the estimate: thin old sets
        will need more sellers, deep modern sets fewer.</p>
     <p>Every yellow “to finish” figure <b>includes</b> this estimated shipping. Hover it to see
        the split between cards and postage. Rates were pulled directly from
        <a href="https://www.cardmarket.com/en/Magic/Help/ShippingCosts" target="_blank"
           rel="noopener">Cardmarket's own shipping cost page</a>, which also lists every other
        country and shipping method in full detail.</p>`,
   rules:`<h3>Which cards count</h3>
     <p>Sets are excluded by default when they are digital-only, promos, tokens, memorabilia,
        Un-sets, or not released yet. Collectors' Edition, International Edition and 30th
        Anniversary fall under memorabilia, so they never count as a valid printing.
        You can override any of this under <i>Settings → Excluded Sets</i>.</p>
     <h3>Notes on individual cards</h3>
     <ul><li><b>Other printing</b> — you already own this card name elsewhere in the set,
         so it is not on the want list.</li>
     <li><b>Endgame</b> — the cheapest printing is at or above the price threshold (300 € by
         default; change or disable it under Settings → Completion). These are left out of
         “remaining cost” so the figure stays realistic; the Home tile shows them separately.</li>
     <li><b>Special printings</b> — borderless, showcase, surge foil and similar are labelled
         in orange next to the card name.</li></ul>
     <h3>Want lists</h3>
     <p>Cardmarket uses two different bracket orders. A regular printing reads
        <code>Card Name (Set) (V.n)</code>, a special treatment reads
        <code>Card Name (V.n) (Set: Extras)</code>. Special treatments such as borderless or
        surge foil are not part of the main set on Cardmarket; they live in an expansion
        called <code>&lt;Set&gt;: Extras</code>, and the number Cardmarket shows as
        “Version 1/2/3” is the <code>V.n</code> inside that expansion. A borderless Smaug
        therefore reads <code>Smaug the Magnificent (V.1) (The Hobbit: Extras)</code>.</p>
     <p>Quantities are written as a prefix: <code>2x Sol Ring (V.1) (Commander: Kaldheim)</code>.
        Cardmarket accepts 150 entries per list, so longer lists are split into numbered
        blocks you copy one after another.</p>
     <p><b>Secret Lair</b> cards can't be put in the Wantlist-Cart yet: Cardmarket
        splits Secret Lair into hundreds of separate expansions with no reliable
        mapping, so a generated want-list line wouldn't match. Buy those directly
        from the card's Cardmarket page.</p>`,
   data:`<h3>Your data</h3>
     <p>Everything lives in a SQLite file on your Mac. Nothing is uploaded anywhere.</p>
     <ul><li><b>Replace</b> import wipes the stored collection and uses the file as the new truth.</li>
     <li><b>Add</b> keeps what is there and adds the quantities on top.</li>
     <li><b>Export</b> writes a CSV in ManaBox column layout, so you can move collections
         back and forth or keep backups.</li>
     <li>The last 100 changes are listed under <i>History</i>.</li></ul>
     <h3>Refreshing card data</h3>
     <p>Binduno checks once an hour in the background and downloads a fresh copy automatically
        once the card data is more than 24 hours old — mainly to keep Cardmarket prices
        current. Turn this off under <i>Settings → Update Collection</i> if you'd rather trigger
        it by hand. Either way it replaces the card table; your collection is untouched.</p>
     <h3>Price history</h3>
     <p>Every refresh logs any Cardmarket price that changed with today's date — that is what
        feeds the sparklines on the <i>Watchlist</i> (Home page). A one-off button under
        <i>Settings → Update Collection</i> backfills 90 real days of past prices from MTGJSON
        instead of waiting weeks for history to build up on its own; Binduno also runs that
        automatically if it notices a gap of a couple of days or more since the last logged
        price, e.g. after not being opened for a while.</p>`,
  };
  const T_DE={
   basics:`<h3>Wozu die App da ist</h3>
     <p>Sie verfolgt zwei unterschiedliche Ziele parallel. <b>Kartennamen</b> zählt jede Karte,
        die du mindestens einmal irgendwo besitzt — dein „von jedem eine“-Projekt. <b>Drucke</b>
        zählt jede Set-und-Nummer-Kombination einzeln, also die vollständige
        Set-Vervollständigung. Dieselbe Sammlung ergibt unter beiden Regeln sehr
        unterschiedliche Prozentzahlen.</p>
     <h3>Seiten</h3>
     <ul><li><b>Start</b> — die zwei Hauptprozentzahlen, Sammlungswert, was noch fehlt, und die
         Preis-<b>Watchlist</b> (bis zu 100 Karten mit 7-Tage-Trend).</li>
     <li><b>Sammlung</b> — drei Ansichten über den Umschalter oben: <b>Sets</b> (jedes Set, oder
         jeder Druck, mit Filtern — Sets lassen sich gruppieren, sodass Subsets unter ihrem
         übergeordneten Set stehen), <b>Karten</b> (Suche über alle Drucke), und
         <b>Fehlende Namen</b> (eine Zeile pro Kartenname, den du nirgends besitzt, zum Preis
         des günstigsten Drucks — die Einkaufsliste fürs Namensziel).</li>
     <li><b>Wantlist-Cart</b> — Karten über Sets hinweg sammeln, dann Wantlisten erzeugen. Der
         einzige Ort, an dem Wantlist-Text erzeugt wird.</li>
     <li><b>Einstellungen</b> — Imports, Versand, Sprache, Ausschlüsse, Verlauf, Updates, diese
         Seite. Die Ersteinrichtung kann jederzeit erneut gestartet werden über
         <i>Einstellungen → App → Setup-Assistent</i>.</li></ul>`,
   prices:`<h3>Woher die Preise kommen</h3>
     <p>Scryfall veröffentlicht täglich eine Sammel-Datei mit Cardmarkets EUR-Preisen. Die App
        lädt sie herunter, wenn du auf <i>Neueste Kartendaten herunterladen</i> klickst.
        Während du browst, wird nichts nachgeladen — die App funktioniert also offline, bis auf
        Kartenbilder und Set-Icons.</p>
     <h3>Was die Zahlen bedeuten</h3>
     <ul><li>Preise sind Cardmarkets <b>Trendpreis</b>, nicht das günstigste aktuelle Angebot.
         Was du tatsächlich zahlst, ist meist niedriger.</li>
     <li>Es gibt <b>keinen Aufschlag für deutsche Verkäufer</b> in diesen Zahlen. Nur bei
         deutschen Verkäufern zu kaufen kostet spürbar mehr, bei seltenen Karten manchmal
         deutlich mehr.</li>
     <li>Foil und Nonfoil werden getrennt gespeichert. Eine als Foil besessene Karte wird zum
         Foil-Preis bewertet; manche Karten haben ausschließlich einen Foil-Preis.</li>
     <li>Karten ganz ohne Cardmarket-Preis zählen als null. Filtere danach unter
         Sammlung → Karten anzeigen.</li></ul>`,
   shipping:`<h3>Wie der Versand geschätzt wird</h3>
     <p>Cardmarket verlangt getrackten Versand, sobald eine Bestellung 25 € übersteigt; darunter
        können Verkäufer einen günstigeren ungetrackten Brief nutzen. Die Tarife hängen stark
        vom Land des Verkäufers ab — Binduno nutzt das unter <i>Einstellungen → Versand</i>
        eingestellte Land, aktuell <b>${cur?cur.name:SHIP_COUNTRY}</b>:</p>
     <table><thead><tr><th></th><th class="num">Ungetrackt</th><th class="num">Getrackt</th></tr></thead>
     <tbody><tr><td>Günstigster Tarif für ${cur?cur.name:SHIP_COUNTRY}</td>
       <td class="num">${cur?money(cur.untracked):"—"}</td>
       <td class="num">${cur?money(cur.tracked):"—"}</td></tr></tbody></table>
     <p>Das Modell geht von etwa <b>${CPS} Karten pro Verkäufer</b> aus, da kein einzelner
        Verkäufer alle benötigten Karten vorrätig hat. Das ist der schwächste Teil der
        Schätzung: dünn besetzte alte Sets brauchen mehr Verkäufer, umfangreiche moderne Sets
        weniger.</p>
     <p>Jede goldene „bis zur Fertigstellung“-Zahl <b>enthält</b> diese geschätzten
        Versandkosten. Hover zeigt die Aufteilung zwischen Karten und Porto. Die Tarife stammen
        direkt von <a href="https://www.cardmarket.com/de/Magic/Help/ShippingCosts"
        target="_blank" rel="noopener">Cardmarkets eigener Versandkosten-Seite</a>, die auch
        jedes andere Land und jede Versandart im Detail auflistet.</p>`,
   rules:`<h3>Welche Karten zählen</h3>
     <p>Sets werden standardmäßig ausgeschlossen, wenn sie rein digital, Promos, Token,
        Merchandise, Un-Sets sind, oder noch nicht erschienen sind. Collectors' Edition,
        International Edition und 30th Anniversary fallen unter Merchandise und zählen daher
        nie als gültiger Druck. Das lässt sich unter <i>Einstellungen → Ausgeschlossene
        Sets</i> übersteuern.</p>
     <h3>Hinweise zu einzelnen Karten</h3>
     <ul><li><b>Anderer Druck</b> — du besitzt diesen Kartennamen schon anderswo im Set, daher
         steht er nicht auf der Wantlist.</li>
     <li><b>Endgame</b> — der günstigste Druck erreicht den Preis-Schwellwert (standardmäßig
         300 €; unter Einstellungen → Vervollständigung änderbar oder abschaltbar). Diese werden
         aus den „Restkosten“ herausgelassen, damit die Zahl realistisch bleibt; die Home-Kachel
         zeigt sie separat.</li>
     <li><b>Sonderdrucke</b> — Borderless, Showcase, Surge-Foil und Ähnliches werden orange
         neben dem Kartennamen markiert.</li></ul>
     <h3>Wantlisten</h3>
     <p>Cardmarket nutzt zwei unterschiedliche Klammerreihenfolgen. Ein regulärer Druck liest
        sich <code>Card Name (Set) (V.n)</code>, ein Sonderdruck
        <code>Card Name (V.n) (Set: Extras)</code>. Sonderdrucke wie Borderless oder Surge-Foil
        gehören auf Cardmarket nicht zum Hauptset; sie leben in einer Erweiterung namens
        <code>&lt;Set&gt;: Extras</code>, und die Nummer, die Cardmarket als „Version 1/2/3“
        zeigt, ist das <code>V.n</code> innerhalb dieser Erweiterung. Ein Borderless-Smaug liest
        sich daher <code>Smaug the Magnificent (V.1) (The Hobbit: Extras)</code>.</p>
     <p>Mengen werden als Präfix geschrieben: <code>2x Sol Ring (V.1) (Commander: Kaldheim)</code>.
        Cardmarket akzeptiert 150 Einträge pro Liste, längere Listen werden daher in
        nummerierte Blöcke aufgeteilt, die du nacheinander kopierst.</p>
     <p><b>Secret Lair</b>-Karten können noch nicht in den Wantlist-Cart: Cardmarket
        teilt Secret Lair in hunderte einzelne Erweiterungen ohne verlässliche
        Zuordnung auf, eine erzeugte Wantlist-Zeile würde also nicht treffen. Solche
        Karten direkt über die Cardmarket-Seite der Karte kaufen.</p>`,
   data:`<h3>Deine Daten</h3>
     <p>Alles liegt in einer SQLite-Datei auf deinem Mac. Nichts wird irgendwohin hochgeladen.</p>
     <ul><li><b>Ersetzen</b>-Import löscht die gespeicherte Sammlung und nutzt die Datei als
         neue Wahrheit.</li>
     <li><b>Hinzufügen</b> behält den bestehenden Stand und addiert die Mengen obendrauf.</li>
     <li><b>Export</b> schreibt eine CSV im ManaBox-Spaltenformat, damit du Sammlungen hin-
         und herbewegen oder Backups aufheben kannst.</li>
     <li>Die letzten 100 Änderungen stehen unter <i>Verlauf</i>.</li></ul>
     <h3>Kartendaten aktualisieren</h3>
     <p>Binduno prüft im Hintergrund stündlich, ob die Kartendaten älter als 24 Stunden sind,
        und lädt dann automatisch eine frische Kopie — vor allem um Cardmarket-Preise aktuell
        zu halten. Das lässt sich unter <i>Einstellungen → Sammlung aktualisieren</i>
        abschalten, falls du es lieber manuell auslöst. So oder so wird nur die Kartentabelle
        ersetzt; deine Sammlung bleibt unberührt.</p>
     <h3>Preishistorie</h3>
     <p>Bei jeder Aktualisierung wird jeder geänderte Cardmarket-Preis mit heutigem Datum
        geloggt — das speist die Sparklines auf der <i>Watchlist</i> (Start-Seite). Ein
        einmaliger Button unter <i>Einstellungen → Sammlung aktualisieren</i> holt 90 echte
        Tage vergangener Preise von MTGJSON nach, statt wochenlang auf eigene Historie zu
        warten; Binduno macht das auch automatisch, sobald eine Lücke von ein paar Tagen oder
        mehr seit dem letzten geloggten Preis auffällt, z. B. nach längerer Pause.</p>`,
  };
  const T = LANG==="de" ? T_DE : T_EN;
  $(sel||"#sub").innerHTML=`<h2 style="margin-top:0">${t("manage.tabHelp")}</h2>
    <div class="helpnav">${Object.entries(
    {basics:t("help.basics"),prices:t("help.prices"),shipping:t("help.shipping"),
     rules:t("help.rules"),data:t("help.data")})
    .map(([k,l])=>`<button data-h="${k}" class="${HSUB===k?"on":""}">${l}</button>`).join("")}</div>
    <div class="help">${T[HSUB]}</div>`;
  document.querySelectorAll("[data-h]").forEach(b=>b.onclick=()=>{HSUB=b.dataset.h;helpPane(sel);});
}

async function historyPane(sel){
  const h=await fetch("/api/history").then(r=>r.json());
  $(sel||"#sub").innerHTML=`<h2 style="margin-top:0">${t("history.title")}</h2>
  <p class="sub">${t("history.desc")}</p>
  <div class="list" style="padding:4px 15px"><div class="hist">
  ${h.length?h.map(e=>{
    const d=new Date(e.ts);
    const ts=d.toLocaleString("en-US",{month:"long",day:"numeric",year:"numeric",
      hour:"numeric",minute:"2-digit"});
    return `<div class="row"><span class="ts">${ts}</span>
      <span class="ac">${e.action}</span><span>${e.detail}</span></div>`;}).join("")
   :`<div class="row">${t("history.none")}</div>`}
  </div></div>`;
}

/* ---------------- routing ---------------- */
const SCROLL={};
let SCROLLTICK=0;
addEventListener("scroll",()=>{
  clearTimeout(SCROLLTICK);
  SCROLLTICK=setTimeout(()=>{SCROLL[location.hash||"#home"]=window.scrollY;},120);
});
// popstate fires on *any* same-document hash change, not just actual browser
// Back/Forward, so it can't tell them apart on its own. Every in-app forward
// navigation goes through one of the three spots below and marks itself with
// SUPPRESS_RESTORE first; a hashchange that arrives with the flag still unset
// can only be a real Back/Forward, since nothing else changes location.hash.
// Only that case should restore a remembered scroll position — landing on a
// hash any other way (nav tab, breadcrumb, card link) always starts at the top.
let SUPPRESS_RESTORE=false;
// A nav tab remembers the deepest page you last had open in its section, so
// leaving Collection on a set page and coming back via the tab returns to that
// set (at its scroll position), not the overview. RESTORE_NEXT carries the
// "restore scroll" intent through the forward navigation go() would otherwise
// treat as a fresh top-of-page visit.
let SECTION_HASH={}, RESTORE_NEXT=false, RESTORE_SECTION=null, CUR_SECTION="home";
// card/* and buy/* are always drilled into from another page, so they belong to
// whatever section we were already in rather than a section of their own.
function sectionFor(p){
  if(p==="collection"||p==="missing"||p.startsWith("set/")) return "collection";
  if(p==="cart") return "cart";
  if(p==="manage") return "manage";
  if(p==="wizard") return null;
  if(p.startsWith("card/")||p.startsWith("buy/")) return CUR_SECTION;
  return "home";
}
function go(p){SCROLL[location.hash||"#home"]=window.scrollY;SUPPRESS_RESTORE=true;location.hash=p;}
function restoreScroll(){
  const key=location.hash||"#home", y=SCROLL[key]||0;
  if(!y){scrollTo(0,0);return;}
  let tries=0;
  const tick=()=>{
    scrollTo(0,y);
    if(++tries<8 && Math.abs(window.scrollY-y)>4) setTimeout(tick,80);
  };
  setTimeout(tick,50);
}
let ROUTING=false;
function trailFor(){return CRUMBS.length?CRUMBS:[{label:t("nav.collection"),hash:"collection"}];}
async function route(){
  if(ROUTING)return;          // guard against overlapping navigations
  ROUTING=true;
  try{ await doRoute(); } finally { ROUTING=false; }
}
async function doRoute(){
  const p=(location.hash||"#home").slice(1);
  const restoreSec=RESTORE_SECTION; RESTORE_SECTION=null;
  const cameFromHistory=!SUPPRESS_RESTORE; SUPPRESS_RESTORE=false;
  const navP=p==="missing"?"collection":p;
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.dataset.p===navP));
  try{
    if(FORCE_RELOAD||!SETS.length||!STATS){FORCE_RELOAD=false;await load();}
    if($("#ver"))$("#ver").textContent="v"+(window.HAS.version||"");
    paintCartBadge();
    if(!ONBOARDING_DONE&&p!=="wizard"){
      SUPPRESS_RESTORE=true;location.hash="wizard";return;
    }
    if(p==="wizard"){
      wizardPage();
    }else if(p.startsWith("buy/")){
      await buyPage(p.split("/")[1]);
    }else if(p.startsWith("set/")){
      CRUMBS=[{label:t("nav.collection"),hash:"collection"}];
      await setPage(p.split("/")[1]);
    }else if(p.startsWith("card/")){
      const [,sc,nr]=p.split("/");
      await cardPage(sc,decodeURIComponent(nr));
    }else{
      if(p==="collection")CRUMBS=[{label:t("nav.collection"),hash:"collection"}];
      if(p==="missing"){CRUMBS=[{label:t("nav.collection"),hash:"collection"}];CMODE="missing";}
      if(p==="cart")CRUMBS=[{label:t("nav.cart"),hash:"cart"}];
      ({home,collection,cart:cartPage,manage,explain:explainPage}[navP]||home)();
    }
    const sec=restoreSec||sectionFor(p);
    if(sec){CUR_SECTION=sec;SECTION_HASH[sec]=p;}
    if(cameFromHistory||RESTORE_NEXT)restoreScroll(); else scrollTo(0,0);
    RESTORE_NEXT=false;
  }catch(e){offline(e);}
}
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  const p=t.dataset.p, deep=SECTION_HASH[p];
  if(deep&&deep!==p&&CUR_SECTION!==p){RESTORE_NEXT=true;RESTORE_SECTION=p;go(deep);}
  else go(p);
});
addEventListener("hashchange",route);
// Closing the tab (or navigating away / reloading — a real browser popup
// can't tell JS which button was pressed, so there is no way to ask "quit
// or keep running?" and act on the answer) quits the background server.
addEventListener("beforeunload",()=>{navigator.sendBeacon("/api/quit");});
(function(){
  const box=$("#tipbox");
  addEventListener("mouseover",e=>{
    const el=e.target.closest("[data-tip]");
    if(!el){box.style.display="none";return;}
    box.innerHTML=`<b>${el.dataset.tipTitle||t("tip.default")}</b>${el.dataset.tip}`;
    box.style.display="block";
    const r=el.getBoundingClientRect(),bw=box.offsetWidth,bh=box.offsetHeight;
    let x=r.left, y=r.bottom+8;
    if(x+bw>innerWidth-12)x=innerWidth-bw-12;
    if(y+bh>innerHeight-12)y=r.top-bh-8;
    box.style.left=Math.max(12,x)+"px";box.style.top=Math.max(12,y)+"px";
  });
  addEventListener("mouseout",e=>{
    if(e.target.closest("[data-tip]"))box.style.display="none";});
  addEventListener("scroll",()=>{box.style.display="none"},true);
})();
// Card-image preview: same popup behaviour as the shipping tooltip, anchored
// to the hovered element, big enough to read the rules text. Shows while the
// pointer is over a [data-pop] element (the card name in the set table).
(function(){
  const pop=$("#cardpop"), img=pop.firstElementChild;
  let cur=null;
  const hide=()=>{pop.style.display="none";cur=null;};
  img.onerror=hide;
  addEventListener("mouseover",e=>{
    const el=e.target.closest("[data-pop]");
    if(!el||!el.dataset.pop){hide();return;}
    if(el.dataset.pop!==cur){cur=el.dataset.pop;img.src=cur;}
    pop.style.display="block";
    const r=el.getBoundingClientRect(),pw=pop.offsetWidth,ph=pop.offsetHeight;
    let x=r.right+10, y=r.top-20;
    if(x+pw>innerWidth-12) x=r.left-pw-10;              // flip to the left edge
    if(x<12) x=12;
    y=Math.max(12,Math.min(y,innerHeight-ph-12));
    pop.style.left=x+"px";pop.style.top=y+"px";
  });
  addEventListener("mouseout",e=>{ if(e.target.closest("[data-pop]")) hide(); });
  addEventListener("scroll",hide,true);
})();
$("#brand").onclick=()=>go("home");
$("#navQuit").onclick=async()=>{
  if(!confirm(t("nav.confirmQuit")))return;
  try{await fetch("/api/quit",{method:"POST",body:JSON.stringify({explicit:true})});}catch(e){}
  document.body.innerHTML='<div class="empty" style="padding-top:140px">'+
    `<h2>${t("nav.stopped")}</h2><p>${t("nav.stoppedDesc")}</p></div>`;
};
function paintNav(){
  $("#navHome").textContent=t("nav.home");
  $("#navCollection").textContent=t("nav.collection");
  $("#navCart").textContent=t("nav.cart");
  $("#navManage").textContent=t("nav.manage");
  $("#navQuit").title=t("nav.quitTitle");
  $("#navQuit").setAttribute("aria-label",t("nav.quit"));
  $("#brand").title=t("nav.homeTitle");
}
paintNav();
fetch("/api/ui-lang",{method:"POST",body:JSON.stringify({lang:LANG})}).catch(()=>{});
route();
</script></body></html>"""




# ------------------------------------------------------------ LAN address + QR
def _lan_ip():
    """Best guess at this machine's address on the local network. The UDP
    'connect' sends nothing — it just makes the OS pick the outbound
    interface — so this works offline and contacts no one."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 9))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:                                          # noqa: BLE001
        return None


def _lan_url():
    ip = _lan_ip()
    if not ip or ip.startswith("127."):
        return ""
    return f"http://{ip}:{PORT}/"


# --- QR encoder: just enough of ISO/IEC 18004 for a short URL --------------
# Byte mode, error-correction level L, versions 1-10 (up to 57x57 modules) —
# far more than a "http://192.168.x.x:8770/" address ever needs. Hand-rolled
# because the project takes no third-party packages.
_QR_EXP = [1] * 256
_QR_LOG = [0] * 256
_qx = 1
for _qi in range(1, 255):
    _qx <<= 1
    if _qx & 0x100:
        _qx ^= 0x11D
    _QR_EXP[_qi] = _qx
    _QR_LOG[_qx] = _qi
_QR_EXP[255] = _QR_EXP[0]


def _qr_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _QR_EXP[(_QR_LOG[a] + _QR_LOG[b]) % 255]


def _qr_gen_poly(n):
    g = [1]
    for i in range(n):
        ng = [0] * (len(g) + 1)
        for j, gv in enumerate(g):
            ng[j] ^= gv
            ng[j + 1] ^= _qr_mul(gv, _QR_EXP[i])
        g = ng
    return g


def _qr_ecc(data, n):
    res = list(data) + [0] * n
    gen = _qr_gen_poly(n)
    for i in range(len(data)):
        coef = res[i]
        if coef:
            for j, gv in enumerate(gen):
                res[i + j] ^= _qr_mul(gv, coef)
    return res[len(data):]


# version -> (total data codewords, ecc codewords per block, [block sizes]) at ECC L.
# Stops at version 6 (41x41): a LAN address is ~30 bytes and version 6 holds
# 136, so this never runs out — and it keeps the alignment-pattern placement
# simple (no patterns land on the timing row until version 7).
_QR_L = {
    1: (19, 7, [19]), 2: (34, 10, [34]), 3: (55, 15, [55]), 4: (80, 20, [80]),
    5: (108, 26, [108]), 6: (136, 18, [68, 68]),
}
_QR_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}


def _qr_matrix(text):
    raw = text.encode("utf-8")
    cbits = 8
    for ver in range(1, 7):
        if 4 + cbits + 8 * len(raw) <= _QR_L[ver][0] * 8:
            break
    else:
        raise ValueError("QR payload too long")
    total_dc, ecc_n, blocks = _QR_L[ver]

    bits = []
    def put(val, n):
        bits.extend((val >> k) & 1 for k in range(n - 1, -1, -1))
    put(0b0100, 4)                       # byte mode
    put(len(raw), cbits)
    for b in raw:
        put(b, 8)
    put(0, min(4, total_dc * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    cw = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pi = 0
    while len(cw) < total_dc:
        cw.append(0xEC if pi % 2 == 0 else 0x11)
        pi += 1

    dblk, eblk, pos = [], [], 0
    for size in blocks:
        b = cw[pos:pos + size]
        pos += size
        dblk.append(b)
        eblk.append(_qr_ecc(b, ecc_n))
    ordered = []
    for i in range(max(len(b) for b in dblk)):
        for b in dblk:
            if i < len(b):
                ordered.append(b[i])
    for i in range(ecc_n):
        for b in eblk:
            ordered.append(b[i])
    stream = []
    for v in ordered:
        stream.extend((v >> k) & 1 for k in range(7, -1, -1))

    size = 17 + 4 * ver
    mat = [[0] * size for _ in range(size)]
    res = [[False] * size for _ in range(size)]

    def fset(r, cc, v):
        mat[r][cc] = v
        res[r][cc] = True

    def finder(r0, c0):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, cc = r0 + dr, c0 + dc
                if 0 <= r < size and 0 <= cc < size:
                    on = (0 <= dr <= 6 and 0 <= dc <= 6 and
                          (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)))
                    fset(r, cc, 1 if on else 0)
    finder(0, 0); finder(0, size - 7); finder(size - 7, 0)

    for i in range(size):
        if not res[6][i]:
            fset(6, i, 1 if i % 2 == 0 else 0)
        if not res[i][6]:
            fset(i, 6, 1 if i % 2 == 0 else 0)

    ac = _QR_ALIGN[ver]
    for r in ac:
        for cc in ac:
            if res[r][cc]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    on = dr in (-2, 2) or dc in (-2, 2) or (dr == 0 and dc == 0)
                    fset(r + dr, cc + dc, 1 if on else 0)

    # Format-info module positions, bit 0 (LSB) .. bit 14 (MSB). Copy 1 wraps
    # the top-left finder; copy 2 is split across the other two finders. Bit 7
    # of copy 2 sits at (8, size-8) — NOT on the always-dark module at
    # (size-8, 8), which is a separate function module.
    fmt_a = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
             (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    fmt_b = [(size - 1 - i, 8) for i in range(7)] + [(8, size - 8 + i) for i in range(8)]
    for (r, cc) in fmt_a + fmt_b:
        res[r][cc] = True
    res[size - 8][8] = True                      # always-dark module

    di = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        for r in (range(size - 1, -1, -1) if upward else range(size)):
            for cc in (col, col - 1):
                if not res[r][cc]:
                    mat[r][cc] = stream[di] if di < len(stream) else 0
                    di += 1
        upward = not upward
        col -= 2

    def mask_bit(r, cc, m):
        return [(r + cc) % 2, r % 2, cc % 3, (r + cc) % 3,
                (r // 2 + cc // 3) % 2, (r * cc) % 2 + (r * cc) % 3,
                ((r * cc) % 2 + (r * cc) % 3) % 2,
                ((r + cc) % 2 + (r * cc) % 3) % 2][m] == 0

    def penalty(g):
        n = len(g)
        pen = 0
        lines = [list(row) for row in g] + [list(c) for c in zip(*g)]
        for ln in lines:
            run = 1
            for i in range(1, n):
                if ln[i] == ln[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        pen += 3 + (run - 5)
                    run = 1
            if run >= 5:
                pen += 3 + (run - 5)
        for r in range(n - 1):
            for cc in range(n - 1):
                if g[r][cc] == g[r][cc + 1] == g[r + 1][cc] == g[r + 1][cc + 1]:
                    pen += 3
        p1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
        p2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
        for ln in lines:
            for i in range(n - 10):
                seg = ln[i:i + 11]
                if seg == p1 or seg == p2:
                    pen += 40
        dark = sum(sum(row) for row in g)
        pen += 10 * (abs(dark * 100 // (n * n) - 50) // 5)
        return pen

    best = None
    for m in range(8):
        g = [[(mat[r][cc] ^ 1 if (not res[r][cc] and mask_bit(r, cc, m)) else mat[r][cc])
              for cc in range(size)] for r in range(size)]
        data5 = (0b01 << 3) | m
        rem = data5
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        full = ((data5 << 10) | rem) ^ 0x5412
        # format string is laid along each path most-significant bit first
        for k, (r, cc) in enumerate(fmt_a):
            g[r][cc] = (full >> (14 - k)) & 1
        for k, (r, cc) in enumerate(fmt_b):
            g[r][cc] = (full >> (14 - k)) & 1
        g[size - 8][8] = 1
        pv = penalty(g)
        if best is None or pv < best[0]:
            best = (pv, g)
    return best[1]


def _qr_png(text, scale=6, quiet=4):
    g = _qr_matrix(text)
    n = len(g)
    dim = (n + 2 * quiet) * scale
    px = bytearray(b"\xff" * (dim * dim * 4))
    for i in range(3, len(px), 4):
        px[i] = 255
    for y in range(n):
        for x in range(n):
            if g[y][x]:
                for sy in range((y + quiet) * scale, (y + quiet + 1) * scale):
                    base = (sy * dim + (x + quiet) * scale) * 4
                    for sx in range(scale):
                        o = base + sx * 4
                        px[o] = px[o + 1] = px[o + 2] = 0
    return _png(bytes(px), dim, dim)


# ----------------------------------------------------------------- icon maker
def _png(rgba, w, h):
    """Minimal RGBA PNG encoder (stdlib only)."""
    import struct, zlib
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def _render(size, tile=True, palette="dark", mono=None):
    """Draw the tracker mark: four card-spine bars ("Ruckenreihe" concept,
    logo review option 10). With tile=True they sit on the dark rounded
    square used for the Dock / taskbar / browser-tab icon; with tile=False
    only the bars are drawn (transparent background, zoomed up) for the
    in-app wordmark. palette="light" recolours the pale bars to a dark
    slate so they don't vanish on the light theme's nav bar. mono=(r,g,b)
    (0..1) draws every bar in one flat colour — used for the macOS menu-bar
    icon, which is a monochrome white glyph like the system icons."""
    from math import hypot

    S = size
    buf = bytearray(S * S * 4)

    def rr(px, py, hw, hh, r):                      # signed distance, rounded rect
        qx, qy = abs(px) - hw + r, abs(py) - hh + r
        return min(max(qx, qy), 0.0) + hypot(max(qx, 0.0), max(qy, 0.0)) - r

    def mix(a, b, t):
        return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))

    aa = 1.5 / S                                    # edge softness in unit space
    TOP, BOT = (0.129, 0.161, 0.204), (0.043, 0.059, 0.078)
    if palette == "light":
        GOLD, GOLD2 = (0.576, 0.412, 0.059), (0.706, 0.514, 0.098)
        PALE = (0.176, 0.216, 0.271)                # dark slate instead of cream
    else:
        GOLD, GOLD2 = (0.831, 0.651, 0.161), (0.960, 0.820, 0.380)
        PALE = (0.910, 0.894, 0.847)
    # (offset-x, offset-y, half-width, half-height, color, alpha) per bar,
    # left to right, echoing a row of binder/card spines seen edge-on
    if mono is not None:
        PALE = GOLD = GOLD2 = tuple(mono)
    BARS = [
        (-0.37, 0.12, 0.09, 0.28, PALE, 1.0),
        (-0.11, 0.02, 0.09, 0.38, GOLD, 1.0),
        (0.15, -0.06, 0.09, 0.46, GOLD2, 1.0),
        (0.41, 0.06, 0.09, 0.34, PALE, 1.0 if mono is not None else 0.85),
    ]
    BAR_R = 0.06
    zoom = 1.0 if tile else 1.62                    # bars fill the frame when there's no tile

    for y in range(S):
        v = ((y + 0.5) / S * 2 - 1) / zoom
        for x in range(S):
            u = ((x + 0.5) / S * 2 - 1) / zoom
            i = (y * S + x) * 4
            if tile:
                d_tile = rr(u, v, 0.94, 0.94, 0.42)
                a_tile = max(0.0, min(1.0, 0.5 - d_tile / aa))
                if a_tile <= 0.0:
                    continue
                col = mix(TOP, BOT, (v + 1) / 2)
                for cx, cy, hw, hh, color, alpha in BARS:
                    a_bar = max(0.0, min(1.0, 0.5 - rr(u - cx, v - cy, hw, hh, BAR_R) / aa)) * alpha
                    if a_bar > 0:
                        col = mix(col, color, a_bar)
                buf[i] = int(max(0, min(255, col[0] * 255)))
                buf[i + 1] = int(max(0, min(255, col[1] * 255)))
                buf[i + 2] = int(max(0, min(255, col[2] * 255)))
                buf[i + 3] = int(a_tile * 255)
            else:
                # bars don't overlap, so the strongest-covering bar wins the pixel
                best_a, best_c = 0.0, None
                for cx, cy, hw, hh, color, alpha in BARS:
                    a_bar = max(0.0, min(
                        1.0, 0.5 - rr(u - cx, v - cy, hw, hh, BAR_R) / aa))
                    if mono is None:
                        a_bar *= alpha
                    if a_bar > best_a:
                        best_a, best_c = a_bar, color
                if best_c is None:
                    continue
                buf[i] = int(max(0, min(255, best_c[0] * 255)))
                buf[i + 1] = int(max(0, min(255, best_c[1] * 255)))
                buf[i + 2] = int(max(0, min(255, best_c[2] * 255)))
                buf[i + 3] = int(best_a * 255)
    return bytes(buf)


def _icns(sizes):
    """Bundle rendered PNGs into an .icns container."""
    import struct
    table = [(b"icp4", 16), (b"icp5", 32), (b"ic11", 32), (b"ic12", 64),
             (b"ic07", 128), (b"ic13", 256), (b"ic08", 256),
             (b"ic14", 512), (b"ic09", 512), (b"ic10", 1024)]
    body = b""
    for tag, px in table:
        png = sizes[px]
        body += tag + struct.pack(">I", len(png) + 8) + png
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def build_icon(path):
    pngs = {}
    for px in (16, 32, 64, 128, 256, 512, 1024):
        pngs[px] = _png(_render(px), px, px)
    with open(path, "wb") as f:
        f.write(_icns(pngs))
    return path


def _ico(pngs, order=(16, 32, 48, 64, 128, 256)):
    """Bundle rendered PNGs into a Windows .ico (PNG-compressed entries,
    which Windows Vista and newer read natively)."""
    import struct
    entries = [(px, pngs[px]) for px in order if px in pngs]
    offset = 6 + 16 * len(entries)
    head = struct.pack("<HHH", 0, 1, len(entries))
    directory, body = b"", b""
    for px, png in entries:
        dim = 0 if px >= 256 else px                  # 0 in the byte field means 256
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        body += png
        offset += len(png)
    return head + directory + body


def build_ico(path):
    pngs = {px: _png(_render(px), px, px) for px in (16, 32, 48, 64, 128, 256)}
    with open(path, "wb") as f:
        f.write(_ico(pngs))
    return path


def build_windows_exe():
    """Build a single self-contained Binduno.exe with PyInstaller.
    Run this ON Windows:  py binduno.py --build-exe  [--console]
    The .exe needs no Python or anything else on the target machine."""
    if sys.platform != "win32":
        sys.exit("Run this on Windows: py binduno.py --build-exe")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("PyInstaller is missing. Install it once, then re-run:\n"
                 "    py -m pip install --upgrade pyinstaller pystray pillow")
    import subprocess
    # The tray icon needs pystray + Pillow bundled into the .exe. Install them
    # automatically (like --install-app does on macOS) so the user doesn't have
    # to remember — non-fatal if it fails, the .exe just runs without an icon.
    have_tray = True
    try:
        import pystray, PIL  # noqa: F401
    except ImportError:
        print("Installing the tray-icon packages (pystray + pillow) ...")
        rc = subprocess.call([sys.executable, "-m", "pip", "install", "--no-input",
                              "--quiet", "pystray", "pillow"])
        try:
            import pystray, PIL  # noqa: F401
            have_tray = rc == 0
        except ImportError:
            have_tray = False
    if not have_tray:
        print("Note: pystray / pillow unavailable — the .exe will work but with "
              "no tray icon. Install them by hand and rebuild:\n"
              "    py -m pip install pystray pillow\n")
    here = os.path.dirname(os.path.abspath(__file__))
    # PyInstaller deletes the old dist\Binduno.exe near the end of the build; if
    # a previously built copy is still running it holds a lock and the whole
    # build fails with "Access is denied" after minutes of work. Check up front.
    old_exe = os.path.join(here, "dist", "Binduno.exe")
    if os.path.exists(old_exe):
        try:
            os.remove(old_exe)
        except PermissionError:
            sys.exit("Can't overwrite the existing dist\\Binduno.exe — it is "
                     "still running.\nQuit Binduno (tray icon -> Quit, or end "
                     "'Binduno.exe' in Task Manager), then re-run this.")
    ico = os.path.join(here, "Binduno.ico")
    build_ico(ico)
    print(f"Icon written to {ico}")
    args = [sys.executable, "-m", "PyInstaller", "--onefile", "--name", "Binduno",
            "--icon", ico, "--noconfirm",
            "--distpath", os.path.join(here, "dist"),
            "--workpath", os.path.join(here, "build"),
            "--specpath", here]
    if have_tray:
        # --collect-all pulls pystray's backend submodules + Pillow's plugins
        # reliably; the bare hidden-imports missed pystray._win32 in onefile.
        args += ["--collect-all", "pystray", "--collect-all", "PIL",
                 "--hidden-import", "pystray._win32"]
    if "--console" not in sys.argv:
        args.append("--noconsole")                    # plain double-click, no terminal window
    args.append(os.path.abspath(__file__))
    print("Running:", " ".join(args))
    rc = subprocess.run(args).returncode
    if rc == 0:
        print(f"\nDone. Ship this file:  {os.path.join(here, 'dist', 'Binduno.exe')}")
    sys.exit(rc)


def _win_error_box(msg):
    """Surface a fatal startup problem when there is no console (windowed .exe)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(msg), "Binduno", 0x10)
        except Exception:                                       # noqa: BLE001
            pass


# The browser-tab favicon and the Dock/taskbar icon keep the dark rounded
# tile. Inside the app only the bare bars are shown next to the wordmark
# (bigger, no box) — in two colourways so they read on both the dark and the
# light nav bar; CSS picks one by the active theme.
_favicon_b64 = base64.b64encode(_png(_render(64), 64, 64)).decode()
_brand_dark_b64 = base64.b64encode(_png(_render(72, tile=False, palette="dark"), 72, 72)).decode()
_brand_light_b64 = base64.b64encode(_png(_render(72, tile=False, palette="light"), 72, 72)).decode()
PAGE = PAGE.replace(
    "<!--FAVICON-->",
    f'<link rel="icon" type="image/png" href="data:image/png;base64,{_favicon_b64}">')
PAGE = PAGE.replace(
    "<!--BRANDICON-->",
    f'<img class="brandicon bi-d" src="data:image/png;base64,{_brand_dark_b64}" alt="">'
    f'<img class="brandicon bi-l" src="data:image/png;base64,{_brand_light_b64}" alt="">')


CM_USERSCRIPT = r'''// ==UserScript==
// @name         Binduno Cardmarket Helper
// @namespace    binduno.local
// @version      __VERSION__
// @description  Marks Cardmarket single offers by whether the card is already in your Binduno collection.
// @match        *://*.cardmarket.com/*/Magic/*
// @connect      localhost
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @run-at       document-idle
// @noframes
// ==/UserScript==
(function(){
  "use strict";
  var API = "http://localhost:__PORT__";
  console.log("[Binduno] cm-helper __VERSION__ loaded on " + location.href);
  // green = this exact printing owned · yellow = card owned in another
  // set/version/finish · red = not in the collection. unknown* -> no marking.
  var COLOR = { exact:"#3fb950", otherFinish:"#e3b341", otherVersion:"#e3b341",
                otherSet:"#e3b341", missing:"#f0554a" };
  var TINT  = { exact:"rgba(63,185,80,.14)", otherFinish:"rgba(227,179,65,.15)",
                otherVersion:"rgba(227,179,65,.15)", otherSet:"rgba(227,179,65,.15)",
                missing:"rgba(240,85,74,.13)" };
  var LABEL = { exact:"in collection", otherFinish:"other finish", otherVersion:"other version",
                otherSet:"other set", missing:"missing", on:"on", off:"off",
                addPurchase:"Add this purchase to Binduno", adding:"Adding…",
                addFailed:"Failed - is Binduno running?", added:"Added",
                notMatched:"not matched" };   // replaced by the server's copy (Binduno's UI language)
  var CACHE = {};                                             // articleId -> server result
  var enabled = true, started = false;

  // Cross-origin call to the local server. GM_xmlhttpRequest (Violentmonkey /
  // Tampermonkey) or GM.xmlHttpRequest (Safari "Userscripts") bypass the page
  // CSP; plain fetch is a last resort (works only because localhost is exempt
  // from mixed-content blocking and the server sends permissive CORS).
  var gmx = (typeof GM_xmlhttpRequest !== "undefined") ? GM_xmlhttpRequest
          : (typeof GM !== "undefined" && GM && GM.xmlHttpRequest) ? GM.xmlHttpRequest.bind(GM)
          : null;
  function req(method, path, data, cb){
    if(gmx){
      var o = { method:method, url:API+path, timeout:9000,
        onload:function(r){ try{ cb(JSON.parse(r.responseText)); }catch(e){ cb(null); } },
        onerror:function(){ cb(null); }, ontimeout:function(){ cb(null); } };
      if(data){ o.headers = {"Content-Type":"application/json"}; o.data = JSON.stringify(data); }
      try{ gmx(o); }catch(e){ cb(null); }
      return;
    }
    var init = { method:method, mode:"cors" };
    if(data){ init.headers = {"Content-Type":"application/json"}; init.body = JSON.stringify(data); }
    fetch(API+path, init).then(function(r){ return r.json(); }).then(cb).catch(function(){ cb(null); });
  }
  function post(path, data, cb){ req("POST", path, data, cb); }
  function get(path, cb){ req("GET", path, null, cb); }
  // Cardmarket's tooltip JS moves title -> data-bs-original-title and clears
  // title, so read both.
  function ttl(el){ return el ? (el.getAttribute("title") || el.getAttribute("data-bs-original-title") || "").trim() : ""; }
  function parseRow(row){
    var a = row.querySelector(".col-seller a");
    var exp = row.querySelector('a[href*="/Magic/Expansions/"]');
    if(!a || !exp) return null;
    var href = exp.getAttribute("href") || "";
    var foil = false, sp = row.querySelectorAll(".st_SpecialIcon");
    for(var i=0;i<sp.length;i++){ var x = ttl(sp[i]); if(x === "Foil" || x === "Folie") foil = true; }
    return {
      name: (a.textContent || "").trim(),
      setSlug: (href.split("/Magic/Expansions/")[1] || "").split(/[?#]/)[0],
      setTitle: ttl(exp),
      foil: foil
    };
  }
  function rowId(row){ var m = /stockRow(\d+)/.exec(row.id || ""); return m ? m[1] : ""; }
  // Wantlist pages ("Wants" list detail): no offer-scraper markup, so read
  // the wantlist table instead — same idea, "did I already buy this?".
  function parseWantRow(row){
    var a = row.querySelector("td.name a");
    if(!a) return null;
    // A want with no specific printing chosen renders no .expansion-symbol
    // at all - not "not rendered yet", just "any set is fine". Leave
    // setTitle empty then; cm-match already falls back to a by-name-only
    // ownership check when the set can't be resolved.
    var exp = row.querySelector(".expansion-symbol");
    var tern = row.querySelectorAll("td.ternary-header");
    var foilTxt = tern[0] ? (tern[0].textContent || "").trim().toLowerCase() : "";
    return {
      name: (a.textContent || "").trim(),
      setSlug: "",
      setTitle: exp ? ttl(exp) : "",
      foil: foilTxt === "yes" || foilTxt === "ja"
    };
  }
  function wantRowId(row){
    var i = row.querySelector('input[name="checkWantsRow[]"]');
    return i ? (i.getAttribute("data-id-want") || "") : "";
  }
  function paint(row, res, host){
    var status = res.status, c = COLOR[status];
    var b = row.querySelector(".bnd-badge");
    if(b) b.remove();
    if(!c){ row.style.boxShadow=""; row.style.background=""; return; }
    // a left accent bar drawn inside the row — no layout shift, and no
    // top/bottom edges that would double up between adjacent marked rows
    row.style.boxShadow = "inset 4px 0 0 " + c;
    row.style.background = TINT[status] || "";
    b = document.createElement("span");
    b.className = "bnd-badge";
    b.textContent = (LABEL[status] || status) + (res.qty > 0 ? " · " + res.qty + "×" : "");
    b.style.cssText = "display:inline-block;padding:1px 7px;border-radius:4px;white-space:nowrap;"
      + "font:700 11px/1.5 system-ui;vertical-align:middle;background:" + c
      + ";color:" + (status === "missing" || status === "exact" ? "#fff" : "#241c00");
    var h = host || row.querySelector(".col-seller");
    if(!h) return;
    if(h.tagName === "TD"){
      // wantlist rows: card names wrap, so an in-cell badge either drops to
      // its own line or overlaps the name. Put it in the left gutter,
      // outside the table, just left of the checkbox.
      var cell = row.firstElementChild || h;      // <td class="select"> with the checkbox
      cell.style.position = "relative";
      b.style.position = "absolute";
      b.style.right = "100%";
      b.style.marginRight = "6px";
      b.style.top = "50%";
      b.style.transform = "translateY(-50%)";
      cell.appendChild(b);
      return;
    }
    b.style.marginLeft = "8px";
    h.appendChild(b);
  }
  function clearRow(row){
    row.style.boxShadow=""; row.style.background="";
    var b=row.querySelector(".bnd-badge"); if(b) b.remove();
  }
  // Idempotent. Cardmarket builds the rows with JS and re-renders their
  // innards after load, so: never blacklist a row that isn't parseable yet
  // (retry next pass), and re-apply a cached result whenever its badge got
  // wiped. Only genuine server results are cached.
  // quiet by default; turn on from the page with  localStorage.bnd_debug = "1"
  // (then reload). Turn off with  localStorage.removeItem("bnd_debug")
  var DBG = false;
  try{ DBG = localStorage.getItem("bnd_debug") === "1"; }catch(e){}
  if(DBG) console.log("[Binduno] debug logging is ON");
  var INFLIGHT = {};                                          // id -> true while a fetch for it is pending
  function ensure(){
    if(!enabled) return;
    var offers = [].slice.call(document.querySelectorAll(".article-row")).map(function(row){
      return { row:row, id:"o"+rowId(row), parse:parseRow, host:null };
    });
    var wants = [].slice.call(document.querySelectorAll('input[name="checkWantsRow[]"]'))
      .map(function(inp){ return inp.closest("tr"); }).filter(Boolean).map(function(row){
        return { row:row, id:"w"+wantRowId(row), parse:parseWantRow,
                 host:row.querySelector("td.name") };
      });
    var all = offers.concat(wants);
    var need=[], map=[], noId=0, notReady=[], cached=0, inflight=0;
    all.forEach(function(e){
      var row = e.row, id = e.id;
      if(id === "o" || id === "w"){ noId++; return; }
      var hit = CACHE[id];
      if(hit){
        cached++;
        if(COLOR[hit.status] && !row.querySelector(".bnd-badge")) paint(row, hit, e.host);
        return;
      }
      if(INFLIGHT[id]){ inflight++; return; }   // a fetch for this row is already on its way
      var p = e.parse(row);
      if(!p){ notReady.push(id); return; }       // not rendered yet — retry next pass
      p.id = id; p.i = need.length; need.push(p);
      map.push({id:id, row:row, host:e.host});
    });
    if(!need.length) return;
    need.forEach(function(p){ INFLIGHT[p.id] = true; });
    if(DBG) console.log("[Binduno] ensure: rows="+all.length+" noId="+noId+" cached="+cached
      +" inflight="+inflight+" notReady="+notReady.length+" toFetch="+need.length
      + "\n" + need.map(function(p){return p.i+": "+p.name+"  ||  "+(p.setTitle||"(no set icon)");}).join("\n"));
    post("/api/cm-match", {items:need}, function(res){
      need.forEach(function(p){ delete INFLIGHT[p.id]; });
      if(!res || !res.results){ if(DBG) console.warn("[Binduno] cm-match failed", res); return; }
      if(DBG) console.log("[Binduno] results\n" + res.results.map(function(x){
        var m = map[x.i]; return x.i+": "+(m?m.row.textContent.trim().replace(/\s+/g," ").slice(0,40):"?")
          +"  -> "+x.status+" set="+(x.set||"-")+" qty="+(x.qty||0)+" exact="+(x.exactQty||0);
      }).join("\n"));
      res.results.forEach(function(x){
        var m = map[x.i];
        if(!m){ if(DBG) console.warn("[Binduno] no row for i="+x.i, x); return; }
        CACHE[m.id] = x; paint(m.row, x, m.host);
      });
    });
  }
  var mo = new MutationObserver(function(){ clearTimeout(mo._t); mo._t = setTimeout(ensure, 200); });
  function start(){
    if(started) return; started = true;
    mo.observe(document.body, {childList:true, subtree:true});
    ensure();
    setInterval(ensure, 2000);
    initPurchaseImport();
    setInterval(initPurchaseImport, 2000);
  }

  // Purchase pages ("My Purchases" order detail): every article row carries
  // its data straight in data-* attributes, no scraping needed — name,
  // collector number and quantity are exact, only the set still needs the
  // usual slug/title resolution. One button adds the whole order at once.
  var CM_LANG = {1:"en",2:"fr",3:"de",4:"es",5:"it",6:"zh",7:"ja",8:"pt",9:"ru",10:"ko",11:"zh"};
  function purchaseRows(){
    return [].slice.call(document.querySelectorAll("tr[data-article-id][data-expansion-name]"));
  }
  function parsePurchaseRow(row){
    var expLink = row.querySelector('a[href*="/Magic/Expansions/"]');
    var setSlug = "", setTitle = row.getAttribute("data-expansion-name") || "";
    if(expLink){
      setSlug = (expLink.getAttribute("href") || "").split("/Magic/Expansions/")[1] || "";
      setSlug = setSlug.split(/[?#]/)[0];
      setTitle = ttl(expLink) || setTitle;
    }
    var foil = false, ic = row.querySelectorAll(".col-extras [title], .col-extras [data-bs-original-title]");
    for(var i=0;i<ic.length;i++){ var x = ttl(ic[i]); if(x === "Foil" || x === "Folie") foil = true; }
    return {
      name: row.getAttribute("data-name") || "",
      number: row.getAttribute("data-number") || "",
      qty: parseInt(row.getAttribute("data-amount"), 10) || 1,
      setSlug: setSlug, setTitle: setTitle, foil: foil,
      lang: CM_LANG[row.getAttribute("data-language")] || "en"
    };
  }
  function initPurchaseImport(){
    if(document.getElementById("bnd-imp-btn") || !purchaseRows().length) return;
    var b = document.createElement("button");
    b.id = "bnd-imp-btn";
    // Anchor next to the seller name at the top of the page (justify-content:
    // between pushes it to the far right of that row, above the status
    // timeline). Falls back to a floating button if Cardmarket's layout
    // doesn't have that container.
    var anchor = document.getElementById("SellerBuyerInfo");
    b.style.cssText = anchor
      ? "padding:5px 10px;border-radius:6px;border:1px solid #3fb950;background:#1a1d24;"
        + "color:#e8ebef;font:600 12px/1.2 system-ui;cursor:pointer;white-space:nowrap;margin-left:8px"
      : "position:fixed;left:14px;bottom:14px;z-index:99999;padding:7px 12px;"
        + "border-radius:8px;border:1px solid #3fb950;background:#1a1d24;color:#e8ebef;"
        + "font:600 12px/1.2 system-ui;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.4)";
    var reset = function(){ b.textContent = LABEL.addPurchase; };
    reset();
    b.onclick = function(){
      var items = purchaseRows().map(parsePurchaseRow);
      b.disabled = true; b.textContent = LABEL.adding;
      post("/api/cm-purchase-import", {items:items, mode:"add"}, function(res){
        b.disabled = false;
        if(!res || !res.ok){ b.textContent = LABEL.addFailed; setTimeout(reset, 4000); return; }
        b.textContent = LABEL.added + " " + res.cards
          + (res.skipped ? " (" + res.skipped + " " + LABEL.notMatched + ")" : "");
        if(res.skipped && DBG) console.log("[Binduno] purchase-import: not matched", res.skippedNames);
        setTimeout(reset, 6000);
      });
    };
    (anchor || document.body).appendChild(b);
  }

  var btn = document.createElement("button");
  btn.style.cssText = "position:fixed;right:14px;bottom:14px;z-index:99999;padding:7px 12px;"
    + "border-radius:8px;border:1px solid #b7791f;background:#1a1d24;color:#e8ebef;"
    + "font:600 12px/1.2 system-ui;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.4)";
  function render(){ btn.textContent = "Binduno: " + (enabled ? LABEL.on : LABEL.off); btn.style.opacity = enabled ? "1" : ".6"; }
  function saveOn(v){
    try{ if(typeof GM_setValue !== "undefined"){ GM_setValue("on", v); return; } }catch(e){}
    try{ localStorage.setItem("bnd_on", v ? "1" : "0"); }catch(e){}
  }
  function loadOn(){
    try{ if(typeof GM_getValue !== "undefined") return GM_getValue("on", true); }catch(e){}
    try{ var s = localStorage.getItem("bnd_on"); return s === null ? true : s === "1"; }catch(e){}
    return true;
  }
  btn.onclick = function(){
    enabled = !enabled; render();
    saveOn(enabled);
    post("/api/cm-helper-pref", {on:enabled}, function(){});
    if(enabled) ensure();
    else{
      [].slice.call(document.querySelectorAll(".article-row")).forEach(clearRow);
      [].slice.call(document.querySelectorAll('input[name="checkWantsRow[]"]'))
        .forEach(function(inp){ var r = inp.closest("tr"); if(r) clearRow(r); });
    }
  };
  document.body.appendChild(btn);
  console.log("[Binduno] toggle button added");

  enabled = loadOn();
  render();
  get("/api/cm-helper-pref", function(r){
    if(r && typeof r.on === "boolean"){ enabled = r.on; render(); }
    if(r && r.labels){                       // badges + toggle in Binduno's UI language
      LABEL = r.labels;
      render();
      [].slice.call(document.querySelectorAll(".bnd-badge")).forEach(function(b){ b.remove(); });
    }
    start();
    ensure();
  });
  setTimeout(start, 3000);
})();
'''


# The bookmarklet: the same marking logic as the userscript, but as one
# self-contained snippet the browser runs once per click (bookmarklets are
# exempt from page CSP, but can't keep running across navigations). It refreshes
# for ~90 s to catch in-page filtering/sorting, then stops.
CM_BOOKMARKLET = r'''(function(){
var A="http://127.0.0.1:__PORT__",
C={exact:"#3fb950",otherFinish:"#e3b341",otherVersion:"#e3b341",otherSet:"#e3b341",missing:"#f0554a"},
T={exact:"rgba(63,185,80,.14)",otherFinish:"rgba(227,179,65,.15)",otherVersion:"rgba(227,179,65,.15)",otherSet:"rgba(227,179,65,.15)",missing:"rgba(240,85,74,.13)"},
L={exact:"in collection",otherFinish:"other finish",otherVersion:"other version",otherSet:"other set",missing:"missing"},
CA={};
function tl(e){return e?(e.getAttribute("title")||e.getAttribute("data-bs-original-title")||"").trim():"";}
function pr(r){var a=r.querySelector(".col-seller a"),x=r.querySelector('a[href*="/Magic/Expansions/"]');if(!a||!x)return null;
var h=x.getAttribute("href")||"",f=false,s=r.querySelectorAll(".st_SpecialIcon"),i;
for(i=0;i<s.length;i++){var v=tl(s[i]);if(v=="Foil"||v=="Folie")f=true;}
return{name:(a.textContent||"").trim(),setSlug:(h.split("/Magic/Expansions/")[1]||"").split(/[?#]/)[0],setTitle:tl(x),foil:f};}
function id(r){var m=/stockRow(\d+)/.exec(r.id||"");return m?m[1]:"";}
function pt(r,res){var st=res.status,c=C[st],b=r.querySelector(".bnd-b");if(b)b.remove();
if(!c){r.style.boxShadow="";r.style.background="";return;}
r.style.boxShadow="inset 4px 0 0 "+c;r.style.background=T[st]||"";
b=document.createElement("span");b.className="bnd-b";
b.textContent=(L[st]||st)+(res.qty>0?" · "+res.qty+"×":"");
b.style.cssText="display:inline-block;margin-left:8px;padding:1px 7px;border-radius:4px;font:700 11px/1.5 system-ui;vertical-align:middle;background:"+c+";color:"+(st=="missing"||st=="exact"?"#fff":"#241c00");
var hs=r.querySelector(".col-seller");if(hs)hs.appendChild(b);}
function run(){
var rows=[].slice.call(document.querySelectorAll(".article-row")),need=[],map=[];
rows.forEach(function(r){var i=id(r);if(!i)return;
if(CA[i]){if(C[CA[i].status]&&!r.querySelector(".bnd-b"))pt(r,CA[i]);return;}
var p=pr(r);if(!p)return;p.i=need.length;need.push(p);map.push(r);});
if(!need.length)return;
fetch(A+"/api/cm-match",{method:"POST",mode:"cors",headers:{"Content-Type":"application/json"},body:JSON.stringify({items:need})})
.then(function(x){return x.json();}).then(function(res){
if(!res||!res.results)return;
res.results.forEach(function(x){var r=map[x.i];if(r){CA[id(r)]=x;pt(r,x);}});})
.catch(fail);}
function fail(){if(!S.err){S.err=true;
alert("Binduno: could not reach the app on "+A+".\nChrome/Firefox: make sure Binduno is running, then click the bookmarklet again.\nSafari blocks this connection - use the extension method instead.");}
S.stop();}
if(window.__bndS)window.__bndS.stop();
var S=window.__bndS={err:false,n:0,stop:function(){clearInterval(S.iv);try{S.mo.disconnect();}catch(e){}}};
run();
S.iv=setInterval(function(){if(S.err)return;run();if(++S.n>28)S.stop();},3000);
S.mo=new MutationObserver(function(){if(S.err)return;clearTimeout(S.t);S.t=setTimeout(run,300);});
S.mo.observe(document.body,{childList:true,subtree:true});
setTimeout(S.stop,92000);
var t=document.createElement("div");t.textContent="Binduno: checking this page…";
t.style.cssText="position:fixed;right:14px;bottom:14px;z-index:99999;padding:6px 11px;border-radius:8px;background:#1a1d24;color:#e8ebef;border:1px solid #b7791f;font:600 12px system-ui;box-shadow:0 4px 16px rgba(0,0,0,.4)";
document.body.appendChild(t);
setTimeout(function(){t.style.transition="opacity .6s";t.style.opacity="0";setTimeout(function(){t.remove();},700);},4000);
})();'''


APP_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Binduno</string>
<key>CFBundleDisplayName</key><string>Binduno</string>
<key>CFBundleIdentifier</key><string>local.mtg.tracker</string>
<key>CFBundleVersion</key><string>{version}</string>
<key>CFBundleShortVersionString</key><string>{version}</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleExecutable</key><string>run</string>
<key>CFBundleIconFile</key><string>icon</string>
<key>NSHighResolutionCapable</key><true/>
<key>LSMinimumSystemVersion</key><string>11.0</string>
<key>LSUIElement</key><true/>
</dict></plist>
"""

# Bundled Python runtime (python-build-standalone, pinned so a rebuild months
# from now doesn't silently pull a different interpreter). The app ships its
# own interpreter instead of relying on a system python3, so double-clicking
# the .app works on a fresh Mac with nothing else installed — the tradeoff
# the user explicitly chose over a smaller download that depends on the
# system having Python at all.
BUNDLED_PY_VERSION = "3.12.14"
BUNDLED_PY_RELEASE = "20260825"
BUNDLED_PY_URLS = {
    "arm64": f"https://github.com/astral-sh/python-build-standalone/releases/download/"
             f"{BUNDLED_PY_RELEASE}/cpython-{BUNDLED_PY_VERSION}%2B{BUNDLED_PY_RELEASE}"
             f"-aarch64-apple-darwin-install_only_stripped.tar.gz",
    "x86_64": f"https://github.com/astral-sh/python-build-standalone/releases/download/"
              f"{BUNDLED_PY_RELEASE}/cpython-{BUNDLED_PY_VERSION}%2B{BUNDLED_PY_RELEASE}"
              f"-x86_64-apple-darwin-install_only_stripped.tar.gz",
}

RUNNER = r'''#!/bin/bash
# Launcher for Binduno. Uses the Python runtime bundled in Resources/python —
# no system python3 required.
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$HERE/Resources/binduno.py"
PY="$HERE/Resources/python/bin/python3"
LOG="$HOME/Library/Logs/Binduno.log"
mkdir -p "$(dirname "$LOG")"

# A previous run that wedged (menu-bar loop failed to start, etc.) stays
# registered as "the app"; macOS then refuses to open it again. Clear it first.
pkill -f "$SCRIPT" 2>/dev/null && sleep 1

if [ ! -x "$PY" ]; then
  osascript -e 'display alert "Binduno" message "The bundled Python runtime is missing from the app. Re-run the installer (python3 binduno.py --install-app)." as critical'
  exit 1
fi

if [ ! -f "$SCRIPT" ]; then
  osascript -e 'display alert "Binduno" message "The app is incomplete: binduno.py is missing from the bundle. Re-run the installer." as critical'
  exit 1
fi

echo "--- $(date) launching with bundled $PY" >> "$LOG"
"$PY" "$SCRIPT" >> "$LOG" 2>&1
STATUS=$?
# 0 = clean exit (incl. the tray "Quit" menu). 130/137/143 = SIGINT/KILL/TERM,
# i.e. deliberately stopped (Activity Monitor, logout, a kill) — not a crash.
# Only a real unexpected failure gets a dialog.
case $STATUS in
  0|130|137|143) ;;
  *)
    TAIL=$(tail -n 6 "$LOG" | sed 's/"/\\"/g')
    osascript -e "display alert \"Binduno stopped\" message \"Exit code $STATUS.\n\n$TAIL\n\nFull log: $LOG\" as critical"
    ;;
esac
exit $STATUS
'''


def _bundled_python_tarball():
    """Download (once, cached in BASE) the portable Python build for this Mac's CPU."""
    arch = platform.machine()
    url = BUNDLED_PY_URLS.get(arch)
    if not url:
        raise RuntimeError(
            f"No bundled Python build available for architecture '{arch}' "
            f"(only arm64 and x86_64 Macs are supported).")
    cache_dir = os.path.join(BASE, "_runtime_cache")
    os.makedirs(cache_dir, exist_ok=True)
    dest = os.path.join(cache_dir, f"cpython-{BUNDLED_PY_VERSION}-{arch}.tar.gz")
    if not os.path.exists(dest):
        print(f"Downloading the Python {BUNDLED_PY_VERSION} runtime for {arch} "
              f"(~25 MB, once — cached in {cache_dir}) ...")
        urllib.request.urlretrieve(url, dest + ".part")
        os.replace(dest + ".part", dest)
    return dest


def _install_bundled_python(res_dir):
    tarball = _bundled_python_tarball()
    print("Extracting the bundled Python runtime ...")
    with tarfile.open(tarball) as tf:
        tf.extractall(res_dir)          # -> res_dir/python/bin/python3 etc.
    # Add the (only) third-party packages Binduno uses — the menu-bar icon.
    # These go INTO the bundle, so the end user still installs nothing. Failure
    # is non-fatal: the app just runs without the tray icon.
    py = os.path.join(res_dir, "python", "bin", "python3")
    import subprocess
    print("Adding the menu-bar icon support (pystray + Pillow) ...")
    rc = subprocess.call([py, "-m", "pip", "install", "--no-input", "--quiet",
                          "--no-warn-script-location",
                          "pystray", "pillow", "pyobjc-framework-Cocoa"])
    if rc != 0:
        print("  (pip step failed — the app will still work, just without a "
              "menu-bar icon)")


def install_app():
    """Create a self-contained, double-clickable Binduno.app."""
    import shutil
    script = os.path.abspath(__file__)
    home_apps = os.path.expanduser("~/Applications")
    dest_dir = home_apps if os.path.isdir(home_apps) else os.path.dirname(script)
    target = os.path.join(dest_dir, "Binduno.app")
    if os.path.isdir(target):
        shutil.rmtree(target)
    macos = os.path.join(target, "Contents", "MacOS")
    res = os.path.join(target, "Contents", "Resources")
    os.makedirs(macos, exist_ok=True)
    os.makedirs(res, exist_ok=True)

    shutil.copy2(script, os.path.join(res, "binduno.py"))
    with open(os.path.join(target, "Contents", "Info.plist"), "w") as f:
        f.write(APP_PLIST.format(version=VERSION))
    runner = os.path.join(macos, "run")
    with open(runner, "w") as f:
        f.write(RUNNER)
    os.chmod(runner, 0o755)

    _install_bundled_python(res)

    print("Drawing icon ...")
    build_icon(os.path.join(res, "icon.icns"))
    os.system(f'touch "{target}" 2>/dev/null')
    os.system('/System/Library/Frameworks/CoreServices.framework/Frameworks/'
              'LaunchServices.framework/Support/lsregister -f '
              f'"{target}" >/dev/null 2>&1')
    print(f"Installed: {target}")
    print(f"Data:      {BASE}")
    print("Double-click it in Finder, or find it via Spotlight.")


def already_running(url):
    try:
        with urllib.request.urlopen(url + "api/refresh-status", timeout=2):
            return True
    except Exception:                                       # noqa: BLE001
        return False


AUTO_SYNC_INTERVAL = 24 * 3600     # how old cards_updated must be to trigger
AUTO_SYNC_CHECK = 3600             # how often the background thread checks
PRICE_GAP_BACKFILL_DAYS = 2        # missing at least this many days triggers a catch-up


def auto_sync_enabled(c):
    return meta_get(c, "auto_sync") != "0"          # on by default


def _auto_sync_check():
    c = connect()
    if not auto_sync_enabled(c) or REFRESH["running"]:
        return
    last = meta_get(c, "cards_updated")
    stale = True
    if last:
        try:
            stale = (datetime.now() - datetime.fromisoformat(last)
                      ).total_seconds() >= AUTO_SYNC_INTERVAL
        except ValueError:
            stale = True
    if stale:
        log(c, "App", "Automatic daily card data sync started")
        refresh_cards()
        bust()


def _price_gap_check():
    """Two reasons to (re-)run the MTGJSON backfill without being asked:
    never having run it at all (so a fresh install gets 90 days of real
    depth immediately instead of waiting 90 days to accumulate it), or a
    real gap in the daily log — log_price_history only ever writes *today*,
    it never looks backward, so time Binduno wasn't running leaves a hole
    that the ordinary daily sync can never fill on its own. MTGJSON's
    AllPrices always covers the last 90 days up to now, so re-running the
    same backfill used for the initial load transparently fills either
    case — the user shouldn't have to remember to click the button again."""
    c = connect()
    if not price_logging_enabled(c) or REFRESH["running"]:
        return
    if not meta_get(c, "price_backfill_done"):
        log(c, "Price history", "No backfill yet, loading 90 days from MTGJSON")
        backfill_price_history()
        return
    last = c.execute("SELECT MAX(date) FROM price_history").fetchone()[0]
    gap = True
    if last:
        try:
            gap = (datetime.now().date() - datetime.fromisoformat(last).date()
                    ).days >= PRICE_GAP_BACKFILL_DAYS
        except ValueError:
            gap = True
    if gap:
        log(c, "Price history", "Gap since last use detected, backfilling from MTGJSON")
        backfill_price_history()


def auto_sync_loop():
    time.sleep(5)                  # let the server finish starting up first
    while True:
        try:
            _auto_sync_check()
            _price_gap_check()
        except Exception as e:                              # noqa: BLE001
            REFRESH["error"] = str(e)
        time.sleep(AUTO_SYNC_CHECK)


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer, but without HTTPServer.server_bind()'s reverse-DNS
    lookup (socket.getfqdn) — that call hangs ~30 s when binding on 0.0.0.0 on
    a network with slow or missing DNS, which is what caused Binduno to take
    half a minute to open. The FQDN is only used for the cosmetic Server:
    header, so a fixed name is fine."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "binduno"
        self.server_port = self.server_address[1]


def run_tray(url, autoopen=False):
    """Show a menu-bar / system-tray icon with Open / Quit. Optional: needs
    pystray + Pillow, which the packaged builds bundle. If they aren't
    importable (e.g. running the plain script), this returns False and the
    caller keeps the server on the main thread as before. On macOS the tray
    event loop MUST own the main thread, so the HTTP server is expected to be
    running in a background thread by the time this is called. When `autoopen`
    is set the browser is opened from inside the setup callback, after the
    menu-bar item exists."""
    global TRAY_ACTIVE
    try:
        import io as _io
        import pystray
        from PIL import Image
    except Exception:                                          # noqa: BLE001
        return False
    try:
        mac = sys.platform == "darwin"
        if mac:
            # supersample: render at 88 px, hand pystray a 44 px glyph
            img = Image.open(_io.BytesIO(_png(
                _render(88, tile=False, mono=(0, 0, 0)), 88, 88))).resize(
                (44, 44), Image.LANCZOS)
        else:
            img = Image.open(_io.BytesIO(_png(_render(64), 64, 64)))
        icon = pystray.Icon("binduno", img, "Binduno")
        global TRAY_ICON
        TRAY_ICON = icon

        def _open(_i=None, _item=None):
            webbrowser.open(url)

        def _quit(_i=None, _item=None):
            try:
                icon.stop()
            except Exception:                                  # noqa: BLE001
                pass
            os._exit(0)

        icon.menu = pystray.Menu(
            pystray.MenuItem("Open Binduno", _open, default=True),
            pystray.MenuItem("Quit Binduno", _quit),
        )

        def _open_browser():
            if autoopen:
                threading.Timer(0.6, lambda: webbrowser.open(url)).start()

        def _setup(ic):
            ic.visible = True
            if not mac:
                _open_browser()
                return
            # pystray downsizes the icon to the 22 pt bar height with no @2x
            # copy, so on a Retina screen macOS upscales a 22 px bitmap and it
            # looks fuzzy next to the system glyphs. Replace it with a 44 px
            # image flagged as a 22 pt template — one crisp @2x representation.
            ns = None
            try:
                import AppKit
                import Foundation
                png = _png(_render(88, tile=False, mono=(0, 0, 0)), 88, 88)
                data = Foundation.NSData.dataWithBytes_length_(png, len(png))
                ns = AppKit.NSImage.alloc().initWithData_(data)
                ns.setSize_((22, 22))
                ns.setTemplate_(True)
                ic._icon_image = ns                            # keep a ref
                ic._status_item.button().setImage_(ns)
            except Exception:                                  # noqa: BLE001
                pass

            # First launch after an update/rebuild sometimes drops the status
            # item (NSApp still settling / focus handed to the browser). Re-show
            # it a moment later on the main run loop — a no-op when it's fine.
            def _reassert(_t=None):
                try:
                    b = ic._status_item.button()
                    if ns is not None:
                        b.setImage_(ns)
                    b.setHidden_(False)
                    ic.visible = True
                except Exception:                             # noqa: BLE001
                    pass
            try:
                Foundation.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                    1.5, False, lambda _t: _reassert())
            except Exception:                                 # noqa: BLE001
                pass
            _open_browser()

        TRAY_ACTIVE = True
        icon.run(setup=_setup)                                 # blocks
        return True
    except Exception as e:                                     # noqa: BLE001
        TRAY_ACTIVE = False
        print(f"(tray icon unavailable: {e})")
        return False


def main():
    if "--install-app" in sys.argv:
        install_app()
        return
    if "--build-exe" in sys.argv:
        build_windows_exe()
        return
    global PORT
    os.makedirs(BASE, exist_ok=True)
    c = connect(); init(c)
    # An instance of Binduno itself already on the preferred port -> just
    # surface it, don't start a second one.
    if already_running(f"http://127.0.0.1:{PORT}/"):
        print(f"Binduno is already running — opening http://127.0.0.1:{PORT}/")
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return
    # Bind on 0.0.0.0 so phones on the same Wi-Fi can reach it, and walk a
    # few ports up if the preferred one is taken by something else.
    srv, last_err = None, None
    for cand in range(PORT, PORT + 10):
        try:
            srv = _Server(("0.0.0.0", cand), Handler)
            PORT = cand
            break
        except OSError as e:
            last_err = e
    if srv is None:
        msg = (f"Cannot listen on ports {PORT}-{PORT + 9}: {last_err}\n"
               "Something else is using them. Close it, or set BINDUNO_PORT.")
        print(msg)
        _win_error_box(msg)
        sys.exit(1)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Binduno\n  data: {DB}\n  open: {url}\n  stop: Ctrl+C "
          f"or the Quit button in Manage Collection")
    autoopen = not _env("NO_AUTOOPEN", "MTG_TRACKER_NO_AUTOOPEN")
    threading.Thread(target=auto_sync_loop, daemon=True).start()
    threading.Thread(target=_startup_update_check, daemon=True).start()
    # Serve in a background thread so a menu-bar / tray icon can own the main
    # thread (required on macOS). If no tray support is available, run_tray()
    # returns immediately and the server just keeps going on this thread.
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # run_tray opens the browser itself once the menu-bar item is up, so
        # the icon is claimed before the browser steals focus (that focus race
        # left the icon missing on the first launch after an update/rebuild).
        if not run_tray(url, autoopen):
            if autoopen:
                threading.Timer(0.8, lambda: webbrowser.open(url)).start()
            # The .app is a menu-bar agent (LSUIElement). A process that never
            # pumps an event loop is flagged "not responding" by macOS and then
            # blocks every relaunch. If the tray couldn't start, still run a
            # bare NSApplication loop so the app stays launchable.
            if sys.platform == "darwin":
                try:
                    import AppKit
                    _app = AppKit.NSApplication.sharedApplication()
                    _app.setActivationPolicy_(2)     # Prohibited (agent)
                    _app.run()
                except Exception:                    # noqa: BLE001
                    pass
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
