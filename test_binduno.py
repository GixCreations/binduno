"""Unit tests for Binduno's pure and DB-backed helpers.

Run:  python3 -m unittest test_binduno -v
Not part of the app — never imported by binduno.py, so it isn't bundled by
--build-exe / --install-app.
"""
import datetime as _dt
import importlib.util
import sqlite3
import sys
import unittest

sys.argv = ["binduno.py"]                       # keep argv clean before import
_spec = importlib.util.spec_from_file_location("binduno", "binduno.py")
b = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(b)                      # guarded by __main__, safe to import


def fresh_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    b.init(c)                                    # creates + migrates the schema
    return c


def add_set(c, code, name, **kw):
    c.execute("INSERT INTO sets(code,name,set_type,released,printed_size,icon,digital,parent,lang_only)"
              " VALUES(?,?,?,?,?,?,?,?,?)",
              (code, name, kw.get("set_type", "expansion"), kw.get("released", "2020-01-01"),
               kw.get("printed_size", 0), "", kw.get("digital", 0), None, None))


def add_card(c, set_code, number, name, **kw):
    cols = dict(num_int=int("".join(ch for ch in number if ch.isdigit()) or 0),
                type_line="", rarity="r", eur=kw.get("eur"), eur_foil=kw.get("eur_foil"),
                booster=0, digital=0, extra=kw.get("extra", 0), mana="", cmc=0, oracle="",
                artist="", colors="", pt="", cm_uri=kw.get("cm_uri", ""), scry_uri="", legal="",
                variant="", finishes="normal", ver=1, extras_idx=0,
                cm_suffix=kw.get("cm_suffix", ""), cm_ver=kw.get("cm_ver", 1),
                cm_product_id=kw.get("cm_product_id", 0), cm_expansion=kw.get("cm_expansion"),
                name_de="", type_de="", oracle_de="")
    keys = ["set_code", "number", "name"] + list(cols)
    vals = [set_code, number, name] + list(cols.values())
    c.execute("INSERT INTO cards(%s) VALUES(%s)" % (",".join(keys), ",".join("?" * len(keys))), vals)


def own(c, set_code, number, name, qty, foil="normal", lang="en"):
    c.execute("INSERT INTO collection(set_code,number,name,qty,lang,foil) VALUES(?,?,?,?,?,?)",
              (set_code, number, name, qty, lang, foil))


class NormName(unittest.TestCase):
    def test_collapses_doubled(self):
        self.assertEqual(b._norm_name("Fire // Fire"), "Fire")
        self.assertEqual(b._norm_name("  Boseiju, Who Endures // Boseiju, Who Endures  "),
                         "Boseiju, Who Endures")

    def test_keeps_real_dfc(self):
        self.assertEqual(b._norm_name("Fire // Ice"), "Fire // Ice")
        self.assertEqual(b._norm_name("Lightning Bolt"), "Lightning Bolt")


class VerTuple(unittest.TestCase):
    def test_ordering(self):
        self.assertGreater(b._ver_tuple("6.06"), b._ver_tuple("6.5"))
        self.assertGreater(b._ver_tuple("6.03"), b._ver_tuple("5.101"))
        self.assertGreater(b._ver_tuple("6.0"), b._ver_tuple("5.99"))
        self.assertEqual(b._ver_tuple("6.06"), (6, 6))


class CmProductRe(unittest.TestCase):
    def test_extract(self):
        m = b._CM_PID_RE.search(
            "https://www.cardmarket.com/en/Magic/Products?idProduct=794071&referrer=scryfall")
        self.assertEqual(m.group(1), "794071")

    def test_none(self):
        self.assertIsNone(b._CM_PID_RE.search("https://www.cardmarket.com/en/Magic/Products/Singles/x"))


