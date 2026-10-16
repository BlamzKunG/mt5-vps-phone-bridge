#!/bin/bash
# สั่งหยุด Scalper Cache Engine
pkill -f "phone_scalper_cache_engine.py"
if [ $? -eq 0 ]; then
    echo "🛑 หยุดการทำงาน Scalper Cache Engine เรียบร้อยแล้ว"
else
    echo "ℹ️ ไม่พบ Scalper Cache Engine ที่กำลังทำงานอยู่"
fi
