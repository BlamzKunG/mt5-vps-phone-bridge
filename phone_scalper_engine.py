#!/usr/bin/env python3
"""
M5 Price Action Scalping Engine (AI-Driven)
-------------------------------------------
- Model: gpt-6-sol (MaxPlus AI, reasoning_effort: xhigh)
- Data: 100-bar Normalized Context + 20-bar Detailed M5 OHLCV (NO Balance sent)
- Strategy: Price Action Scalping 100 - 500 points on XAUUSD
- Order Management: Supports Dynamic Trailing Stop, Breakeven, and Early Emergency Close
- Timeout: Dynamic timeout up to ~270s (waiting until close to next cycle) with auto-retry
- Sync: Automatically triggers on every M5 candle close (+1s buffer) using phone local time
- Local Guard: Enforces 100-500 points SL/TP, spread check, and protective SL tightening only
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List
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
        - ตรวจสอบ Active Position เพื่อทำ Order Management
        - NO BALANCE LEAKAGE
        """
        try:
            df = self.bridge.get_historical_data(symbol=self.symbol, timeframe="5m", num_candles=105)
            if df.empty or len(df) < 100:
                logging.error(f"❌ ดึงข้อมูลแท่งเทียนไม่พอ: {len(df)} แท่ง")
                return None

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

            # 3. ตรวจสอบ Active Position สำหรับ Order Management
            active_trade_info = None
            try:
                if hasattr(self.bridge, "get_positions"):
                    open_positions = self.bridge.get_positions(symbol=self.symbol)
                else:
                    open_positions = self.bridge.get_open_positions(symbol=self.symbol)
                
                if open_positions:
                    pos = open_positions[0]
                    t_id = pos.get("ticket") or pos.get("id")
                    dir_pos = pos.get("direction", "BUY").upper()
                    entry_p = float(pos.get("entry_price", current_close))
                    cur_sl = float(pos.get("sl", 0.0)) if pos.get("sl") is not None else None
                    cur_tp = float(pos.get("tp", 0.0)) if pos.get("tp") is not None else None
                    
                    if dir_pos == "BUY":
                        pnl_points = round((current_close - entry_p) / 0.01, 1)
                    else:
                        pnl_points = round((entry_p - current_close) / 0.01, 1)

                    active_trade_info = {
                        "ticket": t_id,
                        "direction": dir_pos,
                        "lot": float(pos.get("lot", self.lot_size)),
                        "entry_price": entry_p,
                        "current_price": current_close,
                        "current_sl": cur_sl,
                        "current_tp": cur_tp,
                        "pnl_points": pnl_points
                    }
            except Exception as e:
                logging.warning(f"⚠️ ตรวจสอบ Active Position ไม่สำเร็จ: {e}")

            return {
                "candle_time": last_candle_time,
                "current_price": current_close,
                "min_100": min_100,
                "max_100": max_100,
                "range_100": rng_100,
                "norm_closes": norm_closes,
                "detailed_20": detailed_list,
                "active_trade": active_trade_info
            }
        except Exception as e:
            logging.error(f"❌ เกิดข้อผิดพลาดในการเตรียม Context ข้อมูล: {e}")
            return None

    def call_ai_decision(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        ส่งข้อมูลแท่งเทียนให้ gpt-6-sol ประมวลผลและส่งค่า Pure JSON กลับมา
        - Dynamic Timeout: รอจนกว่าจะใกล้รอบแท่งถัดไป (สูงสุด 270s)
        - Research-based System Prompt รองรับทั้ง Flat Mode และ Active Order Management Mode
        """
        now = datetime.now()
        minutes_to_add = 5 - (now.minute % 5)
        target_time = (now + timedelta(minutes=minutes_to_add)).replace(second=1, microsecond=0)
        seconds_remaining = (target_time - now).total_seconds()
        timeout_seconds = max(90, min(270, int(seconds_remaining) - 15))

        system_prompt = """You are an elite Institutional Quantitative Price Action Scalper and Risk Manager for Gold (XAUUSD M5).
Your objective is to capture and protect high-probability intraday scalps of 100 to 500 points (1.00 to 5.00 USD).
You are evaluated on consistency, disciplined risk-to-reward (>= 1.2), and precision trade management.

CORE METHODOLOGY:
1. Macro Context (100-bar Normalized Curve [0.00 to 1.00]):
   - Overextended Extremes (>0.85 or <0.15): Look for liquidity sweep exhaustion or sharp mean reversion.
   - Equilibrium Band (0.40 - 0.60): Trend continuation if backed by high-volume displacement, else avoid low-volume chop.
2. Micro Price Action (20-bar Detailed OHLCV):
   - Liquidity Sweeps & Wick Rejections: Wicks >= 40% of candle range piercing swing highs/lows and closing back inside.
   - Market Structure Shift (MSS/CHoCH): Aggressive displacement breaking opposing swing points.
   - Volume Confirmation: High tick volume confirming institutional order flow vs low-volume fakeouts.

DECISION PROTOCOLS:

A. When FLAT (No Active Position):
   - If clear confluence emerges: Output "BUY" or "SELL".
   - Stop Loss (sl_points): 100 to 500 points placed beyond the structural invalidation wick.
   - Take Profit (tp_points): 100 to 500 points targeting the opposing internal liquidity pool.
   - If market is choppy, ambiguous, or lacks structural confirmation: Output "HOLD".

B. When IN POSITION (Active Trade Running):
   - Evaluate trade health based on current price, floating PnL (points), and latest candle structure:
   - "HOLD_POSITION": The thesis remains healthy; price is moving toward TP or experiencing normal low-volume pullback.
   - "MODIFY_SLTP": Active risk protection:
     * If floating profit >= 150 points: Move SL to Breakeven (entry_price + 20 pts for BUY, entry_price - 20 pts for SELL) to lock in a risk-free trade.
     * If trend made a new structural swing pivot: Trail SL behind the newest M5 swing low (for BUY) or swing high (for SELL).
     * Rule: You may ONLY tighten SL (lower risk). NEVER widen SL.
   - "CLOSE_POSITION": Early emergency exit. If an adverse liquidity sweep, opposing CHoCH with strong displacement, or thesis invalidation occurs, exit immediately at market to preserve capital rather than taking full SL.

OUTPUT FORMAT REQUIREMENTS:
Return PURE JSON ONLY (no markdown code blocks, no backticks, no conversational filler):
{
  "action": "BUY" | "SELL" | "HOLD" | "HOLD_POSITION" | "MODIFY_SLTP" | "CLOSE_POSITION",
  "confidence": <float 0.0 to 1.0>,
  "sl_points": <int 100 to 500 or null>,
  "tp_points": <int 100 to 500 or null>,
  "new_sl": <float or null>,
  "new_tp": <float or null>,
  "market_regime": "<concise regime description>",
  "reasoning": "<structured quantitative rationale: 1) 100-bar macro, 2) 20-bar candle action, 3) active trade evaluation if any, 4) invalidation level>"
}"""

        active_trade_str = json.dumps(ctx.get("active_trade"), indent=1) if ctx.get("active_trade") else "FLAT (No Open Positions)"

        user_prompt = f"""Asset: {self.symbol} (M5)
Completed Candle Timestamp: {ctx['candle_time']}
Latest Closed Price: {ctx['current_price']:.2f}

[ACTIVE POSITION STATUS]
{active_trade_str}

[100-Bar Macro Context]
- 100-Bar Low: {ctx['min_100']:.2f}
- 100-Bar High: {ctx['max_100']:.2f}
- 100-Bar Normalized Closes (0.00 to 1.00, oldest to newest):
{ctx['norm_closes']}

[20-Bar Detailed M5 OHLCV (Bars -20 to -1)]
{json.dumps(ctx['detailed_20'], separators=(',', ':'))}

Analyze the market structure, evaluate trade health (if in position) or setup confluence (if flat), and output your pure JSON decision:"""

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

        max_retries = 2
        for attempt in range(max_retries):
            try:
                t0 = time.time()
                logging.info(f"⏳ เรียก AI ด้วย Dynamic Timeout: {timeout_seconds}s (Attempt {attempt+1}/{max_retries})...")
                resp = requests.post(
                    "https://api.maxplus-ai.cc/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=timeout_seconds
                )
                elapsed = time.time() - t0

                if resp.status_code != 200:
                    logging.error(f"❌ MaxPlus API Error: HTTP {resp.status_code} - {resp.text}")
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
                    return None

                data = resp.json()
                raw_content = data["choices"][0]["message"]["content"].strip()
                clean_json = raw_content.strip("```json").strip("```").strip()
                decision = json.loads(clean_json)
                decision["latency_seconds"] = round(elapsed, 2)
                decision["usage"] = data.get("usage", {})
                return decision
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as net_err:
                logging.warning(f"⚠️ Network issue on attempt {attempt+1}/{max_retries}: {net_err}")
                if attempt < max_retries - 1:
                    time.sleep(3)
            except Exception as e:
                logging.error(f"❌ การเรียก AI หรือ Parse JSON ล้มเหลว: {e}")
                return None
        return None

    def execute_decision(self, decision: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> bool:
        """
        ตรวจสอบเงื่อนไขความปลอดภัยและส่งออเดอร์/จัดการออเดอร์ไปยัง MT5 ผ่าน Remote Bridge
        """
        action = decision.get("action", "HOLD").upper()
        confidence = decision.get("confidence", 0.0)
        reasoning = decision.get("reasoning", "")
        active_trade = ctx.get("active_trade") if ctx else None

        logging.info(f"📊 AI Decision: {action} (Conf: {confidence:.2f}) | Regime: {decision.get('market_regime')}")
        logging.info(f"💡 Rationale: {reasoning}")

        # 1. HOLD หรือ HOLD_POSITION
        if action in ["HOLD", "HOLD_POSITION"]:
            if active_trade:
                logging.info(f"⏸️ AI สั่ง HOLD_POSITION -> ถือออเดอร์ #{active_trade.get('ticket')} ต่อไปตามแผน (PnL: {active_trade.get('pnl_points')} pts)")
            else:
                logging.info("⏸️ Action เป็น HOLD -> สภาวะตลาดยังไม่ชัดเจน ไม่เปิดออเดอร์ใหม่")
            return True

        # 2. CLOSE_POSITION หรือ CLOSE_ALL
        if action in ["CLOSE_POSITION", "CLOSE_ALL"]:
            if active_trade:
                t_id = active_trade.get("ticket")
                logging.info(f"🧹 AI สั่งปิดออเดอร์ Ticket #{t_id} ทันทีเพื่อรักษากำไร/ตัดความเสี่ยง...")
                if self.dry_run:
                    logging.info(f"🧪 [DRY RUN] จำลองปิดออเดอร์ #{t_id} สำเร็จ")
                    return True
                res = self.bridge.close_position(ticket=t_id, symbol=self.symbol)
                if res and res.get("status") == "SUCCESS":
                    logging.info(f"✅ ปิดออเดอร์ #{t_id} สำเร็จ!")
                    return True
                else:
                    logging.error(f"❌ ปิดออเดอร์ล้มเหลว: {res}")
                    return False
            else:
                logging.info("ℹ️ ไม่มีออเดอร์เปิดอยู่ จึงไม่ต้องปิด")
                return True

        # 3. MODIFY_SLTP (ปรับ Trailing Stop หรือเลื่อนไป Breakeven)
        if action == "MODIFY_SLTP":
            if not active_trade:
                logging.warning("⚠️ AI สั่ง MODIFY_SLTP แต่ไม่มีออเดอร์เปิดอยู่")
                return False

            t_id = active_trade.get("ticket")
            dir_pos = active_trade.get("direction")
            cur_sl = active_trade.get("current_sl")
            cur_tp = active_trade.get("current_tp")
            new_sl = decision.get("new_sl")
            new_tp = decision.get("new_tp") or cur_tp

            if new_sl is None:
                logging.warning("⚠️ AI สั่ง MODIFY_SLTP แต่ไม่ได้ระบุ new_sl")
                return False

            new_sl = round(float(new_sl), 2)
            if new_tp is not None:
                new_tp = round(float(new_tp), 2)

            # Safety Guard: ห้ามขยาย Stop Loss (ขยับได้เฉพาะทางที่ลดความเสี่ยงเท่านั้น)
            if cur_sl is not None and cur_sl > 0:
                if dir_pos == "BUY" and new_sl < cur_sl:
                    logging.warning(f"🛡️ Guard Blocked: ห้ามเลื่อน SL กว้างขึ้นสำหรับ BUY ({new_sl} < {cur_sl})")
                    return False
                elif dir_pos == "SELL" and new_sl > cur_sl:
                    logging.warning(f"🛡️ Guard Blocked: ห้ามเลื่อน SL กว้างขึ้นสำหรับ SELL ({new_sl} > {cur_sl})")
                    return False

            logging.info(f"🛡️ ปรับปรุงความเสี่ยงออเดอร์ #{t_id} -> SL ใหม่: {new_sl} (เดิม: {cur_sl}) | TP ใหม่: {new_tp}")
            if self.dry_run:
                logging.info(f"🧪 [DRY RUN] จำลองแก้ไข SL/TP สำเร็จ: #{t_id}")
                return True

            res = self.bridge.modify_position(ticket=t_id, new_sl=new_sl, new_tp=new_tp)
            if res and res.get("status") == "SUCCESS":
                logging.info(f"✅ แก้ไข SL/TP ออเดอร์ #{t_id} สำเร็จ!")
                return True
            else:
                logging.error(f"❌ แก้ไข SL/TP ล้มเหลว: {res}")
                return False

        # 4. BUY หรือ SELL (เปิดออเดอร์ใหม่)
        if action in ["BUY", "SELL"]:
            if active_trade:
                logging.info(f"⚠️ มีออเดอร์ #{active_trade.get('ticket')} เปิดอยู่แล้ว -> ข้ามการเปิดใหม่เพื่อคุมความเสี่ยง")
                return False

            price_info = self.bridge.get_current_price(symbol=self.symbol)
            if not price_info:
                logging.error("❌ ไม่สามารถดึงราคา Realtime เพื่อเปิดออเดอร์ได้")
                return False

            spread_points = price_info.get("spread", 0.0)
            bid = price_info.get("bid", 0.0)
            ask = price_info.get("ask", 0.0)

            if spread_points > self.max_spread_points:
                logging.warning(f"⚠️ Spread สูงเกินเกณฑ์ ({spread_points} > {self.max_spread_points}) -> ข้ามรอบนี้")
                return False

            sl_points = max(self.min_sl_tp_points, min(self.max_sl_tp_points, int(decision.get("sl_points") or 200)))
            tp_points = max(self.min_sl_tp_points, min(self.max_sl_tp_points, int(decision.get("tp_points") or 300)))
            point_value = 0.01

            if action == "BUY":
                entry_price = ask
                sl_price = round(entry_price - (sl_points * point_value), 2)
                tp_price = round(entry_price + (tp_points * point_value), 2)
            else:
                entry_price = bid
                sl_price = round(entry_price + (sl_points * point_value), 2)
                tp_price = round(entry_price - (tp_points * point_value), 2)

            logging.info(f"🛡️ Safety Guard Checked: {action} @ {entry_price} | SL: {sl_price} ({sl_points} pts) | TP: {tp_price} ({tp_points} pts)")

            if self.dry_run:
                logging.info(f"🧪 [DRY RUN] จำลองส่งออเดอร์สำเร็จ: {action} {self.lot_size} lot @ {entry_price}")
                return True

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

        logging.warning(f"⚠️ ไม่รู้จักคำสั่ง Action: {action}")
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

        trade_status_str = f"In Trade #{ctx['active_trade']['ticket']} ({ctx['active_trade']['direction']} {ctx['active_trade']['pnl_points']} pts)" if ctx.get("active_trade") else "FLAT"
        logging.info(f"📈 วิเคราะห์แท่งเทียนที่เพิ่งปิด: {ctx['candle_time']} | ราคาปิด: {ctx['current_price']:.2f} | สถานะ: {trade_status_str}")
        logging.info(f"📊 100-Bar Macro: [{ctx['min_100']:.2f} - {ctx['max_100']:.2f}] (Range: {ctx['range_100']:.2f})")

        decision = self.call_ai_decision(ctx)
        if not decision:
            logging.error("❌ ไม่ได้รับคำตอบจาก AI")
            return False

        self.last_processed_candle_time = ctx["candle_time"]
        success = self.execute_decision(decision, ctx)
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
                minutes_to_add = 5 - (now.minute % 5)
                target_time = (now + timedelta(minutes=minutes_to_add)).replace(second=1, microsecond=0)
                
                wait_seconds = (target_time - now).total_seconds()
                if wait_seconds <= 0:
                    wait_seconds += 300
                    target_time += timedelta(minutes=5)

                wait_m = int(wait_seconds) // 60
                wait_s = int(wait_seconds) % 60
                logging.info(f"⏰ เวลาเครื่อง: {now.strftime('%H:%M:%S')} -> รอบถัดไปคือ {target_time.strftime('%H:%M:%S')} (รออีก {wait_m} นาที {wait_s} วินาที)...")
                
                time.sleep(wait_seconds)
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
