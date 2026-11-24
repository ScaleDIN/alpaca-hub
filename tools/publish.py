"""Exports this repository's source to the public repository (UPDATES-PLAN.md, section 2).

    python tools/publish.py --check                  what would be published, and the checks (changes nothing)
    python tools/publish.py --to <public clone>      copies it into a local clone of the public repository and stages it
    python tools/publish.py --to <public clone> --commit    ... and commits it as "Release <hub version>"

It never pushes: pushing the public clone is a separate, confirmed step.

What goes: every file git tracks here, except PRIVATE (development notes, session settings, tools for the private
firmware repositories). Refused when:
- a file that goes names personal details (PERSONAL: a name, a home folder, an email address, private repositories);
- this repository has uncommitted changes (the export names the commit it came from);
- a pull request merged on the public repository since the last export isn't here yet: the export would undo it.
  Each export commit carries "Dev-Commit: <sha>". Every public commit after the last one must be either named by a
  "Public-Commit: <sha>" line in a commit here (the usual way: bring the pull request over with gh pr diff, keep its
  author, add that line) or contained in the new tree as it is (its diff applies in reverse to what is staged).
References to the private files (HANDOFF.md, CLAUDE.md, ...) in what goes are listed as notes, not refusals.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PRIVATE = [
    "HANDOFF.md",               # session notes: hardware state, the user's setup
    "CLAUDE.md",                # instructions for coding sessions in this repository
    ".claude/",                 # their settings and launch configurations
    "SOURCES.md",               # where the drivers came from (private repositories)
    "tools/firmware_watch.py",  # watches the private firmware repositories
    "tools/publish-private.txt",  # the personal details to refuse (it names them)
]
# Personal details that must never be published (case-insensitive): a real home folder, plus the patterns in
# tools/publish-private.txt (names, the email address, private repositories), which is never published itself.
# An address that may be published is in ALLOWED_EMAILS.
PERSONAL = [
    r"[A-Za-z]:[\\/]+Users[\\/]+(?![\\/]|<|Public|\{)",     # C:\Users\<someone> (not <name>, Public, {var})
    r"/(?:home|Users)/(?!<)[A-Za-z]",
    r"\\\\tsclient\\[A-Z]\\Users",
]
PRIVATE_PATTERNS = os.path.join(ROOT, "tools", "publish-private.txt")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
ALLOWED_EMAILS = {"noreply@anthropic.com"}
TRAILER = "Dev-Commit:"       # in an export commit: the commit here it came from
PORTED = "Public-Commit:"     # in a commit here: the public commit (a merged pull request) it brings over


def git(*args, cwd=ROOT, check=True, input=None, raw=False):
    """git's output as text (raw: bytes). Input and output go as bytes, so Windows never turns \\n into \\r\\n in a
    diff or a commit message on the way."""
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       input=input.encode("utf-8") if isinstance(input, str) else input)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout if raw else r.stdout.decode("utf-8", "replace")


def private(path):
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in PRIVATE)


def files_to_publish(root=ROOT):
    """(published, left out): the tracked files, as git paths."""
    tracked = [p for p in git("ls-files", "-z", cwd=root).split("\0") if p]
    return [p for p in tracked if not private(p)], [p for p in tracked if private(p)]


def personal_patterns(path=PRIVATE_PATTERNS):
    extra = []
    try:
        with open(path, encoding="utf-8") as f:
            extra = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except OSError:
        pass
    return [re.compile(p, re.I) for p in PERSONAL + extra]


def scan(root, paths, pats=None):
    """([refusal], [note]): personal details in the published files, and references to the private ones."""
    pats = pats if pats is not None else personal_patterns()
    refs = re.compile(r"\b(" + "|".join(re.escape(p.rstrip("/")) for p in PRIVATE if "." in p) + r")\b")
    bad, notes = [], []
    for path in paths:
        try:
            with open(os.path.join(root, path), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (UnicodeDecodeError, OSError):
            continue                                     # binary files: nothing to read
        for n, line in enumerate(lines, 1):
            for p in pats:
                if p.search(line):
                    bad.append(f"{path}:{n}: {line.strip()[:120]}")
            for m in EMAIL.findall(line):
                if m.lower() not in ALLOWED_EMAILS and not m.lower().endswith(("@example.org", "@example.com")):
                    bad.append(f"{path}:{n}: an email address ({m})")
            if refs.search(line):
                notes.append(f"{path}:{n}: names {refs.search(line).group(1)}, which isn't published")
    return sorted(set(bad)), notes


def hub_version(root=ROOT):
    with open(os.path.join(root, "hub", "__init__.py"), encoding="utf-8") as f:
        return re.search(r'^VERSION = "([^"]+)"', f.read(), re.M).group(1)


def last_export(clone):
    """(sha of the last export commit in the public clone, its Dev-Commit), or (None, None)."""
    out = git("log", "--format=%H%x00%B%x01", "HEAD", cwd=clone, check=False)
    for rec in out.split("\x01"):
        if "\x00" not in rec:
            continue
        sha, body = rec.strip().split("\x00", 1)
        m = re.search(re.escape(TRAILER) + r"\s*([0-9a-f]{7,40})", body)
        if m:
            return sha, m.group(1)
    return None, None


def copy_into(clone, paths, root=ROOT):
    """Makes the clone's working tree exactly paths (its .git untouched) and stages it."""
    keep = set(paths)
    for p in git("ls-files", "-z", cwd=clone).split("\0"):
        if p and p not in keep:
            try:
                os.remove(os.path.join(clone, p))
            except FileNotFoundError:
                pass
    for p in paths:
        dst = os.path.join(clone, p)
        os.makedirs(os.path.dirname(dst) or clone, exist_ok=True)
        shutil.copy2(os.path.join(root, p), dst)
    git("add", "-A", cwd=clone)


