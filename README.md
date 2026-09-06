# MultiSocksDownloader 多代理下載器

使用多個 SOCKS5 代理、多線程並行下載，支援斷點續傳的桌面下載工具。

> ## ⚠️ 搭配 NetRedirector（PPPoE 本地網路）必讀
>
> 若你使用 [**NetRedirector**](https://github.com/tokyoxpa3/NetRedirector) 做流量轉送、並以 **PPPoE 本地網路**運作，
> **務必把 `MultiSocksDownloader.exe` 加入 NetRedirector 的直連清單（Direct / Bypass）**。
>
> 否則本程式通往 SOCKS5 代理的連線會被 NetRedirector 再次轉送，
> 導致**多代理聚合失效、速度變慢，甚至完全無法連線**。

> ## ⚠️ 搭配 5G-Proxy-Pro（手機 5G SOCKS5）注意事項
>
> 使用 [5G-Proxy-Pro](https://github.com/tokyoxpa3/5G-Proxy-Pro) 這類「手機 5G 架 SOCKS5」的線路時，
> **同一條線路不要同時開太多 BT 任務**。
> 5G-Proxy-Pro 的 SOCKS5 握手是阻塞式、握手執行緒池固定 64 條，
> 每個 BT 任務在代理線路最多開 30 條連線；單線同時 2 個 BT 任務（約 60 條握手）已接近瓶頸，
> 3 個以上就會互相拖慢、甚至丟連線。
>
> 程式預設「每線同時 BT 任務上限 = 2」（設定 → 每線同時 BT 任務上限，可調整，0 = 不提醒）。
> 超過上限仍會繼續新增，但會跳出提醒。

## 為什麼 5G 行動網路也能下載 BT？

一般 5G 行動網路想下載 BT / 磁力其實非常困難，主因有二：

- **電信級 NAT（CGNAT）**：手機沒有公網 IPv4，外部 peer 無法主動連進來，只能靠「主動向外連線」找 peer。
- **UDP / DHT 被封**：5G 與 SOCKS5 轉接環境下，DHT、UDP tracker、uTP 這些 BT 找 peer 的主要管道常被封或不轉發。

本程式用以下方法繞過這些限制，讓 5G 也能跑 BT：

- **SOCKS5 主動對外連線**：BT 流量經 SOCKS5 代理（如 5G-Proxy-Pro 的手機 5G）主動連向 peer，繞開 CGNAT「無法被連入」的困境。
- **僅用 TCP**：SOCKS5 不轉發 UDP，代理線路強制走 TCP、停用 uTP/UDP，避免無效連線浪費資源。
- **HTTP tracker 後備**：DHT/UDP tracker 被封時，改走 TCP 的 HTTP/HTTPS tracker 取得 metadata 與 peer。
- **偽裝 qBittorrent 4.6.0**：避免自製客戶端被 tracker 白名單拒收。
- **多線路聚合**：單一檔案切成不重疊分段，同時經多條直連/SOCKS5 線路分段下載。

也正因如此，本程式**必須自己掌控這些 SOCKS5 連線**。若搭配 NetRedirector 做流量轉送，務必把 `MultiSocksDownloader.exe` 加入直連清單——否則它通往各代理的連線會被 NetRedirector 再轉一次，上面這整套「繞過 5G 限制」的機制就會失效（見開頭提醒）。

## 功能特點

- **多代理並行下載**：單一檔案可同時透過多個 SOCKS5 代理分段下載，提升速度與穩定性。
- **多線程分片**：檔案切成多個區塊（bitmap 追蹤），每個代理以多條線程同時抓取不同區塊。
- **斷點續傳**：進度以 `.progress` 檔持久化，關閉程式後重啟可自動恢復未完成的任務。
- **BT 下載（magnet/.torrent）**：支援磁力連結與 `.torrent` 檔，以 libtorrent 為引擎；公開種子支援多線路聚合下載（多個 session 各綁定直連或 SOCKS5 代理、分片下載），private（PT）種子與選擇性下載（僅勾選部分檔案）維持單線路。
- **Chrome 擴充功能**：攔截瀏覽器下載事件，自動把連結送進本程式（見 `chrome_extension/`）。
- **區塊進度視覺**：磁碟叢集風格的區塊圖，即時顯示各分段下載狀態。

## 架構

- `downloader.py` — 下載核心（`DownloadTask`、`DownloadManager`）
- `bt_downloader.py` — BT 下載（libtorrent，多 session 多線路聚合）
- `ftp_downloader.py` — FTP 下載（SOCKS5 控制/資料通道）
- `ui.py` — PySide6 圖形介面
- `http_server.py` — 接收 Chrome 擴充功能請求的本機 HTTP 伺服器
- `MultiSocksDownloader.py` — 程式入口
- `chrome_extension/` — Chrome 擴充功能（Manifest V3）

## 安裝

```bash
pip install -r requirements.txt
```

建置套件（用 Nuitka 編譯成獨立執行檔）另裝：

```bash
pip install -r requirements-dev.txt
```

## 執行

```bash
python MultiSocksDownloader.py
```

## 測試

```bash
python -m unittest discover -s tests -v
```

## 建置（獨立執行檔）

```bash
build.bat
```

編譯完成後會在專案根目錄產出 `MultiSocksDownloader.dist/` 資料夾，內含可直接執行的 `MultiSocksDownloader.exe`，無需安裝 Python。

> 編譯需要 Windows 上的 C 編譯器：Microsoft Visual Studio（MSVC）或 MinGW64。對應的 Nuitka 指令如下：

```bash
nuitka --standalone --windows-console-mode=disable --enable-plugin=pyside6 MultiSocksDownloader.py
```

## 設定檔

程式設定儲存於 `%USERPROFILE%\.multi_socks_downloader\config.json`，主要欄位：

- `save_dir`：預設下載目錄
- `socks_proxies`：已設定的 SOCKS5 代理（可在圖形介面中新增）
- `speed_limit`：全局限速（bytes/sec，0 為不限速）
- `custom_headers`：自訂請求標頭
- `history`：歷史下載紀錄

## HTTP API

本機 HTTP 伺服器預設監聽 `127.0.0.1:8765`，供 Chrome 擴充功能呼叫：

| 方法 | 路徑 | 說明 |
|------|------|------|
| `GET` | `/ping` | 連線檢查，回傳 `{"status":"ok"}` |
| `GET` | `/tasks` | 查詢所有任務的下載進度 |
| `POST` | `/` | 新增下載任務 |

`POST /` 的 JSON 主體範例：

```json
{
  "url": "https://example.com/file.zip",
  "filename": "file.zip",
  "chunks_per_part": 0,
  "threads_per_proxy": 6,
  "headers": {}
}
```

- `chunks_per_part` 設為 `0` 表示依檔案大小自適應分片。
- `threads_per_proxy` 為每個代理的下載線程數，預設 `6`。

## Chrome 擴充功能

安裝與使用方式請見 [`chrome_extension/README.md`](chrome_extension/README.md)。
