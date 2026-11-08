#!/usr/bin/env python3
"""Make files dropped onto VS Code webviews (the Claude Code chat) attach without holding Shift.

Without the patch VS Code intercepts a file dropped on the chat and opens it as an editor
tab; the file attaches to the message only while Shift is held. There is no setting for it.
The behaviour comes from the drag monitor class in the bundled workbench.desktop.main.js:

    G(o,re.DRAG,n=>{n.shiftKey?i():t()})       # i() lets the file into the webview, t() hands it to VS Code
    G(o,re.DRAG_OVER,n=>{n.shiftKey?i():t()})

The patch puts "!" before shiftKey in both places: without Shift the file goes to the
webview, with Shift to VS Code. The places are found by neighbouring text
(windowDidDragStart / windowDidDragEnd), not by names: names change between builds.

There is a third place. The webview itself (pre/index.html) sends VS Code a "drag-start"
message when a file enters without Shift, and VS Code then disables the webview for the
mouse, so the file goes to a tab again (two "!" are not enough). The same bundle handles it:

    this.on("drag-start",()=>{this._startBlockingIframeDragEvents()})

The patch puts "0&&" before the call so the message does nothing. Holding Shift still
disables the webview (through the separate "drag" message, which carries shiftKey into the
inverted monitor), so the drop goes to VS Code, which ignores a Shift-drop there (live result
08.11: with the patch, Shift over the chat does nothing). To open a file as a tab, drop it
on the tab bar or the editor.
index.html is not touched: its script hash is pinned in the Content-Security-Policy.
If not all three places are found, nothing is written. If only some are patched (an earlier
version of the script), the rest are added.

VS Code keeps a checksum of the bundle in product.json and shows "Your Code installation
appears to be corrupt" when it does not match. The script recomputes it. The main process
reads product.json once at startup, so after patching you need a full quit (Cmd+Q) and
reopen; a window reload is not enough.
Before the change *.orig copies are saved next to the files (refreshed whenever the patch is
applied from a fully unpatched state; --restore deletes the ones identical to the restored
files). A VS Code update
reinstalls the app without the patch; --auto is for the launchd agent that re-applies it.

    python3 patch_vscode_drop.py              # apply the patch
    python3 patch_vscode_drop.py --check      # only show the current state
    python3 patch_vscode_drop.py --restore    # remove the patch (back to stock VS Code)
    python3 patch_vscode_drop.py --auto       # for launchd: silent if applied; else patch and notify

Set VSCODE_APP to patch an app other than /Applications/Visual Studio Code.app.
--app-dir PATH names the folder that holds product.json and out/ directly (on macOS that is
<VS Code.app>/Contents/Resources/app); use it on other platforms. Untested outside macOS.
Unsupported: this modifies a signed application. Use at your own risk.
"""
import argparse
import base64
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP = Path(os.environ.get("VSCODE_APP", "/Applications/Visual Studio Code.app"))
APP_RES = Path("Contents/Resources/app")
APP_DIR = None  # set by --app-dir: folder with product.json and out/
MAIN_REL = "vs/workbench/workbench.desktop.main.js"

# let t=()=>{e()?.windowDidDragStart()},i=()=>{e()?.windowDidDragEnd()};
HEAD = re.compile(r"([\w$]+)=\(\)=>\{[\w$]+\(\)\?\.windowDidDragStart\(\)\},"
                  r"([\w$]+)=\(\)=>\{[\w$]+\(\)\?\.windowDidDragEnd\(\)\}")
# Both places sit in the same constructor right after HEAD (about 280 chars in 1.145.2).
WINDOW = 800
EVENTS = ("DRAG", "DRAG_OVER")
# windowDidDragStart(){this._startBlockingIframeDragEvents()} gives the blocking method name.
BLOCKER = re.compile(r"windowDidDragStart\(\)\{this\.([\w$]+)\(\)\}")
MUTE = "0&&"
RESTART_HINT = ("Quit VS Code (Cmd+Q) and reopen it. A window reload is not enough: VS Code will "
                "show \"installation appears to be corrupt\" until a full restart.")
