"""Install lifecycle fixtures; run only in the joined system proof."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest

from scripts import install

ROOT = Path(__file__).resolve().parents[1]


class InstallFixtureTests(unittest.TestCase):
    def test_isolated_install_stages_executable_and_owned_launchers_then_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex_root = base / "codex-home"
            data = base / "isolated-data"
            bindir = base / "bin"
            binary = base / "ctrlissues"
            binary.write_bytes(b"fixture-native-binary")

            first = install.install(ROOT, None, binary, codex_root, data, bindir,
                                    "codex-unused", True, False)
            self.assertFalse(first["registered"])
            payload1 = Path(first["payload"])
            native1 = payload1 / "plugins/ctrlissues/runtime/bin/ctrlissues"
            self.assertEqual(native1.read_bytes(), b"fixture-native-binary")
            if os.name != "nt":
                self.assertEqual(native1.stat().st_mode & 0o777, 0o755)
            stable = data / "ctrlissues"
            user = bindir / "ctrlissues"
            self.assertIn(b"CtrlIssues stable launcher", stable.read_bytes())
            self.assertIn(b"CtrlIssues user launcher", user.read_bytes())

            binary.write_bytes(b"fixture-native-binary generation two")
            second = install.install(ROOT, None, binary, codex_root, data, bindir,
                                     "codex-unused", True, False)
            self.assertNotEqual(first["generation"], second["generation"])
            self.assertEqual(install.confine_payload(data, (data / "current").read_text().strip()).name, second["generation"])
            with contextlib.redirect_stdout(io.StringIO()):
                install.main(["--codex-root", str(codex_root), "--prefix", str(data), "--rollback"])
            self.assertEqual(install.confine_payload(data, (data / "current").read_text()).name, first["generation"])
            self.assertTrue(stable.is_file())
            self.assertTrue(user.is_file())

    def test_unrelated_user_launcher_is_preserved_and_blocks_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex_root, data, bindir = base / "codex-home", base / "data", base / "bin"
            bindir.mkdir()
            unrelated = bindir / "ctrlissues"
            unrelated.write_text("owner's file\n")
            binary = base / "ctrlissues-bin"
            binary.write_bytes(b"fixture")
            with self.assertRaisesRegex(ValueError, "refusing_unrelated_user_launcher"):
                install.install(ROOT, None, binary, codex_root, data, bindir,
                                "codex-unused", True, False)
            self.assertEqual(unrelated.read_text(), "owner's file\n")
            self.assertFalse(data.exists())


if __name__ == "__main__":
    unittest.main()
