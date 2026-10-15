#!/bin/bash
# รัน Scalper Engine ใน Background ด้วย nohup (ไม่ต้องเปิดจอ Termux ทิ้งไว้)
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# ตรวจสอบว่ารันอยู่แล้วหรือไม่
EXISTING_PID=$(pgrep -f "phone_scalper_engine.py")
if [ -n "$EXISTING_PID" ]; then
    echo "⚠️ Scalper Engine กำลังรันอยู่แล้ว (PID: $EXISTING_PID)"
    echo "หากต้องการหยุดก่อน ให้พิมพ์: ./stop_scalper.sh"
    exit 0
fi

nohup python3 -u phone_scalper_engine.py > scalper.log 2>&1 &
PID=$!
echo "🚀 Scalper Engine เริ่มรันใน Background สำเร็จ! (PID: $PID)"
echo "⏰ ระบบจะซิงค์เวลาโทรศัพท์ทุก 5 นาที (:00, :05, :10, :15, ...)"
echo "📄 ดูความคืบหน้าสดได้ด้วยคำสั่ง:"
echo "   tail -f scalper.log"
