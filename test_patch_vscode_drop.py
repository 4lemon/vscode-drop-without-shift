"""Tests for patch_vscode_drop.py on a fake VS Code app in a temporary directory."""
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import patch_vscode_drop as pd

REAL_NOTIFY = pd.notify
REAL_MAIN_STARTED = pd.main_started

# As in 1.145.2, plus a decoy with the same text in another class, \r\n and non-ASCII.
MONITOR = ('var Fme=class extends x{constructor(o,e){super();let t=()=>{e()?.windowDidDragStart()},'
           'i=()=>{e()?.windowDidDragEnd()};this._register(G(o,re.DRAG_START,()=>{t()})),'
           'this._register(G(o,re.DRAG_END,i)),this._register(G(o,re.MOUSE_MOVE,n=>{n.buttons===0&&i()})),'
           'this._register(G(o,re.DRAG,n=>{n.shiftKey?i():t()})),'
           'this._register(G(o,re.DRAG_OVER,n=>{n.shiftKey?i():t()}))}};')
DECOY = 'var Q=class{f(){G(o,re.DRAG,n=>{n.shiftKey?i():t()})}};'
# The extension webview (as in 1.145.2) and a wrapper with a similar windowDidDragStart that must not match.
ELEMENT = ('var W=class{constructor(){this._register(this.on("drag-start",()=>{this._startBlockingIframeDragEvents()})),'
           'this._register(this.on("drag",f=>{this.handleDragEvent("drag",f)}))}'
           '_startBlockingIframeDragEvents(){this.element&&(this.element.style.pointerEvents="none")}'
           'windowDidDragStart(){this._startBlockingIframeDragEvents()}'
           'windowDidDragEnd(){this._stopBlockingIframeDragEvents()}};'
           'var O=class{windowDidDragStart(){this._webview.value?.windowDidDragStart()}};')
MAIN = 'var a="café";\r\n' + MONITOR + "\r\n" + "x" * (pd.WINDOW + 10) + DECOY + ELEMENT
OLD_PATCH = MAIN.replace("re.DRAG,n=>{n.shiftKey", "re.DRAG,n=>{!n.shiftKey", 1) \
                .replace("re.DRAG_OVER,n=>{n.shiftKey", "re.DRAG_OVER,n=>{!n.shiftKey", 1)
PATCHED = OLD_PATCH.replace('"drag-start",()=>{this.', '"drag-start",()=>{0&&this.', 1)


def product_text(main: str, ver="1.145.2") -> str:
    return json.dumps({"version": ver, "checksums": {
        "vs/base/parts/sandbox/electron-browser/preload.js": "AAAA",
        pd.MAIN_REL: pd.checksum(main.encode("utf-8")),
        "vs/workbench/workbench.desktop.main.css": "BBBB"}}, indent="\t") + "\n"


class PatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Path(self.tmp.name) / "Visual Studio Code.app"
        mock.patch.object(pd, "APP", self.app).start()
        mock.patch.object(pd, "APP_DIR", None).start()
        mock.patch.object(pd, "notify", lambda msg: self.notes.append(msg)).start()
        mock.patch.object(pd, "AUTO_PAUSE_S", 0).start()
        # Tests never look at real processes: by default VS Code is "not running".
        mock.patch.object(pd, "main_started", lambda: None).start()
        self.failed = Path(self.tmp.name) / "state" / "last-failure"
        mock.patch.object(pd, "FAILED_PATH", self.failed).start()
        self.notes = []
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.tmp.cleanup)
        self.main, self.product = pd.paths()
        self.install(MAIN)

    def install(self, main: str, product: str = None):
        self.main.parent.mkdir(parents=True, exist_ok=True)
        self.main.write_bytes(main.encode("utf-8"))
        self.product.write_bytes((product if product is not None else product_text(main)).encode("utf-8"))

    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.argv", ["patch_vscode_drop.py", *args]):
            code = pd.main()
        return code, out.getvalue()

    def stored_sum(self) -> str:
        return json.loads(self.product.read_text())["checksums"][pd.MAIN_REL]

    def test_patch_both_sites_only_and_fix_checksum(self):
        orig_product = self.product.read_bytes()
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.main.read_bytes(), PATCHED.encode("utf-8"), "\\r\\n and e-acute intact, decoy untouched")
        self.assertEqual(self.stored_sum(), pd.checksum(PATCHED.encode("utf-8")))
        self.assertEqual(json.loads(self.product.read_text())["checksums"]["vs/workbench/workbench.desktop.main.css"],
                         "BBBB")
        self.assertEqual((self.main.parent / (self.main.name + ".orig")).read_bytes(), MAIN.encode("utf-8"))
        self.assertEqual((self.product.parent / "product.json.orig").read_bytes(), orig_product)
        self.assertEqual(sorted(p.name for p in self.main.parent.iterdir()),
                         [self.main.name, self.main.name + ".orig"], "no temporary files left")

    def test_repeat_changes_nothing_and_keeps_first_copy(self):
        self.run_main()
        before = (self.main.read_bytes(), self.product.read_bytes())
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual((self.main.read_bytes(), self.product.read_bytes()), before)
        self.assertEqual((self.main.parent / (self.main.name + ".orig")).read_bytes(), MAIN.encode("utf-8"))

    def test_check_writes_nothing(self):
        code, out = self.run_main("--check")
        self.assertEqual(code, 0)
        self.assertIn("patch not applied", out)
        self.assertIn("matches", out)
        self.assertEqual(self.main.read_bytes(), MAIN.encode("utf-8"))
        self.assertFalse((self.main.parent / (self.main.name + ".orig")).exists())

    def test_restore_returns_bytes_exactly(self):
        orig_product = self.product.read_bytes()
        self.run_main()
        code, _ = self.run_main("--restore")
        self.assertEqual(code, 0)
        self.assertEqual(self.main.read_bytes(), MAIN.encode("utf-8"))
        self.assertEqual(self.product.read_bytes(), orig_product)

    def test_stale_checksum_alone_is_fixed(self):
        self.install(PATCHED, product_text(MAIN))
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.main.read_bytes(), PATCHED.encode("utf-8"))
        self.assertEqual(self.stored_sum(), pd.checksum(PATCHED.encode("utf-8")))

    def assert_refused(self, main, product=None, expect=""):
        self.install(main, product)
        before = (self.main.read_bytes(), self.product.read_bytes())
        code, out = self.run_main()
        self.assertEqual(code, 1, out)
        self.assertIn("nothing was modified", out)
        self.assertIn(expect, out)
        self.assertEqual((self.main.read_bytes(), self.product.read_bytes()), before)
        self.assertFalse((self.main.parent / (self.main.name + ".orig")).exists())

    def test_patch_has_three_sites(self):
        self.assertEqual(PATCHED.count("!n.shiftKey"), 2)
        self.assertEqual(PATCHED.count('"drag-start",()=>{0&&this.'), 1)
        self.assertEqual(len(PATCHED) - len(MAIN), 2 + len(pd.MUTE))

    def test_old_two_site_patch_is_completed(self):
        # An earlier version of the script left Shift flipped but drag-start untouched.
        self.install(OLD_PATCH)
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("applied only in 2 places of 3", out)
        self.assertIn("applied in 1 places", out)
        self.assertEqual(self.main.read_bytes(), PATCHED.encode("utf-8"))
        self.assertEqual(self.stored_sum(), pd.checksum(PATCHED.encode("utf-8")))

    def test_half_patched_completed_and_restored(self):
        half = MAIN.replace("re.DRAG,n=>{n.shiftKey", "re.DRAG,n=>{!n.shiftKey", 1)
        self.install(half)
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(self.main.read_bytes(), PATCHED.encode("utf-8"))
        self.install(half)
        code, out = self.run_main("--restore")
        self.assertEqual(code, 0, out)
        self.assertIn("removed in 1 places", out)
        self.assertEqual(self.main.read_bytes(), MAIN.encode("utf-8"))

    def test_check_patched_says_applied(self):
        self.install(PATCHED)
        code, out = self.run_main("--check")
        self.assertEqual(code, 0)
        self.assertIn("patch applied,", out)

    def test_missing_drag_start_refused(self):
        self.assert_refused(MAIN.replace('"drag-start"', '"dragstart"', 1), expect="drag-start")

    def test_two_drag_start_handlers_refused(self):
        self.assert_refused(MAIN + 'this.on("drag-start",()=>{this._startBlockingIframeDragEvents()})',
                            expect="drag-start")

    def test_drag_start_with_other_method_refused(self):
        # The handler calls a method other than windowDidDragStart's - the logic has changed.
        other = MAIN.replace('"drag-start",()=>{this._startBlockingIframeDragEvents()}',
                             '"drag-start",()=>{this._somethingElse()}', 1)
        self.assert_refused(other, expect="drag-start")

    def test_missing_window_did_drag_start_refused(self):
        other = MAIN.replace("windowDidDragStart(){this._start", "windowDidDragStart(){this._webview?._start", 1)
        self.assert_refused(other, expect="webview windowDidDragStart")

    def test_missing_site_refused(self):
        self.assert_refused(MAIN.replace("re.DRAG_OVER,", "re.DRAGOVER,", 1), expect="DRAG_OVER")

    def test_site_beyond_window_not_used(self):
        far = MAIN.replace("this._register(G(o,re.DRAG_OVER", "/*" + "y" * pd.WINDOW + "*/this._register(G(o,re.DRAG_OVER", 1)
        self.assert_refused(far, expect="DRAG_OVER")

    def test_swapped_functions_not_mistaken(self):
        # If the new version already has other logic (t and i swapped), this is not our case.
        other = MAIN.replace("re.DRAG,n=>{n.shiftKey?i():t()}", "re.DRAG,n=>{n.shiftKey?t():i()}", 1)
        self.assert_refused(other, expect="DRAG:")

    def test_two_monitors_refused(self):
        self.assert_refused(MAIN + MONITOR, expect="2 times")

    def test_product_without_checksum_refused(self):
        self.assert_refused(MAIN, json.dumps({"version": "1", "checksums": {}}), expect="checksum")

    def test_missing_app_refused(self):
        self.main.unlink()
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("cannot read", out)

    def test_unknown_arg_refused(self):
        with self.assertRaises(SystemExit) as cm, mock.patch("sys.stderr", io.StringIO()):
            self.run_main("--bogus")
        self.assertEqual(cm.exception.code, 2)

    def test_help_touches_nothing(self):
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            self.run_main("--help")
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(self.main.read_bytes(), MAIN.encode("utf-8"))

    def test_auto_silent_when_already_patched(self):
        self.run_main()
        code, out = self.run_main("--auto")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(self.notes, [])

    def test_auto_patches_new_version_and_notifies(self):
        self.install(MAIN, product_text(MAIN, ver="1.146.0"))
        code, _ = self.run_main("--auto")
        self.assertEqual(code, 0)
        self.assertEqual(self.main.read_bytes(), PATCHED.encode("utf-8"))
        self.assertEqual(len(self.notes), 1)
        self.assertIn("1.146.0", self.notes[0])
        self.assertIn("Cmd+Q", self.notes[0])

    def test_auto_retries_then_notifies_failure(self):
        self.install(MAIN.replace("windowDidDragEnd", "windowDragEnded"))
        calls = []
        real = pd.locate
        mock.patch.object(pd, "locate", lambda *a: calls.append(1) or real(*a)).start()
        code, out = self.run_main("--auto")
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), pd.AUTO_TRIES)
        self.assertIn("not patched", out)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("no longer matches", self.notes[0])
        self.assertIn("vscode-drop-fix.log", self.notes[0])

    def test_auto_write_denied_notifies_once_without_retry(self):
        calls = []

        def deny(path, data):
            calls.append(path)
            raise PermissionError(1, "Operation not permitted", str(path))
        mock.patch.object(pd, "write_atomic", deny).start()
        code, out = self.run_main("--auto")
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("could not write", out)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("App Management", self.notes[0])

    def test_write_failure_reported_by_main(self):
        mock.patch.object(pd, "write_atomic", mock.Mock(side_effect=OSError(28, "No space"))).start()
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("write failed", out)

    def test_atomic_write_keeps_mode_and_cleans_tmp(self):
        self.main.chmod(0o640)
        pd.write_atomic(self.main, b"new")
        self.assertEqual(self.main.read_bytes(), b"new")
        self.assertEqual(self.main.stat().st_mode & 0o777, 0o640)
        self.assertFalse(any(p.name.endswith(".patch-tmp") for p in self.main.parent.iterdir()))

    def test_concurrent_atomic_writes_do_not_share_tmp_file(self):
        # A second run (installer + RunAtLoad agent) starts while the first is between tmp write and replace.
        real_copymode = pd.shutil.copymode
        state = {"first": True}

        def copymode(src, dst):
            if state["first"]:
                state["first"] = False
                pd.write_atomic(self.main, b"inner")
            real_copymode(src, dst)
        with mock.patch.object(pd.shutil, "copymode", copymode):
            pd.write_atomic(self.main, b"outer")
        self.assertEqual(self.main.read_bytes(), b"outer")
        self.assertEqual(sorted(p.name for p in self.main.parent.iterdir()), [self.main.name])

    def test_auto_other_oserror_is_retried_like_notfound(self):
        calls = []

        def vanished(path, data):
            calls.append(path)
            raise FileNotFoundError(2, "No such file or directory", str(path))
        mock.patch.object(pd, "write_atomic", vanished).start()
        code, out = self.run_main("--auto")
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), pd.AUTO_TRIES)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("no longer matches", self.notes[0])
        self.assertNotIn("App Management", self.notes[0])

    # The agent also runs once an hour: one failure must not notify every hour.
    def test_auto_same_failure_notifies_once_until_version_changes(self):
        broken = MAIN.replace("windowDidDragEnd", "windowDragEnded")
        self.install(broken)
        self.run_main("--auto")
        code, out = self.run_main("--auto")
        self.assertEqual(code, 1)
        self.assertIn("not patched", out)
        self.assertIn("already notified", out)
        self.assertEqual(len(self.notes), 1)
        self.install(broken, product_text(broken, ver="1.147.0"))
        self.run_main("--auto")
        self.assertEqual(len(self.notes), 2)

    def test_auto_success_forgets_failure(self):
        broken = MAIN.replace("windowDidDragEnd", "windowDragEnded")
        self.install(broken)
        self.run_main("--auto")
        self.assertTrue(self.failed.exists())
        self.install(MAIN)
        code, _ = self.run_main("--auto")
        self.assertEqual(code, 0)
        self.assertFalse(self.failed.exists())
        self.install(broken)
        self.run_main("--auto")
        self.assertEqual(len(self.notes), 3)  # failure, re-applied, failure again

    def test_auto_denied_every_hour_notifies_once(self):
        mock.patch.object(pd, "write_atomic",
                          mock.Mock(side_effect=PermissionError(1, "Operation not permitted"))).start()
        self.run_main("--auto")
        code, out = self.run_main("--auto")
        self.assertEqual(code, 1)
        self.assertIn("could not write", out)
        self.assertEqual(len(self.notes), 1)

    def test_denied_then_notfound_are_different_failures(self):
        deny = mock.patch.object(pd, "write_atomic",
                                 mock.Mock(side_effect=PermissionError(1, "Operation not permitted")))
        deny.start()
        self.run_main("--auto")
        deny.stop()
        self.install(MAIN.replace("windowDidDragEnd", "windowDragEnded"))
        self.run_main("--auto")
        self.assertEqual(len(self.notes), 2)

    def test_memory_not_writable_still_notifies_and_logs(self):
        self.failed.parent.parent.mkdir(parents=True, exist_ok=True)
        self.failed.parent.write_text("a file where the folder should be")
        self.install(MAIN.replace("windowDidDragEnd", "windowDragEnded"))
        self.run_main("--auto")
        code, out = self.run_main("--auto")
        self.assertEqual(code, 1)
        self.assertIn("could not remember", out)
        self.assertEqual(len(self.notes), 2)

    def test_auto_eacces_is_the_app_management_case(self):
        calls = []

        def deny(path, data):
            calls.append(path)
            raise OSError(13, "Permission denied", str(path))
        mock.patch.object(pd, "write_atomic", deny).start()
        code, _ = self.run_main("--auto")
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("App Management", self.notes[0])

    def test_orig_refreshed_when_applied_from_unpatched_state(self):
        self.run_main()
        new_main = MAIN.replace('"café"', '"new-version"')
        self.install(new_main)  # in-place update: unpatched files, stale .orig left behind
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual((self.main.parent / (self.main.name + ".orig")).read_bytes(), new_main.encode("utf-8"))
        self.assertEqual((self.product.parent / "product.json.orig").read_bytes(), product_text(new_main).encode("utf-8"))

    def test_orig_kept_when_partially_patched(self):
        self.run_main()
        orig = self.main.parent / (self.main.name + ".orig")
        orig.write_bytes(b"first original")
        self.install(OLD_PATCH)
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(orig.read_bytes(), b"first original")

    def test_restore_removes_identical_orig_copies(self):
        self.run_main()
        code, _ = self.run_main("--restore")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in self.main.parent.iterdir()), [self.main.name])
        self.assertFalse((self.product.parent / "product.json.orig").exists())

    def test_restore_keeps_differing_orig_and_says_so(self):
        self.run_main()
        orig = self.main.parent / (self.main.name + ".orig")
        orig.write_bytes(b"something else")
        code, out = self.run_main("--restore")
        self.assertEqual(code, 0)
        self.assertEqual(orig.read_bytes(), b"something else")
        self.assertIn("kept", out)
        self.assertFalse((self.product.parent / "product.json.orig").exists())

    def test_docstring_does_not_claim_shift_works(self):
        self.assertNotIn("Shift still works", pd.__doc__)
        self.assertIn("ignores a Shift-drop", pd.__doc__.replace("\n", " "))

    # --check also says whether the patch is active in the running VS Code: the main process reads
    # product.json once at launch, so a Reload Window after patching shows "corrupt".

    def ps_out(self, stdout, code=0):
        return mock.Mock(returncode=code, stdout=stdout, stderr="ps: denied" if code else "")

    def test_main_started_takes_main_process_of_this_app_only(self):
        exe = f"{self.app}/Contents/MacOS/Code"
        out = (f"Thu Nov 12 18:12:41 2026     {exe}\n"
               f"Sun Nov  1 08:00:00 2026     {self.app}/Contents/Frameworks/Code Helper.app/Contents/MacOS/Code Helper\n"
               f"Sun Nov  1 08:00:00 2026     {self.app}/Contents/MacOS/sub/tool\n"
               "Thu Oct  1 08:00:00 2026     /Applications/Other.app/Contents/MacOS/Other\n"
               f"Mon Nov  2 09:05:00 2026     {exe}\n")
        seen = []
        with mock.patch.object(pd.subprocess, "run", lambda argv, **kw: seen.append(kw) or self.ps_out(out)):
            got = REAL_MAIN_STARTED()
        self.assertEqual(got, time.mktime((2026, 11, 2, 9, 5, 0, 0, 0, -1)),
                         "earliest of the two main processes; day with a padded space")
        self.assertEqual(seen[0]["env"]["LC_ALL"], "C")

    def test_main_started_none_when_not_running(self):
        with mock.patch.object(pd.subprocess, "run",
                               lambda argv, **kw: self.ps_out("Sun Nov  1 08:00:00 2026     /bin/zsh\n")):
            self.assertIsNone(REAL_MAIN_STARTED())

    def test_main_started_ps_failure_raises(self):
        with mock.patch.object(pd.subprocess, "run", lambda argv, **kw: self.ps_out("", code=1)):
            with self.assertRaises(OSError):
                REAL_MAIN_STARTED()

    def test_check_warns_when_vscode_started_before_patch(self):
        self.install(PATCHED)
        mtime = self.product.stat().st_mtime
        with mock.patch.object(pd, "main_started", lambda: mtime - 3600):
            code, out = self.run_main("--check")
        self.assertEqual(code, 0)
        self.assertIn("patch applied", out)
        self.assertIn("before product.json was written", out)
        self.assertIn("Cmd+Q", out)

    def test_check_says_effective_when_started_after_patch(self):
        self.install(PATCHED)
        mtime = self.product.stat().st_mtime
        with mock.patch.object(pd, "main_started", lambda: mtime + 60):
            _, out = self.run_main("--check")
        self.assertIn("patch is active", out)
        self.assertNotIn("Cmd+Q", out)

    def test_check_without_ps_says_not_checked(self):
        self.install(PATCHED)
        def denied():
            raise PermissionError(1, "Operation not permitted")
        with mock.patch.object(pd, "main_started", denied):
            code, out = self.run_main("--check")
        self.assertEqual(code, 0)
        self.assertIn("not checked", out)

    def test_check_says_not_running(self):
        self.install(PATCHED)
        _, out = self.run_main("--check")
        self.assertIn("VS Code is not running", out)

    def test_check_stock_install_never_says_active(self):
        # Review 23.11: stock VS Code (or a checksum mismatch) must not print "the patch is active".
        # MAIN: stock; OLD_PATCH: applied in 2 places of 3 (an all()->any() mutant must die here).
        for main, product in ((MAIN, None), (OLD_PATCH, None)):
            self.install(main, product)
            mtime = self.product.stat().st_mtime
            with mock.patch.object(pd, "main_started", lambda: mtime + 60):
                code, out = self.run_main("--check")
            self.assertEqual(code, 0)
            self.assertNotIn("the patch is active", out)
            self.assertIn("not active in this install", out)
            self.assertIn("applied only in part", out)

    def test_check_checksum_mismatch_text_is_not_a_restart_hint(self):
        # Patched everywhere but product.json holds the stock checksum: the patched JS loads, the
        # "corrupt" warning comes from the checksum, a restart does not help.
        self.install(PATCHED, product_text(MAIN))
        mtime = self.product.stat().st_mtime
        with mock.patch.object(pd, "main_started", lambda: mtime + 60):
            code, out = self.run_main("--check")
        self.assertEqual(code, 0)
        self.assertNotIn("the patch is active", out)
        self.assertNotIn("not active in this install", out)
        self.assertIn("checksum in product.json does not match", out)
        self.assertIn("does load", out)

    def test_check_files_vanish_after_the_check_gives_clear_message(self):
        self.install(PATCHED)
        real = pd.run

        def run_then_vanish(want, quiet=False):
            r = real(want, quiet)
            self.main.unlink()
            return r
        with mock.patch.object(pd, "run", run_then_vanish):
            code, out = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("could not re-read the files", out)

    def test_check_product_without_checksum_after_the_check_gives_clear_message(self):
        self.install(PATCHED)
        real = pd.run

        def run_then_break(want, quiet=False):
            r = real(want, quiet)
            self.product.write_text(json.dumps({"version": "1", "checksums": {}}))
            return r
        with mock.patch.object(pd, "run", run_then_break):
            code, out = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("found 0 times", out)

    def test_check_with_app_dir_skips_process_lookup(self):
        called = []
        other = Path(self.tmp.name) / "linux" / "resources" / "app"
        (other / "out" / "vs" / "workbench").mkdir(parents=True)
        (other / "out" / pd.MAIN_REL).write_bytes(PATCHED.encode("utf-8"))
        (other / "product.json").write_bytes(product_text(PATCHED).encode("utf-8"))
        with mock.patch.object(pd, "main_started", lambda: called.append(1)):
            _, out = self.run_main("--check", "--app-dir", str(other))
        self.assertEqual(called, [])
        self.assertIn("not checked", out)

    def test_patch_does_not_look_at_processes(self):
        called = []
        with mock.patch.object(pd, "main_started", lambda: called.append(1)):
            _, out = self.run_main()
        self.assertEqual(called, [])
        self.assertIn("Cmd+Q", out)

    def test_checksum_matches_vscode_format(self):
        # sha256 base64 without padding.
        s = pd.checksum(b"abc")
        self.assertEqual(s, "ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0")
        self.assertFalse(s.endswith("="))

    def test_app_dir_points_at_fake_folder(self):
        other = Path(self.tmp.name) / "linux" / "resources" / "app"
        (other / "out" / "vs" / "workbench").mkdir(parents=True)
        (other / "out" / pd.MAIN_REL).write_bytes(MAIN.encode("utf-8"))
        (other / "product.json").write_bytes(product_text(MAIN).encode("utf-8"))
        code, out = self.run_main("--app-dir", str(other))
        self.assertEqual(code, 0, out)
        self.assertEqual((other / "out" / pd.MAIN_REL).read_bytes(), PATCHED.encode("utf-8"))
        self.assertEqual(self.main.read_bytes(), MAIN.encode("utf-8"), "the macOS fake app is untouched")

    def test_notify_skipped_off_macos(self):
        with mock.patch.object(pd.subprocess, "run") as run, mock.patch.object(pd.sys, "platform", "linux"), \
                redirect_stdout(io.StringIO()) as out:
            REAL_NOTIFY("hello")
        run.assert_not_called()
        self.assertIn("hello", out.getvalue())

    def test_notify_passes_text_as_argument(self):
        seen = []
        ok = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(pd.subprocess, "run", lambda argv, **kw: seen.append(argv) or ok):
            with mock.patch.object(pd.sys, "platform", "darwin"):
                REAL_NOTIFY("Screenshot \"in quotes\"")
        self.assertEqual(seen[0][-2:], ["Screenshot \"in quotes\"", pd.NOTIFY_TITLE])
        self.assertNotIn("Screenshot", " ".join(seen[0][:-2]))
        self.assertEqual(pd.NOTIFY_TITLE, "VS Code Drop Fix")


if __name__ == "__main__":
    unittest.main()
