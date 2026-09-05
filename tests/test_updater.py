import os
import sys
import unittest
import hashlib
import tempfile
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updater


class TestIsNewer(unittest.TestCase):
    def test_string_compare_pitfall(self):
        # 字串比對會誤判 1.10.0 < 1.9.0，整數 tuple 比對不應有此問題
        self.assertTrue(updater._is_newer("1.10.0", "1.9.0"))
        self.assertFalse(updater._is_newer("1.9.0", "1.10.0"))

    def test_equal_versions(self):
        self.assertFalse(updater._is_newer("1.6.0", "1.6.0"))

    def test_v_prefix_is_ignored(self):
        self.assertTrue(updater._is_newer("v1.7.0", "1.6.0"))
        self.assertFalse(updater._is_newer("1.6.0", "v1.6.0"))

    def test_different_length_tuples(self):
        self.assertTrue(updater._is_newer("2.0", "1.9.9"))
        self.assertTrue(updater._is_newer("1.6.1", "1.6"))
        self.assertFalse(updater._is_newer("1.6", "1.6.1"))


class TestParseSha256Sums(unittest.TestCase):
    HASH = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    def test_parses_double_space_format(self):
        text = "{}  MultiSocksDownloader-v1.6.0-windows-x64.zip\n".format(self.HASH)
        self.assertEqual(
            updater._parse_sha256_sums(text, "MultiSocksDownloader-v1.6.0-windows-x64.zip"),
            self.HASH,
        )

    def test_uppercase_hash_is_lowered(self):
        text = "{}  app.zip\n".format(self.HASH.upper())
        self.assertEqual(updater._parse_sha256_sums(text, "app.zip"), self.HASH)

    def test_missing_asset_raises(self):
        text = "{}  other.zip\n".format(self.HASH)
        with self.assertRaises(ValueError):
            updater._parse_sha256_sums(text, "app.zip")


class TestSha256File(unittest.TestCase):
    def test_matches_hashlib(self):
        content = b"hello world"
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content)
            path = f.name
        try:
            self.assertEqual(
                updater._sha256_file(path), hashlib.sha256(content).hexdigest()
            )
        finally:
            os.remove(path)


class TestExtractZip(unittest.TestCase):
    def _make_zip(self, entries):
        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries:
                zf.writestr(name, data)
        return path

    def test_zip_slip_is_blocked(self):
        zip_path = self._make_zip([("../evil.txt", b"boom")])
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(ValueError):
                updater._extract_zip(zip_path, os.path.join(out, "dist.new"))
            # 解壓目錄外不應被寫入任何檔案
            self.assertFalse(os.path.exists(os.path.join(out, "evil.txt")))
        os.remove(zip_path)

    def test_normal_entry_extracts(self):
        zip_path = self._make_zip([("MultiSocksDownloader.exe", b"exe")])
        with tempfile.TemporaryDirectory() as out:
            dest = os.path.join(out, "dist.new")
            updater._extract_zip(zip_path, dest)
            self.assertTrue(os.path.isfile(os.path.join(dest, "MultiSocksDownloader.exe")))
        os.remove(zip_path)


class TestFrozenExePath(unittest.TestCase):
    def test_prefers_argv0_exe(self):
        with mock.patch.object(updater.sys, "argv", ["C:\\app\\MultiSocksDownloader.exe"]):
            self.assertTrue(updater.frozen_exe_path().endswith("MultiSocksDownloader.exe"))

    def test_falls_back_to_executable(self):
        with mock.patch.object(updater.sys, "argv", ["C:\\app\\MultiSocksDownloader.py"]), \
             mock.patch.object(updater.sys, "executable", "C:\\Python\\python.exe"):
            self.assertTrue(updater.frozen_exe_path().endswith("python.exe"))


class TestGenerateApplyScript(unittest.TestCase):
    def test_ascii_only_and_contains_gotcha_fixes(self):
        script = updater.generate_apply_script(
            "C:\\app\\MultiSocksDownloader.exe",
            "C:\\app\\dist",
            "C:\\app\\dist.new",
            "C:\\app\\dist.old",
            "C:\\log\\update.log",
        )
        # 全 ASCII：避免 PS 5.1 讀無 BOM UTF-8 亂碼
        script.encode("ascii")
        # 坑 3：用 .NET ProcessStartInfo + UseShellExecute=$false
        self.assertIn("UseShellExecute = $false", script)
        self.assertIn("System.Diagnostics.ProcessStartInfo", script)
        # 坑 6：不得使用 Start-Job（會卡住管道）
        self.assertNotIn("Start-Job", script)
        # 坑 8：等正確程序名退出
        self.assertIn("Get-Process -Name", script)
        self.assertIn("'MultiSocksDownloader'", script)


if __name__ == "__main__":
    unittest.main()
