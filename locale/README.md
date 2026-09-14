# locale/ — 多國語系檔

本目錄為 MultiSocksDownloader 的語系檔，機制與 `4Games/NetRedirector` 相同。

## 格式

每個 `<語系代碼>.json` 都是一個 JSON 物件：

```json
{
  "_info": "說明文字（會被忽略）",
  "原始中文字串": "翻譯後字串"
}
```

- **key 是原始字串**（正體中文），必須與程式碼中的字面值完全一致（含全形標點與空白）。
- **查不到的字串會直接顯示原文**，因此語系檔可以只翻譯一部分，不會造成介面空白。
- 字串中的 `{}` 是變數佔位符（例如 `"已加入下載: {}"`），翻譯時必須保留相同數量與順序。
- 檔名需為 `i18n.py` 中 `SUPPORTED_LANGS` 列出的代碼之一。

## 支援語系（17）

`zh_TW`（預設）、`zh_CN`、`en_US`、`ja_JP`、`ko_KR`、`es_ES`、`pt_BR`、`fr_FR`、
`de_DE`、`ru_RU`、`it_IT`、`vi_VN`、`th_TH`、`id_ID`、`tr_TR`、`pl_PL`、`nl_NL`

## 新增語系

1. 在 `i18n.py` 的 `SUPPORTED_LANGS` / `LANG_NAMES` 加入代碼與顯示名稱。
2. 於本目錄新增 `<代碼>.json`。
3. `build.bat` 與 `.github/workflows/release.yml` 會把整個 `locale/` 打包進發行檔。
