"""Record what an installer adds to Windows, so it can be removed completely later.

Made 2026-11-20 to watch the Wanderer Empire install. Take a snapshot before installing, another after, then compare:

    %LOCALAPPDATA%\\Python\\pythoncore-3.14-64\\python.exe tools\\install_snapshot.py snap before
    (install the program, run it once, connect the devices if you like, close it)
    %LOCALAPPDATA%\\Python\\pythoncore-3.14-64\\python.exe tools\\install_snapshot.py snap after
    %LOCALAPPDATA%\\Python\\pythoncore-3.14-64\\python.exe tools\\install_snapshot.py compare before after

Snapshots and the report go to install-snapshots\\ (ignored by git). Read only: it never changes anything.

What it records:
- files and folders (path, size, time) in Program Files (both), ProgramData, the user's AppData, Documents, Desktop,
  Public, the Start menu, the .NET assembly caches (ASCOM drivers register there), System32\\drivers, and the
  top level of System32 / SysWOW64 / INF
- registry keys under HKLM\\SOFTWARE (64- and 32-bit views), HKCU\\Software, the services key and the system
  environment, each key with a hash of its values (so changed values show too)
- installed programs (uninstall entries), driver packages (pnputil), scheduled tasks, firewall rules

Run without admin rights; folders and keys it may not read are skipped (an installer rarely writes only there).
"""
import gzip
import hashlib
import os
import subprocess
import sys
import time
import winreg
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "install-snapshots"

ENV = os.environ
WIN = ENV.get("SystemRoot", r"C:\Windows")
HOME = ENV["USERPROFILE"]
TREES = [  # walked recursively
    ENV.get("ProgramFiles", r"C:\Program Files"),
    ENV.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ENV.get("ProgramData", r"C:\ProgramData"),
    ENV["APPDATA"],
    ENV["LOCALAPPDATA"],
    os.path.join(HOME, "Documents"),
    os.path.join(HOME, "Desktop"),
    ENV.get("PUBLIC", r"C:\Users\Public"),
    os.path.join(WIN, "Microsoft.NET", "assembly"),
    os.path.join(WIN, "assembly"),
    os.path.join(WIN, "System32", "drivers"),
]
FLAT = [os.path.join(WIN, "System32"), os.path.join(WIN, "SysWOW64"), os.path.join(WIN, "INF"),
        os.path.join(WIN, "System32", "DriverStore", "FileRepository")]
SKIP = [os.path.join(ENV["LOCALAPPDATA"], "Temp"), os.path.join(ENV["LOCALAPPDATA"], "Packages")]

