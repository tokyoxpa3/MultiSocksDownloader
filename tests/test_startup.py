import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import startup


class TestFrozenDetection(unittest.TestCase):
    def test_not_frozen_in_source_run(self):
        # 測試環境本身即原始碼執行，不應被判定為打包版
        self.assertFalse(startup._is_frozen())

    def test_nuitka_compiled_marker_counts_as_frozen(self):
        # Nuitka 不設 sys.frozen，只注入 __compiled__；須能正確辨識
        startup.__compiled__ = True
        try:
            self.assertTrue(startup._is_frozen())
        finally:
            del startup.__compiled__


class TestExecutableCommand(unittest.TestCase):
    def test_source_command_targets_entry_script(self):
        with mock.patch.object(startup, "_is_frozen", return_value=False):
            cmd = startup.executable_command()
        self.assertIn("MultiSocksDownloader.py", cmd)
        self.assertNotIn("%1", cmd)  # 開機啟動不帶檔案參數

    def test_source_command_quotes_paths(self):
        with mock.patch.object(startup, "_is_frozen", return_value=False):
            cmd = startup.executable_command()
        self.assertTrue(cmd.startswith('"'))
        self.assertTrue(cmd.count('"') >= 4)

    def test_frozen_command_points_at_real_exe(self):
        # 打包版：sys.executable 會是 dist\python.exe，真正 exe 在 argv[0]，
        # 命令必須指向 argv[0]，且不得含 .py 腳本或 python.exe。
        with mock.patch.object(startup, "_is_frozen", return_value=True), \
             mock.patch.object(startup.sys, "argv",
                               [r"D:\app\MultiSocksDownloader.exe"]), \
             mock.patch.object(startup.sys, "executable",
                               r"D:\app\python.exe"):
            cmd = startup.executable_command()
        self.assertEqual(cmd, '"D:\\app\\MultiSocksDownloader.exe"')
        self.assertNotIn(".py", cmd)
        self.assertNotIn("python.exe", cmd)


class TestCommandTarget(unittest.TestCase):
    def test_quoted_path(self):
        self.assertEqual(
            startup._command_target('"C:\\a b\\app.exe" "C:\\x.py"'),
            "C:\\a b\\app.exe")

    def test_unquoted_path(self):
        self.assertEqual(
            startup._command_target("C:\\app.exe --flag"), "C:\\app.exe")

    def test_empty(self):
        self.assertIsNone(startup._command_target(""))
        self.assertIsNone(startup._command_target(None))


class TestIsEnabled(unittest.TestCase):
    def test_returns_bool_without_raising(self):
        self.assertIsInstance(startup.is_enabled(), bool)

    def test_enabled_only_when_target_exists(self):
        # 指向存在的檔案 -> 啟用；指向不存在的路徑（舊版錯誤命令）-> 未啟用
        existing = os.path.abspath(__file__)
        for value, expected in (
            (f'"{existing}"', True),
            ('"D:\\ghost\\python.exe" "D:\\ghost\\app.py"', False),
        ):
            with mock.patch.object(startup.winreg, "OpenKey",
                                   return_value=mock.MagicMock()), \
                 mock.patch.object(startup.winreg, "QueryValueEx",
                                   return_value=(value, 1)):
                self.assertIs(startup.is_enabled(), expected)


# 以 mock 取代 winreg 個別函式，驗證啟用/停用寫入的鍵值，不實際動到登錄檔。
class TestEnableDisableWithMock(unittest.TestCase):
    def test_enable_writes_run_value(self):
        with mock.patch.object(startup.winreg, "CreateKeyEx",
                               return_value=mock.MagicMock()) as create, \
             mock.patch.object(startup.winreg, "SetValueEx") as setval:
            self.assertTrue(startup.enable())

        self.assertEqual(create.call_args.args[1], startup.RUN_KEY)
        # SetValueEx(key, name, 0, REG_SZ, value)
        self.assertEqual(setval.call_args.args[1], startup.VALUE_NAME)
        self.assertEqual(setval.call_args.args[4], startup.executable_command())

    def test_disable_removes_value(self):
        with mock.patch.object(startup.winreg, "OpenKey",
                               return_value=mock.MagicMock()), \
             mock.patch.object(startup.winreg, "DeleteValue") as delval:
            startup.disable()
            delval.assert_called_once()
            self.assertEqual(delval.call_args.args[1], startup.VALUE_NAME)

    def test_disable_when_absent_does_not_raise(self):
        with mock.patch.object(startup.winreg, "OpenKey",
                               side_effect=FileNotFoundError):
            startup.disable()

    def test_non_windows_is_noop(self):
        with mock.patch.object(startup.sys, "platform", "linux"):
            self.assertFalse(startup.is_enabled())
            self.assertFalse(startup.enable())
            startup.disable()  # 不應拋出例外


if __name__ == "__main__":
    unittest.main()