class Shipping(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(b.shipping(0, 100), 0.0)

    def test_untracked_small_order(self):
        # 5 cards worth 10 EUR -> 1 order, under 25 EUR -> untracked rate
        rate = b.SHIP_RATES["DE"][1]
        self.assertEqual(b.shipping(5, 10.0, country="DE"), round(rate, 2))

    def test_tracked_when_over_25(self):
        rate = b.SHIP_RATES["DE"][2]
        self.assertEqual(b.shipping(5, 40.0, country="DE"), round(rate, 2))

    def test_tracked_forced(self):
        rate = b.SHIP_RATES["DE"][2]
        self.assertEqual(b.shipping(5, 1.0, tracked_only=True, country="DE"), round(rate, 2))

    def test_multiple_orders(self):
        n = b.CARDS_PER_SELLER * 3 + 1              # -> 4 orders
        out = b.shipping(n, 4.0, country="DE")
        self.assertEqual(out, round(4 * b.SHIP_RATES["DE"][1], 2))


class ParseDecklist(unittest.TestCase):
    def names(self, parsed):
        return [p["name"] for p in parsed]

    def test_plain_no_counts(self):
        p = b.parse_decklist("Sol Ring\nArcane Signet\nCommand Tower", "plain")
        self.assertEqual(self.names(p), ["Sol Ring", "Arcane Signet", "Command Tower"])
        self.assertTrue(all(x["section"] == "deck" and x["qty"] == 1 for x in p))

    def test_mtga_blank_line_starts_sideboard(self):
        p = b.parse_decklist("1 Sol Ring\n1 Mana Crypt\n\n1 Brainstorm", "mtga")
        secs = {x["name"]: x["section"] for x in p}
        self.assertEqual(secs["Sol Ring"], "deck")
        self.assertEqual(secs["Brainstorm"], "sideboard")

    def test_archidekt_set_number_foil(self):
        p = b.parse_decklist("1x Sol Ring (LTC) 284 *F*", "archidekt")
        self.assertEqual(len(p), 1)
        it = p[0]
        self.assertEqual((it["qty"], it["name"], it["set"], it["num"], it["foil"]),
                         (1, "Sol Ring", "ltc", "284", True))

    def test_archidekt_category_headers_become_sections(self):
        txt = ("Commander\n1x Magda, Brazen Outlaw (khm) 142\n\n"
               "Ramp\n1x Sol Ring (ltc) 284\n1x Arcane Signet (ltc) 284\n\n"
               "Burn\n1x Lightning Bolt (2x2) 117\n")
        p = b.parse_decklist(txt, "auto")
        self.assertEqual(self.names(p),
                         ["Magda, Brazen Outlaw", "Sol Ring", "Arcane Signet", "Lightning Bolt"])
        secs = {x["name"]: x["section"] for x in p}
        self.assertEqual(secs["Magda, Brazen Outlaw"], "commander")
        self.assertEqual(secs["Sol Ring"], "ramp")
        self.assertEqual(secs["Lightning Bolt"], "burn")

    def test_no_false_headers_in_plain_list(self):
        # a bare list with only a couple of lines must NOT treat any as a header
        p = b.parse_decklist("Sol Ring\nArcane Signet\nPonder\nOpt\nDuress\nBrainstorm", "plain")
        self.assertEqual(len(p), 6)
        self.assertTrue(all(x["section"] == "deck" for x in p))

    def test_sb_prefix_and_comment_header(self):
        p = b.parse_decklist("1 Sol Ring\n// Sideboard\n1 Pyroblast\nSB: 1 Red Elemental Blast", "plain")
        secs = {x["name"]: x["section"] for x in p}
        self.assertEqual(secs["Pyroblast"], "sideboard")
        self.assertEqual(secs["Red Elemental Blast"], "sideboard")

    def test_dfc_slash_name(self):
        p = b.parse_decklist("1 Fire / Ice (2X2) 296", "archidekt")
        self.assertEqual(p[0]["name"], "Fire // Ice")

    def test_letter_collector_number(self):
        p = b.parse_decklist("1x Universal Automaton (plst) MH1-235", "archidekt")
        self.assertEqual((p[0]["set"], p[0]["num"]), ("plst", "MH1-235"))

    def test_quantities_aggregate(self):
        p = b.parse_decklist("2 Forest (KHM) 282\n3 Forest (KHM) 282", "archidekt")
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["qty"], 5)