SUM_KEY = re.compile(r'("' + re.escape(MAIN_REL) + r'"\s*:\s*")([A-Za-z0-9+/]+=*)(")')

# --auto: an update first removes the old app, then puts the new one. If a run lands in the
# middle the file is missing: wait and try again.
AUTO_TRIES = 6
AUTO_PAUSE_S = 5

LOG_PATH = "~/Library/Logs/vscode-drop-fix.log"
NOTIFY_TITLE = "VS Code Drop Fix"
# The text goes as an argument, not inside the AppleScript string, so quotes cannot break it.
NOTIFY_ARGV = ["/usr/bin/osascript",
               "-e", "on run argv",
               "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
               "-e", "end run"]


class NotFound(Exception):
    """A file, a patch place or the checksum is missing or ambiguous."""


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def notify(msg: str) -> None:
    """macOS notification; on failure writes the reason to the log and carries on."""
    if sys.platform != "darwin":
        log(f"notification (not shown, not macOS): {msg}")
        return
    try:
        r = subprocess.run([*NOTIFY_ARGV, msg, NOTIFY_TITLE], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if r.returncode:
            log(f"notification failed: osascript exit {r.returncode}: {r.stderr.strip()[:300]}")
    except (OSError, subprocess.SubprocessError) as err:
        log(f"notification failed: {err}")


def paths():
    res = Path(APP_DIR) if APP_DIR else APP / APP_RES
    return res / "out" / MAIN_REL, res / "product.json"


def checksum(data: bytes) -> str:
    """As VS Code computes it: sha256, base64, no trailing '='."""
    return base64.b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")


def read(path: Path) -> str:
    # Bytes and decode, not read_text: that translates \r\n to \n and would corrupt the file.
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise NotFound(f"{path}: cannot read ({err})")


def only_one(found: list, what: str):
    if len(found) != 1:
        raise NotFound(f"{what}: found {len(found)} times instead of one - "
                       "VS Code code changed, the script needs an update")
    return found[0]


def locate(text: str):
    """Patch places: [(insert position, already there, what to insert)] - two Shifts and drag-start."""
    m = only_one(list(HEAD.finditer(text)), "drag monitor")
    start, end = re.escape(m.group(1)), re.escape(m.group(2))
    base = m.end()
    win = text[base:base + WINDOW]
    sites = []
    for ev in EVENTS:
        rx = re.compile(rf"\.{ev},([\w$]+)=>\{{(!?)\1\.shiftKey\?{end}\(\):{start}\(\)\}}")
        f = only_one(list(rx.finditer(win)), f"{ev}: Shift place")
        sites.append((base + f.start(2), f.group(2) == "!", "!"))
    blocker = re.escape(only_one(list(BLOCKER.finditer(text)), "webview windowDidDragStart").group(1))
    rx = re.compile(r'\.on\("drag-start",\(\)=>\{((?:' + re.escape(MUTE) + r')?)this\.' + blocker + r"\(\)\}\)")
    f = only_one(list(rx.finditer(text)), "drag-start: handler")
    sites.append((f.start(1), f.group(1) == MUTE, MUTE))
    return sites


def vs_version(ptext: str) -> str:
    try:
        return str(json.loads(ptext).get("version", "?"))
    except ValueError:
        return "?"


def write_atomic(path: Path, data: bytes) -> None:
    """All or nothing: VS Code may open a window and read the file at any moment."""
    # A unique name: the installer and the RunAtLoad agent may run at the same moment.
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".patch-tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def drop_identical_backups(files) -> None:
    """After --restore: a *.orig equal to the restored file is redundant; a different one stays."""
    for p in files:
        orig = p.with_name(p.name + ".orig")
        if not orig.exists():
            continue
        if orig.read_bytes() == p.read_bytes():
            orig.unlink()
            log(f"  backup {orig.name} removed (identical to the restored file)")
        else:
            log(f"  backup {orig.name} kept: it differs from the restored file")


def run(want, quiet: bool = False) -> bool:
    """want: True - apply, False - remove, None - only show. Returns whether files were written."""
    main, product = paths()
    text, ptext = read(main), read(product)
    sites = locate(text)
    sums = list(SUM_KEY.finditer(ptext))
    if len(sums) != 1:
        raise NotFound(f"{product}: checksum of {MAIN_REL} found {len(sums)} times instead of one")
    sum_ok = sums[0].group(2).rstrip("=") == checksum(text.encode("utf-8"))
    ver = vs_version(ptext)
    on = sum(1 for _, p, _ in sites if p)
    done = on == len(sites) if want else on == 0
    if want is None or not quiet or not done or not sum_ok:
        state = ("applied" if on == len(sites) else "not applied" if on == 0
                 else f"applied only in {on} places of {len(sites)}")
        log(f"VS Code {ver}: patch {state}, checksum {'matches' if sum_ok else 'DOES NOT match'}")
    if want is None or (done and sum_ok):
        if want is False:
            drop_identical_backups((main, product))
        return False
    new, changed = text, 0
    for pos, p, mark in sorted(sites, reverse=True):
        if want and not p:
            new = new[:pos] + mark + new[pos:]
            changed += 1
        elif not want and p:
            new = new[:pos] + new[pos + len(mark):]
            changed += 1
    data = new.encode("utf-8")
    s = sums[0]
    new_ptext = ptext[:s.start(2)] + checksum(data) + ptext[s.end(2):]
    if want:
        for p in (main, product):
            orig = p.with_name(p.name + ".orig")
            # Fully unpatched: the files are the stock ones of this version, so refresh the copy
            # (an in-place update leaves a stale one). Partly patched: keep the existing copy.
            if on == 0 or not orig.exists():
                shutil.copy2(p, orig)
                log(f"  backup: {orig.name}")
    if new != text:
        write_atomic(main, data)
        log(f"  {main.name}: patch {'applied' if want else 'removed'} in {changed} places")
    write_atomic(product, new_ptext.encode("utf-8"))
    log("  product.json: checksum recomputed")
    if not want:
        drop_identical_backups((main, product))
    return True


def is_permission_error(e: OSError) -> bool:
    return isinstance(e, PermissionError) or e.errno in (errno.EPERM, errno.EACCES)


def auto() -> int:
    err = None
    for i in range(AUTO_TRIES):
        try:
            if run(True, quiet=True):
                ver = vs_version(read(paths()[1]))
                notify(f"Re-applied the drop-without-Shift patch to VS Code {ver}. "
                       "Quit VS Code (Cmd+Q) and reopen it; a window reload is not enough.")
            return 0
        except OSError as e:
            if not is_permission_error(e):
                # E.g. the file vanished while an update replaces the app: same as NotFound.
                err = e
                if i + 1 < AUTO_TRIES:
                    time.sleep(AUTO_PAUSE_S)
                continue
            # Retrying will not help: this is the macOS App Management protection.
            log(f"could not write VS Code files: {e}")
            notify("macOS did not allow re-applying the drop-without-Shift patch to VS Code. "
                   "Enable \"VS Code Drop Fix\" in System Settings → Privacy & Security → App Management.")
            return 1
        except NotFound as e:
            err = e
            if i + 1 < AUTO_TRIES:
                time.sleep(AUTO_PAUSE_S)
    log(f"not patched after {AUTO_TRIES} tries: {err}")
    notify("VS Code changed and the drop-without-Shift patch no longer matches; nothing was "
           f"modified. Details: {LOG_PATH}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="only show the current state")
    g.add_argument("--restore", action="store_true", help="remove the patch")
    g.add_argument("--auto", action="store_true", help="for launchd: quiet, with a notification")
    ap.add_argument("--app-dir", metavar="PATH",
                    help="folder containing product.json and out/ (default: derived from the macOS app)")
    a = ap.parse_args()
    global APP_DIR
    if a.app_dir:
        APP_DIR = a.app_dir
    if a.auto:
        return auto()
    log(f"App: {APP_DIR or APP}")
    try:
        changed = run(None if a.check else not a.restore)
    except NotFound as e:
        log(f"nothing was modified: {e}")
        return 1
    except OSError as e:
        log(f"write failed: {e}. Run --check to see the current state")
        return 1
    if changed:
        log(RESTART_HINT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
