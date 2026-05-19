#!/bin/bash
# ==================================================
# Stock Alarm Bot — nohup 실행 스크립트
# 사용법: bash run.sh [start|stop|status|logs|restart]
#
# 시작:    bash run.sh          (또는 bash run.sh start)
# 중지:    bash run.sh stop
# 재시작:  bash run.sh restart
# 상태:    bash run.sh status
# 로그:    bash run.sh logs
# ==================================================

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3 2>/dev/null || command -v python 2>/dev/null)"
LOG_DIR="$APP_DIR/logs"
PID_FILE="$APP_DIR/main.pid"

mkdir -p "$LOG_DIR"

# ── 중지 ──────────────────────────────────────────
stop() {
    echo "중지 중..."
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID"
            echo "  종료: PID $PID"
        else
            echo "  이미 종료된 프로세스 (PID: $PID)"
        fi
        rm -f "$PID_FILE"
    else
        # PID 파일 없어도 잔여 프로세스 정리
        pkill -f "python.*main\.py" 2>/dev/null && echo "  잔여 main.py 프로세스 종료" || echo "  실행 중인 프로세스 없음"
    fi
    echo "중지 완료"
}

# ── 상태 ──────────────────────────────────────────
status() {
    echo "프로세스 상태:"
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "  실행 중 (PID: $PID)"
            echo "  로그: $LOG_DIR/main.log"
        else
            echo "  비정상 종료 — PID 파일 정리 (로그 확인: bash run.sh logs)"
            rm -f "$PID_FILE"
        fi
    else
        echo "  실행 안 됨"
    fi
}

# ── 로그 ──────────────────────────────────────────
logs() {
    echo "=== 최근 50줄 로그 ==="
    tail -50 "$LOG_DIR/main.log" 2>/dev/null || echo "(로그 없음)"
    echo ""
    echo "실시간 로그 보기: tail -f $LOG_DIR/main.log"
}

# ── 시작 ──────────────────────────────────────────
start() {
    # Python 확인
    if [ -z "$PYTHON" ]; then
        echo "Python을 찾을 수 없습니다. Python이 설치되어 있는지 확인하세요."
        exit 1
    fi

    # config.json 확인
    if [ ! -f "$APP_DIR/config.json" ]; then
        echo "config.json 파일이 없습니다. 먼저 생성하세요:"
        echo "   cp $APP_DIR/config.example.json $APP_DIR/config.json"
        exit 1
    fi

    # 이미 실행 중인지 확인
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "이미 실행 중입니다. (PID: $(cat "$PID_FILE"))"
        echo "   재시작: bash run.sh restart"
        exit 1
    fi

    # 의존성 설치
    echo "패키지 확인 중..."
    "$PYTHON" -m pip install --quiet -r "$APP_DIR/requirements.txt"
    echo "의존성 확인 완료"

    cd "$APP_DIR"

    # main.py 백그라운드 실행
    nohup "$PYTHON" main.py >> "$LOG_DIR/main.log" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Stock Alarm Bot 시작 (PID: $!)"
    echo ""
    echo "완료! 로그 확인: bash run.sh logs"
    echo "실시간 로그:     tail -f $LOG_DIR/main.log"
}

# ── 명령 분기 ──────────────────────────────────────
case "${1}" in
    "" | start)
        start
        ;;
    stop)
        stop
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    restart)
        stop
        pkill -f "python.*main\.py" 2>/dev/null || true
        rm -f "$PID_FILE"
        sleep 2
        start
        ;;
    *)
        echo "사용법: bash run.sh [start|stop|status|logs|restart]"
        exit 1
        ;;
esac
