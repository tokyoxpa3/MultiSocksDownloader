@echo off
pip install -r requirements.txt
nuitka --standalone --windows-console-mode=disable --enable-plugin=pyside6 MultiSocksDownloader.py