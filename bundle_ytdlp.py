#!/usr/bin/env python3
"""把 yt-dlp 以「未編譯的純 Python 套件」形式打包到 dist 目錄。

為什麼不讓 Nuitka 編譯它
------------------------
yt-dlp 有 1045 個模組，佔整個 Nuitka 建置的 92%（其中 971 個是 extractor）。
編譯它們既慢、又幾乎沒有收益：

- yt-dlp 的程式碼幾乎不需要我們改動，卻讓每次重新打包都要多跑 1045 次
  Python→C 產生與編譯。
- extractor 因為站台改版而需要頻繁更新；編進 exe 就變成「更新 yt-dlp 得重打包
  整個程式」。

改成隨程式附帶一個外部 `yt_dlp/` 資料夾後：

- Nuitka 只需編譯 MSD 自己的 91 個模組，建置時間大幅縮短。
- 更新 yt-dlp 只要換掉這個資料夾，不必重新打包 exe。
- 順帶解掉 `YTDLP_NO_LAZY_EXTRACTORS=1` 這個只為繞開 MSVC C1002 而存在的
  權宜設定（見 stream_resolver.py 的說明）。

輸出
----
預設輸出「無原始碼」的 .pyc（sourceless）版本：體積約為原始碼的 1/3，
匯入也更快。加 `--keep-source` 則保留 .py。

用法
----
    python bundle_ytdlp.py                      # 打包到 MultiSocksDownloader.dist
    python bundle_ytdlp.py --target DIR         # 指定輸出目錄
    python bundle_ytdlp.py --keep-source        # 保留 .py 原始碼
"""

import argparse
import compileall
import os
import shutil
import sys

# CI（GitHub Actions）的 stdout 可能是 cp1252，print 中文會拋 UnicodeEncodeError
# 讓整個步驟以非 0 結束；強制以 UTF-8 輸出，讓訊息在 CI 與本地都能正常列印。
# 同樣的處理見 fetch_ffmpeg.py（該檔在 9a087cc 已修過同一個問題）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

DEFAULT_DIST = "MultiSocksDownloader.dist"
PACKAGE_NAME = "yt_dlp"


def locate_package():
    """回傳已安裝的 yt_dlp 套件目錄（需在已安裝 yt-dlp 的環境執行）。"""
    try:
        import yt_dlp
    except ImportError:
        raise SystemExit("找不到 yt_dlp，請先 pip install -r requirements.txt")
    return os.path.dirname(os.path.abspath(yt_dlp.__file__))


def copy_package(src, target_dir):
    """複製套件到 target_dir/yt_dlp；略過快取檔，確保是乾淨的來源。"""
    dst = os.path.join(target_dir, PACKAGE_NAME)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    return dst


def byte_compile(pkg_dir):
    """就地編譯成 legacy 佈局的 .pyc（與 .py 同層，非 __pycache__）。

    legacy 佈局是重點：只有這樣才能在刪掉 .py 之後仍以「無原始碼」形式匯入。
    """
    ok = compileall.compile_dir(pkg_dir, quiet=1, force=True, legacy=True)
    if not ok:
        raise SystemExit("byte-compile 失敗，未產生完整的 .pyc")
    return sum(
        1 for _root, _dirs, files in os.walk(pkg_dir)
        for f in files if f.endswith('.pyc'))


def strip_sources(pkg_dir):
    """刪除 .py，只留 .pyc（Python 會直接匯入同層的 .pyc）。"""
    removed = 0
    for root, _dirs, files in os.walk(pkg_dir):
        for name in files:
            if name.endswith('.py'):
                os.remove(os.path.join(root, name))
                removed += 1
    return removed


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f"{n:.1f} {unit}"
        n /= 1024


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把 yt-dlp 以純 Python 形式打包到 dist（不經 Nuitka 編譯）")
    ap.add_argument("--target", default=None,
                    help=f"輸出目錄（預設：本腳本旁的 {DEFAULT_DIST}）")
    ap.add_argument("--keep-source", action="store_true",
                    help="保留 .py 原始碼（預設只留 .pyc，體積較小、匯入較快）")
    args = ap.parse_args(argv)

    root = os.path.dirname(os.path.abspath(__file__))
    target = args.target or os.path.join(root, DEFAULT_DIST)
    if not os.path.isdir(target):
        raise SystemExit(
            f"找不到輸出目錄：{target}\n"
            f"請先執行 Nuitka 打包，或改用 --target 指定。")

    src = locate_package()
    print(f"來源套件：{src}")
    print(f"輸出目錄：{target}")

    pkg = copy_package(src, target)
    count = byte_compile(pkg)
    print(f"已編譯 {count} 個 .pyc")

    if not args.keep_source:
        removed = strip_sources(pkg)
        print(f"已移除 {removed} 個 .py（無原始碼模式）")
    else:
        print("保留 .py 原始碼")

    print(f"完成：{pkg}（{human(dir_size(pkg))}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