class ResolveCmSet(unittest.TestCase):
    def setUp(self):
        self.c = fresh_db()
        add_set(self.c, "khm", "Kaldheim")
        add_set(self.c, "sld", "Secret Lair Drop")
        add_set(self.c, "ltc", "Tales of Middle-earth Commander")

    def test_by_slug(self):
        code, extras = b.resolve_cm_set(self.c, "kaldheim", "Kaldheim")
        self.assertEqual(code, "khm")
        self.assertFalse(extras)

    def test_secret_lair_slug(self):
        code, _ = b.resolve_cm_set(self.c, "secret-lair-drop-series", "Secret Lair Drop Series")
        self.assertEqual(code, "sld")

    def test_extras_flag(self):
        code, extras = b.resolve_cm_set(self.c, "kaldheim", "Kaldheim: Extras")
        self.assertEqual(code, "khm")
        self.assertTrue(extras)

    def test_unknown(self):
        code, _ = b.resolve_cm_set(self.c, "not-a-real-expansion-xyz", "Not A Real Expansion XYZ")
        self.assertIsNone(code)


class SecretLairCodes(unittest.TestCase):
    def test_matches_by_name(self):
        c = fresh_db()
        add_set(c, "sld", "Secret Lair Drop")
        add_set(c, "slc", "Secret Lair Countdown")
        add_set(c, "khm", "Kaldheim")
        self.assertEqual(b.secret_lair_codes(c), {"sld", "slc"})


class OwnsName(unittest.TestCase):
    def test_front_face_tolerant_sum(self):
        c = fresh_db()
        add_set(c, "khm", "Kaldheim")
        add_set(c, "2x2", "Double Masters 2022")
        add_card(c, "khm", "142", "Magda, Brazen Outlaw")
        add_card(c, "2x2", "1", "Magda, Brazen Outlaw")
        own(c, "khm", "142", "Magda, Brazen Outlaw", 2)
        own(c, "2x2", "1", "Magda, Brazen Outlaw", 1)
        codes = ["khm", "2x2"]
        marks = ",".join("?" * len(codes))
        self.assertEqual(b._owns_name(c, codes, marks, "Magda, Brazen Outlaw"), 3)
        self.assertEqual(b._owns_name(c, codes, marks, "Nonexistent Card"), 0)


class PriceHistorySeries(unittest.TestCase):
    def setUp(self):
        self.c = fresh_db()
        add_set(self.c, "mh2", "Modern Horizons 2")
        add_card(self.c, "mh2", "228", "Liquimetal Torque", eur=2.40, eur_foil=3.10)
        today = _dt.date.today()

        def ins(days_ago, cents):
            d = (today - _dt.timedelta(days=days_ago)).isoformat()
            self.c.execute("INSERT INTO price_history(set_code,number,date,eur_cents,eur_foil_cents)"
                           " VALUES('mh2','228',?,?,?)", (d, cents, cents + 50))
        ins(120, 0)          # a logged zero (no CM price yet) -> must be dropped
        ins(60, 300)
        ins(20, 250)
        self.today = today.isoformat()

    def test_drops_leading_zero_and_anchors(self):
        h = b.price_history_series(self.c, "mh2", "228", 90)
        eurs = [p["eur"] for p in h["series"]]
        self.assertNotIn(0.0, eurs)                 # the logged 0 is gone
        self.assertEqual(h["series"][0]["eur"], 3.0)   # carried in from the 60-day point
        self.assertEqual(h["series"][-1]["d"], self.today)
        self.assertEqual(h["series"][-1]["eur"], 2.40)  # ends at the live price
        self.assertEqual(h["lo"], 2.40)
        self.assertEqual(h["hi"], 3.0)
        self.assertIsNotNone(h["changePct"])

    def test_max_range_returns_all_real_points(self):
        h = b.price_history_series(self.c, "mh2", "228", None)
        # 2 real logged points (300, 250) + today's anchor
        self.assertEqual(h["npts"], 3)

    def test_no_history_flat(self):
        add_card(self.c, "mh2", "999", "Nothing Logged", eur=1.00)
        h = b.price_history_series(self.c, "mh2", "999", 30)
        self.assertEqual(h["npts"], 1)
        self.assertEqual(h["series"][0]["eur"], 1.00)


if __name__ == "__main__":
    unittest.main(verbosity=2)
