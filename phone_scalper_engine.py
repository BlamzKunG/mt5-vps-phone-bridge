#!/usr/bin/env python3
"""
M5 Price Action Scalping Engine (AI-Driven)
-------------------------------------------
- Model: gpt-6-sol (MaxPlus AI, reasoning_effort: xhigh)
- Data: 100-bar Normalized Context + 20-bar Detailed M5 OHLCV (NO Balance sent)
- Strategy: Price Action Scalping 100 - 500 points on XAUUSD
- Sync: Automatically triggers on every M5 candle close (+2s buffer)
- Local Guard: Enforces 100-500 points SL/TP, spread check, and position management
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple
import pandas as pd
import requests

from vps_bridge_client import RemoteMT5Bridge

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scalper_engine.log", encoding="utf-8")
    ]
)

# Helper to load .env if present
env_file = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_file):
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

class PriceActionScalperEngine:
    def __init__(
        self,
        vps_url: Optional[str] = None,
        bridge_api_key: Optional[str] = None,
        maxplus_api_key: Optional[str] = None,
        model: str = "gpt-6-sol",
        reasoning_effort: str = "xhigh",
        symbol: str = "XAUUSD",
        lot_size: float = 0.01,
        max_spread_points: float = 40.0,
        min_sl_tp_points: int = 100,
        max_sl_tp_points: int = 500,
        dry_run: bool = False
    ):
        self.vps_url = vps_url or os.getenv("VPS_URL", "http://154.197.116.121:8000")
        bridge_key = bridge_api_key or os.getenv("BRIDGE_API_KEY", "llm-trade-secret-key-2026")
        self.bridge = RemoteMT5Bridge(vps_url=self.vps_url, api_key=bridge_key)
        self.maxplus_api_key = maxplus_api_key or os.getenv("MAXPLUS_API_KEY", "")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.symbol = symbol
        self.lot_size = lot_size
        self.max_spread_points = max_spread_points
        self.min_sl_tp_points = min_sl_tp_points
        self.max_sl_tp_points = max_sl_tp_points
        self.dry_run = dry_run

        self.last_processed_candle_time: Optional[str] = None
        logging.info("🚀 Price Action Scalper Engine Initialized")
        logging.info(f"Target: {self.symbol} | Model: {self.model} (effort: {self.reasoning_effort}) | Lot: {self.lot_size}")
        logging.info(f"Scalp Bounds: [{self.min_sl_tp_points}, {self.max_sl_tp_points}] points | Dry Run: {self.dry_run}")

    def prepare_market_context(self) -> Optional[Dict[str, Any]]:
        """
        ดึง 105 แท่ง M5 ล่าสุด และคัดกรองเฉพาะแท่งที่ปิดสมบูรณ์แล้ว
        - 100 แท่ง Normalized [0.00 - 1.00]
        - 20 แท่ง Detailed OHLCV
        - NO BALANCE LEAKAGE
        """
        try:
            df = self.bridge.get_historical_data(symbol=self.symbol, timeframe="5m", num_candles=105)
            if df.empty or len(df) < 100:
                logging.error(f"❌ ดึงข้อมูลแท่งเทียนไม่พอ: {len(df)} แท่ง")
                return None

            # ใช้เฉพาะแท่งที่ปิดแล้วเท่านั้น (ตัดแท่งกำลังฟอร์มตัวออก)
            completed_df = df.iloc[-101:-1].copy() if len(df) > 100 else df.iloc[-100:].copy()
            last_candle_time = str(completed_df.iloc[-1]["timestamp"])

            # 1. รายละเอียด 20 แท่งล่าสุด
            detailed_20 = completed_df.iloc[-20:].copy()
            detailed_list = []
            for _, row in detailed_20.iterrows():
                detailed_list.append({
                    "bar": len(detailed_list) - 20,
                    "time": str(row["timestamp"])[-8:],
                    "open": round(float(row["open"]), 2),
                    "high": round(float(row["high"]), 2),
                    "low": round(float(row["low"]), 2),
                    "close": round(float(row["close"]), 2),
                    "vol": int(row["volume"])
                })

            # 2. ทำ Normalized 100 แท่ง
            min_100 = float(completed_df["low"].min())
            max_100 = float(completed_df["high"].max())
            rng_100 = max_100 - min_100 if max_100 > min_100 else 1.0
            norm_closes = [round((float(c) - min_100) / rng_100, 3) for c in completed_df["close"]]

            current_close = float(completed_df.iloc[-1]["close"])

            return {
                "candle_time": last_candle_time,
                "current_price": current_close,
                "min_100": min_100,
                "max_100": max_100,
                "range_100": rng_100,
                "norm_closes": norm_closes,
                "detailed_20": detailed_list
            }
        except Exception as e:
            logging.error(f"❌ เกิดข้อผิดพลาดในการเตรียม Context ข้อมูล: {e}")
            return None

    def call_ai_decision(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        ส่งข้อมูลแท่งเทียนให้ gpt-6-sol ประมวลผลและส่งค่า Pure JSON กลับมา
        """
        system_prompt = """You are an elite Price Action Scalper for Gold (XAUUSD).
Your objective is to capture high-probability intraday scalps of 100 to 500 points (1.00 to 5.00 USD).
You are evaluated on consistency, disciplined execution, and strict risk-to-reward.

Input Data:
1. 100-bar Normalized Curve [0.00 to 1.00]: Shows macro structure, key swing levels, and whether price is at the top/bottom of the 100-bar range.
2. 20-bar Detailed M5 OHLCV: Shows immediate price action, candle wicks, rejection, momentum, and volume.

Trading Rules:
- Scalp Target: tp_points must be between 100 and 500 points.
- Stop Loss: sl_points must be between 100 and 500 points.
- Reward/Risk Ratio should be >= 1.2.
- Only trigger BUY or SELL on clear structural confluence (wick rejection, breakout-retest, strong engulfing, or extreme range mean-reversion).
- If market is choppy, indecisive, or in the middle of a range without confirmation, choose HOLD.

Return PURE JSON ONLY (no markdown, no backticks):
{
  "action": "BUY" | "SELL" | "HOLD" | "CLOSE_ALL",
  "confidence": <float 0.0 to 1.0>,
  "sl_points": <int 100 to 500>,
  "tp_points": <int 100 to 500>,
  "market_regime": "<string>",
  "reasoning": "<concise quantitative rationale based on 100-bar structure and 20-bar candle action>"
}"""

        user_prompt = f"""Asset: {self.symbol} (M5)
Completed Candle Timestamp: {ctx['candle_time']}
Latest Closed Price: {ctx['current_price']:.2f}

[100-Bar Macro Context]
- 100-Bar Low: {ctx['min_100']:.2f}
- 100-Bar High: {ctx['max_100']:.2f}
- 100-Bar Normalized Closes (0.00 to 1.00, oldest to newest):
{ctx['norm_closes']}

[20-Bar Detailed M5 OHLCV (Bars -20 to -1)]
{json.dumps(ctx['detailed_20'], separators=(',', ':'))}

Analyze the market structure and output your pure JSON scalping decision:"""

        headers = {
            "Authorization": f"Bearer {self.maxplus_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "reasoning_effort": self.reasoning_effort
        }

        try:
            t0 = time.time()
            resp = requests.post(
                "https://api.maxplus-ai.cc/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=120
            )
            elapsed = time.time() - t0

            if resp.status_code != 200:
                logging.error(f"❌ MaxPlus API Error: HTTP {resp.status_code} - {resp.text}")
                return None

            data = resp.json()
            raw_content = data["choices"][0]["message"]["content"].strip()
            # ทำความสะอาดกรณีมี backtick หลุดมา
            clean_json = raw_content.strip("```json").strip("```").strip()
            decision = json.loads(clean_json)
            decision["latency_seconds"] = round(elapsed, 2)
            decision["usage"] = data.get("usage", {})
            return decision
        except Exception as e:
            logging.error(f"❌ การเรียก AI หรือ Parse JSON ล้มเหลว: {e}")
            return None

    def execute_decision(self, decision: Dict[str, Any]) -> bool:
        """
        ตรวจสอบเงื่อนไขความปลอดภัยและส่งออเดอร์ไปยัง MT5 ผ่าน Remote Bridge
        """
        action = decision.get("action", "HOLD").upper()
        confidence = decision.get("confidence", 0.0)
        sl_points = decision.get("sl_points", 200)
        tp_points = decision.get("tp_points", 300)
        reasoning = decision.get("reasoning", "")

        logging.info(f"📊 AI Decision: {action} (Conf: {confidence:.2f}) | Regime: {decision.get('market_regime')}")
        logging.info(f"💡 Rationale: {reasoning}")

        if action == "HOLD":
            logging.info("⏸️ Action เป็น HOLD -> ไม่เปิดออเดอร์ใหม่")
            return True

        # ดึงราคา Bid/Ask/Spread ปัจจุบัน
        price_info = self.bridge.get_current_price(symbol=self.symbol)
        if not price_info:
            logging.error("❌ ไม่สามารถดึงราคา Realtime เพื่อเปิดออเดอร์ได้")
            return False

        spread_points = price_info.get("spread", 0.0)
        bid = price_info.get("bid", 0.0)
        ask = price_info.get("ask", 0.0)

        # 1. Spread Guard
        if spread_points > self.max_spread_points:
            logging.warning(f"⚠️ Spread สูงเกินเกณฑ์ ({spread_points} > {self.max_spread_points}) -> ข้ามรอบนี้")
            return False

        # 2. ตรวจสอบ Open Positions เพื่อไม่ให้เทรดซ้ำซ้อน (รองรับทั้ง get_positions และ get_open_positions)
        if hasattr(self.bridge, "get_positions"):
            open_positions = self.bridge.get_positions(symbol=self.symbol)
        else:
            open_positions = self.bridge.get_open_positions(symbol=self.symbol)

        if action == "CLOSE_ALL":
            if open_positions:
                logging.info(f"🧹 กำลังปิด Position ทั้งหมด {len(open_positions)} รายการตามคำสั่ง AI...")
                for pos in open_positions:
                    t_id = pos.get("ticket") or pos.get("id")
                    if t_id:
                        self.bridge.close_position(ticket=t_id, symbol=self.symbol)
            return True

        if len(open_positions) >= 1:
            logging.info(f"⚠️ มีออเดอร์เปิดค้างอยู่แล้ว {len(open_positions)} ออเดอร์ -> ข้ามการเปิดใหม่เพื่อคุมความเสี่ยง")
            return False

        # 3. ล็อกขอบเขต SL/TP (Clamping Guard)
        sl_points = max(self.min_sl_tp_points, min(self.max_sl_tp_points, int(sl_points)))
        tp_points = max(self.min_sl_tp_points, min(self.max_sl_tp_points, int(tp_points)))

        # คำนวณราคา SL/TP (สำหรับ XAUUSD 100 จุด = 1.00 USD, 1 จุด = 0.01)
        point_value = 0.01

        if action == "BUY":
            entry_price = ask
            sl_price = round(entry_price - (sl_points * point_value), 2)
            tp_price = round(entry_price + (tp_points * point_value), 2)
        elif action == "SELL":
            entry_price = bid
            sl_price = round(entry_price + (sl_points * point_value), 2)
            tp_price = round(entry_price - (tp_points * point_value), 2)
        else:
            logging.warning(f"⚠️ ไม่รู้จักคำสั่ง Action: {action}")
            return False

        logging.info(f"🛡️ Safety Guard Checked: {action} @ {entry_price} | SL: {sl_price} ({sl_points} pts) | TP: {tp_price} ({tp_points} pts)")

        if self.dry_run:
            logging.info(f"🧪 [DRY RUN] จำลองส่งออเดอร์สำเร็จ: {action} {self.lot_size} lot @ {entry_price}")
            return True

        # ส่งออเดอร์ไปยัง MT5 Bridge (รองรับทั้ง send_order และ open_position)
        if hasattr(self.bridge, "send_order"):
            result = self.bridge.send_order(
                action=action,
                symbol=self.symbol,
                volume=self.lot_size,
                sl=sl_price,
                tp=tp_price,
                comment=f"Scalp-{action[:1]}-{sl_points}/{tp_points}"
            )
        else:
            result = self.bridge.open_position(
                direction=action,
                lot=self.lot_size,
                sl=sl_price,
                tp=tp_price,
                symbol=self.symbol,
                comment=f"Scalp-{action[:1]}-{sl_points}/{tp_points}"
            )

        if result and result.get("status") == "SUCCESS":
            logging.info(f"✅ เปิดออเดอร์สำเร็จ! Ticket: {result.get('ticket')}")
            return True
        else:
            logging.error(f"❌ ยิงออเดอร์ล้มเหลว: {result}")
            return False

    def run_single_cycle(self) -> bool:
        """
        ทำงาน 1 รอบวัฏจักร (Single Cycle)
        """
        local_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info("=" * 60)
        logging.info(f"🔄 เริ่มรอบการทำงาน: {local_time_str} (เวลาเครื่อง)")
        
        ctx = self.prepare_market_context()
        if not ctx:
            logging.warning("⚠️ ข้อมูลไม่พร้อม ข้ามรอบนี้")
            return False

        if ctx["candle_time"] == self.last_processed_candle_time:
            logging.info(f"ℹ️ แท่งเทียน {ctx['candle_time']} ได้รับการประมวลผลไปแล้ว")
            return False

        logging.info(f"📈 วิเคราะห์แท่งเทียนที่เพิ่งปิด: {ctx['candle_time']} | ราคาปิด: {ctx['current_price']:.2f}")
        logging.info(f"📊 100-Bar Macro: [{ctx['min_100']:.2f} - {ctx['max_100']:.2f}] (Range: {ctx['range_100']:.2f})")

        decision = self.call_ai_decision(ctx)
        if not decision:
            logging.error("❌ ไม่ได้รับคำตอบจาก AI")
            return False

        self.last_processed_candle_time = ctx["candle_time"]
        success = self.execute_decision(decision)
        return success

    def run_forever(self, run_immediately: bool = True):
        """
        รันระบบอัตโนมัติต่อเนื่อง 24/7 โดยซิงค์ตามเวลาเครื่องโทรศัพท์ทุก 5 นาทีเป๊ะ
        เช่น 01:00, 01:05, 01:10, 01:15, ... (+ 1 วินาที buffer ให้แท่งปิดสมบูรณ์)
        """
        logging.info("🎯 เข้าสู่โหมดรันอัตโนมัติ 24/7 (ซิงค์เวลาโทรศัพท์ทุก 5 นาที)")
        
        if run_immediately:
            logging.info("⚡ รันรอบแรกทันทีเมื่อสตาร์ท...")
            self.run_single_cycle()

        while True:
            try:
                now = datetime.now()
                # คำนวณเวลาเป้าหมายถัดไปที่ลงท้ายด้วย 0 หรือ 5 นาที (เช่น :05, :10, :15, ...)
                minutes_to_add = 5 - (now.minute % 5)
                target_time = (now + timedelta(minutes=minutes_to_add)).replace(second=1, microsecond=0)
                
                wait_seconds = (target_time - now).total_seconds()
                # เผื่อกรณีคลาดเคลื่อน
                if wait_seconds <= 0:
                    wait_seconds += 300
                    target_time += timedelta(minutes=5)

                wait_m = int(wait_seconds) // 60
                wait_s = int(wait_seconds) % 60
                logging.info(f"⏰ เวลาเครื่อง: {now.strftime('%H:%M:%S')} -> รอบถัดไปคือ {target_time.strftime('%H:%M:%S')} (รออีก {wait_m} นาที {wait_s} วินาที)...")
                
                time.sleep(wait_seconds)

                # ดำเนินการเรียก AI วิเคราะห์แท่งเทียน
                self.run_single_cycle()

            except KeyboardInterrupt:
                logging.info("\n🛑 ผู้ใช้หยุดการทำงานของระบบ (Ctrl+C)")
                break
            except Exception as e:
                logging.error(f"❌ เกิดข้อผิดพลาดใน Loop: {e}")
                time.sleep(5)

if __name__ == "__main__":
    once = "--once" in sys.argv
    engine = PriceActionScalperEngine(dry_run=False)
    if once:
        engine.run_single_cycle()
    else:
        engine.run_forever(run_immediately=True)
