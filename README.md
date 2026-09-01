# MultiSocksDownloader 多代理下載器

使用多個 SOCKS5 代理、多線程並行下載，支援斷點續傳的桌面下載工具。

## 功能特點

- **多代理並行下載**：單一檔案可同時透過多個 SOCKS5 代理分段下載，提升速度與穩定性。
- **多線程分片**：檔案切成多個區塊（bitmap 追蹤），每個代理以多條線程同時抓取不同區塊。
- **斷點續傳**：進度以 `.progress` 檔持久化，關閉程式後重啟可自動恢復未完成的任務。
- **BT 下載（magnet/.torrent）**：支援磁力連結與 `.torrent` 檔，以 libtorrent 為引擎；單一 session 綁定單一線路（直連或指定的 SOCKS5 代理），目前為單線路下載。
- **Chrome 擴充功能**：攔截瀏覽器下載事件，自動把連結送進本程式（見 `chrome_extension/`）。
- **區塊進度視覺**：磁碟叢集風格的區塊圖，即時顯示各分段下載狀態。

## 架構

- `downloader.py` — 下載核心（`DownloadTask`、`DownloadManager`）
- `bt_downloader.py` — BT 下載（libtorrent，單一 session 單一線路）
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
