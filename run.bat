@echo off
chcp 65001 > nul
echo.
echo ============================================================
echo   주식 뉴스 Discord 알람 실행 중...
echo   종료하려면 Ctrl+C 를 누르세요.
echo ============================================================
echo.
cd /d "%~dp0"
python main.py
pause
