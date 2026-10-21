# -*- coding: utf-8 -*-
"""netredirector_guard 單元測試。

以替身取代 urllib 與 token 檔，驗證各種回應對應的 status，不真的連線。
可直接執行：python tests/test_netredirector_guard.py
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import netredirector_guard as guard
import runtime_env


class _FakeResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class GuardTestBase(unittest.TestCase):
    def setUp(self):
        self._orig_urlopen = guard.urllib.request.urlopen
        self._orig_token_file = guard.TOKEN_FILE
        self._had_frozen = hasattr(sys, "frozen")
        self._orig_frozen = getattr(sys, "frozen", None)
        # 預設模擬「已打包成 exe」：ensure_direct 才會走請求流程。開發模式的
        # 跳過行為由 test_skipped_in_dev_mode 單獨驗證。
        sys.frozen = True
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        guard.urllib.request.urlopen = self._orig_urlopen
        guard.TOKEN_FILE = self._orig_token_file
        if self._had_frozen:
            sys.frozen = self._orig_frozen
        elif hasattr(sys, "frozen"):
            del sys.frozen

    def _patch_urlopen(self, fn):
        guard.urllib.request.urlopen = fn

    def _write_token(self, value="tok"):
        path = os.path.join(self.tmpdir, "control.token")
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
        guard.TOKEN_FILE = path


class TestEnsureDirect(GuardTestBase):
    def test_skipped_in_dev_mode(self):
        """開發模式（未打包，進程名 python.exe）預設不加入，避免波及所有 Python。"""
        if hasattr(sys, "frozen"):
            del sys.frozen
        called = []
        self._patch_urlopen(lambda *a, **k: called.append(1))
        self._write_token()
        result = guard.ensure_direct()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result.get("reason"), "dev_mode")
        self.assertEqual(called, [], "開發模式不該發出請求")

    def test_skipped_when_no_token(self):
        called = []
        self._patch_urlopen(lambda *a, **k: called.append(1))
        guard.TOKEN_FILE = os.path.join(self.tmpdir, "missing.token")
        result = guard.ensure_direct()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(called, [], "沒有 token 時不該發出請求")

    def test_added(self):
        self._write_token()
        self._patch_urlopen(lambda *a, **k: _FakeResp({"added": True, "rule_id": 3}))
        result = guard.ensure_direct()
        self.assertEqual(result["status"], "added")
        self.assertEqual(result["target"], guard.own_process_name())

    def test_existed(self):
        self._write_token()
        self._patch_urlopen(lambda *a, **k: _FakeResp({"existed": True}))
        result = guard.ensure_direct()
        self.assertEqual(result["status"], "existed")

    def test_connection_error_is_skipped(self):
        self._write_token()

        def boom(*a, **k):
            raise urllib.error.URLError("connection refused")

        self._patch_urlopen(boom)
        result = guard.ensure_direct()
        self.assertEqual(result["status"], "skipped")

    def test_sends_target_and_token(self):
        self._write_token("abc123")
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url
            seen["token"] = req.headers.get("X-nr-token")
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp({"added": True})

        self._patch_urlopen(fake)
        guard.ensure_direct()
        self.assertEqual(seen["body"]["target"], guard.own_process_name())
        self.assertEqual(seen["token"], "abc123")
        self.assertTrue(seen["url"].endswith("/ensure-direct"))

    def test_error_on_unexpected_response(self):
        self._write_token()
        self._patch_urlopen(lambda *a, **k: _FakeResp({"error": "boom"}))
        result = guard.ensure_direct()
        self.assertEqual(result["status"], "error")


class TestFrozenDetection(unittest.TestCase):
    """偵測邏輯已集中到 runtime_env（見 tests/test_runtime_env.py）。

    這裡只確認 guard 沒有自己另寫一份 —— v1.10.2 的 bug 就是它自己寫了
    一份、而且只看了 sys.frozen。
    """

    def test_guard_has_no_local_detection(self):
        self.assertFalse(hasattr(guard, "is_frozen"))
        self.assertFalse(hasattr(guard, "_frozen_exe_path"))

    def test_not_frozen_in_source_run(self):
        self.assertFalse(runtime_env.is_frozen())


class TestEnsureDirectUnderNuitka(unittest.TestCase):
    """回歸測試：Nuitka 打包版沒有 sys.frozen。

    過去 ensure_direct 只看 sys.frozen，打包版會被當成開發模式、整個直連
    設定靜默略過（使用者實際回報的症狀）。
    """

    def setUp(self):
        self._had_frozen = hasattr(sys, "frozen")
        self._orig_frozen = getattr(sys, "frozen", None)
        if self._had_frozen:
            del sys.frozen
        self._orig_urlopen = guard.urllib.request.urlopen
        self._orig_token_file = guard.TOKEN_FILE
        # 模擬 Nuitka 打包後的世界：__compiled__ 只注入 runtime_env（判斷所在）
        self._compiled = mock.patch.object(
            runtime_env, "__compiled__", True, create=True)
        self._compiled.start()
        self.tmpdir = tempfile.mkdtemp()
        path = os.path.join(self.tmpdir, "control.token")
        with open(path, "w", encoding="utf-8") as f:
            f.write("tok")
        guard.TOKEN_FILE = path

    def tearDown(self):
        self._compiled.stop()
        guard.urllib.request.urlopen = self._orig_urlopen
        guard.TOKEN_FILE = self._orig_token_file
        if self._had_frozen:
            sys.frozen = self._orig_frozen
        elif hasattr(sys, "frozen"):
            del sys.frozen

    def test_not_skipped_as_dev_mode(self):
        seen = {}

        def fake(req, timeout=None):
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp({"added": True})

        guard.urllib.request.urlopen = fake
        with mock.patch.object(runtime_env.sys, "argv",
                               [r"D:\app\MultiSocksDownloader.exe"]), \
             mock.patch.object(runtime_env.sys, "executable", r"D:\app\python.exe"):
            result = guard.ensure_direct()

        self.assertEqual(result["status"], "added")
        # 送出的必須是真正的程式 exe；Nuitka 的 sys.executable 是 dist 內建的
        # python.exe，用它會變成「讓所有 Python 直連」，正是本功能要避免的。
        self.assertEqual(seen["body"]["target"], "MultiSocksDownloader.exe")


class TestOwnProcessName(unittest.TestCase):
    def test_returns_basename(self):
        name = guard.own_process_name()
        self.assertTrue(name)
        self.assertNotIn(os.sep, name)

    def test_frozen_uses_real_exe_not_bundled_python(self):
        # 打包版：argv[0] 才是真正的程式 exe，sys.executable 是內建 python.exe
        with mock.patch.object(runtime_env, "is_frozen", return_value=True), \
             mock.patch.object(runtime_env.sys, "argv",
                               [r"D:\app\MultiSocksDownloader.exe"]), \
             mock.patch.object(runtime_env.sys, "executable", r"D:\app\python.exe"):
            self.assertEqual(guard.own_process_name(), "MultiSocksDownloader.exe")


if __name__ == "__main__":
    unittest.main()
