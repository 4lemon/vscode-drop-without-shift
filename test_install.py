"""Tests for install.py: temporary directories only; launchctl and the patch run are mocked."""
import io
import os
import plistlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import install as ins

HAVE_TOOLS = Path("/usr/bin/clang").exists() and Path("/usr/bin/codesign").exists()


def done(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, stdout=out)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        # Fake download folder with a path containing a space.
        self.src = root / "down load"
        self.src.mkdir()
        (self.src / "patch_vscode_drop.py").write_text("# fake script\n")
        (self.src / "launcher.c").write_bytes((Path(ins.__file__).parent / "launcher.c").read_bytes())
        self.support = root / "Library" / "Application Support" / "vscode-drop-fix"
        self.app = root / "Applications" / "VS Code Drop Fix.app"
        self.plist = root / "Library" / "LaunchAgents" / "local.vscode-drop-fix.plist"
        self.log = root / "Library" / "Logs" / "vscode-drop-fix.log"
        for name, val in [("SOURCE_SCRIPT", self.src / "patch_vscode_drop.py"),
                          ("SOURCE_C", self.src / "launcher.c"), ("SUPPORT_DIR", self.support),
                          ("APP_PATH", self.app), ("AGENT_PLIST", self.plist), ("LOG_FILE", self.log)]:
            mock.patch.object(ins, name, val).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.tmp.cleanup)
        mock.patch.dict(os.environ).start()
        os.environ.pop("VSCODE_APP", None)
        self.ctl = []
        self.patch_calls = []
        self.patch_result = done(0, "VS Code 1.0: patch applied")
        mock.patch.object(ins, "launchctl", lambda *a: self.ctl.append(a) or done()).start()
        mock.patch.object(ins, "run_patch", self.fake_patch).start()
        mock.patch.object(ins, "uid", lambda: 501).start()
        self.probe = root / "probe dir" / "python3"
        self.probe.parent.mkdir()
        self.args_file = root / "args.txt"
        self.probe.write_text(f'#!/bin/sh\nprintf "%s|" "$@" > "{self.args_file}"\nexit 7\n')
        self.probe.chmod(0o755)

    def fake_patch(self, python, script, *args):
        self.patch_calls.append((python, Path(script), args))
        return self.patch_result

    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = ins.main(list(args))
        return code, out.getvalue()

    def exe(self):
        return self.app / "Contents" / "MacOS" / ins.EXE

    @unittest.skipUnless(HAVE_TOOLS, "no clang or codesign")
    def test_real_build_bakes_command_and_signs(self):
        code, out = self.run_main("--python", str(self.probe))
        self.assertEqual(code, 0, out)
        self.assertTrue(self.exe().exists())
        with open(self.app / "Contents" / "Info.plist", "rb") as f:
            info = plistlib.load(f)
        self.assertEqual(info["CFBundleIdentifier"], "local.vscode-drop-fix")
        self.assertEqual(info["CFBundleExecutable"], "vscode-drop-fix")
        self.assertTrue(info["LSUIElement"])
        r = subprocess.run([str(self.exe()), "--evil", "arg"], capture_output=True)
        self.assertEqual(r.returncode, 7, "exit code of the baked command is passed through")
        script = self.support / "patch_vscode_drop.py"
        self.assertEqual(self.args_file.read_text(), f"-I|{script}|--auto|", "extra arguments are ignored")
        sig = subprocess.run(["/usr/bin/codesign", "-dv", str(self.app)], capture_output=True, text=True)
        self.assertIn("Identifier=local.vscode-drop-fix", sig.stderr)
        self.assertFalse(self.app.with_name(self.app.name + ".build").exists())

    def test_script_copied_to_support_dir_and_patch_run_from_there(self):
        with mock.patch.object(ins, "build_app"):
            code, out = self.run_main("--python", str(self.probe))
        self.assertEqual(code, 0, out)
        self.assertEqual((self.support / "patch_vscode_drop.py").read_text(), "# fake script\n")
        python, script, args = self.patch_calls[0]
        self.assertEqual(python, str(self.probe))
        self.assertEqual(script, self.support / "patch_vscode_drop.py")
        self.assertEqual(args, ())
        self.assertIn("Cmd+Q", out)

    def test_plist_content(self):
        with mock.patch.object(ins, "build_app"):
            self.run_main("--python", str(self.probe))
        with open(self.plist, "rb") as f:
            p = plistlib.load(f)
        self.assertEqual(p["Label"], "local.vscode-drop-fix")
        self.assertEqual(p["ProgramArguments"], [str(self.exe())])
        self.assertEqual(p["WatchPaths"], ["/Applications"])
        self.assertTrue(p["RunAtLoad"])
        self.assertEqual(p["ThrottleInterval"], 20)
        self.assertEqual(p["StandardOutPath"], str(self.log))
        self.assertEqual(p["StandardErrorPath"], str(self.log))
        self.assertTrue(self.log.parent.is_dir())
        self.assertTrue(os.path.isabs(p["StandardOutPath"]))

    def read_plist(self):
        with open(self.plist, "rb") as f:
            return plistlib.load(f)

    def test_plist_carries_vscode_app_and_watches_its_parent(self):
        os.environ["VSCODE_APP"] = "/Users/x/Applications/Visual Studio Code.app"
        with mock.patch.object(ins, "build_app"):
            self.run_main("--python", str(self.probe))
        p = self.read_plist()
        self.assertEqual(p["EnvironmentVariables"], {"VSCODE_APP": "/Users/x/Applications/Visual Studio Code.app"})
        self.assertEqual(p["WatchPaths"], ["/Applications", "/Users/x/Applications"])

    def test_plist_watch_paths_deduplicated_for_applications(self):
        os.environ["VSCODE_APP"] = "/Applications/Code Insiders.app"
        with mock.patch.object(ins, "build_app"):
            self.run_main("--python", str(self.probe))
        p = self.read_plist()
        self.assertEqual(p["WatchPaths"], ["/Applications"])
        self.assertEqual(p["EnvironmentVariables"], {"VSCODE_APP": "/Applications/Code Insiders.app"})

    def test_plist_without_vscode_app_has_no_environment(self):
        with mock.patch.object(ins, "build_app"):
            self.run_main("--python", str(self.probe))
        self.assertNotIn("EnvironmentVariables", self.read_plist())

    def test_python_with_existing_app_needs_rebuild(self):
        self.exe().parent.mkdir(parents=True)
        self.exe().write_bytes(b"old")
        with self.assertRaises(SystemExit) as cm, mock.patch("sys.stderr", io.StringIO()) as err:
            self.run_main("--python", str(self.probe))
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--rebuild", err.getvalue())
        self.assertEqual(self.ctl, [])
        self.assertEqual(self.patch_calls, [])
        self.assertFalse(self.support.exists())
        self.assertFalse(self.plist.exists())
        self.assertEqual(self.exe().read_bytes(), b"old")

    def test_launchctl_bootout_then_bootstrap(self):
        with mock.patch.object(ins, "build_app"):
            self.run_main("--python", str(self.probe))
        self.assertEqual(self.ctl, [("bootout", "gui/501/local.vscode-drop-fix"),
                                    ("bootstrap", "gui/501", str(self.plist))])

    def test_existing_app_not_rebuilt_without_flag(self):
        self.exe().parent.mkdir(parents=True)
        self.exe().write_bytes(b"old")
        with mock.patch.object(ins, "build_app") as b:
            code, out = self.run_main()
        self.assertEqual(code, 0)
        b.assert_not_called()
        self.assertEqual(self.exe().read_bytes(), b"old")
        self.assertIn("--rebuild", out)

    def test_rebuild_warns_about_permission(self):
        self.exe().parent.mkdir(parents=True)
        self.exe().write_bytes(b"old")
        with mock.patch.object(ins, "build_app") as b:
            code, out = self.run_main("--rebuild", "--python", str(self.probe))
        self.assertEqual(code, 0)
        b.assert_called_once()
        self.assertIn("forget", out)

    def test_python_default_prefers_system_python(self):
        with mock.patch.object(ins, "SYSTEM_PYTHON", str(self.probe)):
            self.assertEqual(ins.default_python(), str(self.probe))
        with mock.patch.object(ins, "SYSTEM_PYTHON", str(self.root / "nope")):
            self.assertEqual(ins.default_python(), ins.sys.executable)

    def test_permission_denied_prints_app_management_hint(self):
        self.patch_result = done(1, "write failed: [Errno 1] Operation not permitted: 'x'")
        with mock.patch.object(ins, "build_app"):
            code, out = self.run_main("--python", str(self.probe))
        self.assertEqual(code, 1)
        self.assertIn("App Management", out)
        self.assertIn("launchctl kickstart -k gui/$(id -u)/local.vscode-drop-fix", out)
        self.assertIn("integrated terminal", out)

    def test_other_patch_failure_is_not_blamed_on_permissions(self):
        self.patch_result = done(1, "nothing was modified: drag monitor: found 0 times")
        with mock.patch.object(ins, "build_app"):
            code, out = self.run_main("--python", str(self.probe))
        self.assertEqual(code, 1)
        self.assertNotIn("App Management", out)

    def test_uninstall_removes_everything_and_restores(self):
        with mock.patch.object(ins, "build_app"):
            self.run_main("--python", str(self.probe))
        self.exe().parent.mkdir(parents=True, exist_ok=True)
        self.exe().write_bytes(b"x")
        self.ctl.clear()
        self.patch_calls.clear()
        code, out = self.run_main("--uninstall")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl, [("bootout", "gui/501/local.vscode-drop-fix")])
        self.assertEqual(self.patch_calls[0][2], ("--restore",))
        self.assertEqual(self.patch_calls[0][1], self.support / "patch_vscode_drop.py")
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.app.exists())
        self.assertFalse(self.support.exists())

    def test_uninstall_reports_denied_restore_but_still_removes(self):
        self.patch_result = done(1, "write failed: Operation not permitted")
        self.support.mkdir(parents=True)
        (self.support / "patch_vscode_drop.py").write_text("#")
        code, out = self.run_main("--uninstall")
        self.assertEqual(code, 1)
        self.assertIn("did not allow restoring", out)
        self.assertFalse(self.support.exists())

    def test_unknown_arg_refused(self):
        with self.assertRaises(SystemExit) as cm, mock.patch("sys.stderr", io.StringIO()):
            self.run_main("--bogus")
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.ctl, [])

    def test_help_touches_nothing(self):
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            self.run_main("--help")
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(self.ctl, [])
        self.assertEqual(self.patch_calls, [])
        self.assertFalse(self.support.exists())
        self.assertFalse(self.plist.exists())

    def test_non_macos_exits_one_with_pointer(self):
        with mock.patch.object(ins.sys, "platform", "linux"):
            code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("Windows and Linux (untested)", out)
        self.assertEqual(self.ctl, [])
        self.assertFalse(self.support.exists())

    def test_c_string_refuses_quotes(self):
        for bad in ('/a"b', "/a\\b", "/a\nb"):
            with self.assertRaises(ins.InstallError):
                ins.c_string(bad)

    def test_missing_python_reported(self):
        code, out = self.run_main("--python", str(self.root / "missing"))
        self.assertEqual(code, 1)
        self.assertIn("python not found", out)
        self.assertFalse(self.support.exists())


if __name__ == "__main__":
    unittest.main()
