#!/usr/bin/env python3
"""Install (or remove) the VS Code drop-without-Shift patch and its auto re-apply agent.

    python3 install.py              # install: patch VS Code now and after every update
    python3 install.py --rebuild    # also rebuild the helper app (macOS then forgets its permission)
    python3 install.py --python P   # bake interpreter P into the helper app (default /usr/bin/python3)
    python3 install.py --uninstall  # remove the agent, the app, the copy of the script; restore VS Code

What install does:
  1. copies patch_vscode_drop.py to ~/Library/Application Support/vscode-drop-fix/
     (a background agent must not read from Downloads/Documents/Desktop: macOS prompts there);
  2. builds ~/Applications/VS Code Drop Fix.app from launcher.c (clang + ad-hoc codesign).
     The app exists so that the macOS "App Management" permission can be given to it alone;
  3. writes ~/Library/LaunchAgents/local.vscode-drop-fix.plist and loads it with launchctl
     (runs at login and whenever /Applications changes, e.g. a VS Code update);
  4. runs the patch once.

Needs: macOS, Xcode command line tools (clang, codesign), Python 3.9+.
Unsupported: this modifies a signed application. Use at your own risk.
"""
import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABEL = "local.vscode-drop-fix"
APP_NAME = "VS Code Drop Fix"
BUNDLE_ID = "local.vscode-drop-fix"
EXE = "vscode-drop-fix"
HOME = Path.home()
SUPPORT_DIR = HOME / "Library" / "Application Support" / "vscode-drop-fix"
APP_PATH = HOME / "Applications" / f"{APP_NAME}.app"
AGENT_PLIST = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_FILE = HOME / "Library" / "Logs" / "vscode-drop-fix.log"
SCRIPT_NAME = "patch_vscode_drop.py"
SOURCE_SCRIPT = HERE / SCRIPT_NAME
SOURCE_C = HERE / "launcher.c"
CLANG = "/usr/bin/clang"
CODESIGN = "/usr/bin/codesign"
LAUNCHCTL = "/bin/launchctl"
SYSTEM_PYTHON = "/usr/bin/python3"
WATCH_PATH = "/Applications"

DENIED_MARKERS = ("Operation not permitted", "Permission denied")


class InstallError(Exception):
    pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def uid() -> int:
    return os.getuid()


def default_python() -> str:
    return SYSTEM_PYTHON if Path(SYSTEM_PYTHON).exists() else sys.executable


def c_string(s: str) -> str:
    """Macro value as a C string literal."""
    if any(ch in s for ch in '"\\\n'):
        raise InstallError(f"path contains a character not allowed in a C literal: {s!r}")
    return f'"{s}"'


def info_plist() -> dict:
    return {
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": EXE,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSUIElement": True,
        "LSMinimumSystemVersion": "13.0",
    }


def agent_plist(exe: Path) -> dict:
    watch = [WATCH_PATH]
    env = {}
    vscode_app = os.environ.get("VSCODE_APP")
    if vscode_app:
        vscode_app = os.path.abspath(vscode_app)
        env["VSCODE_APP"] = vscode_app
        parent = os.path.dirname(vscode_app)
        if parent not in watch:
            watch.append(parent)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(exe)],
        "WatchPaths": watch,
        "RunAtLoad": True,
        "ThrottleInterval": 20,
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(LOG_FILE),
    }
    if env:
        plist["EnvironmentVariables"] = env
    return plist


def exe_path() -> Path:
    return APP_PATH / "Contents" / "MacOS" / EXE


# ---- external effects: small functions so tests can replace them ----

def run_tool(argv: list) -> subprocess.CompletedProcess:
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return run_tool([LAUNCHCTL, *args])


def compile_launcher(python: str, script: Path, out: Path) -> None:
    argv = [CLANG, "-O2", "-Wall", "-Werror",
            f"-DPYTHON={c_string(python)}", f"-DSCRIPT={c_string(str(script))}",
            "-o", str(out), str(SOURCE_C)]
    r = run_tool(argv)
    if r.returncode:
        raise InstallError(f"clang failed (exit {r.returncode}):\n{r.stdout}\n"
                           "If clang is missing, run: xcode-select --install")


def sign_app(bundle: Path) -> None:
    r = run_tool([CODESIGN, "--force", "--sign", "-", "--identifier", BUNDLE_ID, str(bundle)])
    if r.returncode:
        raise InstallError(f"codesign failed (exit {r.returncode}):\n{r.stdout}")
    r = run_tool([CODESIGN, "--verify", "--strict", str(bundle)])
    if r.returncode:
        raise InstallError(f"codesign --verify failed (exit {r.returncode}):\n{r.stdout}")


def run_patch(python: str, script: Path, *args: str) -> subprocess.CompletedProcess:
    """Runs the patch script from the installing terminal (not through the helper app)."""
    return run_tool([python, "-I", str(script), *args])


# ---- steps ----

def copy_script() -> Path:
    if not SOURCE_SCRIPT.is_file():
        raise InstallError(f"{SOURCE_SCRIPT} not found: keep install.py and {SCRIPT_NAME} together")
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    dest = SUPPORT_DIR / SCRIPT_NAME
    shutil.copy2(SOURCE_SCRIPT, dest)
    return dest


def build_app(python: str, script: Path) -> None:
    if not SOURCE_C.is_file():
        raise InstallError(f"{SOURCE_C} not found: keep install.py and launcher.c together")
    tmp = APP_PATH.with_name(APP_PATH.name + ".build")
    if tmp.exists():
        shutil.rmtree(tmp)
    macos = tmp / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    try:
        with open(tmp / "Contents" / "Info.plist", "wb") as f:
            plistlib.dump(info_plist(), f)
        compile_launcher(python, script, macos / EXE)
        sign_app(tmp)
        if APP_PATH.exists():
            shutil.rmtree(APP_PATH)
        os.replace(tmp, APP_PATH)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def write_agent() -> None:
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AGENT_PLIST.with_name(AGENT_PLIST.name + ".tmp")
    with open(tmp, "wb") as f:
        plistlib.dump(agent_plist(exe_path()), f)
    os.replace(tmp, AGENT_PLIST)


