"""版本資訊 - 全專案唯一的版本號來源。

發佈流程（固定不變）：
  1. 手動把 APP_VERSION 遞增。
  2. 打與其一致的 git tag（例 v1.6.0）。
  3. push 後 CI 自動把 tag 版本寫回本檔的 APP_VERSION 再編譯進 exe，
     避免手動改兩處（Nuitka 會把常數字面量凍進二進位檔）。
"""

APP_VERSION = "1.6.0"                      # 唯一版本來源，發佈前手動遞增
GITHUB_REPO = "tokyoxpa3/MultiSocksDownloader"
UPDATE_API_URL = "https://api.github.com/repos/{}/releases/latest".format(GITHUB_REPO)

# 打包後的執行檔名（不含 .exe）。替換腳本用「Get-Process -Name」等它退出時用到，
# 必須與真正的執行檔一致，不能信任 sys.executable（Nuitka 會指向內建 python.exe）。
EXE_NAME = "MultiSocksDownloader"
