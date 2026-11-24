"""The release step (UPDATES-PLAN.md, step 2): the version list the hub's update check reads, and the files to publish.

    python packaging/build.py release --url-base https://example.org/downloads/r1 [--previous releases.json]

Writes dist/release/: releases.json, CHANGES.md and the installers that are new in this release. It publishes
nothing: uploading those files is a separate step.

releases.json:
    {"feed": 1, "released": "2026-11-24",
     "hub":     {"version": "0.3.0", "api": 1, "file": "AlpacaHub-0.3.0-Setup.exe", "url": "...", "sha256": "...",
                 "size": 13000000, "date": "2026-11-24", "notes": "- ..."},
     "drivers": {"optec": {"version": "1.0.1", "title": "...", "api": 1, "min_hub": "0.3.0", "file": ..., ...}}}

The hub and each driver in drivers/ are taken at the version their source says. One already in the previous list at
that version keeps its entry as it was (its installer is not published again). A new version needs its installer in
dist/ (build it first) and an entry in CHANGES.md; its url is --url-base plus the file name. A driver in the previous
list that this repository no longer has stays listed.
"""
import datetime
import hashlib
import json
import os
import re
import shutil

FEED = 1
_HEAD = re.compile(r"^## +(\S+) +(\d{1,6}(?:\.\d{1,6}){0,3})\b.*$")


class ReleaseError(Exception):
    def __init__(self, problems):
        super().__init__("\n".join(problems))
        self.problems = problems


def vt(v):
    return tuple(int(x) for x in v.split("."))


def parse_changes(text):
    """{(name, version): notes} from CHANGES.md; name is "hub" or a driver key, in lower case."""
    out, cur, lines = {}, None, []
    for ln in text.splitlines() + ["## end 0"]:
        m = _HEAD.match(ln)
        if m or ln.startswith("## "):
            if cur:
                out[cur] = "\n".join(lines).strip()
            cur, lines = ((m.group(1).lower(), m.group(2)) if m else None), []
        elif cur:
            lines.append(ln)
    return {k: v for k, v in out.items() if v}


def current(root):
    """What this repository holds now: ({"version", "api"} for the hub, {key: driver.json} for drivers/)."""
    import sys
    sys.path.insert(0, root)
    from hub import VERSION
    from hub.api import API_VERSION
    drivers = {}
    ddir = os.path.join(root, "drivers")
    for key in sorted(os.listdir(ddir)):
        p = os.path.join(ddir, key, "driver.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                drivers[key] = json.load(f)
    return {"version": VERSION, "api": API_VERSION}, drivers


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_feed(hub, drivers, dist, changes, url_base=None, previous=None, today=None):
    """(feed, [installer paths to publish]). hub/drivers as from current(); changes as from parse_changes()."""
    today = today or datetime.date.today().isoformat()
    previous = previous or {}
    problems, new_files, new_names = [], [], []
    good_url = bool(url_base) and url_base.startswith("https://")
    if previous and previous.get("feed") != FEED:
        problems.append(f"the previous list is feed {previous.get('feed')!r}, this writes feed {FEED}")

    def entry(name, file, version, old, extra):
        if old and old.get("version") == version:
            return old
        if old and vt(old["version"]) > vt(version):
            problems.append(f"{name} {version} is older than the published {old['version']}")
            return old
        path = os.path.join(dist, file)
        notes = changes.get((name.lower(), version))
        if not os.path.isfile(path):
            problems.append(f"{name} {version}: {file} isn't in {dist} (build it first)")
        if not notes:
            problems.append(f"{name} {version}: no entry in CHANGES.md ('## {name} {version} ({today})')")
        if not os.path.isfile(path) or not notes or not good_url:
            new_names.append(f"{name} {version}")
            return None
        new_files.append(path)
        return dict(extra, version=version, file=file, url=url_base.rstrip("/") + "/" + file, sha256=sha256(path),
                    size=os.path.getsize(path), date=today, notes=notes)

    feed = {"feed": FEED, "released": today, "hub": None, "drivers": dict(previous.get("drivers") or {})}
    feed["hub"] = entry("Hub", f"AlpacaHub-{hub['version']}-Setup.exe", hub["version"], previous.get("hub"),
                        {"api": hub["api"]})
    for key, d in drivers.items():
        extra = {"title": d["title"], "api": d["api"]}
        if d.get("min_hub"):
            extra["min_hub"] = d["min_hub"]
        e = entry(key, f"AlpacaHub-{key}-{d['version']}-Setup.exe", d["version"], feed["drivers"].get(key), extra)
        if e:
            feed["drivers"][key] = e
    if new_names and not good_url:
        problems.append(f"new in this release: {', '.join(n for n in new_names)}; give --url-base, an https:// address "
                        "(where the installers will be downloaded from)")
    if not problems and not new_files:
        problems.append("nothing new: every version is already in the previous list")
    if problems:
        raise ReleaseError(problems)
    feed["drivers"] = dict(sorted(feed["drivers"].items()))
    return feed, new_files


def release(root, dist, url_base=None, previous_path=None, today=None):
    """Writes dist/release/ and returns its path."""
    previous = None
    if previous_path:
        with open(previous_path, encoding="utf-8") as f:
            previous = json.load(f)
    changes_path = os.path.join(root, "CHANGES.md")
    with open(changes_path, encoding="utf-8") as f:
        changes = parse_changes(f.read())
    hub, drivers = current(root)
    feed, files = make_feed(hub, drivers, dist, changes, url_base, previous, today)
    out = os.path.join(dist, "release")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    with open(os.path.join(out, "releases.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(feed, f, indent=1, ensure_ascii=False)
        f.write("\n")
    shutil.copy(changes_path, os.path.join(out, "CHANGES.md"))
    for p in files:
        shutil.copy(p, out)
    return out, feed, files