HKLM, HKCU = winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER
REG = [  # (root, name, path, view)
    (HKLM, "HKLM", r"SOFTWARE", winreg.KEY_WOW64_64KEY),
    (HKLM, "HKLM32", r"SOFTWARE", winreg.KEY_WOW64_32KEY),  # the 32-bit view: WOW6432Node, its Classes
    (HKCU, "HKCU", r"Software", 0),
    (HKLM, "HKLM", r"SYSTEM\CurrentControlSet\Services", winreg.KEY_WOW64_64KEY),
    (HKLM, "HKLM", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", winreg.KEY_WOW64_64KEY),
]
UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

# Paths that change on their own: listed apart in the report, not mixed in with the installer's work.
NOISE = [r"\appdata\local\temp", r"\appdata\local\microsoft", r"\appdata\roaming\microsoft\windows\recent",
         r"\user data\\", r"\programdata\microsoft", r"claude", r"alpaca-hub", r"__pycache__", r"\appdata\local\crashdumps",
         r"\microsoft\windows\currentversion\explorer", r"\software\microsoft\windows\shell", r"cloudstore",
         r"\services\bam\state", r"\microsoft\windows nt\currentversion\appcompatflags", r"\sqmclient",
         r"\microsoft\windows defender", r"\software\microsoft\office", r"\microsoft\edge", r"\google\chrome",
         r"\microsoft\onedrive", r"\software\microsoft\speech", r"\gamebar", r"\searchsettings"]


def say(msg):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def is_noise(p):
    p = p.lower()
    return any(n in p for n in NOISE)


# ---- snapshot ---------------------------------------------------------------------------------------------------

def walk(top, out, recurse=True):
    skip = {s.lower() for s in SKIP}
    stack = [top]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink() or e.is_junction():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        out.write(f"D\t{e.path}\t\t\n")
                        if recurse and e.path.lower() not in skip:
                            stack.append(e.path)
                    else:
                        st = e.stat(follow_symlinks=False)
                        out.write(f"F\t{e.path}\t{st.st_size}\t{int(st.st_mtime)}\n")
                except OSError:
                    continue


def reg_walk(root, name, path, view, out):
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            k = winreg.OpenKey(root, p, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with k:
            h = hashlib.sha1()
            i = 0
            while True:
                try:
                    v = winreg.EnumValue(k, i)
                except OSError:
                    break
                h.update(repr(v).encode("utf-8", "replace"))
                i += 1
            out.write(f"{name}\\{p}\t{h.hexdigest()[:16] if i else '-'}\n")
            i = 0
            while True:
                try:
                    stack.append(p + "\\" + winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1


def programs():
    rows = []
    for root, name, view in [(HKLM, "HKLM", winreg.KEY_WOW64_64KEY), (HKLM, "HKLM32", winreg.KEY_WOW64_32KEY),
                             (HKCU, "HKCU", 0)]:
        try:
            k = winreg.OpenKey(root, UNINSTALL, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                vals = {}
                try:
                    with winreg.OpenKey(k, sub) as s:
                        for f in ("DisplayName", "DisplayVersion", "Publisher", "InstallLocation", "UninstallString"):
                            try:
                                vals[f] = str(winreg.QueryValueEx(s, f)[0])
                            except OSError:
                                vals[f] = ""
                except OSError:
                    continue
                rows.append("\t".join([f"{name}\\{UNINSTALL}\\{sub}"] + list(vals.values())))
    return sorted(rows)


def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=300)
        return r.stdout
    except (OSError, subprocess.SubprocessError) as e:
        return f"!! {cmd[0]} failed: {e}\n"


def snap(name):
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with gzip.open(d / "files.tsv.gz", "wt", encoding="utf-8", errors="replace") as out:
        for t in TREES:
            say(f"files: {t}")
            walk(t, out)
        for t in FLAT:
            say(f"files (top level): {t}")
            walk(t, out, recurse=False)
    with gzip.open(d / "registry.tsv.gz", "wt", encoding="utf-8", errors="replace") as out:
        for root, rname, path, view in REG:
            say(f"registry: {rname}\\{path}")
            reg_walk(root, rname, path, view, out)
    say("programs, drivers, tasks, firewall")
    (d / "programs.tsv").write_text("\n".join(programs()) + "\n", encoding="utf-8")
    (d / "drivers.txt").write_text(run(["pnputil", "/enum-drivers"]), encoding="utf-8")
    tasks = run(["schtasks", "/query", "/fo", "csv", "/nh"]).splitlines()
    (d / "tasks.txt").write_text("\n".join(sorted({l.split('","')[0].strip('"') for l in tasks if l})) + "\n",
                                 encoding="utf-8")
    (d / "firewall.txt").write_text(run(["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "verbose"]),
                                    encoding="utf-8")
    say(f"snapshot '{name}' done in {time.time() - t0:.0f} s: {d}")


# ---- compare ----------------------------------------------------------------------------------------------------

def load_files(d):
    m = {}
    with gzip.open(d / "files.tsv.gz", "rt", encoding="utf-8") as f:
        for line in f:
            kind, path, size, mt = line.rstrip("\n").split("\t")
            m[path] = (kind, size, mt)
    return m


def load_reg(d):
    m = {}
    with gzip.open(d / "registry.tsv.gz", "rt", encoding="utf-8") as f:
        for line in f:
            path, h = line.rstrip("\n").rsplit("\t", 1)
            m[path] = h
    return m


def tops(added, existed):
    """Collapse new paths to their topmost new folder/key: one line per new folder, with how many items it holds."""
    groups = {}
    for p in sorted(added, key=str.lower):
        parts = p.split("\\")
        top = p
        for i in range(1, len(parts)):
            anc = "\\".join(parts[:i])
            if anc in added and anc not in existed:
                top = anc
                break
        groups[top] = groups.get(top, 0) + 1
    return groups


def block_lines(path):
    """Firewall/pnputil output as blocks (separated by blank lines), so a new rule or driver shows whole."""
    if not path.exists():
        return []
    blocks, cur = [], []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            cur.append(line.rstrip())
        elif cur:
            blocks.append("\n".join(cur))
            cur = []
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def compare(a, b):
    da, db = OUT / a, OUT / b
    say("loading files")
    fa, fb = load_files(da), load_files(db)
    say("loading registry")
    ra, rb = load_reg(da), load_reg(db)
    rep = [f"# What changed between '{a}' and '{b}'", "",
           f"Snapshots in `{OUT}`. Made {time.strftime('%Y-%m-%d %H:%M')}. "
           "New folders and keys are collapsed to the topmost new one (item count in brackets).", ""]

    def section(title, items, fmt):
        real = [i for i in items if not is_noise(i if isinstance(i, str) else i[0])]
        noise = [i for i in items if i not in real]
        rep.append(f"## {title} ({len(real)})")
        rep.extend(fmt(i) for i in real)
        if not real:
            rep.append("(none)")
        if noise:
            rep.append(f"<details><summary>Probably unrelated (Windows, browsers, this session): {len(noise)}</summary>")
            rep.append("")
            rep.extend(fmt(i) for i in noise)
            rep.append("</details>")
        rep.append("")

    pa, pb = set((da / "programs.tsv").read_text(encoding="utf-8").splitlines()), \
        set((db / "programs.tsv").read_text(encoding="utf-8").splitlines())
    section("Installed programs added", sorted(pb - pa), lambda r: "- " + " | ".join(x for x in r.split("\t") if x))
    section("Installed programs removed", sorted(pa - pb), lambda r: "- " + " | ".join(x for x in r.split("\t") if x))

    added = set(fb) - set(fa)
    g = tops(added, set(fa))
    section("New files and folders", sorted(g.items()), lambda kv: f"- `{kv[0]}`" + (f" ({kv[1]})" if kv[1] > 1 else ""))
    changed = sorted(p for p in set(fa) & set(fb) if fa[p][0] == "F" and fa[p] != fb[p])
    section("Changed files", changed, lambda p: f"- `{p}`")
    removed = set(fa) - set(fb)
    section("Removed files and folders", sorted(tops(removed, set(fb)).items()),
            lambda kv: f"- `{kv[0]}`" + (f" ({kv[1]})" if kv[1] > 1 else ""))

    radd = set(rb) - set(ra)
    section("New registry keys", sorted(tops(radd, set(ra)).items()),
            lambda kv: f"- `{kv[0]}`" + (f" ({kv[1]})" if kv[1] > 1 else ""))
    rchg = sorted(p for p in set(ra) & set(rb) if ra[p] != rb[p])
    section("Registry keys whose values changed", rchg, lambda p: f"- `{p}`")
    section("Registry keys removed", sorted(tops(set(ra) - set(rb), set(rb)).items()),
            lambda kv: f"- `{kv[0]}`" + (f" ({kv[1]})" if kv[1] > 1 else ""))

    for title, fname in [("Driver packages added", "drivers.txt"), ("Firewall rules added", "firewall.txt")]:
        ba, bb = block_lines(da / fname), block_lines(db / fname)
        new = [x for x in bb if x not in set(ba)]
        rep.append(f"## {title} ({len(new)})")
        rep.extend(["```", "\n\n".join(new), "```"] if new else ["(none)"])
        rep.append("")
    ta, tb = set((da / "tasks.txt").read_text(encoding="utf-8").splitlines()), \
        set((db / "tasks.txt").read_text(encoding="utf-8").splitlines())
    section("Scheduled tasks added", sorted(tb - ta), lambda t: f"- `{t}`")

    out = OUT / f"report-{a}-{b}.md"
    out.write_text("\n".join(rep) + "\n", encoding="utf-8")
    say(f"report: {out}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "snap":
        snap(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == "compare":
        compare(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
