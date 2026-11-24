"""hub --firewall (hub/firewall.py): the rules it removes and adds, and when it asks for administrator rights.
netsh is never really called here."""
import os
import tempfile
import unittest

from hub import firewall


class FirewallTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for p in firewall.PROGRAMS:
            open(os.path.join(self.dir, p), "w").close()
        self.calls = []

    def netsh(self, args):
        self.calls.append(args)
        return (1, "No rules match the specified criteria.") if args[2] == "delete" else (0, "Ok.")

    def test_removes_every_rule_for_the_programs_then_adds_private_allow_rules(self):
        code = firewall.run(self.dir, netsh=self.netsh, is_admin=lambda: True, elevate=None, pause=False)
        self.assertEqual(code, 0)          # nothing to delete the first time is not an error
        deletes = [c for c in self.calls if c[2] == "delete"]
        adds = [c for c in self.calls if c[2] == "add"]
        self.assertEqual(self.calls, deletes + adds)       # every delete first: our new rules must survive
        self.assertIn(["advfirewall", "firewall", "delete", "rule", "name=Alpaca Hub"], deletes)
        for p in firewall.PROGRAMS:
            path = os.path.join(self.dir, p)
            self.assertIn(f"program={path}", [c[-1] for c in deletes])
            add = next(c for c in adds if f"program={path}" in c)
            self.assertIn("action=allow", add)
            self.assertIn("dir=in", add)
            self.assertIn("profile=private,domain", add)     # never public (user, 2026-11-22)
        self.assertFalse(any("public" in a for c in adds for a in c))

    def test_not_admin_starts_an_elevated_copy_and_changes_nothing(self):
        asked = []
        code = firewall.run(self.dir, netsh=self.netsh, is_admin=lambda: False,
                            elevate=lambda: asked.append(1) or True, pause=False)
        self.assertEqual((code, asked, self.calls), (0, [1], []))

    def test_declined_elevation_fails(self):
        code = firewall.run(self.dir, netsh=self.netsh, is_admin=lambda: False, elevate=lambda: False, pause=False)
        self.assertEqual((code, self.calls), (1, []))

    def test_failed_add_fails(self):
        code = firewall.run(self.dir, netsh=lambda a: (1, "denied"), is_admin=lambda: True, elevate=None,
                            pause=False)
        self.assertEqual(code, 1)

    def test_no_programs_in_the_folder(self):
        code = firewall.run(tempfile.mkdtemp(), netsh=self.netsh, is_admin=lambda: True, elevate=None, pause=False)
        self.assertEqual((code, self.calls), (1, []))


if __name__ == "__main__":
    unittest.main()
