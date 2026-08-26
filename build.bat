@echo off
pip install -r requirements.txt
nuitka --standalone --windows-console-mode=disable --windows-icon-from-ico=app_icon.ico --enable-plugin=pyside6 MultiSocksDownloader.py