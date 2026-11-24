"""The release step (packaging/release.py): the version list for the hub's update check, from CHANGES.md and dist/."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "packaging"))
sys.path.insert(0, ROOT)
import release  # noqa: E402

CHANGES = """# Changes
Intro text.

## Hub 0.4.0 (2026-12-01)
- Hub thing.

## optec 1.1.0 (2026-12-01)
- Optec thing.
  Second line.

## wcover 1.0.3 (2026-11-24)
- Older.

## Not a version heading
- ignored
"""
HUB = {"version": "0.4.0", "api": 1}
DRIVERS = {"optec": {"key": "optec", "title": "Optec", "version": "1.1.0", "api": 1, "min_hub": "0.3.0"},
           "wcover": {"key": "wcover", "title": "Cover", "version": "1.0.3", "api": 1}}
URL = "https://example.org/dl/r2"


class ChangesTest(unittest.TestCase):
    def test_parse(self):
        c = release.parse_changes(CHANGES)
        self.assertEqual(c[("hub", "0.4.0")], "- Hub thing.")
        self.assertEqual(c[("optec", "1.1.0")], "- Optec thing. Second line.", "a wrapped bullet is one line")
        self.assertEqual(set(c), {("hub", "0.4.0"), ("optec", "1.1.0"), ("wcover", "1.0.3")})

    def test_this_repositorys_changes_cover_its_versions(self):
        with open(os.path.join(ROOT, "CHANGES.md"), encoding="utf-8") as f:
            c = release.parse_changes(f.read())
        hub, drivers = release.current(ROOT)
        self.assertIn(("hub", hub["version"]), c, "CHANGES.md has the hub's version")
        for key, d in drivers.items():
            self.assertIn((key, d["version"]), c, f"CHANGES.md has {key} {d['version']}")


class FeedTest(unittest.TestCase):
    def setUp(self):
        self.dist = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dist, True)
        self.changes = release.parse_changes(CHANGES)

    def exe(self, name, body=b"MZ installer"):
        with open(os.path.join(self.dist, name), "wb") as f:
            f.write(body)

    def all_exes(self):
        self.exe("AlpacaHub-0.4.0-Setup.exe", b"hub")
        self.exe("AlpacaHub-optec-1.1.0-Setup.exe", b"optec")
        self.exe("AlpacaHub-wcover-1.0.3-Setup.exe", b"wcover")

    def test_first_release_lists_everything(self):
        self.all_exes()
        feed, files, new = release.make_feed(HUB, DRIVERS, self.dist, self.changes, URL, today="2026-12-01")
        self.assertEqual(new, ["Hub 0.4.0", "optec 1.1.0", "wcover 1.0.3"])
        self.assertEqual(feed["feed"], 1)
        h = feed["hub"]
        self.assertEqual((h["version"], h["api"], h["date"], h["notes"]), ("0.4.0", 1, "2026-12-01", "- Hub thing."))
        self.assertEqual(h["url"], URL + "/AlpacaHub-0.4.0-Setup.exe")
        self.assertEqual(h["sha256"], hashlib.sha256(b"hub").hexdigest())
        self.assertEqual(h["size"], 3)
        o = feed["drivers"]["optec"]
        self.assertEqual((o["title"], o["min_hub"]), ("Optec", "0.3.0"))
        self.assertNotIn("min_hub", feed["drivers"]["wcover"])
        self.assertEqual(len(files), 3)

    def test_every_release_carries_every_current_installer(self):
        self.all_exes()
        prev, _, _ = release.make_feed(HUB, DRIVERS, self.dist, self.changes, URL, today="2026-12-01")
        drivers = dict(DRIVERS, optec=dict(DRIVERS["optec"], version="1.2.0"))
        self.exe("AlpacaHub-optec-1.2.0-Setup.exe", b"optec2")
        changes = {**self.changes, ("optec", "1.2.0"): "- Newer."}
        r3 = "https://example.org/dl/r3"
        feed, files, new = release.make_feed(HUB, drivers, self.dist, changes, r3, prev, today="2026-12-05")
        self.assertEqual(new, ["optec 1.2.0"])
        self.assertEqual(sorted(os.path.basename(f) for f in files),
                         ["AlpacaHub-0.4.0-Setup.exe", "AlpacaHub-optec-1.2.0-Setup.exe", "AlpacaHub-wcover-1.0.3-Setup.exe"])
        for e in [feed["hub"]] + list(feed["drivers"].values()):
            self.assertTrue(e["url"].startswith(r3 + "/"), e["url"])          # all from this release
        self.assertEqual({k: feed["hub"][k] for k in ("version", "date", "notes", "sha256")},
                         {k: prev["hub"][k] for k in ("version", "date", "notes", "sha256")}, "unchanged: same entry")
        self.assertEqual(feed["drivers"]["optec"]["date"], "2026-12-05")
        self.assertEqual(feed["drivers"]["optec"]["notes"], "- Newer.")
        notes = release.release_notes(feed, new)
        self.assertIn("[AlpacaHub-0.4.0-Setup.exe](https://example.org/dl/r3/AlpacaHub-0.4.0-Setup.exe)", notes)
        self.assertIn("| Cover | [AlpacaHub-wcover-1.0.3-Setup.exe]", notes)
        self.assertIn("**Optec 1.2.0**", notes)
        self.assertNotIn("**Cover", notes, "only what's new is listed under What's new")

    def test_an_unchanged_version_must_be_the_published_file(self):
        self.all_exes()
        prev, _, _ = release.make_feed(HUB, DRIVERS, self.dist, self.changes, URL)
        self.exe("AlpacaHub-wcover-1.0.3-Setup.exe", b"wcover rebuilt")             # same version, other bytes
        os.remove(os.path.join(self.dist, "AlpacaHub-0.4.0-Setup.exe"))             # not here at all
        drivers = dict(DRIVERS, optec=dict(DRIVERS["optec"], version="1.2.0"))
        self.exe("AlpacaHub-optec-1.2.0-Setup.exe", b"optec2")
        with self.assertRaises(release.ReleaseError) as e:
            release.make_feed(HUB, drivers, self.dist, {**self.changes, ("optec", "1.2.0"): "- x"}, URL, prev)
        text = str(e.exception)
        self.assertIn("wcover 1.0.3: AlpacaHub-wcover-1.0.3-Setup.exe in", text)
        self.assertIn("isn't the published file", text)
        self.assertIn("Hub 0.4.0: AlpacaHub-0.4.0-Setup.exe isn't in", text)
        self.assertIn("download the published one", text)

    def test_a_driver_no_longer_here_stays_listed(self):
        self.all_exes()
        prev, _, _ = release.make_feed(HUB, DRIVERS, self.dist, self.changes, URL)
        hub = {"version": "0.5.0", "api": 1}
        self.exe("AlpacaHub-0.5.0-Setup.exe", b"hub5")
        feed, _, _ = release.make_feed(hub, {"optec": DRIVERS["optec"]}, self.dist,
                                    {**self.changes, ("hub", "0.5.0"): "- x"}, URL, prev)
        self.assertIn("wcover", feed["drivers"])

    def test_refusals(self):
        self.all_exes()
        os.remove(os.path.join(self.dist, "AlpacaHub-optec-1.1.0-Setup.exe"))
        changes = {k: v for k, v in self.changes.items() if k[0] != "hub"}
        with self.assertRaises(release.ReleaseError) as e:
            release.make_feed(HUB, DRIVERS, self.dist, changes, None)
        text = str(e.exception)
        self.assertIn("AlpacaHub-optec-1.1.0-Setup.exe isn't in", text)
        self.assertIn("Hub 0.4.0: no entry in CHANGES.md", text)
        self.assertIn("give --url-base", text)
        with self.assertRaises(release.ReleaseError) as e:
            release.make_feed(HUB, DRIVERS, self.dist, self.changes, "http://example.org/plain")
        self.assertIn("https://", str(e.exception))

    def test_nothing_new_and_going_backwards(self):
        self.all_exes()
        prev, _, _ = release.make_feed(HUB, DRIVERS, self.dist, self.changes, URL)
        with self.assertRaises(release.ReleaseError) as e:
            release.make_feed(HUB, DRIVERS, self.dist, self.changes, URL, prev)
        self.assertIn("nothing new", str(e.exception))
        with self.assertRaises(release.ReleaseError) as e:
            release.make_feed({"version": "0.3.9", "api": 1}, DRIVERS, self.dist, self.changes, URL, prev)
        self.assertIn("older than the published 0.4.0", str(e.exception))

    def test_release_writes_the_folder(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "drivers", "optec"))
        with open(os.path.join(root, "drivers", "optec", "driver.json"), "w", encoding="utf-8") as f:
            json.dump(DRIVERS["optec"], f)
        from hub import VERSION   # the hub's version comes from this repository's hub package
        with open(os.path.join(root, "CHANGES.md"), "w", encoding="utf-8") as f:
            f.write(CHANGES.replace("Hub 0.4.0", f"Hub {VERSION}"))
        self.exe(f"AlpacaHub-{VERSION}-Setup.exe", b"hub")
        self.exe("AlpacaHub-optec-1.1.0-Setup.exe", b"optec")
        out, feed, files, new = release.release(root, self.dist, URL)
        self.assertEqual(sorted(os.listdir(out)), sorted(["releases.json", "CHANGES.md", "NOTES.md",
                                                          f"AlpacaHub-{VERSION}-Setup.exe", "AlpacaHub-optec-1.1.0-Setup.exe"]))
        with open(os.path.join(out, "releases.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), feed)


if __name__ == "__main__":
    unittest.main()
