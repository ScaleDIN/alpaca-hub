"""The release step (UPDATES-PLAN.md, step 2): the version list the hub's update check reads, and the files to publish.

    python packaging/build.py release --url-base https://example.org/downloads/r1 [--previous releases.json]

Writes dist/release/: releases.json, CHANGES.md, NOTES.md (the release page's text: how to install, what's new) and
every current installer, so each release, and so the "latest" one, has everything a new user needs. It publishes
nothing: uploading those files (all but NOTES.md, which is the page's text) is a separate step.

releases.json:
    {"feed": 1, "released": "2026-11-24",
     "hub":     {"version": "0.3.0", "api": 1, "file": "AlpacaHub-0.3.0-Setup.exe", "url": "...", "sha256": "...",
                 "size": 13000000, "date": "2026-11-24", "notes": "- ..."},
     "drivers": {"optec": {"version": "1.0.1", "title": "...", "api": 1, "min_hub": "0.3.0", "file": ..., ...}}}

The hub and each driver in drivers/ are taken at the version their source says; every url is --url-base plus the
file name. One already in the previous list at that version keeps its date and notes, and its installer in dist/ must
be the published file. A new version needs its installer in dist/ (build it first) and an entry in CHANGES.md. A
driver in the previous list that this repository no longer has stays listed as it was.
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
    """{(name, version): notes} from CHANGES.md; name is "hub" or a driver key, in lower case. A bullet wrapped onto
    indented lines in the file is one line in the notes (the Overview shows them as they are)."""
    out, cur, lines = {}, None, []
    for ln in text.splitlines() + ["## end 0"]:
        m = _HEAD.match(ln)
        if m or ln.startswith("## "):
            if cur:
                out[cur] = "\n".join(lines).strip()
            cur, lines = ((m.group(1).lower(), m.group(2)) if m else None), []
        elif cur:
            if lines and ln[:1] in (" ", "\t") and ln.strip() and lines[-1].strip():
                lines[-1] = lines[-1].rstrip() + " " + ln.strip()
            else:
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
    """(feed, [every current installer to publish], [the names new in this release, e.g. "optec 1.0.3"]).
    hub/drivers as from current(); changes as from parse_changes().

    Every release carries every current installer, so its page (and "latest") always has everything someone needs;
    every url in the list points at this release. A version already published keeps its date and notes, and its
    installer in dist/ must be the published file (same SHA-256): a rebuild of an unchanged version is refused."""
    today = today or datetime.date.today().isoformat()
    previous = previous or {}
    problems, files, new_names = [], [], []
    good_url = bool(url_base) and url_base.startswith("https://")
    if previous and previous.get("feed") != FEED:
        problems.append(f"the previous list is feed {previous.get('feed')!r}, this writes feed {FEED}")

    def entry(name, file, version, old, extra):
        if old and vt(old["version"]) > vt(version):
            problems.append(f"{name} {version} is older than the published {old['version']}")
            return old
        same = bool(old) and old.get("version") == version
        path = os.path.join(dist, file)
        notes = old.get("notes", "") if same else changes.get((name.lower(), version))
        if not os.path.isfile(path):
            problems.append(f"{name} {version}: {file} isn't in {dist} "
                            + ("(every release carries every current installer: download the published one into it, "
                               "e.g. gh release download)" if same else "(build it first)"))
        elif same and old.get("sha256") and sha256(path) != old["sha256"]:
            problems.append(f"{name} {version}: {file} in {dist} isn't the published file (rebuilt?): raise its "
                            "version, or put the published file back")
        if not same and not notes:
            problems.append(f"{name} {version}: no entry in CHANGES.md ('## {name} {version} ({today})')")
        if not same:
            new_names.append(f"{name} {version}")
        if not os.path.isfile(path) or not good_url or (not same and not notes):
            return old if same else None
        files.append(path)
        return dict(extra, version=version, file=file, url=url_base.rstrip("/") + "/" + file, sha256=sha256(path),
                    size=os.path.getsize(path), date=old.get("date", today) if same else today, notes=notes)

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
    if not good_url:
        problems.append("give --url-base, an https:// address (where this release's installers will be downloaded "
                        "from)")
    if not problems and not new_names:
        problems.append("nothing new: every version is already in the previous list")
    if problems:
        raise ReleaseError(problems)
    feed["drivers"] = dict(sorted(feed["drivers"].items()))
    return feed, files, new_names


def release_notes(feed, new_names):
    """The release page's text: how to install (every current installer, linked), then what's new."""
    hub = feed["hub"]
    link = lambda e: f"[{e['file']}]({e['url']})"  # noqa: E731
    out = ["## Install", "",
           f"1. Download and run {link(hub)}: the hub (Windows 10 or 11, 64-bit).",
           "2. Download and run the driver for each device you have:", "",
           "   | Device | Installer |", "   |---|---|"]
    out += [f"   | {e.get('title') or k} | {link(e)} |" for k, e in feed["drivers"].items() if e.get("url")]
    out += ["", "Windows may warn that the publisher is unknown (the installers aren't code-signed yet): choose "
            "*More info*, then *Run anyway*.", "",
            "Already running Alpaca Hub 0.3.1 or newer? It offers these updates on its Overview: press **Install** "
            "there instead.", "", "## What's new", ""]
    for n in new_names:
        key, version = n.rsplit(" ", 1)
        e = hub if key == "Hub" else feed["drivers"][key]
        title = "Alpaca Hub" if key == "Hub" else (e.get("title") or key)
        out += [f"**{title} {version}**", "", e.get("notes", ""), ""]
    out += ["Every version's changes: CHANGES.md, attached below."]
    return "\n".join(out) + "\n"


def release(root, dist, url_base=None, previous_path=None, today=None):
    """Writes dist/release/: releases.json, CHANGES.md, NOTES.md (the release page's text) and every current
    installer. Returns (its path, the list, the installers, the names new in it)."""
    previous = None
    if previous_path:
        with open(previous_path, encoding="utf-8") as f:
            previous = json.load(f)
    changes_path = os.path.join(root, "CHANGES.md")
    with open(changes_path, encoding="utf-8") as f:
        changes = parse_changes(f.read())
    hub, drivers = current(root)
    feed, files, new_names = make_feed(hub, drivers, dist, changes, url_base, previous, today)
    out = os.path.join(dist, "release")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    with open(os.path.join(out, "releases.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(feed, f, indent=1, ensure_ascii=False)
        f.write("\n")
    with open(os.path.join(out, "NOTES.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(release_notes(feed, new_names))
    shutil.copy(changes_path, os.path.join(out, "CHANGES.md"))
    for p in files:
        shutil.copy(p, out)
    return out, feed, files, new_names
