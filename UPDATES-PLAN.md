# Plan: the hub finds new versions online

Status: plan only (2026-11-24). Nothing here is built yet. Decided: a public repository with release snapshots of the
source and the releases (development stays in a private repository), and a `CHANGES.md` written at each release. The check starts on, and can be turned off.

## Today

- **Drivers on the same PC already update themselves.** A newer driver installer puts the new version in its own
  folder (`%ProgramData%\AlpacaHub\drivers\<key>\<version>\`). The running hub switches to it at once, or, if an
  Alpaca program is using the device, when that program disconnects.
- **The hub itself** updates when its installer runs: the installer stops the hub, replaces the files, and leaves it
  stopped.
- **Nothing goes online.** The hub makes no internet connections at all. New installers are copied to each PC by
  hand.
- **The installers are not code-signed.** Windows SmartScreen shows "unknown publisher" for them.

## The aim

The Overview says when a newer hub or driver exists, shows what changed, and installs it with one click on the
hub's PC. Nothing installs by itself: a driver swapped in the middle of a night's imaging is the wrong time.

## Design

### 1. A release feed

One small file, `releases.json`, published alongside the installers:

```json
{
  "hub":     {"version": "0.3.0", "url": ".../AlpacaHub-0.3.0-Setup.exe", "sha256": "…", "notes": "…", "api": 1},
  "drivers": {
    "optec": {"version": "1.0.1", "url": ".../AlpacaHub-optec-1.0.1-Setup.exe", "sha256": "…", "notes": "…",
              "api": 1, "min_hub": "0.3.0"}
  }
}
```

- `min_hub` is new. A driver states the oldest hub it works with: this week's drivers need hub 0.3.0's chart kit.
  It goes in `driver.json`, and the driver installer refuses an older hub, as it already does for `api`.
- `build.py release` writes `releases.json` from `dist/` (versions, SHA-256, notes taken from HANDOFF or a
  `CHANGES.md`) and, only when asked, publishes it with the installers.

### 2. Where it lives (decided 2026-11-24)

- **The development repository** (this one): private. Development, HANDOFF and the
  whole history stay here.
- **`ScaleDIN/alpaca-hub`** (public, to be created): at each release a cleaned snapshot of the source as one commit,
  plus the installers, `releases.json` and `CHANGES.md` as a GitHub release. The hub reads
  `https://github.com/ScaleDIN/alpaca-hub/releases/latest/download/releases.json`. MIT licence (`LICENSE`).
- **The export** (`tools/publish.py`, to write in step 5): copies the source into a local clone of the public
  repository without the private files (`HANDOFF.md`, `CLAUDE.md`, `.claude/`, `soak/`, `install-snapshots/`, …),
  refuses if anything left names personal details (names, settings paths, the email address), and refuses if a pull
  request merged on the public repository since the last export isn't in dev yet (an export must never undo one).
  Pushing stays a confirmed step.
- **Pull requests from others** are reviewed and merged on the public repository, then applied here with their
  author kept (`gh pr diff`).

The feed's address is built into the hub. The hub never follows an address it reads from the feed to a different
host.

### 3. The check (`hub/updates.py`)

- A background thread: shortly after start, then once a day. One HTTPS GET of `releases.json` with a 10 s
  timeout. It sends nothing but the request: the user agent is `AlpacaHub/<version>`, with no machine, user, device
  or serial details.
- It compares the feed with what is installed: the hub's `VERSION`, and each driver's running and installed
  versions from the catalog. It offers a driver only when `min_hub` and `api` allow it; otherwise it says "needs hub
  X first".
- Offline, a timeout or a bad feed: it logs one line and tries again the next day. It never bothers the Overview
  about it.
- A setting, **"Check for updates online"**, on the Overview next to the history-days setting, with the date of the
  last check and a "Check now" button. On by default; turned off, the hub sends nothing.

### 4. The Overview

- One line above the devices when something is newer, for example: "Updates: hub 0.3.1, optec 1.0.2. What's
  new". It opens the notes.
- **"Download and install"** appears only when the page is opened on the hub's own PC (the request comes from
  127.0.0.1). A phone or another PC sees "Install it on the hub's PC", because the installer needs that PC's
  administrator prompt.
- While an Alpaca program is connected, the button asks first: "An Alpaca program is connected to the focuser. Install anyway?"
  A driver still waits for the disconnect, as today. The hub restarts, so its clients lose their connection.

### 5. Installing

