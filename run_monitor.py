#!/usr/bin/env python3
"""CLI 監看：記錄每個 run 的切換時間軸 + 同步取樣速率，用於定位斷崖式掉速。

每個 worker 下載的「範圍」（run）會經歷：認領(claim) → 發起 GET(get_start)
→ 收到回應頭(get_done) → 首個 body chunk(first_byte) → 寫完(write_done)。
對 HuggingFace，get_start→get_done 主要就是 302 跳轉 + CDN TLS + 簽名驗證的
固定延遲。此工具把這些階段各自打上 monotonic 時間戳，並平行取樣整體速率，
最後統計：每 run 請求延遲、worker 空轉空窗、以及速率斷崖與同步切換的關聯。

用法：
  python run_monitor.py                     # 直連，6 線，最多 50 秒 / 2 GiB
  python run_monitor.py --use-proxy         # 加上 config.json 裡的可用 SOCKS5 代理
  python run_monitor.py --threads 12 --max-seconds 90
"""

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from downloader import DownloadTask, TARGET_RUN_BYTES, format_size

URL = ("https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/"
       "model_patches/minimax_h3_fun_controlnet_union_pruned_bf16.safetensors"
       "?download=true")


def load_available_proxies():
    cfg = os.path.join(os.path.expanduser("~"), ".multi_socks_downloader", "config.json")
    if not os.path.isfile(cfg):
        return []
    try:
        with open(cfg, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for p in (data.get("socks_proxies") or {}).values():
        st = p.get("status", "")
        if st.startswith("可用") or st.startswith("有限可用"):
            out.append({
                "host": p["host"], "port": int(p["port"]),
                "username": p.get("username") or "",
                "password": p.get("password") or "",
            })
    return out


class MonitoredTask(DownloadTask):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.events = []          # (t_mono, kind, wid, detail)
        self._ev_lock = threading.Lock()
        self._wid_map = {}

    def _wid(self):
        ident = threading.current_thread().ident
        if ident not in self._wid_map:
            self._wid_map[ident] = f"W{len(self._wid_map)}"
        return self._wid_map[ident]

    def _log(self, kind, detail=""):
        with self._ev_lock:
            self.events.append((time.monotonic(), kind, self._wid(), detail))

    def _request_run(self, start_idx, end_idx, session, stop, proxy=None):
        b0, _ = self._block_bounds(start_idx)
        _, b1e = self._block_bounds(end_idx)
        self._log("claim", f"bytes={b0}-{b1e - 1} size={b1e - b0} line={self._line_key(proxy)}")

        orig_get = session.get

        def timed_get(*a, **kw):
            t0 = time.monotonic()
            self._log("get_start")
            try:
                r = orig_get(*a, **kw)
            except Exception as e:
                self._log("get_err", str(e))
                raise
            dt = (time.monotonic() - t0) * 1000
            self._log("get_done", f"http={r.status_code} dt={dt:.0f}ms")
            return r

        session.get = timed_get
        try:
            return super()._request_run(start_idx, end_idx, session, stop, proxy)
        finally:
            session.get = orig_get

    def _write_run(self, start_idx, end_idx, req_start, end, r, stop, proxy=None):
        self._log("write_start", f"req={req_start}-{end}")
        t0 = time.monotonic()
        first = {"seen": False}
        orig_iter = r.iter_content

        def timed_iter(cs):
            for chunk in orig_iter(cs):
                if chunk and not first["seen"]:
                    first["seen"] = True
                    self._log("first_byte", f"dt={(time.monotonic() - t0) * 1000:.0f}ms")
                yield chunk

        r.iter_content = timed_iter
        result = super()._write_run(start_idx, end_idx, req_start, end, r, stop, proxy)
        self._log("write_done", f"result={result} dt={(time.monotonic() - t0) * 1000:.0f}ms")
        return result


def _percentile(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def analyze(task, samples):
    ev = list(task.events)
    t0 = ev[0][0] if ev else 0.0

    # 每 worker 的 get_start -> get_done 請求延遲
    req_lat = []
    stalls = []          # (get_start_t, get_done_t) 供重疊掃描
    open_get = {}
    for t, kind, wid, detail in ev:
        if kind == "get_start":
            open_get[wid] = t
        elif kind == "get_done":
            if wid in open_get:
                req_lat.append(t - open_get.pop(wid))
                stalls.append((open_get.get(wid, t), t))  # fallback: use t
    # 修正 stalls：直接用 get_start 重掃
    stalls = []
    get_s = {}
    for t, kind, wid, detail in ev:
        if kind == "get_start":
            get_s[wid] = t
        elif kind == "get_done" and wid in get_s:
            stalls.append((get_s[wid], t))
            del get_s[wid]

    # 同步停頓：掃描所有 get 區間，算同一時刻有多少 worker 正在等回應
    sweep = []
    for s, e in stalls:
        sweep.append((s, +1))
        sweep.append((e, -1))
    sweep.sort()
    cur = 0
    peak_stall = 0
    for _, d in sweep:
        cur += d
        peak_stall = max(peak_stall, cur)

    # 每 worker「run 寫完 → 下一次 get_done」的空轉（含跳轉延遲）
    gaps = []
    last_done = {}
    for t, kind, wid, detail in ev:
        if kind == "write_done":
            last_done[wid] = t
        elif kind == "get_done" and wid in last_done:
            gaps.append(t - last_done[wid])
            del last_done[wid]

    # run 大小（claim 事件）
    run_sizes = []
    import re
    for t, kind, wid, detail in ev:
        if kind == "claim":
            m = re.search(r"size=(\d+)", detail)
            if m:
                run_sizes.append(int(m.group(1)))

    # 速率序列
    speeds = []
    for i in range(1, len(samples)):
        dt = samples[i][0] - samples[i - 1][0]
        ds = samples[i][1] - samples[i - 1][1]
        if dt > 0:
            speeds.append(ds / dt)
    peak = max(speeds) if speeds else 0.0
    cliff = [s for s in speeds if s < peak * 0.30] if peak > 0 else []

    # 速度歸零偵測：連續取樣未有位元組前進的區段（>=150ms），並關聯當時的事件
    zero_events = []
    i = 0
    n = len(samples)
    while i < n - 1:
        if samples[i + 1][1] == samples[i][1]:
            zstart = samples[i][0]
            zdl = samples[i][1]
            zab = samples[i][2]
            j = i
            while j < n - 1 and samples[j + 1][1] == samples[j][1]:
                j += 1
            dur = samples[j][0] - zstart
            if dur >= 0.15:
                kinds = {}
                for (et, ek, _ew, _ed) in ev:
                    if zstart <= et <= samples[j][0]:
                        kinds[ek] = kinds.get(ek, 0) + 1
                zero_events.append((zstart, dur, zdl, zab, kinds))
            i = j + 1
        else:
            i += 1

    print("\n===== 分析結果 =====")
    print(f"run 數={len(run_sizes)}  取樣點={len(samples)}  速率樣本={len(speeds)}")
    print(f"run 大小 (bytes): min={format_size(min(run_sizes)) if run_sizes else 0} "
          f"median={format_size(_percentile(run_sizes, 50))} "
          f"max={format_size(max(run_sizes)) if run_sizes else 0}")
    print(f"目標 run 大小 ~= {format_size(TARGET_RUN_BYTES)} "
          f"(={TARGET_RUN_BYTES // task.block_size} 塊 x {format_size(task.block_size)})")

    print(f"\n請求延遲 get_start->get_done: n={len(req_lat)} "
          f"min={_percentile(req_lat, 0) * 1000:.0f}ms "
          f"median={_percentile(req_lat, 50) * 1000:.0f}ms "
          f"p90={_percentile(req_lat, 90) * 1000:.0f}ms "
          f"max={_percentile(req_lat, 100) * 1000:.0f}ms")
    print(f"worker 空轉(寫完→下次收到回應頭): n={len(gaps)} "
          f"median={_percentile(gaps, 50) * 1000:.0f}ms "
          f"p90={_percentile(gaps, 90) * 1000:.0f}ms "
          f"max={_percentile(gaps, 100) * 1000:.0f}ms")
    print(f"同時等待回應的 worker 峰值 = {peak_stall} / {len(task._workers) or 1}")

    print(f"\n整體速率: peak={format_size(peak)}/s "
          f"median={format_size(_percentile(speeds, 50))}/s")
    print(f"低於峰值30%的取樣(=斷崖)佔比: "
          f"{100.0 * len(cliff) / len(speeds) if speeds else 0:.1f}% ({len(cliff)}/{len(speeds)})")

    print(f"\n速度歸零事件: {len(zero_events)} 次")
    for zstart, dur, zdl, zab, kinds in zero_events[:25]:
        pct = (zdl / (task.total_size or 1)) * 100
        print(f"  @{pct:.1f}% 持續 {dur*1000:.0f}ms  active_blocks={zab}  事件={kinds}")

    # 時間軸(前 40 筆)
    print("\n===== 事件時間軸(前40筆, t 為相對首事件秒) =====")
    for t, kind, wid, detail in ev[:40]:
        print(f"  {t - t0:8.3f}s  {wid:>4}  {kind:<12} {detail}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="斷崖掉速 CLI 監看")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--use-proxy", action="store_true")
    ap.add_argument("--max-seconds", type=float, default=50)
    ap.add_argument("--max-bytes", type=int, default=2 * 1024 ** 3)
    ap.add_argument("--save-dir", default="D:/tmp")
    args = ap.parse_args(argv)

    proxies = load_available_proxies() if args.use_proxy else []
    task = MonitoredTask(URL, args.save_dir, proxies=proxies,
                         threads_per_proxy=args.threads, chunks_per_part=0)
    if not task.start():
        print("start failed:", task.error_message, file=sys.stderr)
        return 1

    print(f"total={format_size(task.total_size)} blocks={task.block_count} "
          f"block_size={format_size(task.block_size)} "
          f"workers={len(task._workers)} lines={len(task._build_lines())}"
          f" proxies={len(proxies)}")

    start = time.monotonic()
    samples = []  # (t, downloaded, n_active_blocks)
    last_print = start
    prev_dl = None
    zero_since = None
    while True:
        time.sleep(0.05)
        p = task.get_progress()
        dl = p["downloaded_size"]
        samples.append((time.monotonic(), dl, len(task._active_blocks)))
        # 即時歸零偵測：位元組在 50ms 樣本內未前進
        if prev_dl is not None:
            if dl == prev_dl:
                if zero_since is None:
                    zero_since = time.monotonic()
            else:
                if zero_since is not None:
                    dur = time.monotonic() - zero_since
                    if dur >= 0.15:
                        print(f"  [ZERO] 速度歸零 {dur*1000:.0f}ms "
                              f"@{p['percentage']:.1f}% "
                              f"active_blocks={len(task._active_blocks)}", flush=True)
                    zero_since = None
        prev_dl = dl
        if time.monotonic() - last_print >= 2.0:
            last_print = time.monotonic()
            print(f"  [live] {format_size(dl)} / {format_size(p['total_size'])} "
                  f"({p['percentage']:.1f}%) speed={format_size(p['speed'])}/s "
                  f"active_blocks={len(task._active_blocks)}", flush=True)
        if task.status in ("completed", "error", "canceled"):
            break
        if time.monotonic() - start >= args.max_seconds:
            break
        if dl >= args.max_bytes:
            break

    task.pause()

    # 下載少於一個 run 就不值得分析
    if len(task.events) < 2:
        print("事件太少，可能下載速度過慢或未真正開始。", file=sys.stderr)
    else:
        analyze(task, samples)

    # 清理半成品，避免留下數 GB 的 .downloading
    task.cancel()
    print("\n(已清理暫存/進度檔)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
