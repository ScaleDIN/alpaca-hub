"""hub --firewall: lets other computers on a private network reach the installed hub again (the Start menu entry
"Allow network access").

Windows asks "allow public and private networks?" the first time the hub listens on a network its rule doesn't
cover (a Public one). Cancel there adds block rules for the program, and a block rule beats any allow rule, so the
installer's rule stops working with no way back short of Windows Firewall's own settings. This removes every
inbound rule for the hub's two programs (Windows' block rules, any earlier allow rules, our own) and adds the
installer's allow rules again: private and domain networks only, never public.
"""
import ctypes
import os
import subprocess
import sys

RULE = "Alpaca Hub"                    # the installer's rule name (packaging/hub.iss, FwRule)
PROGRAMS = ("AlpacaHub.exe", "alpaca-hub.exe")
PROFILES = "private,domain"


def programs(folder):
    return [os.path.join(folder, p) for p in PROGRAMS if os.path.isfile(os.path.join(folder, p))]


def commands(paths):
    """The netsh argument lists, in order: first every inbound rule for each program, then our allow rules."""
    out = [["advfirewall", "firewall", "delete", "rule", f"name={RULE}"]]
    out += [["advfirewall", "firewall", "delete", "rule", "name=all", "dir=in", f"program={p}"] for p in paths]
    out += [["advfirewall", "firewall", "add", "rule", f"name={RULE}", "dir=in", "action=allow", f"program={p}",
             f"profile={PROFILES}", "enable=yes"] for p in paths]
    return out


def _netsh(args):
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "netsh.exe")
    r = subprocess.run([exe] + args, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _elevate():
    """Starts this program again with --firewall as administrator (Windows asks). True if it started."""
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, "--firewall", None, 1) > 32


def run(folder=None, netsh=_netsh, is_admin=_is_admin, elevate=_elevate, pause=True):
    """Exit code 0 when the rules are in place (or the elevated copy was started)."""
    if not getattr(sys, "frozen", False) and folder is None:
        print("--firewall is for the installed hub (its AlpacaHub.exe); nothing to do from source")
        return 1
    folder = folder or os.path.dirname(sys.executable)
    paths = programs(folder)
    if not paths:
        print(f"no Alpaca Hub programs in {folder}")
        return 1
    if not is_admin():
        if elevate():
            return 0
        print("Windows Firewall rules need administrator rights, and that was declined")
        return 1
    ok = True
    for args in commands(paths):
        code, text = netsh(args)
        # deleting finds nothing the first time ("No rules match"): not an error
        if code != 0 and args[2] == "add":
            ok = False
            print(f"netsh {' '.join(args)}: {text}")
    if ok:
        print("Alpaca Hub can be reached from other computers on private networks again.\n"
              "If it still can't: is this PC's network set to Private (Windows Settings > Network & internet)?")
    else:
        print("Could not add the firewall rules (see above).")
    if pause:
        try:
            input("Press Enter to close.")
        except (EOFError, RuntimeError):
            pass
    return 0 if ok else 1