- The hub downloads into `%LOCALAPPDATA%\alpaca-hub\updates\` and checks the SHA-256 against the feed. It deletes
  the file and says so on a mismatch.
- It runs the installer, and Windows shows its administrator prompt on the hub's PC.
  - **Drivers:** the existing installer as is. The hub switches over by itself.
  - **The hub:** the installer stops the hub, so it needs a new `/RESTART` switch that starts the hub again
    afterwards (its "Start now" box stays unticked for normal installs). The page shows "restarting…" and reloads
    when the new version answers. It already has the "New version: reload" pill.
  - **Both at once:** the hub first, then the drivers.
- Old downloads are deleted after a successful install.

### 6. Trust

- HTTPS and the SHA-256 stop a broken or altered download. They don't help if the release repository itself is
  taken over: anyone who can publish there can ship an installer. For a hobby tool, a GitHub account with two-factor
  login is a fair line.
- Code signing would make SmartScreen trust the installers and let the hub check the signature before running one.
  Azure Trusted Signing (about USD 10/month) or a certificate. Optional, and it can come later.

### 7. Tests

- A local test feed server (the tests already run fake HTTP servers): newer hub, newer driver, a driver that needs a
  newer hub, a bad hash, a feed that times out, the setting off (no request at all).
- Version comparison (`1.0.10` > `1.0.9`).
- The Overview line and that the install button shows only on 127.0.0.1.
- `build.py release` on a fake `dist/`.
- By hand: the hub installer's `/RESTART`, and one real round trip from the release repository on the mini-PC.

### 8. Documents

- **User guide:** what the check sends, how to turn it off, what the install button does.
- **README:** the release step.
- **HANDOFF:** where the feed lives.

## Order of work

1. `min_hub` in `driver.json`, the driver installer's check, the contract tests. Useful now, without any of the rest.
   **Done 2026-11-24:** the four drivers say `0.3.0`; the hub's catalog refuses a driver for a newer hub, `build.py`
   checks the field and the driver installer compares it with the installed hub's version. Hubs up to 0.3.0 ignore it.
2. `releases.json` and `build.py release` (writes the file; publishing stays a separate, confirmed step).
   **Done 2026-11-24:** `packaging/release.py`, `CHANGES.md` (from hub 0.2.2 on), tests in `tests/test_release.py`.
   Each entry also has `file`, `size` and `date`; `released` is the list's date. The driver installers now in `dist/`
   predate `min_hub`: rebuild them before the first real release.
3. `hub/updates.py`, the setting, the Overview line, and the tests.
   **Done 2026-11-24:** `hub/updates.py` (`FEED_URL` is empty until the releases repository exists, so installed
   hubs check nothing yet), hub.json's `updates`, `POST /hub/updates` (on=0/1) and `/hub/updates/check`, the
   Overview's line with what's new and Download links, and its Updates setting. The last result is kept in
   `updates.json`, so a restart doesn't check again. `--update-feed URL` points a hub at another list (a trial run;
   a simulated hub checks only with it). Tests: `tests/test_updates.py`. The user guide's section waits for step 5.
4. Download, hash check, and running the installer; the hub installer's `/RESTART`.
   **Done 2026-11-24:** `Updates.install` downloads into `updates/`, checks size and SHA-256, and runs the
   installer through the Windows shell (`os.startfile`, so the administrator prompt shows); one at a time, https on
   the list's own host only. `POST /hub/updates/install` from the hub's PC only; the Overview's Install / Try again
   buttons with progress, a confirmation (and whether an Alpaca program is connected), and a reload when the hub's
   version changes. The hub installer's switch is `/RESTARTHUB`. Tests: `tests/test_updates.py` (with a stand-in
   for running the installer). **Not yet tried:** a real installer run through Windows' prompt; that comes with the
   first real release (step 5).
5. The release repository and a first real release (0.3.1) to try the whole path on the mini-PC.
   **Done 2026-11-24:** the public repository exists with the 0.3.1 source (`tools/publish.py`, tests in
   `tests/test_publish.py`) and the release `v0.3.1`; the hub's built-in address reads its list. Still to try: an
   Install from the Overview through Windows' prompt, once a later release exists.
6. Later, if wanted: code signing.

## Decisions for you

1. ~~Where releases live~~: **decided 2026-11-24**: the public `ScaleDIN/alpaca-hub` holds release snapshots of the
   source and the releases; development stays in this private repository. MIT licence.
2. ~~On or off~~: **decided 2026-11-24, on by default**, with the Overview setting to turn it off (it then sends
   nothing at all).
3. ~~Release notes~~: **decided 2026-11-24, `CHANGES.md`**, written when a new version is released. The feed's
   `notes` for each hub and driver version come from it.
