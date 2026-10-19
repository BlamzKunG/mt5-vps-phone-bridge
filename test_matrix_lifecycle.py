#!/usr/bin/env python3
"""
Test Multi-Magic Order Lifecycle & History on MT5 VPS Server
1. Health & Price Check
2. Send Multiple Orders with Different Magic Numbers (123456, 2222, 3333)
3. Verify Positions & Filter by Magic
4. Modify SL / TP
5. Verify Modified Positions
6. Close Positions
7. Verify Closed Trades & History Filtering
"""

import sys
import time
import json
from vps_bridge_client import RemoteMT5Bridge

def run_test():
    print("=" * 60)
    print("🧪 เริ่มต้นการทดสอบ Multi-Magic Orders & History Lifecycle")
    print("=" * 60)

    bridge = RemoteMT5Bridge(vps_url="http://154.197.116.121:8000", api_key="llm-trade-secret-key-2026")
    
    # 1. Health & Connection
    health = bridge.check_health()
    print(f"\n[1] Health Check: {health.get('status')} | Terminal: {health.get('terminal_connected')} | Balance: ${health.get('balance')}")
    if health.get("status") != "HEALTHY":
        print("❌ Server MT5 ไม่พร้อมทำงาน")
        sys.exit(1)

    # 2. Get Price
    quote = bridge.get_current_price("XAUUSD")
    if not quote:
        print("❌ ไม่สามารถดึงราคา XAUUSD ได้")
        sys.exit(1)
    
    ask = quote["ask"]
    bid = quote["bid"]
    spread = quote["spread"]
    print(f"[2] Quote: Bid={bid:.2f}, Ask={ask:.2f}, Spread={spread:.1f} pts")

    # กำหนดค่า SL / TP สำหรับการทดสอบ (250 pts SL, 350 pts TP)
    # สำหรับ BUY: SL = Ask - 2.50, TP = Ask + 3.50
    sl_buy = round(ask - 2.50, 2)
    tp_buy = round(ask + 3.50, 2)

    test_magics = [
        (123456, "LLM Pure Autonomous"),
        (2222, "Break-Even 50% TP"),
        (3333, "Fast Trailing 100/50")
    ]

    opened_tickets = {}

    # 3. Send Orders with Multiple Magic Numbers
    print("\n[3] 🚀 ทดสอบยิงออเดอร์ 3 Magic Numbers พร้อมกัน...")
    for magic, desc in test_magics:
        comment = f"Test-{magic}"
        print(f"  -> ส่งคำสั่ง BUY 0.01 XAUUSD [Magic: {magic} ({desc})] SL={sl_buy}, TP={tp_buy}...")
        res = bridge.open_position(
            direction="BUY",
            lot=0.01,
            sl=sl_buy,
            tp=tp_buy,
            symbol="XAUUSD",
            magic=magic,
            comment=comment
        )
        if res.get("status") == "SUCCESS":
            ticket = res.get("ticket")
            opened_tickets[magic] = ticket
            print(f"     ✅ สำเร็จ! Ticket: {ticket}")
        else:
            print(f"     ❌ ล้มเหลว: {res.get('message')}")
        time.sleep(0.5)

    if not opened_tickets:
        print("❌ ไม่สามารถเปิดออเดอร์ทดสอบได้")
        sys.exit(1)

    time.sleep(1.0)

    # 4. Verify Positions in MT5 & Filter by Magic
    print("\n[4] 🔍 ตรวจสอบรายการออเดอร์ใน MT5 และทดสอบกรองตาม Magic Number...")
    all_pos = bridge.get_open_positions(symbol="XAUUSD")
    print(f"  -> พบออเดอร์เปิดค้างทั้งหมด {len(all_pos)} ไม้:")
    for p in all_pos:
        print(f"     • Ticket: {p['ticket']} | Magic: {p['magic']} | {p['direction']} {p['lot']} @ {p['entry_price']} | SL: {p['sl']} | TP: {p['tp']} | PnL: ${p['pnl']}")

    for magic in opened_tickets.keys():
        magic_pos = bridge.get_open_positions(symbol="XAUUSD", magic=magic)
        print(f"  -> กรองเฉพาะ Magic {magic}: พบ {len(magic_pos)} ไม้ (ตรงกับ Ticket {opened_tickets[magic]}: {'✅ ผ่าน' if any(p['ticket'] == opened_tickets[magic] for p in magic_pos) else '❌ ไม่พบ'})")

    time.sleep(1.0)

    # 5. Modify SL / TP
    print("\n[5] 🛠️ ทดสอบปรับปรุง Modify SL / TP...")
    for magic, ticket in opened_tickets.items():
        # ขยับ SL ขึ้น 1.00 (100 points) และ TP ขึ้น 1.00
        new_sl = round(sl_buy + 1.00, 2)
        new_tp = round(tp_buy + 1.00, 2)
        print(f"  -> กำลังแก้ไข Ticket {ticket} (Magic {magic}): SL -> {new_sl}, TP -> {new_tp}...")
        mod_res = bridge.modify_position(ticket=ticket, new_sl=new_sl, new_tp=new_tp)
        if mod_res.get("status") == "SUCCESS":
            print(f"     ✅ แก้ไขสำเร็จ: {mod_res.get('message')}")
        else:
            print(f"     ❌ แก้ไขไม่สำเร็จ: {mod_res.get('message')}")

    time.sleep(1.0)

    # ตรวจสอบว่าค่าเปลี่ยนจริงใน MT5
    print("\n[6] 🔍 ตรวจสอบค่า SL/TP หลัง Modify จาก MT5:")
    pos_after_mod = {p['ticket']: p for p in bridge.get_open_positions(symbol="XAUUSD")}
    for magic, ticket in opened_tickets.items():
        p = pos_after_mod.get(ticket)
        if p:
            print(f"     • Ticket {ticket} (Magic {magic}): SL={p['sl']}, TP={p['tp']} (Expected: {round(sl_buy + 1.0, 2)} / {round(tp_buy + 1.0, 2)})")

    time.sleep(1.0)

    # 7. Close Orders
    print("\n[7] 🚪 ทดสอบสั่งปิดออเดอร์ (Close Positions)...")
    for magic, ticket in opened_tickets.items():
        print(f"  -> ปิด Ticket {ticket} (Magic {magic})...")
        close_res = bridge.close_position(ticket=ticket, symbol="XAUUSD")
        if close_res.get("status") == "SUCCESS":
            print(f"     ✅ ปิดออเดอร์สำเร็จ! Ticket {ticket}")
        else:
            print(f"     ❌ ปิดออเดอร์ไม่สำเร็จ: {close_res.get('message')}")

    time.sleep(1.5)

    # 8. Check Positions are cleared
    remaining = bridge.get_open_positions(symbol="XAUUSD")
    print(f"\n[8] 🔍 ตรวจสอบออเดอร์คงเหลือหลังปิด: เหลือ {len(remaining)} ไม้")

    # 9. Verify Closed Trades & History Endpoints
    print("\n[9] 📊 ทดสอบประวัติการเทรดที่ปิดแล้ว (History / Closed Trades API)...")
    closed = bridge.get_closed_trades(days=1)
    closed_dict = {t['ticket']: t for t in closed}
    
    print(f"  -> พบรายการปิดในประวัติทั้งหมด {len(closed)} ไม้")
    for magic, ticket in opened_tickets.items():
        trade = closed_dict.get(ticket)
        if trade:
            print(f"     ✅ Ticket {ticket} [Magic {trade.get('magic')}]: Entry={trade.get('open_price')} Close={trade.get('close_price')} | Profit=${trade.get('profit')} NetPnL=${trade.get('net_pnl')} | Time={trade.get('close_time')}")
        else:
            print(f"     ⚠️ ไม่พบ Ticket {ticket} ในประวัติ closed trades")

    # ทดสอบกรองตาม Magic ใน History
    print("\n[10] 🎯 ทดสอบกรอง History แยกตาม Magic Number:")
    for magic in opened_tickets.keys():
        hist_magic = bridge.get_closed_trades(magic=magic, days=1)
        print(f"  -> History Magic {magic}: พบ {len(hist_magic)} รายการ")

    print("\n" + "=" * 60)
    print("🎉 การทดสอบเสร็จสมบูรณ์ 100%!")
    print("=" * 60)

if __name__ == "__main__":
    run_test()
