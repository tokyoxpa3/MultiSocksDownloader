# BT 多線路聚合下載 — 設計文件

> 狀態：研究階段（尚未實作）
> 對應現況：`bt_downloader.py` 目前為單一 session 單一線路，尚未聚合多線路頻寬。

## 目標

讓單一 BT 任務（公開種子）能同時透過「直連 + 多個 SOCKS5 代理」下載，把不同
piece 分散到不同線路，達成多線路頻寬聚合。

## 現況

- `BTTask` 持有單一 `LineSession`（`bt_downloader.py:214`），一個 `lt.session` 綁定一條線路。
- `proxies` 列表只取第一筆（`bt_downloader.py:186-191`）。
- `partition_ranges`（`bt_downloader.py:91`）為死碼，僅被測試呼叫。
- 進度回報寫死 `thread_count: 1`、單一 `line_bytes`（`bt_downloader.py:638-641`、`656`）。

## 核心機制（libtorrent 2.1.1 已驗證支援）

### 1. 每條線一個 session，且強制流量走代理

每個 `lt.session` 可獨立設代理：

- `proxy_type` / `proxy_hostname` / `proxy_port` / `proxy_username` / `proxy_password`
- `force_proxy`：強制所有流量（含 tracker、peer 連線）走代理
- `proxy_peer_connections`：透過代理建立 peer 連線（SOCKS5 才能轉送 incoming）

目前 `LineSession` 只設了前五項，未開 `force_proxy` / `proxy_peer_connections`，
proxied session 可能繞過代理直連、洩漏流量，需一併補上。

### 2. piece 優先權做分片派工

- `torrent_handle.piece_priority(index, prio)`：設定單一 piece 優先權
- `torrent_handle.prioritize_pieces(list[int])`：一次設定全部 piece（另有 `list[tuple[int,int]]` 範圍版）
- `add_torrent_params.piece_priorities`：加入 torrent 當下就指定（最乾淨）

`priority = 0` 表示不下載該 piece。把 piece 空間切成不重疊區段，每個 session
只讓自己區段的 priority > 0，即可達成「各線路只下自己的 piece」。

### 3. 稀疏儲存讓多 session 寫同一份檔案

`storage_mode_sparse`：未下載的 piece 是檔案空洞。piece 有固定位元組偏移，
不同 session 寫不重疊的 piece 不會互相覆蓋內容。

## 目標架構

- `BTTask` 由單一 `self._line` 改為 `self._lines: list[LineSession]`。
- 線路組合：直連（1 條）+ 每個可用 SOCKS5（N 條）。
- 所有 session 加同一個 torrent、同一個 `save_path`（sparse）。
- 每個 session 用 `piece_priorities` 只啟用自己的區段。
- 速度 = Σ 各 session `download_rate`；piece bitmap = OR；`line_bytes` 每線分開。

## 關鍵風險（依優先序）

### R1：Windows 上多 session 併寫同一檔案可能撞檔案鎖 —— ✅ 已驗證可行（2026-09-01）

libtorrent 每個 session 有自己的 disk I/O thread 各自開檔。Windows 的共用鎖
旗標下，第二個 session 開同一個檔案寫入可能失敗（`file_error_alert`）。

**POC 結果（`tests/poc_bt_multisession.py`）**：兩個 session 共用同一個
`save_path`、`storage_mode_sparse`、piece 不重疊分片，在 Windows 上成功併寫同一
檔案，全程無 `file_error_alert`，合併後內容逐位元組一致（exit 0）。

**結論**：採用「共用存檔 + piece 分片」方案，無需各自存檔再合併。

### R2：靜態分片負載不均

快線下完自己的區段就閒置。需要動態重新派工：定期把慢線停滯的 piece 讓給快線
（把對應 `piece_priority` 從 0 拉回 1）。

### R3：PT（private）種子不得多線

private=1 多 IP 下載會被 tracker ban。magnet 需等 metadata 才知道是否 private，
所以要先單線取 metadata，確認公開才扇出多線、private 維持單線。

## 分階段實作

### MVP（先驗證可行性）

1. R1 POC：確認多 session 併寫同一稀疏檔是否可行。
2. `BTTask` 支援 `_lines` 多 session、共用 `save_path`。
3. 靜態 piece 分片（復活 `partition_ranges` + `piece_priorities`）。
4. 公開種子才多線、private 強制單線。
5. 進度聚合、`get_progress` 回報每線資料。

### 進階（已實作，2026-09-01）

6. 動態重新派工：每 3 秒檢查，閒置線路接手最忙碌線路的一半剩餘 piece（piece 所有權追蹤 + 工作竊取）。
7. resume 合併：多線路定期把合併的 piece 完成位元圖存為 `pieces.json`，重啟時以 `have_pieces` / `verified_pieces` 標記已完成 piece、只重派剩餘 piece。（目前僅 `.torrent` 檔；magnet 仍在 metadata 後重新分片。）

## 未決問題

- [x] R1 的結論（共用檔 vs 各自存檔＋合併）——POC 驗證共用檔可行（2026-09-01）。
- [x] 動態重新派工的觸發頻率與搬移上限——每 3 秒、竊取一半剩餘 piece（2026-09-01）。
- [x] resume 在多 session 下的正確合併策略——合併位元圖 + `have_pieces`（2026-09-01）。
- [ ] magnet 多線路 resume（metadata 階段如何接續）——尚未實作。
