@echo off
chcp 65001 > nul
echo.
echo ============================================================
echo   주식 뉴스 Discord 알람 - 환경 설치
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되어 있지 않습니다.
    echo        https://www.python.org 에서 설치 후 다시 실행하세요.
    pause
    exit /b 1
)

echo [1/2] 의존 패키지 설치 중...
pip install -r requirements.txt
if errorlevel 1 (
    echo [오류] 패키지 설치 실패. pip 및 인터넷 연결을 확인하세요.
    pause
    exit /b 1
)

echo.
echo [2/2] 설치 완료!
echo.
echo ============================================================
echo   다음 단계:
echo   1. Discord 서버에서 웹훅 URL을 복사합니다.
echo      (채널 설정 → 연동 → 웹훅 → 새 웹훅)
echo.
echo   2. 웹훅 URL 설정:
echo      python ticker_manager.py set-webhook YOUR_WEBHOOK_URL
echo.
echo   3. 티커 추가:
echo      python ticker_manager.py add AAPL TSLA NVDA
echo.
echo   4. 실행:
echo      python main.py
echo      (또는 run.bat 더블클릭)
echo ============================================================
echo.
pause