def ported(root=ROOT):
    """The public commits named by a "Public-Commit: <sha>" line in this repository's history (a pull request
    brought over from the public repository)."""
    out = git("log", "--format=%B", "--grep", PORTED, cwd=root, check=False)
    return set(re.findall(re.escape(PORTED) + r"\s*([0-9a-f]{7,40})", out))


def undone_pull_requests(clone, root=ROOT):
    """Public commits since the last export that this export would undo: [(sha, subject)]. A commit is in when a
    commit here names it (Public-Commit:), or when its change is in the staged tree as it is (it applies in reverse)."""
    last, _ = last_export(clone)
    if last is None:
        return []
    named = ported(root)
    out = []
    for line in git("log", "--format=%H %s", f"{last}..HEAD", cwd=clone).splitlines():
        sha, subject = line.split(" ", 1)
        if any(sha.startswith(n) for n in named):
            continue
        diff = git("diff", f"{sha}^", sha, "--binary", cwd=clone, raw=True)
        if diff.strip() and subprocess.run(["git", "apply", "--check", "-R", "--cached"], cwd=clone, input=diff,
                                           capture_output=True).returncode != 0:
            out.append((sha, subject))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="show what would be published and the checks, change nothing")
    ap.add_argument("--to", metavar="CLONE", help="a local clone of the public repository")
    ap.add_argument("--commit", action="store_true", help="with --to: commit the export")
    a = ap.parse_args()
    if not a.check and not a.to:
        ap.error("give --check or --to")

    published, left_out = files_to_publish()
    bad, notes = scan(ROOT, published)
    print(f"{len(published)} files published, {len(left_out)} left out: {', '.join(left_out)}")
    for n in notes:
        print("  note: " + n)
    if bad:
        print("\nRefused: personal details in files that would be published:")
        for b in bad:
            print("  " + b)
        return 1
    print("No personal details found.")
    if a.check:
        return 0

    dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty.strip():
        print("\nRefused: commit the changes here first (the export names the commit it came from):\n" + dirty)
        return 1
    clone = os.path.abspath(a.to)
    if not os.path.isdir(os.path.join(clone, ".git")):
        print(f"\nRefused: {clone} isn't a git clone (clone the public repository there first)")
        return 1
    if git("status", "--porcelain", cwd=clone).strip():
        print(f"\nRefused: {clone} has uncommitted changes")
        return 1
    copy_into(clone, published)
    undone = undone_pull_requests(clone)
    if undone:
        git("reset", "--hard", "HEAD", cwd=clone)
        print("\nRefused: these public commits aren't in this repository yet, and the export would undo them. Apply "
              f"each here first (gh pr diff, keeping its author), commit it with a '{PORTED} <sha>' line, and export "
              "again:")
        for sha, subject in undone:
            print(f"  {sha[:10]} {subject}")
        return 1
    stat = git("diff", "--cached", "--shortstat", cwd=clone).strip()
    print(f"\nStaged in {clone}: {stat or 'no changes'}")
    if a.commit and stat:
        dev = git("rev-parse", "HEAD").strip()
        msg = f"Release {hub_version()}\n\nThe source of Alpaca Hub {hub_version()} and its drivers.\n\n{TRAILER} {dev}\n"
        git("commit", "-q", "-F", "-", cwd=clone, input=msg)
        print(f"Committed: {git('log', '--oneline', '-1', cwd=clone).strip()} (not pushed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
