#!/bin/bash
# รัน Scalper Engine (Cache Edition) ใน Background
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

EXISTING_PID=$(pgrep -f "phone_scalper_cache_engine.py")
if [ -n "$EXISTING_PID" ]; then
    echo "⚠️ Scalper Cache Engine กำลังรันอยู่แล้ว (PID: $EXISTING_PID)"
    echo "หากต้องการหยุด ให้พิมพ์: ./stop_cache_scalper.sh"
    exit 0
fi

nohup python3 -u phone_scalper_cache_engine.py > scalper_cache.log 2>&1 &
PID=$!
echo "🚀 Scalper Cache Engine เริ่มรันใน Background สำเร็จ! (PID: $PID)"
echo "📄 ดู Log สดได้ด้วยคำสั่ง:"
echo "   tail -f scalper_cache.log"
