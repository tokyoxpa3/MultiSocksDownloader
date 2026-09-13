#!/usr/bin/env python3
"""無 Qt GUI 的複現工具：跑通 add_task → start_task 全鏈路，dump 每個階段的狀態與錯誤。

用途：快速定位「某個 URL 為什麼下不了」，不必啟動整包 GUI。每個階段（簿記、
解析、探測、啟動）的狀態與 error_reason 都直接印到終端，且內部細節的 verbose
輸出統一由 --debug 開關（走 logging，不靠散落的 print）。

用法：
  python repro.py <url> [--resolve] [--fake-resolve-fail] [--save-dir DIR]
                         [--no-proxy] [--poll SEC] [--debug]

範例：
  # 複現「解析失敗」——注入假解析器，不需真實網路，直接看 error/reason，且不產生 HTML 檔
  python repro.py https://youtube.com/watch?v=badid --resolve --fake-resolve-fail

  # 真實解析一個不存在的 YouTube 影片，看 yt-dlp 的結構化失敗原因
  python repro.py "https://www.youtube.com/watch?v=AAAAAAAAAAA" --resolve
"""

import argparse
import os
import sys
import tempfile
import time

from logging_setup import setup_logging
import logging

from downloader import DownloadManager
from stream_resolver import StreamResolver, StreamResolveResult

logger = logging.getLogger('repro')

_POLL_DEFAULT = 8.0   # 下載中的任務最多輪詢幾秒，讓「已啟動、正在跑」可見


def _banner(text):
    print("\n" + "=" * 72)
    print(f"  {text}")
    print("=" * 72)


def _print_task(stage, task):
    """把一個任務的核心狀態印出來，重點是 error_reason / error_message。"""
    if task is None:
        print(f"[{stage}] task = None")
        return
    print(f"[{stage}]")
    print(f"    task_id       = {task.task_id}")
    print(f"    url           = {task.url}")
    print(f"    filename      = {task.filename}")
    print(f"    status        = {task.status}")
    print(f"    error_reason  = {getattr(task, 'error_reason', '') or '(空)'}")
    print(f"    error_message = {task.error_message or '(空)'}")
    print(f"    total_size    = {task.total_size}")
    print(f"    block_count   = {task.block_count}")
    print(f"    supports_range= {task.supports_range}")
    print(f"    single_mode   = {task._single_mode}")


def _make_fail_resolver(reason, error):
    """注入一個永遠失敗的 StreamResolver，用於離線複現「解析失敗」路徑。"""

    class _FailingResolver(StreamResolver):
        def resolve(self, url, headers=None, max_height=None, max_fps=None,
                    audio_only=False):
            return StreamResolveResult(ok=False, error_kind=reason, error=error)

    return _FailingResolver()


def main(argv=None):
    ap = argparse.ArgumentParser(description="無 GUI 複現 add_task→start_task 全鏈路")
    ap.add_argument("url", help="要下載/解析的網址")
    ap.add_argument("--resolve", action="store_true",
                    help="要求先解析串流再下載（resolve_stream=True）")
    ap.add_argument("--fake-resolve-fail", action="store_true",
                    help="注入假解析器強制解析失敗（離線複現，不需真實網路）")
    ap.add_argument("--save-dir", default=None, help="儲存目錄（預設用暫存目錄）")
    ap.add_argument("--no-proxy", action="store_true", help="只走直連，不使用 SOCKS5 代理")
    ap.add_argument("--poll", type=float, default=_POLL_DEFAULT,
                    help="下載中任務的輪詢秒數（預設 8 秒）")
    ap.add_argument("--debug", action="store_true", help="開啟 verbose（DEBUG）輸出")
    args = ap.parse_args(argv)

    setup_logging(args.debug)

    _banner("建立 DownloadManager（隔離 config，避免污染真實設定）")
    # 隔離 config：DownloadManager.__init__ 只「讀」真實 config，之後所有
    # save_config 都改寫到暫時檔案，不污染 ~/.multi_socks_downloader/config.json。
    dm = DownloadManager()
    tmp_cfg_dir = tempfile.mkdtemp(prefix="msd_repro_cfg_")
    dm.config_dir = tmp_cfg_dir
    dm.config_file = os.path.join(tmp_cfg_dir, "config.json")
    dm.shutdown_dht()   # headless 下不需要常駐 DHT，關閉避免背景線程/網路干擾
    print(f"  config_file -> {dm.config_file}")
    print(f"  save_dir     -> {dm.save_dir}")
    print(f"  proxies      -> {len(dm.get_available_proxies())} 條可用")

    save_dir = args.save_dir or tempfile.mkdtemp(prefix="msd_repro_save_")
    os.makedirs(save_dir, exist_ok=True)

    resolver = None
    if args.fake_resolve_fail:
        resolver = _make_fail_resolver("fake_resolve_failure", "注入的假解析器：強制解析失敗")
        print("  stream_resolver -> 注入 fake（永遠失敗）")

    _banner("階段 1：add_task（簿記，不碰網路）")
    try:
        task_id = dm.add_task(
            args.url,
            save_dir=save_dir,
            use_proxy=not args.no_proxy,
            resolve_stream=args.resolve,
            stream_resolver=resolver,
        )
    except Exception:
        logger.exception("add_task 拋出例外")
        print("  add_task 失敗（例外）")
        return 1
    task = dm.task_ids.get(task_id)
    print(f"  回傳 task_id = {task_id}")
    _print_task("add_task 後", task)

    _banner("階段 2：start_task（同步跑 prepare → resolve → probe）")
    try:
        ok = dm.start_task(task_id)
    except Exception:
        logger.exception("start_task 拋出例外")
        print("  start_task 失敗（例外）")
        return 1
    task = dm.task_ids.get(task_id)
    print(f"  start_task 回傳值 = {ok}")
    _print_task("start_task 後", task)

    if task is not None and task.status == 'downloading':
        _banner(f"階段 3：下載中，輪詢最多 {args.poll} 秒")
        deadline = time.time() + args.poll
        while time.time() < deadline:
            time.sleep(0.5)
            p = task.get_progress()
            print(f"  [輪詢] status={task.status} "
                  f"{p['downloaded_size']}/{p['total_size']} "
                  f"({p['percentage']:.1f}%) speed={p['speed']:.0f} B/s")
            if task.status != 'downloading':
                break
        _print_task("輪詢結束", task)

        # 清理：取消仍在跑的任務，避免留下 .downloading/.progress 暫存檔
        if task.status == 'downloading':
            dm.cancel_task(task_id)
            print("  已取消仍在跑的任務，清理暫存檔")

    # 若解析失敗，明確輸出結論，強調沒有把網頁 HTML 當檔案下載
    if task is not None and task.status == 'error' and args.resolve:
        _banner("結論")
        print("  解析失敗 → 任務以 error 中止，未產生任何下載檔（不會存成 HTML）。")
        print(f"    reason = {getattr(task, 'error_reason', '') or '(空)'}")
        print(f"    error  = {task.error_message or '(空)'}")

    dm.shutdown_dht()
    _banner("完成")
    print(f"  最終 status = {task.status if task else '?'}")
    return 0 if (task is not None and task.status in ('completed', 'downloading')) else 1


if __name__ == "__main__":
    sys.exit(main())
