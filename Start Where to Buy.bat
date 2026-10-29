@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -c "import webview,curl_cffi,bs4" >nul 2>nul
  if errorlevel 1 py -3 -m pip install -r requirements.txt
  py -3 app.py
  goto :eof
)
python -c "import webview,curl_cffi,bs4" >nul 2>nul
if errorlevel 1 python -m pip install -r requirements.txt
python app.py
