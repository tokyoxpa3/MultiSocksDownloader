import os
import sys
import time
import unittest
import hashlib
import tempfile
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updater
import runtime_env


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
    """路徑推導已集中到 runtime_env（見 tests/test_runtime_env.py）。

    這裡只驗證 updater 的 .dist 推導跟著那份共用判斷走。
    """

    def test_prefers_argv0_exe(self):
        with mock.patch.object(runtime_env.sys, "argv",
                               ["C:\\app\\MultiSocksDownloader.exe"]):
            self.assertTrue(
                runtime_env.frozen_exe_path().endswith("MultiSocksDownloader.exe"))
            self.assertEqual(updater.current_dist_dir(), "C:\\app")

    def test_falls_back_to_executable(self):
        with mock.patch.object(runtime_env.sys, "argv",
                               ["C:\\app\\MultiSocksDownloader.py"]), \
             mock.patch.object(runtime_env.sys, "executable",
                               "C:\\Python\\python.exe"):
            self.assertTrue(runtime_env.frozen_exe_path().endswith("python.exe"))


class TestApplyScriptContent(unittest.TestCase):
    def test_ascii_only_and_contains_gotcha_fixes(self):
        script = updater.apply_script_content()
        # 全 ASCII：避免 PS 5.1 讀無 BOM UTF-8 亂碼
        script.encode("ascii")
        # 坑 3：用 .NET ProcessStartInfo + UseShellExecute=$false
        self.assertIn("UseShellExecute = $false", script)
        self.assertIn("System.Diagnostics.ProcessStartInfo", script)
        # 坑 6：不得使用 Start-Job（會卡住管道）
        self.assertNotIn("Start-Job", script)
        # 坑 8：等正確程序名退出（參數走 param，腳本內以 $ExeName 取代）
        self.assertIn("Get-Process -Name", script)
        self.assertIn("param(", script)


def _probe_info(total, speed=1.0e6, ttfb=0.5, url="http://x/f.bin"):
    return {"final_url": url, "total": total, "supports_range": True,
            "speed": speed, "ttfb": ttfb, "bytes": 1024, "elapsed": 1.0,
            "error": None}


