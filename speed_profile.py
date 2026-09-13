#!/usr/bin/env python3
"""量化下載速率波動：低於門檻的次數/持續、波動幅度統計，並與 run 切換事件對照。

與 run_monitor.py 的差別：run_monitor 只抓「歸零(停滯)」，本工具進一步量化
「低於門檻(例如 25 MB/s)」的掉速區段，並計算波動的統計特徵(標準差/變異係數/
百分位數/直方圖)，最後把掉速區段跟同時間發生的 run 切換(get_start/get_done/
first_byte/write_done)事件對照，判斷掉速是否源於「重新發 Range 請求」。

速度量測用兩條訊號避免誤判：
  * instantaneous：每 50ms 樣本的位元組增量，能抓微停頓，但雜訊大。
  * window：滑動視窗(預設 1s)平均，平滑後拿來算門檻與波動統計。
門檻判斷一律用 window 訊號；instantaneous 僅供對照。

用法：
  python speed_profile.py --use-proxy --threads 10 --max-seconds 140
  python speed_profile.py --use-proxy --threads 10 --threshold-mb 25 --out profile.csv
"""
import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_monitor import MonitoredTask, load_available_proxies, _percentile, URL

MB = 1024 * 1024


def _sliding_speeds(samples, window):
    """從 (t, bytes, active) 樣本計算滑動視窗速度。回傳平行於 samples 的序列。

    對每個樣本 i，找最早 j 使 samples[i].t - samples[j].t >= window，
    速度 = (bytes[i]-bytes[j]) / (t[i]-t[j])。視窗未滿時回 None。
    """
    t = [s[0] for s in samples]
    b = [s[1] for s in samples]
    out = [None] * len(samples)
    j = 0
    for i in range(len(samples)):
        while j < i and t[i] - t[j] > window:
            j += 1
        dt = t[i] - t[j]
        if dt >= window * 0.5:  # 視窗至少填到一半才可信
            out[i] = (b[i] - b[j]) / dt
    return out


def _dips(speeds, samples, threshold):
    """找出滑動速度連續低於門檻的區段(排除啟動暖機期)。"""
    dips = []
    i = 0
    n = len(speeds)
    warmup = samples[0][0] + 3.0  # 前 3 秒屬啟動(302+TLS+首 byte)，單獨統計
    while i < n:
        if speeds[i] is not None and speeds[i] < threshold and samples[i][0] >= warmup:
            start = i
            while i < n and speeds[i] is not None and speeds[i] < threshold:
                i += 1
            end = i - 1
            dur = samples[end][0] - samples[start][0]
            minv = min(speeds[start:end + 1])
            ab = [samples[k][2] for k in range(start, end + 1)]
            avg_ab = sum(ab) / len(ab) if ab else 0
            dips.append({
                't0': samples[start][0], 't1': samples[end][0],
                'dur': dur, 'min': minv, 'avg_active': avg_ab,
                'pct': samples[end][1] / (samples[-1][1] or 1) * 100,
                's0': start, 's1': end,
            })
            i = end + 1
        else:
            i += 1
    return dips


