#!/usr/bin/env python3
"""Check native Brotli release gates without building or installing packages."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "actions/patch-node24/patch-native-brotli.py"
FIXED = "_ERROR_FORMAT_BLOCK_SWITCH"


class NativeBrotliTests(unittest.TestCase):
    def run_bootstrap(self, release, version, block_switch, distribution="ubuntu"):
        current = {"version": version, "blockSwitch": block_switch}
        stdout = io.StringIO()
        with (
            patch("pathlib.Path.read_text", return_value=(
                f'ID={distribution}\nVERSION_ID="{release}"\n'
            )),
            patch("subprocess.check_output", return_value=json.dumps(current)) as probe,
            patch("subprocess.run", side_effect=AssertionError("unexpected package operation")),
            patch("shutil.which", return_value=None) as prerequisite,
            patch("os.umask"),
            redirect_stdout(stdout),
            self.assertRaises(SystemExit) as exited,
        ):
            runpy.run_path(str(SCRIPT), run_name="__main__")
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[0][:2], [sys.executable, "-c"])
        return exited.exception.code, stdout.getvalue(), prerequisite

    def test_fixed_decoder_skips_rebuild_on_any_ubuntu_release(self):
        for release, version in (("24.04", 0x1001001), ("26.04", 0x1002000), ("28.04", 0x1003000)):
            with self.subTest(release=release):
                code, stdout, prerequisite = self.run_bootstrap(release, version, FIXED)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout), {
                    "nativeBrotli": {"version": version, "blockSwitch": FIXED},
                    "changed": False,
                })
                prerequisite.assert_not_called()

    def test_unfixed_decoder_on_2604_fails_with_release_and_version(self):
        code, stdout, prerequisite = self.run_bootstrap("26.04", 0x1002000, "_ERROR_INVALID_ARGUMENTS")
        self.assertIn("Ubuntu 26.04", code)
        self.assertIn("0x1002000", code)
        self.assertIn("requires Ubuntu 24.04", code)
        self.assertEqual(stdout, "")
        prerequisite.assert_not_called()

    def test_2404_uninspected_abi_still_fails(self):
        code, stdout, prerequisite = self.run_bootstrap("24.04", 0x1002000, "_ERROR_INVALID_ARGUMENTS")
        self.assertIn("requires the inspected 1.1.0 ABI", code)
        self.assertEqual(stdout, "")
        prerequisite.assert_not_called()

    def test_2404_inspected_abi_still_reaches_build_prerequisites(self):
        code, stdout, prerequisite = self.run_bootstrap("24.04", 0x1001000, "_ERROR_INVALID_ARGUMENTS")
        self.assertEqual(code, "Native Brotli build prerequisite missing: gcc")
        self.assertEqual(stdout, "")
        prerequisite.assert_called_once_with("gcc")

    def test_non_ubuntu_still_fails_even_with_fixed_decoder(self):
        code, stdout, prerequisite = self.run_bootstrap("13", 0x1002000, FIXED, distribution="debian")
        self.assertIn("requires Ubuntu", code)
        self.assertEqual(stdout, "")
        prerequisite.assert_not_called()


if __name__ == "__main__":
    unittest.main()