def load_agent() -> None:
    target = f"gui/{uid()}"
    launchctl("bootout", f"{target}/{LABEL}")  # fine if it was not loaded
    r = launchctl("bootstrap", target, str(AGENT_PLIST))
    if r.returncode:
        raise InstallError(f"launchctl bootstrap failed (exit {r.returncode}): {r.stdout.strip()}")


def is_denied(text: str) -> bool:
    return any(m in text for m in DENIED_MARKERS)


def print_denied_hint() -> None:
    say()
    say("macOS did not allow writing into VS Code (permission denied).")
    say("This is the App Management protection. To fix it:")
    say(f'  1. Open System Settings -> Privacy & Security -> App Management and enable "{APP_NAME}"')
    say(f"     (if it is not listed, click + and add {APP_PATH}).")
    say(f"  2. Run the agent again:  launchctl kickstart -k gui/$(id -u)/{LABEL}")
    say("  Alternative: run this installer from VS Code's integrated terminal; macOS lets VS Code")
    say("  modify itself.")


def install(python: str, rebuild: bool) -> int:
    if not Path(python).is_file() or not os.access(python, os.X_OK):
        raise InstallError(f"python not found or not executable: {python}")
    script = copy_script()
    say(f"Copied the patch script to {script}")
    if exe_path().exists() and not rebuild:
        say(f"{APP_PATH} already exists, not rebuilding it (macOS keeps its App Management permission).")
        say("Use --rebuild to force it; a rebuilt app has a new signature and needs the permission again.")
    else:
        if exe_path().exists():
            say("Rebuilding the app: macOS will forget its App Management permission; enable it again.")
        build_app(python, script)
        say(f"Built and signed {APP_PATH} (runs {python} -I {script} --auto)")
    write_agent()
    load_agent()
    say(f"Loaded the launchd agent {LABEL} ({AGENT_PLIST}); log: {LOG_FILE}")
    say()
    say("Applying the patch now...")
    r = run_patch(python, script)
    say(r.stdout.rstrip())
    denied = is_denied(r.stdout)
    if r.returncode and denied:
        print_denied_hint()
    elif r.returncode:
        say()
        say("The patch did not apply (see the output above). Nothing was modified unless it says so.")
    say()
    say("Next steps:")
    if r.returncode == 0:
        say("  - Quit VS Code with Cmd+Q and reopen it. A window reload is not enough.")
        say("  - Then drag a screenshot onto the Claude Code chat without Shift.")
    else:
        say("  - Once the patch applies: quit VS Code with Cmd+Q and reopen it.")
    say("  - After VS Code updates the agent re-applies the patch and shows a notification.")
    say("  - Remove everything with: python3 install.py --uninstall (restores VS Code and deletes the *.orig backups)")
    return 1 if r.returncode else 0


def uninstall(python: str) -> int:
    code = 0
    launchctl("bootout", f"gui/{uid()}/{LABEL}")  # fine if it was not loaded
    say(f"Stopped the launchd agent {LABEL}")
    script = SUPPORT_DIR / SCRIPT_NAME
    if not script.is_file():
        script = SOURCE_SCRIPT
    if script.is_file():
        r = run_patch(python, script, "--restore")
        say(r.stdout.rstrip())
        if r.returncode:
            code = 1
            if is_denied(r.stdout):
                say("macOS did not allow restoring VS Code. Enable App Management for the app that runs")
                say(f'this installer (for example VS Code or Terminal), or for "{APP_NAME}", and run again.')
            say("The patch is still in VS Code; restore it later with:")
            say(f"  python3 {SOURCE_SCRIPT} --restore")
    else:
        say(f"{SCRIPT_NAME} not found: could not restore VS Code (originals are *.orig next to its files).")
        code = 1
    if AGENT_PLIST.exists():
        AGENT_PLIST.unlink()
        say(f"Removed {AGENT_PLIST}")
    if APP_PATH.exists():
        shutil.rmtree(APP_PATH)
        say(f"Removed {APP_PATH}")
    if SUPPORT_DIR.exists():
        shutil.rmtree(SUPPORT_DIR)
        say(f"Removed {SUPPORT_DIR}")
    say(f"The log {LOG_FILE} was left in place.")
    if code == 0:
        say("Quit VS Code with Cmd+Q and reopen it so the restored files are used.")
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 epilog=__doc__.split("\n", 1)[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true", help="remove everything and restore VS Code")
    ap.add_argument("--rebuild", action="store_true", help="rebuild an existing helper app")
    ap.add_argument("--python", metavar="PATH", help="interpreter baked into the helper app")
    a = ap.parse_args(argv)
    if sys.platform != "darwin":
        say("This installer is macOS only; see the README section \"Windows and Linux (untested)\".")
        return 1
    if a.uninstall and (a.rebuild or a.python):
        ap.error("--uninstall cannot be combined with --rebuild or --python")
    if a.python and not a.rebuild and not a.uninstall and exe_path().exists():
        ap.error(f"{APP_PATH} already exists and its interpreter is baked in; --python needs --rebuild "
                 "(macOS then forgets the App Management permission of the rebuilt app)")
    python = a.python or default_python()
    try:
        if a.uninstall:
            return uninstall(python)
        return install(python, a.rebuild)
    except InstallError as e:
        say(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