def _hist(speeds, nbin=10):
    vals = [s for s in speeds if s is not None]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return [(hi / MB, len(vals))]
    width = (hi - lo) / nbin
    hist = [0] * nbin
    for v in vals:
        idx = min(nbin - 1, int((v - lo) / width))
        hist[idx] += 1
    return [((lo + (i + 0.5) * width) / MB, c) for i, c in enumerate(hist)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="速率波動量化")
    ap.add_argument("--threads", type=int, default=10)
    ap.add_argument("--use-proxy", action="store_true")
    ap.add_argument("--max-seconds", type=float, default=140)
    ap.add_argument("--max-bytes", type=int, default=4 * 1024 ** 3)
    ap.add_argument("--threshold-mb", type=float, default=25.0,
                    help="低於此視為掉速 (MB/s)")
    ap.add_argument("--window", type=float, default=1.0, help="滑動視窗秒數")
    ap.add_argument("--save-dir", default="D:/tmp")
    ap.add_argument("--out", default=None, help="輸出原始時序 CSV 路徑")
    ap.add_argument("--json", default=None, help="輸出 JSON 摘要路徑")
    args = ap.parse_args(argv)

    threshold = args.threshold_mb * MB
    proxies = load_available_proxies() if args.use_proxy else []
    task = MonitoredTask(URL, args.save_dir, proxies=proxies,
                         threads_per_proxy=args.threads, chunks_per_part=0)
    if not task.start():
        print("start failed:", task.error_message, file=sys.stderr)
        return 1

    print(f"total={task.total_size/MB:.0f}MB workers={len(task._workers)} "
          f"lines={len(task._build_lines())} proxies={len(proxies)} "
          f"threshold={args.threshold_mb}MB/s window={args.window}s", flush=True)

    start = time.monotonic()
    samples = []  # (t_mono, downloaded, n_active_blocks)
    last_print = start
    while True:
        time.sleep(0.05)
        p = task.get_progress()
        samples.append((time.monotonic(), p["downloaded_size"],
                        len(task._active_blocks)))
        if time.monotonic() - last_print >= 2.0:
            last_print = time.monotonic()
            print(f"  [live] {p['downloaded_size']/MB:.0f}MB "
                  f"({p['percentage']:.1f}%) speed={p['speed']/MB:.1f}MB/s "
                  f"active={len(task._active_blocks)}", flush=True)
        if task.status in ("completed", "error", "canceled"):
            break
        if time.monotonic() - start >= args.max_seconds:
            break
        if p["downloaded_size"] >= args.max_bytes:
            break

    task.pause()

    # 速度序列
    speeds = _sliding_speeds(samples, args.window)
    vals = [s for s in speeds if s is not None]
    n_samp = len(vals)
    if n_samp < 10:
        print("速度樣本不足，延長 --max-seconds 再試。", file=sys.stderr)
        task.cancel()
        return 1

    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    std = var ** 0.5
    cv = std / mean if mean else 0.0

    dips = _dips(speeds, samples, threshold)

    # 掉速區段與 run 切換事件的關聯
    ev = list(task.events)
    ev_t0 = ev[0][0] if ev else 0.0
    for d in dips:
        kinds = {}
        for (et, ek, _ew, _ed) in ev:
            if d['t0'] <= et <= d['t1']:
                kinds[ek] = kinds.get(ek, 0) + 1
        d['events'] = kinds

    total_dip = sum(d['dur'] for d in dips)
    span = samples[-1][0] - samples[0][0]

    print("\n===== 波動量化結果 =====")
    print(f"取樣 {len(samples)} 點 / {span:.1f}s，有效速度樣本 {n_samp}")
    print(f"平均 {mean/MB:.1f} MB/s  中位數 {_percentile(vals,50)/MB:.1f} "
          f"峰值 {_percentile(vals,100)/MB:.1f}  谷值 {_percentile(vals,0)/MB:.1f}")
    print(f"標準差 {std/MB:.1f} MB/s  變異係數 CV={cv*100:.0f}%  "
          f"(CV 越大波動越劇烈；<20% 平穩、20~40% 中度、>40% 劇烈)")
    print(f"百分位: p10={_percentile(vals,10)/MB:.1f} "
          f"p25={_percentile(vals,25)/MB:.1f} p50={_percentile(vals,50)/MB:.1f} "
          f"p75={_percentile(vals,75)/MB:.1f} p90={_percentile(vals,90)/MB:.1f} "
          f"p99={_percentile(vals,99)/MB:.1f} MB/s")

    print(f"\n低於 {args.threshold_mb} MB/s 的掉速區段: {len(dips)} 次")
    print(f"累計掉速時間 {total_dip:.1f}s / 全程 {span:.1f}s "
          f"({100*total_dip/span:.1f}%)")
    for d in dips[:30]:
        print(f"  @{d['pct']:4.1f}%  持續 {d['dur']*1000:6.0f}ms  "
              f"谷值 {d['min']/MB:5.1f}MB/s  active~{d['avg_active']:.0f}  "
              f"事件={d['events']}")

    print("\n速度直方圖 (MB/s):")
    for v, c in _hist(speeds, 12):
        bar = "#" * int(c / max(1, max(x[1] for x in _hist(speeds, 12))) * 40)
        print(f"  {v:6.1f} | {bar} {c}")

    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t", "bytes", "active_blocks", "window_speed_mbs"])
            for i, s in enumerate(samples):
                sp = speeds[i] / MB if speeds[i] is not None else ""
                w.writerow([f"{s[0]-samples[0][0]:.3f}", s[1], s[2],
                            f"{sp:.1f}" if sp != "" else ""])
        print(f"\n時序已寫入 {args.out}")

    if args.json:
        summary = {
            "threshold_mb": args.threshold_mb,
            "window_s": args.window,
            "span_s": span,
            "mean_mbs": mean / MB,
            "median_mbs": _percentile(vals, 50) / MB,
            "peak_mbs": _percentile(vals, 100) / MB,
            "min_mbs": _percentile(vals, 0) / MB,
            "std_mbs": std / MB,
            "cv": cv,
            "p10": _percentile(vals, 10) / MB,
            "p25": _percentile(vals, 25) / MB,
            "p75": _percentile(vals, 75) / MB,
            "p90": _percentile(vals, 90) / MB,
            "p99": _percentile(vals, 99) / MB,
            "dip_count": len(dips),
            "dip_total_s": total_dip,
            "dip_ratio": total_dip / span if span else 0,
            "dips": [{k: v for k, v in d.items() if k not in ('s0', 's1')}
                     for d in dips],
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"摘要已寫入 {args.json}")

    task.cancel()
    return 0


if __name__ == "__main__":
    sys.exit(main())