# -*- coding: utf-8 -*-
"""runtime_env 單元測試 —— 「是否為打包版」這個判斷的唯一來源。

Nuitka 不設 sys.frozen、只對每個編譯過的模組注入 __compiled__；這裡以手動
設定模組全域變數模擬打包版，確保判斷不會只看 sys.frozen。
可直接執行：python tests/test_runtime_env.py
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runtime_env


class _SimulatedNuitka:
    """在 with 區塊內把 runtime_env 偽裝成 Nuitka 編譯過的模組。

    Nuitka 是「逐模組」注入 __compiled__，所以模擬時必須注入在
    runtime_env 自己身上，而不是呼叫端模組。
    """

    def __init__(self):
        self._patch = mock.patch.object(
            runtime_env, "__compiled__", True, create=True)

    def __enter__(self):
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


class TestIsFrozen(unittest.TestCase):
    def test_not_frozen_in_source_run(self):
        # 測試環境本身即原始碼執行，不應被判定為打包版
        self.assertFalse(runtime_env.is_frozen())

    def test_nuitka_compiled_marker_counts_as_frozen(self):
        # Nuitka 不設 sys.frozen，只注入 __compiled__；須能正確辨識
        with _SimulatedNuitka():
            self.assertTrue(runtime_env.is_frozen())

    def test_pyinstaller_frozen_flag(self):
        with mock.patch.object(runtime_env.sys, "frozen", True, create=True):
            self.assertTrue(runtime_env.is_frozen())

    def test_pyinstaller_meipass(self):
        with mock.patch.object(runtime_env.sys, "_MEIPASS", r"D:\tmp", create=True):
            self.assertTrue(runtime_env.is_frozen())


class TestFrozenExePath(unittest.TestCase):
    def test_prefers_argv0_exe(self):
        # 打包版：sys.executable 是 dist 內建的 python.exe，真正 exe 在 argv[0]
        with mock.patch.object(runtime_env.sys, "argv",
                               [r"D:\app\MultiSocksDownloader.exe"]), \
             mock.patch.object(runtime_env.sys, "executable", r"D:\app\python.exe"):
            self.assertEqual(
                runtime_env.frozen_exe_path(), r"D:\app\MultiSocksDownloader.exe")

    def test_falls_back_to_executable(self):
        with mock.patch.object(runtime_env.sys, "argv", ["app.py"]), \
             mock.patch.object(runtime_env.sys, "executable", r"D:\Python\python.exe"):
            self.assertTrue(
                runtime_env.frozen_exe_path().endswith("python.exe"))


class TestProcessName(unittest.TestCase):
    def test_frozen_uses_real_exe_not_bundled_python(self):
        with _SimulatedNuitka(), \
             mock.patch.object(runtime_env.sys, "argv",
                               [r"D:\app\MultiSocksDownloader.exe"]), \
             mock.patch.object(runtime_env.sys, "executable", r"D:\app\python.exe"):
            self.assertEqual(runtime_env.process_name(), "MultiSocksDownloader.exe")

    def test_source_run_uses_python_exe(self):
        with mock.patch.object(runtime_env, "is_frozen", return_value=False), \
             mock.patch.object(runtime_env.sys, "executable",
                               r"D:\Python\python.exe"):
            self.assertEqual(runtime_env.process_name(), "python.exe")

    def test_no_separator_in_result(self):
        self.assertNotIn(os.sep, runtime_env.process_name())


if __name__ == "__main__":
    unittest.main()
