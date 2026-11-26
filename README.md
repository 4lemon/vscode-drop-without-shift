# VS Code Drop Fix: attach dropped files to the Claude Code chat without Shift

If dragging a screenshot onto the Claude Code chat in VS Code opens it as an editor tab unless you hold Shift (anthropics/claude-code#26784), this patch reverses that: a plain drop attaches the file. A small launchd agent re-applies the patch after every VS Code update.

> **Disclaimer.** This was vibe-coded with Claude Code (an AI coding agent) for one person's Mac and is shared as is. It patches files inside a signed application and installs a background agent. Read the code before you run it. No warranty, no support: use it at your own risk (see [LICENSE](LICENSE)).

> **Warning: on macOS 26 this can stop VS Code from starting.** On 22 Nov 2026 VS Code updated itself to 1.147.0, and the agent re-applied the patch to the new app within a second, before that version had ever been launched. On its first launch (26 Nov, macOS 26.5.1) Gatekeeper checked the new version, found the Microsoft signature broken and showed "Visual Studio Code is damaged and can't be opened. You should move it to the Trash." Earlier versions survived only because the patch went in after their first launch. Removing the patch did not help: macOS kept refusing the app until a fresh copy was put in place. The author has stopped using the patch. If you keep it, read [If macOS says VS Code is damaged](#if-macos-says-vs-code-is-damaged) first.

## Why it is VS Code, not the extension

The extension's own drop handler does not check Shift. VS Code does, in two places:

1. `WebviewWindowDragMonitor` (`src/vs/workbench/contrib/webview/browser/webviewWindowDragMonitor.ts`) disables the webview for the mouse during a drag unless Shift is held.
2. The webview host page (`pre/index.html`) sends a `drag-start` message when a file enters without Shift, and VS Code then disables the webview again.

Flipping only the first check is not enough. The patch flips both `shiftKey` tests in the bundled `workbench.desktop.main.js` (a `!` each) and neutralises the `drag-start` handler (`0&&` in front of the call). The places are located by stable neighbouring names (`windowDidDragStart`, `windowDidDragEnd`, `DRAG`, `DRAG_OVER`, `drag-start`), not by minified variable names. If any place is missing or ambiguous in a new VS Code, the script writes nothing. `pre/index.html` is left alone (its script hash is pinned in a Content-Security-Policy). The checksum of the bundle in `product.json` is recomputed.

## Install and uninstall (macOS)

Needs Xcode command line tools (`xcode-select --install`) and Python 3.9+. Clone or download this repository and keep its files together:

```
python3 install.py              # install
python3 install.py --uninstall  # remove everything and restore VS Code
python3 install.py --help
```

Then quit VS Code with **Cmd+Q** and reopen it. The main process reads `product.json` once at startup; a window reload shows "Your Code installation appears to be corrupt".

The installer copies the script to `~/Library/Application Support/vscode-drop-fix/` (a background agent reading from Downloads, Documents or Desktop triggers privacy prompts), builds `~/Applications/VS Code Drop Fix.app`, writes and loads `~/Library/LaunchAgents/local.vscode-drop-fix.plist`, then runs the patch once. Log: `~/Library/Logs/vscode-drop-fix.log`. If `VSCODE_APP` is set when you run the installer, the agent gets it too and watches that app instead. `--python` only works together with `--rebuild` once the app exists. Manual use: `python3 patch_vscode_drop.py [--check | --restore]`.

`--check` also tells whether the patch is active in the running VS Code: it compares the start time of the main process (`ps`) with the modification time of `product.json` and prints either "the patch is active" or that VS Code started before the write and needs a full restart (Cmd+Q). If `ps` cannot see the process (for example inside a sandbox) or `--app-dir` is used, it says "not checked".

## The App Management step

macOS only lets an app with the "App Management" permission modify another signed app. A launchd agent running python directly gets `Operation not permitted` when writing into VS Code. Granting that permission to python would let every python script modify any app, so the installer builds a tiny app instead. Its command is baked in at compile time (`python3 -I patch_vscode_drop.py --auto`) and it accepts no arguments, so the permission is useful for nothing else.

Enable it once: System Settings, Privacy & Security, App Management, switch on "VS Code Drop Fix". Then `launchctl kickstart -k gui/$(id -u)/local.vscode-drop-fix`. Running the installer from VS Code's own integrated terminal also works for the first patch. A rebuilt app has a new signature and macOS forgets the permission, so the installer never rebuilds an existing app unless you pass `--rebuild`.

## After a VS Code update

An update replaces the app and drops the patch. The agent runs at login, whenever the VS Code app is replaced, and once an hour as a safety net (a launchd watch on the `/Applications` folder itself does not fire when an app inside it is replaced, so the agent watches the app). If the patch is in place it exits silently; the check takes about a second. Otherwise it re-applies it and shows a notification asking for Cmd+Q. If the app is mid-replacement or the code changed, it retries 6 times 5 seconds apart, then notifies that the patch no longer matches (nothing is modified). If macOS refuses the write, it notifies you to enable App Management. A failure is notified once, not every hour, until it changes or a run succeeds.

## Caveats

- Does **not** fix dragging from VS Code's own Explorer: that is a separate regression since 1.140 (anthropics/claude-code#100023, microsoft/vscode#339278). Workaround: type `@` and the file name.
- Affects all webviews, not only Claude Code.
- With the patch, holding Shift over a webview does nothing (checked live): Shift still disables the webview, so the drop goes to VS Code, which ignores a Shift-drop there. To open a dropped file as a tab, drop it on the tab bar or the editor.
- Needs a full quit (Cmd+Q), see above.
- It modifies a signed application, which breaks its signature: see the warning at the top. Unsupported by Microsoft; use at your own risk. Originals are kept as `*.orig` next to the patched files (refreshed each time the patch is applied to unpatched files), and `--restore` puts them back byte for byte and then deletes each `*.orig` identical to the restored file, so uninstall leaves nothing behind. VS Code's auto-update did replace the patched app twice (1.145.2 to 1.146.0, then 1.147.0).

## If macOS says VS Code is damaged

Click **Cancel**, not "Move to Trash". Your settings and extensions live outside the app (`~/Library/Application Support/Code`, `~/.vscode`), so nothing is lost either way, but there is no need to throw the app away. Steps that worked on 26 Nov 2026 (macOS 26.5.1, VS Code 1.147.0):

1. Stop the agent and restore the original files: `python3 install.py --uninstall`. Writing into VS Code, and moving it in step 3, needs App Management for whatever runs the command: if you get `Operation not permitted`, give your terminal that permission or download a fresh VS Code instead of steps 1 to 3.
2. Check the signature: `codesign --verify --deep --strict "/Applications/Visual Studio Code.app"` must print nothing. Any `file added` or `file modified` line means the app still differs from Microsoft's; a leftover `*.orig` inside the app is enough to break it, so move such files out.
3. A valid signature is not enough. macOS remembers the rejection: the next launch still logged `GK evaluateScanResult: 3` and showed the same dialog, although `codesign`, `spctl --assess` and `syspolicy_check distribution` all passed. Replace the app with a fresh copy of itself:

   ```
   mkdir -p ~/vscode-fresh ~/vscode-rejected
   ditto "/Applications/Visual Studio Code.app" ~/vscode-fresh/"Visual Studio Code.app"
   mv "/Applications/Visual Studio Code.app" ~/vscode-rejected/
   mv ~/vscode-fresh/"Visual Studio Code.app" /Applications/
   ```

   For macOS this is a new app, and it started normally. Delete `~/vscode-rejected` once it does. Reinstalling from code.visualstudio.com should work the same way. A reboot or `sudo killall syspolicyd` is reported elsewhere to clear the remembered result too ([Squirrel.Mac#334](https://github.com/Squirrel/Squirrel.Mac/issues/334), [openai/codex#25719](https://github.com/openai/codex/issues/25719)); not tried here.

## Windows and Linux (untested)

Nothing below has been run. `install.py` is macOS only. The patch itself edits plain JavaScript and JSON, so the same two Shift checks should exist in the same relative files of any install: `resources/app/out/vs/workbench/workbench.desktop.main.js` and `resources/app/product.json`. Find the `resources/app` folder inside your VS Code install folder rather than trusting a version-specific path, then:

```
python3 patch_vscode_drop.py --check --app-dir "<install folder>/resources/app"
python3 patch_vscode_drop.py --app-dir "<install folder>/resources/app"
```

`--app-dir` names the folder that holds `product.json` and `out/`; notifications are skipped off macOS, the script only logs. Fully quit VS Code and reopen it. You need write access to that folder (a system-wide install needs admin or sudo). Snap and Flatpak installs are read-only images and cannot be patched this way. To re-apply after updates, run the command again, or hook it yourself: Windows Task Scheduler, or on Linux an apt/dnf post-install hook or a systemd path unit.

## Tested

macOS 26, VS Code 1.145.2, Claude Code 2.1.319: the patch was applied, VS Code restarted with Cmd+Q, and a screenshot dropped on the chat without Shift attached to the message, with no "corrupt" warning. The agent re-applying the patch through the app bundle was also run once, after App Management was granted (with Homebrew Python; the installer's default `/usr/bin/python3` was not tried). `install.py` itself was tested only on a fake app in temporary directories (real clang and codesign, mocked launchctl), not end to end on a real machine. Windows and Linux: untested.

macOS 26.5.1, VS Code 1.147.0: broke, see the warning at the top. The agent there was the author's private version of this one, with the same logic: watch the app, patch as soon as it is replaced.
