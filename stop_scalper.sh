#!/bin/bash
# สั่งหยุด Scalper Engine
pkill -f "phone_scalper_engine.py"
if [ $? -eq 0 ]; then
    echo "🛑 หยุดการทำงาน Scalper Engine เรียบร้อยแล้ว"
else
    echo "ℹ️ ไม่พบ Scalper Engine ที่กำลังทำงานอยู่"
fi