class TestConfigFlag(unittest.TestCase):
    def _write(self, d, text):
        cfg = os.path.join(d, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(text)
        return cfg

    def test_reads_bool_and_string_forms(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._write(d, '{"adaptive_concurrency": true}')
            self.assertTrue(updater._config_flag("adaptive_concurrency",
                                                 False, [cfg]))
            cfg = self._write(d, '{"adaptive_concurrency": "on"}')
            self.assertTrue(updater._config_flag("adaptive_concurrency",
                                                 False, [cfg]))
            cfg = self._write(d, '{"adaptive_concurrency": false}')
            self.assertFalse(updater._config_flag("adaptive_concurrency",
                                                  True, [cfg]))

    def test_broken_or_missing_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._write(d, "{ not json")
            self.assertFalse(updater._config_flag("adaptive_concurrency",
                                                  False, [cfg]))
            missing = [os.path.join(d, "none.json")]
            self.assertTrue(updater._config_flag("x", True, missing))


class TestAdaptiveThreads(unittest.TestCase):
    URL = "http://x/f.bin"
    TOTAL = 64 * 1024 * 1024

    def test_no_baseline_keeps_base(self):
        n, why = updater._adaptive_threads(self.URL, self.TOTAL, None, 8, {})
        self.assertEqual(n, 8, why)

    def test_small_file_skips_probe(self):
        total = updater.ADAPTIVE_MIN_BYTES - 1
        with mock.patch.object(updater, "_measure_aggregate_throughput",
                               side_effect=AssertionError("小檔案不該試探")):
            n, _why = updater._adaptive_threads(
                self.URL, total, None, 8, _probe_info(total))
        self.assertEqual(n, 8)

    def test_short_estimate_skips_probe(self):
        info = _probe_info(self.TOTAL, speed=100e6, ttfb=0.5)
        with mock.patch.object(updater, "_measure_aggregate_throughput",
                               side_effect=AssertionError("估計太短不該試探")):
            n, _why = updater._adaptive_threads(
                self.URL, self.TOTAL, None, 8, info)
        self.assertEqual(n, 8)

    def test_no_gain_backs_off_to_single(self):
        with mock.patch.object(updater, "_measure_aggregate_throughput",
                               return_value=(1.02e6, 0.5, None)):
            n, why = updater._adaptive_threads(
                self.URL, self.TOTAL, None, 8, _probe_info(self.TOTAL))
        self.assertEqual(n, 1)
        self.assertIn("退回單連線", why)

    def test_high_gain_adopts_probe_threads(self):
        with mock.patch.object(updater, "_measure_aggregate_throughput",
                               return_value=(2.0e6, 0.5, None)):
            n, why = updater._adaptive_threads(
                self.URL, self.TOTAL, None, 8, _probe_info(self.TOTAL))
        self.assertEqual(n, updater.ADAPTIVE_PROBE_THREADS, why)

    def test_latency_rise_vetoes_adoption(self):
        info = _probe_info(self.TOTAL, speed=1.0e6, ttfb=0.5)
        lat = updater.ADAPTIVE_LATENCY_TOLERANCE * 1.2
        with mock.patch.object(updater, "_measure_aggregate_throughput",
                               return_value=(2.0e6, info["ttfb"] * lat, None)):
            n, why = updater._adaptive_threads(
                self.URL, self.TOTAL, None, 8, info)
        self.assertEqual(n, 1)
        self.assertIn("退回單連線", why)

    def test_probe_failure_keeps_base(self):
        with mock.patch.object(updater, "_measure_aggregate_throughput",
                               return_value=(None, None, "boom")):
            n, why = updater._adaptive_threads(
                self.URL, self.TOTAL, None, 8, _probe_info(self.TOTAL))
        self.assertEqual(n, 8)
        self.assertIn("boom", why)


class TestAggregateThroughputGuards(unittest.TestCase):
    def test_zero_total(self):
        bw, _ttfb, _why = updater._measure_aggregate_throughput(
            "http://x/f", 0, None, 4)
        self.assertIsNone(bw)

    def test_slice_too_small(self):
        bw, _ttfb, why = updater._measure_aggregate_throughput(
            "http://x/f", 4 * 1024 * 1024, None, 64)
        self.assertIsNone(bw)
        self.assertIn("太小", why)


class TestMultipartBlockCap(unittest.TestCase):
    def _run(self, total, **kw):
        seen = []

        def fake_fetch(session, url, start, end, dest, block_bytes, idx, lock,
                       stop, cancel_event):
            with lock:
                seen.append((start, end))
                block_bytes[idx] += end - start + 1

        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "o.bin")
            with mock.patch.object(updater, "_fetch_block", fake_fetch):
                updater._download_multipart("http://x/f.bin", dest, total,
                                            None, None, **kw)
            size = os.path.getsize(dest)
        return seen, size

    def test_max_blocks_caps_segment_count(self):
        total = 64 * 1024 * 1024
        seen, size = self._run(total, max_blocks=2)
        self.assertEqual(len(seen), 2)
        self.assertEqual(size, total)
        # 兩段必須無縫、不重疊地覆蓋整個檔
        self.assertEqual(seen[0][0], 0)
        self.assertEqual(seen[-1][1], total - 1)

    def test_default_cap_is_unchanged(self):
        total = 64 * 1024 * 1024
        seen, size = self._run(total)
        self.assertEqual(len(seen), updater.MAX_BLOCKS)
        self.assertEqual(size, total)


class TestRankDownloadPaths(unittest.TestCase):
    """D5（失敗靜默）與 D6（標籤說謊）的回歸測試。

    這個函式過去**完全沒有測試**，正是 D5 得以長期隱形的原因：所有失敗
    都被默默折成「少一個候選」，沒有任何斷言會注意到。
    """
    URL = "http://x/f.bin"

    def _run(self, probes, log_cb=None, proxies=None):
        """跑 _rank_download_paths，以假的探針取代真實網路。

        probes: {proxy_url: (speed|None, info)}；None 這條代表系統路由候選。
        """
        if proxies is None:
            proxies = [{"name": "P1", "url": "socks5h://a:1"},
                       {"name": "P2", "url": "socks5h://b:2"}]

        def fake_measure(url, proxy_url, **kw):
            return probes[proxy_url]

        with mock.patch.object(updater, "load_proxy_urls",
                               return_value=proxies), \
             mock.patch.object(updater, "_measure_path_detailed",
                               side_effect=fake_measure):
            return updater._rank_download_paths(self.URL, log_cb=log_cb)

    def test_direct_label_is_not_called_zhi_lian(self):
        """D6：保底路徑的標籤不能是「直連」（它可能被引擎攔走）。"""
        ranked = self._run({
            "socks5h://a:1": (None, {"error": "x", "ttfb": None}),
            "socks5h://b:2": (None, {"error": "y", "ttfb": None}),
            None: (0.0, _probe_info(100, speed=0.0)),
        })
        _u, _s, label, _i = ranked[-1]
        self.assertEqual(label, updater.DIRECT_LABEL)
        self.assertEqual(updater.DIRECT_LABEL, "系統路由（未指定代理）")
        self.assertNotEqual(label, "直連")

    def test_no_proxy_uses_direct_label(self):
        """D6：沒有設定代理時也走 DIRECT_LABEL，不留舊字面值。"""
        with mock.patch.object(updater, "load_proxy_urls", return_value=[]):
            ranked = updater._rank_download_paths(self.URL)
        self.assertEqual(ranked, [(None, 0.0, updater.DIRECT_LABEL, {})])

    def test_failed_path_is_reported_with_reason(self):
        """D5：量測失敗必須發出訊息，並帶上原因與 TTFB。"""
        logs = []
        self._run({
            "socks5h://a:1": (None, {"error": "HTTP 503", "ttfb": 0.42}),
            "socks5h://b:2": (2.0e6, _probe_info(100, speed=2.0e6)),
            None: (1.0e6, _probe_info(100, speed=1.0e6)),
        }, log_cb=logs.append)

        self.assertEqual(len(logs), 1, logs)
        self.assertIn("P1", logs[0])
        self.assertIn("HTTP 503", logs[0])
        self.assertIn("420 ms", logs[0])

    def test_timeout_is_reported_not_silently_zero(self):
        """D5：逾時沒跑完的候選不能靜默地以 0 分墊底。

        這裡讓「系統路由」那條永遠不回來（模擬逾時），它既不在 results
        也不在 failures，必須由補漏邏輯補上「量測未在 N 秒內完成」。
        """
        logs = []

        def fake_measure(url, proxy_url, **kw):
            if proxy_url is None:
                time.sleep(updater.PROXY_PROBE_TOTAL_TIMEOUT + 5)
                return (0.0, _probe_info(100, speed=0.0))
            return (3.0e6, _probe_info(100, speed=3.0e6))

        with mock.patch.object(updater, "load_proxy_urls",
                               return_value=[{"name": "P1",
                                              "url": "socks5h://a:1"}]), \
             mock.patch.object(updater, "_measure_path_detailed",
                               side_effect=fake_measure), \
             mock.patch.object(updater, "PROXY_PROBE_TOTAL_TIMEOUT", 0.2):
            ranked = updater._rank_download_paths(self.URL,
                                                  log_cb=logs.append)

        self.assertTrue(any("量測未在" in m for m in logs), logs)
        # 逾時那條仍要保底存在，行為與舊版一致
        self.assertTrue(any(u is None for u, _s, _n, _i in ranked))

    def test_log_cb_absence_is_safe(self):
        """D5：沒有 log_cb 時不得拋錯（向後相容）。"""
        ranked = self._run({
            "socks5h://a:1": (None, {"error": "boom", "ttfb": None}),
            "socks5h://b:2": (None, {"error": "boom", "ttfb": None}),
            None: (1.0e6, _probe_info(100, speed=1.0e6)),
        })
        self.assertEqual(ranked, [(None, 1.0e6, updater.DIRECT_LABEL,
                                   _probe_info(100, speed=1.0e6))])

    def test_log_cb_exception_does_not_break_ranking(self):
        """D5：記錄本身失敗不應影響選路結果。"""
        def bad_log(_msg):
            raise RuntimeError("logging is broken")

        ranked = self._run({
            "socks5h://a:1": (None, {"error": "boom", "ttfb": None}),
            "socks5h://b:2": (2.0e6, _probe_info(100, speed=2.0e6)),
            None: (1.0e6, _probe_info(100, speed=1.0e6)),
        }, log_cb=bad_log)
        self.assertEqual(ranked[0][1], 2.0e6)

    def test_sorted_fastest_first_and_direct_kept_last(self):
        """排序語意不變：快到慢，且未指定代理那條保底在最後。"""
        ranked = self._run({
            "socks5h://a:1": (5.0e6, _probe_info(100, speed=5.0e6)),
            "socks5h://b:2": (2.0e6, _probe_info(100, speed=2.0e6)),
            None: (1.0e6, _probe_info(100, speed=1.0e6)),
        })
        self.assertEqual([r[1] for r in ranked], [5.0e6, 2.0e6, 1.0e6])
        self.assertIsNone(ranked[-1][0])

    def test_all_proxies_fail_still_returns_direct_fallback(self):
        """代理全掛時只剩保底那條，且理由是「沒有代理」而非「有代理」。"""
        ranked = self._run({
            "socks5h://a:1": (None, {"error": "x", "ttfb": None}),
            "socks5h://b:2": (None, {"error": "y", "ttfb": None}),
            None: (0.0, _probe_info(100, speed=0.0)),
        })
        self.assertEqual(len(ranked), 1)
        self.assertIsNone(ranked[0][0])
        self.assertEqual(ranked[0][2], updater.DIRECT_LABEL)


if __name__ == "__main__":
    unittest.main()
