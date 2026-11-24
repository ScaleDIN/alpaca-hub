"""The export to the public repository (tools/publish.py): what is left out, the personal-details check, and that an
export never undoes a pull request merged on the public side."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import publish as P  # noqa: E402

ENV = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.org",
       "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.org"}


def git(cwd, *args, input=None):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", input=input,
                       env=dict(os.environ, **ENV))
    if r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout


def write(root, path, text):
    p = os.path.join(root, path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_finds_personal_details_and_notes_private_references(self):
        # built in pieces, so this file itself passes the check it tests
        home, mail = "C:" + "\\Users\\someone", "person" + "@" + "mail.example.net"
        write(self.dir, "a.py", f"x = 1\npath = r'{home}\\settings'\n")
        write(self.dir, "b.md", f"mail me: {mail}\nok: noreply@anthropic.com, x@example.org\n")
        write(self.dir, "c.md", "See HANDOFF.md section 2.\nC:\\Users\\<name>\\AppData and C:\\Users\\Public are fine.\n")
        write(self.dir, "d.txt", "built at the secret-lab\n")
        bad, notes = P.scan(self.dir, ["a.py", "b.md", "c.md", "d.txt"],
                            pats=[re.compile(p, re.I) for p in P.PERSONAL + ["secret-lab"]])
        self.assertEqual(len(bad), 3, bad)
        self.assertTrue(any(b.startswith("a.py:2") for b in bad))
        self.assertTrue(any(mail in b for b in bad))
        self.assertTrue(any(b.startswith("d.txt:1") for b in bad))
        self.assertEqual(notes, ["c.md:1: names HANDOFF.md, which isn't published"])

    def test_this_repository_publishes_nothing_personal(self):
        published, left_out = P.files_to_publish()
        for p in ("HANDOFF.md", "CLAUDE.md", "tools/publish-private.txt"):
            self.assertTrue(P.private(p), p)
            self.assertNotIn(p, published)
        self.assertIn("hub/host.py", published)
        self.assertIn("LICENSE", published)
        bad, _ = P.scan(ROOT, published)
        self.assertEqual(bad, [], "tools/publish.py --check lists them")


class ExportTest(unittest.TestCase):
    """A fake dev tree and a fake public clone."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.dev = os.path.join(self.dir, "dev")
        self.pub = os.path.join(self.dir, "pub")
        for d in (self.dev, self.pub):
            os.makedirs(d)
            git(d, "init", "-q", "-b", "main")
        write(self.dev, "a.txt", "one\ntwo\nthree\n")
        write(self.dev, "b.txt", "bee\n")
        write(self.pub, "old.txt", "gone after the export\n")
        git(self.pub, "add", "-A")
        git(self.pub, "commit", "-q", "-m", "start")

    def export(self, paths, dev_sha="abc1234"):
        P.copy_into(self.pub, paths, root=self.dev)
        undone = P.undone_pull_requests(self.pub, root=self.dev)
        if not undone:
            git(self.pub, "commit", "-q", "-F", "-", input=f"Release\n\n{P.TRAILER} {dev_sha}\n")
        return undone

    def test_export_mirrors_the_files_and_is_found_again(self):
        self.assertEqual(self.export(["a.txt", "b.txt"]), [])
        self.assertEqual(sorted(git(self.pub, "ls-files").split()), ["a.txt", "b.txt"])
        sha, dev = P.last_export(self.pub)
        self.assertEqual(dev, "abc1234")
        self.assertEqual(sha, git(self.pub, "rev-parse", "HEAD").strip())

    def test_a_merged_pull_request_must_be_in_dev_first(self):
        self.export(["a.txt", "b.txt"])
        write(self.pub, "a.txt", "one\nTWO (from a contributor)\nthree\n")       # merged on the public side
        git(self.pub, "commit", "-q", "-am", "Fix two")
        write(self.dev, "b.txt", "bee, changed in dev\n")
        undone = self.export(["a.txt", "b.txt"], "def5678")
        self.assertEqual([s for _, s in undone], ["Fix two"], "dev doesn't have it: refused")
        git(self.pub, "reset", "-q", "--hard", "HEAD")
        write(self.dev, "a.txt", "one\nTWO (from a contributor)\nthree\n")      # applied in dev
        self.assertEqual(self.export(["a.txt", "b.txt"], "def5678"), [])
        self.assertEqual(P.last_export(self.pub)[1], "def5678")

    def test_a_pull_request_changed_again_in_dev_counts_when_named(self):
        self.export(["a.txt"])
        write(self.pub, "c.txt", "new driver\n")
        git(self.pub, "add", "c.txt")
        git(self.pub, "commit", "-q", "-m", "Add a driver")
        pr = git(self.pub, "rev-parse", "HEAD").strip()
        write(self.dev, "c.txt", "new driver\nimproved in dev\n")          # brought over, then changed again
        self.assertEqual([s for _, s in self.export(["a.txt", "c.txt"], "1111111")], ["Add a driver"])
        git(self.pub, "reset", "-q", "--hard", "HEAD")
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-F", "-", input=f"Add a driver (from the public repository)\n\n{P.PORTED} {pr[:10]}\n")
        self.assertEqual(self.export(["a.txt", "c.txt"], "1111111"), [])


if __name__ == "__main__":
    unittest.main()
