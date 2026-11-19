#!/usr/bin/env python3
"""Exercise DMG packaging's host-tool boundary without opening Finder in CI."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackageDMGTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="package dmg test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / "Try Omarchy.app"
        self.app.mkdir()
        self.log = self.root / "commands.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        fake = self.bin / "fake-tool"
        fake.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, shutil, signal, sys
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["DMG_TEST_LOG"], "a") as log:
    log.write(json.dumps([tool, *args]) + "\\n")
if tool == "ditto":
    shutil.copytree(args[0], args[1])
elif tool == "hdiutil" and args[0] == "create":
    pathlib.Path(args[-1]).touch()
elif tool == "hdiutil" and args[0] == "convert":
    if os.environ.get("DMG_TEST_COLLISION"):
        destination = pathlib.Path(os.environ["DMG_TEST_OUTPUT"])
        if os.environ["DMG_TEST_COLLISION"] == "directory":
            destination.mkdir()
        elif os.environ["DMG_TEST_COLLISION"] in ("symlink", "dangling"):
            target = "target" if os.environ["DMG_TEST_COLLISION"] == "symlink" else "missing"
            destination.symlink_to(destination.parent / target)
        else:
            destination.write_text("another packager")
    pathlib.Path(args[args.index("-o") + 1]).write_text("converted")
elif tool == "codesign" and args[0] == "--sign":
    with open(args[-1], "a") as output:
        output.write(" signed")
elif tool == "xcrun" and args[:2] == ["stapler", "staple"] and args[-1].endswith(".dmg"):
    with open(args[-1], "a") as output:
        output.write(" stapled")
step = None
if tool == "hdiutil" and args[0] == "convert":
    step = "convert"
elif tool == "osascript":
    step = "layout"
elif tool == "codesign" and args[-1].endswith(".dmg"):
    step = "sign" if args[0] == "--sign" else "verify"
elif tool == "xcrun":
    if args[0] == "notarytool":
        step = "notarize"
    else:
        step = f"{args[1]}-{pathlib.Path(args[-1]).suffix[1:]}"
if step == "convert" and os.environ.get("DMG_TEST_FAIL_STEP") == "interrupt":
    print("package-dmg-test: interrupt", file=sys.stderr)
    os.kill(os.getppid(), signal.SIGTERM)
if step == os.environ.get("DMG_TEST_FAIL_STEP"):
    print(f"package-dmg-test: {step}", file=sys.stderr)
    sys.exit(1)
''')
        fake.chmod(0o755)
        for tool in ("codesign", "diskutil", "ditto", "hdiutil", "osascript", "sync", "xcrun"):
            (self.bin / tool).symlink_to(fake)

    def package(self, name, *, fail_step="", notarize=False, collision=""):
        environment = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                           HOME=str(self.home), DMG_TEST_LOG=str(self.log),
                           DMG_TEST_FAIL_STEP=fail_step, DMG_TEST_COLLISION=collision,
                           DMG_TEST_OUTPUT=str(self.root / name))
        options = ["--sign-identity", "test identity", "--notarize-profile", "test profile"] if notarize else []
        return subprocess.run(
            ["/bin/bash", str(ROOT / "macos/package-dmg.sh"), *options,
             str(self.app), str(self.root / name)],
            env=environment, capture_output=True, text=True,
        )

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_working_volumes_are_unique_and_final_installer_name_is_restored(self):
        labels = []
        for name in ("first.dmg", "second.dmg"):
            with self.subTest(name=name):
                self.log.unlink(missing_ok=True)
                result = self.package(name)
                self.assertEqual(result.returncode, 0, result.stderr)
                commands = self.commands()
                create = next(c for c in commands if c[:2] == ["hdiutil", "create"])
                labels.append(create[create.index("-volname") + 1])
                self.assertNotEqual(labels[-1], "Try Omarchy")
                attach = next(c for c in commands if c[:2] == ["hdiutil", "attach"])
                mount = attach[attach.index("-mountpoint") + 1]
                self.assertEqual(Path(mount).name, labels[-1])
                layout = next(c for c in commands if c[0] == "osascript")
                rename = ["diskutil", "rename", mount, "Try Omarchy"]
                self.assertEqual(layout[2:], [mount, self.app.name])
                self.assertLess(commands.index(layout), commands.index(rename))
                self.assertLess(commands.index(rename),
                                commands.index(["hdiutil", "detach", mount]))
                self.assertTrue((self.root / name).is_file())
                self.assertFalse(Path(mount).parent.exists())
        self.assertNotEqual(*labels)

    def test_failed_layout_detaches_without_converting_or_renaming(self):
        result = self.package("failed.dmg", fail_step="layout")
        self.assertNotEqual(result.returncode, 0)
        commands = self.commands()
        layout = next(c for c in commands if c[0] == "osascript")
        mount = layout[2]
        self.assertIn(["hdiutil", "detach", mount, "-force"], commands)
        self.assertFalse(any(c[0] == "diskutil" for c in commands))
        self.assertFalse(any(c[:2] == ["hdiutil", "convert"] for c in commands))
        self.assertFalse((self.root / "failed.dmg").exists())
        self.assertFalse(Path(mount).parent.exists())
        self.assert_cleaned()

    def assert_cleaned(self):
        self.assertEqual(list(self.root.glob(".omarchy-dmg.*")), [])
        for command in self.commands():
            if command[:2] == ["hdiutil", "attach"]:
                mount = Path(command[command.index("-mountpoint") + 1])
                self.assertFalse(mount.parent.exists())

    def test_failed_finalization_leaves_no_output_and_can_be_retried(self):
        for step in ("convert", "sign", "verify", "notarize", "staple-app",
                     "validate-app", "staple-dmg", "validate-dmg", "interrupt"):
            with self.subTest(step=step):
                name = f"release {step}.dmg"
                result = self.package(name, fail_step=step, notarize=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"package-dmg-test: {step}", result.stderr)
                if step == "interrupt":
                    self.assertEqual(result.returncode, 143)
                self.assertFalse((self.root / name).exists(), result.stderr)
                self.assert_cleaned()
                retry = self.package(name, notarize=True)
                self.assertEqual(retry.returncode, 0, retry.stderr)
                self.assertEqual((self.root / name).read_text(), "converted signed stapled")
                self.assert_cleaned()

    def test_complete_image_is_signed_and_stapled_before_publication(self):
        name = "signed release.dmg"
        result = self.package(name, notarize=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        convert = next(c for c in commands if c[:2] == ["hdiutil", "convert"])
        staged = convert[convert.index("-o") + 1]
        self.assertNotEqual(staged, str(self.root / name))
        self.assertEqual(Path(staged).parent.parent, self.root)
        self.assertIn(["xcrun", "notarytool", "submit", staged,
                       "--keychain-profile", "test profile", "--wait"], commands)
        self.assertIn(["xcrun", "stapler", "validate", staged], commands)
        self.assertEqual((self.root / name).read_text(), "converted signed stapled")
        self.assert_cleaned()

    def test_existing_destinations_are_preserved_before_running_host_tools(self):
        target = self.root / "target"
        target.write_text("original")
        for kind in ("file", "directory", "symlink", "dangling"):
            with self.subTest(kind=kind):
                name = f"existing {kind}.dmg"
                destination = self.root / name
                if kind == "file":
                    destination.write_text("original")
                elif kind == "directory":
                    destination.mkdir()
                else:
                    destination.symlink_to(target if kind == "symlink" else self.root / "missing")
                result = self.package(name)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("output already exists", result.stderr)
                self.assertFalse(self.log.exists())
                if kind == "file":
                    self.assertEqual(destination.read_text(), "original")
                elif kind == "directory":
                    self.assertEqual(list(destination.iterdir()), [])
                else:
                    self.assertTrue(destination.is_symlink())
                self.assertEqual(target.read_text(), "original")

    def test_destination_created_during_packaging_is_not_replaced_or_entered(self):
        target = self.root / "target"
        target.write_text("original")
        for kind in ("file", "directory", "symlink", "dangling"):
            with self.subTest(kind=kind):
                name = f"concurrent {kind}.dmg"
                result = self.package(name, collision=kind)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cannot publish", result.stderr)
                destination = self.root / name
                if kind == "file":
                    self.assertEqual(destination.read_text(), "another packager")
                elif kind == "directory":
                    self.assertTrue(destination.is_dir(), result.stderr)
                    self.assertEqual(list(destination.iterdir()), [])
                else:
                    self.assertTrue(destination.is_symlink(), result.stderr)
                    self.assertEqual(destination.readlink(), target if kind == "symlink" else self.root / "missing")
                self.assertEqual(target.read_text(), "original")
                self.assertNotIn("[native] Packaged", result.stdout)
                self.assert_cleaned()


if __name__ == "__main__":
    unittest.main()
