#!/usr/bin/env python3
"""下載並解出 LGPL 版的 ffmpeg（essentials build），供打包時放進 .dist。

只在建置／發佈階段使用，不進執行檔本身。輸出到專案根目錄的 ffmpeg/ 資料夾：
  ffmpeg/ffmpeg.exe     （yt-dlp 合併視訊+音訊時呼叫）
  ffmpeg/LICENSE.txt    （LGPL 授權文字；重新散布 ffmpeg 二進位檔依法必須附上）

用法：
  python fetch_ffmpeg.py [--target DIR]

已存在有效 ffmpeg.exe 時直接略過（冪等），不會重複下載。

來源選 gyan.dev 的 "essentials" build：只含 LGPL/GPLv3 元件、不含 x264 等
GPL 編碼器，授權較適合隨程式重新散布；完整版（full）為 GPL，此處不用。
"""

import argparse
import os
import shutil
import sys
import tempfile
import zipfile

import requests

# CI（GitHub Actions）的 stdout 可能是 cp1252，print 中文會拋 UnicodeEncodeError；
# 強制以 UTF-8 輸出，讓訊息在 CI 與本地都能正常列印。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def _download(url, dest, timeout=600):
    """串流下載到 dest，避免整包塞進記憶體（ffmpeg zip 約數十 MB）。"""
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)


def _extract_to(zf, member, dest_path):
    """把 zip 內單一 member 的內容寫到 dest_path（路徑由呼叫端指定，非取自 member）。"""
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".", exist_ok=True)
    with zf.open(member) as src, open(dest_path, "wb") as dst:
        shutil.copyfileobj(src, dst)


def _find_member(names, suffix):
    """找出名稱結尾符合 suffix 的第一個 member。

    essentials zip 內檔案都在版本化資料夾下（如 ffmpeg-7.1-essentials_build/…），
    故用「結尾」比對而非整段路徑。
    """
    s = suffix.lower()
    for n in names:
        if n.lower().endswith(s):
            return n
    return None


def fetch(target_dir):
    """下載並解出 ffmpeg.exe 與 LICENSE 到 target_dir；已存在則略過。"""
    exe_path = os.path.join(target_dir, "ffmpeg.exe")
    lic_path = os.path.join(target_dir, "LICENSE.txt")

    if os.path.isfile(exe_path) and os.path.getsize(exe_path) > 0:
        print("ffmpeg 已存在：{}".format(exe_path))
        return True

    os.makedirs(target_dir, exist_ok=True)
    fd, zip_path = tempfile.mkstemp(suffix=".zip", prefix="ffmpeg_dl_")
    os.close(fd)
    print("下載 {}".format(FFMPEG_URL))
    try:
        _download(FFMPEG_URL, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            exe_member = _find_member(names, "/bin/ffmpeg.exe")
            if not exe_member:
                raise RuntimeError("zip 內找不到 bin/ffmpeg.exe")
            _extract_to(zf, exe_member, exe_path)

            lic_member = _find_member(names, "/LICENSE")
            if lic_member:
                _extract_to(zf, lic_member, lic_path)
            else:
                print("警告：zip 內找不到 LICENSE，已略過（重新散布時請自行補上）")
    finally:
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass

    if not (os.path.isfile(exe_path) and os.path.getsize(exe_path) > 0):
        raise RuntimeError("解出 ffmpeg.exe 失敗")
    print("完成：{}".format(exe_path))
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="下載並解出 ffmpeg（LGPL essentials）")
    ap.add_argument("--target", default=None,
                    help="輸出目錄（預設：本腳本所在目錄的 ffmpeg/）")
    args = ap.parse_args(argv)
    target = args.target or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg")
    fetch(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
