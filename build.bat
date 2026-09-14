@echo off
REM 打包前請先以「python MultiSocksDownloader.py --debug」驗證原始碼行為，
REM 因為改 .py 不會影響已打包的 exe，重新打包才會套用新程式碼。
pip install -r requirements.txt
nuitka --standalone --windows-console-mode=disable --windows-icon-from-ico=app_icon.ico --enable-plugin=pyside6 --include-package=libtorrent --include-package=yt_dlp --nofollow-import-to=yt_dlp.extractor.lazy_extractors MultiSocksDownloader.py
REM 下載 ffmpeg（LGPL essentials）並打包進 .dist\ffmpeg\，供串流視訊+音訊合併使用
python fetch_ffmpeg.py || exit /b 1
if not exist "MultiSocksDownloader.dist\ffmpeg" mkdir "MultiSocksDownloader.dist\ffmpeg"
copy /Y "ffmpeg\ffmpeg.exe" "MultiSocksDownloader.dist\ffmpeg\ffmpeg.exe" >nul
copy /Y "ffmpeg\LICENSE.txt" "MultiSocksDownloader.dist\ffmpeg\LICENSE.txt" >nul
echo ffmpeg 已打包到 MultiSocksDownloader.dist\ffmpeg\
REM 打包 yt-dlp 外掛（movieffm 等站台的來源/集數解析），放在 exe 旁讓 yt-dlp 自動載入
REM yt_dlp_plugins 為本機開發目錄（不進版控），不存在時略過
if exist "yt_dlp_plugins" (
    xcopy /E /I /Y "yt_dlp_plugins" "MultiSocksDownloader.dist\yt_dlp_plugins" >nul
    echo yt-dlp 外掛已打包到 MultiSocksDownloader.dist\yt_dlp_plugins\
) else (
    echo 略過 yt-dlp 外掛（本機無 yt_dlp_plugins 目錄）
)
REM 打包語系檔（locale\*.json），供 i18n 於執行檔旁讀取
if exist "locale" (
    xcopy /E /I /Y "locale" "MultiSocksDownloader.dist\locale" >nul
    echo 語系檔已打包到 MultiSocksDownloader.dist\locale\
)
