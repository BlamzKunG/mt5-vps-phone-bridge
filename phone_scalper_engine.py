#!/usr/bin/env python3
"""
M5 Price Action Scalping Engine (AI-Driven)
-------------------------------------------
- Market Status Guard:
  * ตรวจสอบว่าตลาดเปิดทำการอยู่หรือไม่ (Weekend, Daily Rollover Break, Stale Tick)
  * หากตลาดปิดทำการ จะข้ามการเรียก AI โดยสิ้นเชิงเพื่อประหยัด Token และป้องกัน Error
- Model Architecture:
  * Primary Model (Tier 1): gpt-6-sol (MaxPlus AI, reasoning_effort: xhigh)
  * Secondary Model (Tier 2): gpt-6.1-sol (หาก 6 sol ขัดข้อง สลับมาตัวนี้)
  * Failover Policy: หากทั้ง 6 sol และ 6.1 sol ใช้ไม่ได้ -> ข้าม (SKIP) รอบนี้ทันที
- Data: 100-bar Normalized Context + 20-bar Detailed M5 OHLCV (NO Balance sent)
- Strategy: Price Action Scalping 100 - 500 points on XAUUSD
- Order Management: Dynamic Trailing Stop, Breakeven, and Early Emergency Close
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
        secondary_model: str = "gpt-6.1-sol",
        reasoning_effort: str = "max",
        symbol: str = "XAUUSD",
        lot_size: float = 0.01,
        max_spread_points: float = 40.0,
        min_sl_tp_points: int = 100,
        max_sl_tp_points: int = 500,
        max_recovery_layers: Optional[int] = None,
        max_recovery_lot: Optional[float] = None,
        min_recovery_distance_points: Optional[float] = None,
        dry_run: bool = False
    ):
        self.vps_url = vps_url or os.getenv("VPS_URL", "http://154.197.116.121:8000")
        bridge_key = bridge_api_key or os.getenv("BRIDGE_API_KEY", "llm-trade-secret-key-2026")
        self.bridge = RemoteMT5Bridge(vps_url=self.vps_url, api_key=bridge_key)
        self.maxplus_api_key = maxplus_api_key or os.getenv("MAXPLUS_API_KEY", "")
        self.model = model
        self.secondary_model = secondary_model
        self.reasoning_effort = reasoning_effort
        self.symbol = symbol
        self.lot_size = lot_size
        self.max_spread_points = max_spread_points
        self.min_sl_tp_points = min_sl_tp_points
        self.max_sl_tp_points = max_sl_tp_points
        self.max_recovery_layers = max_recovery_layers if max_recovery_layers is not None else int(os.getenv("MAX_RECOVERY_LAYERS", "20"))
        self.max_recovery_lot = max_recovery_lot if max_recovery_lot is not None else float(os.getenv("MAX_RECOVERY_LOT", "0.05"))
        self.min_recovery_distance_points = min_recovery_distance_points if min_recovery_distance_points is not None else float(os.getenv("MIN_RECOVERY_GAP", "80.0"))
        self.dry_run = dry_run

        self.last_processed_candle_time: Optional[str] = None
        logging.info("🚀 Price Action Scalper Engine Initialized")
        logging.info(f"Target: {self.symbol} | Primary: {self.model} | Secondary: {self.secondary_model} | Failover: Skip")
        logging.info(f"Scalp Bounds: [{self.min_sl_tp_points}, {self.max_sl_tp_points}] points | Dry Run: {self.dry_run}")
        logging.info("🎯 Mode: Dual-Engine [Magic 123456 (Scalp LLM) + Magic 1212 (Recovery LLM - No SL)]")
        logging.info(f"🛡️ Recovery Config: Max {self.max_recovery_layers} Layers | Max Lot {self.max_recovery_lot} | Min Gap {self.min_recovery_distance_points} pts")

    def is_market_open(self) -> Tuple[bool, str]:
        """
        ตรวจสอบว่าตลาดเปิดทำการอยู่หรือไม่
        1. ตรวจสอบปฏิทินเวลาทำการ (Forex / Gold Market Hours):
           - วันเสาร์: ปิดทำการทั้งวัน
           - วันอาทิตย์: ปิดทำการจนถึง ~22:00 UTC
           - คืนวันศุกร์: ปิดทำการหลัง 22:00 UTC
           - ช่วงพักประจำวัน (Daily Rollover Break): วันจันทร์-ศุกร์ ~21:00 - 22:00 UTC สำหรับ XAUUSD
        2. ตรวจสอบความสดของ Tick ล่าสุด (Tick Freshness):
           - หาก Tick ล่าสุดจาก MT5 ห่างจากปัจจุบันเกิน 15 นาที (900 วินาที) แสดงว่าตลาดปิดหรือหยุดพักการซื้อขาย
        """
        now_utc = datetime.now(timezone.utc)
        weekday = now_utc.weekday() # 0=Mon, ..., 4=Fri, 5=Sat, 6=Sun
        hour = now_utc.hour

        # 1. เช็ควันหยุดสุดสัปดาห์ (Weekend Check)
        if weekday == 5:
            return False, "วันเสาร์ (Weekend Closure)"
        if weekday == 6 and hour < 22:
            time_str = now_utc.strftime("%H:%M")
            return False, f"วันอาทิตย์ก่อนเปิดตลาด (Sunday {time_str} UTC < 22:00 UTC)"
        if weekday == 4 and hour >= 22:
            return False, "คืนวันศุกร์ตลาดปิดทำการแล้ว (Friday Post-Market)"

        # 2. เช็คช่วงพักประจำวันของตลาดทองคำ (~21:00-22:00 UTC)
        if "XAU" in self.symbol.upper() or "GOLD" in self.symbol.upper():
            if hour == 21:
                return False, "ช่วงพักประจำวันของตลาดทองคำ (Daily Rollover Break ~21:00-22:00 UTC)"

        # 3. เช็คความสดของ Tick จาก MT5
        try:
            price_info = self.bridge.get_current_price(symbol=self.symbol)
            if not price_info:
                return False, "ไม่สามารถเชื่อมต่อดึงราคา Realtime จาก MT5 ได้"

            tick_time = price_info.get("time")
            if tick_time:
                diff_seconds = now_utc.timestamp() - tick_time
                if diff_seconds > 900: # เกิน 15 นาที
                    diff_hours = diff_seconds / 3600
                    return False, f"Tick ล่าสุดหยุดนิ่งเกิน {diff_hours:.1f} ชม. (ตลาดปิดหรือหยุดพักการซื้อขาย)"
        except Exception as e:
            logging.warning(f"⚠️ ตรวจสอบ Tick Freshness ไม่สำเร็จ: {e}")

        return True, "ตลาดเปิดทำการปกติ (Market Open)"

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

            # 3. ตรวจสอบ Active Positions สำหรับ Magic 123456 (Scalp) และ Magic 1212 (Recovery)
            active_trade_info = None
            recovery_basket_info = None
            try:
                if hasattr(self.bridge, "get_positions"):
                    open_positions = self.bridge.get_positions(symbol=self.symbol)
                else:
                    open_positions = self.bridge.get_open_positions(symbol=self.symbol)
                
                # 3.1 ค้นหา Position สำหรับ Magic 123456 (LLM Autonomous Scalp)
                llm_positions = [p for p in open_positions if p.get("magic") == 123456]
                if llm_positions:
                    pos = llm_positions[0]
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
                        "magic": 123456,
                        "direction": dir_pos,
                        "lot": float(pos.get("lot", self.lot_size)),
                        "entry_price": entry_p,
                        "current_price": current_close,
                        "current_sl": cur_sl,
                        "current_tp": cur_tp,
                        "pnl_points": pnl_points
                    }

                # 3.2 ค้นหา Basket Positions สำหรับ Magic 1212 (Recovery Engine - No SL)
                rec_positions = [p for p in open_positions if p.get("magic") == 1212]
                if rec_positions:
                    rec_dir = rec_positions[0].get("direction", "BUY").upper()
                    tot_lot = round(sum(float(p.get("lot", self.lot_size)) for p in rec_positions), 2)
                    weighted_sum = sum(float(p.get("entry_price", current_close)) * float(p.get("lot", self.lot_size)) for p in rec_positions)
                    avg_entry = round(weighted_sum / tot_lot, 2) if tot_lot > 0 else current_close

                    if rec_dir == "BUY":
                        basket_pnl_pts = round((current_close - avg_entry) / 0.01, 1)
                    else:
                        basket_pnl_pts = round((avg_entry - current_close) / 0.01, 1)

                    layers_list = []
                    for idx, p in enumerate(rec_positions, 1):
                        p_entry = float(p.get("entry_price", current_close))
                        p_dir = p.get("direction", "BUY").upper()
                        lyr_pnl = round(((current_close - p_entry) if p_dir == "BUY" else (p_entry - current_close)) / 0.01, 1)
                        layers_list.append({
                            "layer": idx,
                            "ticket": p.get("ticket") or p.get("id"),
                            "lot": float(p.get("lot", self.lot_size)),
                            "entry_price": p_entry,
                            "current_tp": float(p.get("tp", 0.0)) if p.get("tp") is not None else None,
                            "pnl_points": lyr_pnl
                        })

                    recovery_basket_info = {
                        "magic": 1212,
                        "direction": rec_dir,
                        "layers_count": len(rec_positions),
                        "total_lot": tot_lot,
                        "avg_entry": avg_entry,
                        "current_price": current_close,
                        "basket_pnl_points": basket_pnl_pts,
                        "layers": layers_list
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
                "active_trade": active_trade_info,
                "recovery_basket": recovery_basket_info,
                "all_positions": open_positions if 'open_positions' in locals() else []
            }
        except Exception as e:
            logging.error(f"❌ เกิดข้อผิดพลาดในการเตรียม Context ข้อมูล: {e}")
            return None

    def _call_single_model(
        self,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: int,
        reasoning_effort: Optional[str] = "xhigh",
        tag: str = ""
    ) -> Optional[Dict[str, Any]]:
        """ยิงคำขอไปยังโมเดลที่กำหนดและแปลง JSON กลับมา"""
        headers = {
            "Authorization": f"Bearer {self.maxplus_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        }
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort

        t0 = time.time()
        try:
            resp = requests.post(
                "https://api.maxplus-ai.cc/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout_seconds
            )
            elapsed = time.time() - t0
            if resp.status_code != 200:
                logging.warning(f"⚠️ {tag or model_name} ส่งรหัส: HTTP {resp.status_code} - {resp.text[:150]}")
                return None

            data = resp.json()
            raw_content = data["choices"][0]["message"]["content"].strip()
            clean_json = raw_content.strip("```json").strip("```").strip()
            decision = json.loads(clean_json)
            decision["latency_seconds"] = round(elapsed, 2)
            decision["usage"] = data.get("usage", {})
            decision["raw_model"] = model_name
            return decision
        except Exception as e:
            logging.warning(f"⚠️ {tag or model_name} call/parse failed: {e}")
            return None

    def call_ai_decision(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        ส่งข้อมูลแท่งเทียนให้ AI ประมวลผล:
        - Primary Model: gpt-6-sol (effort: xhigh)
        - Secondary Fallback: gpt-6.1-sol (หาก 6 sol ขัดข้อง)
        - Failover Policy: หากทั้ง 6 sol และ 6.1 sol ใช้ไม่ได้ -> ข้าม (SKIP) รอบนี้ทันที
        """
        now = datetime.now()
        minutes_to_add = 5 - (now.minute % 5)
        target_time = (now + timedelta(minutes=minutes_to_add)).replace(second=1, microsecond=0)
        seconds_remaining = (target_time - now).total_seconds()
        timeout_seconds = max(90, min(270, int(seconds_remaining) - 15))

        system_prompt = """You are an elite Institutional Quantitative Price Action Scalper and Dual-Engine Risk Manager for Gold (XAUUSD M5).
Your objective is to capture high-probability intraday scalps of 100 to 500 points (1.00 to 5.00 USD) and intelligently manage adverse drawdowns via a disciplined recovery engine.

PORTFOLIO & DUAL-ENGINE ARCHITECTURE:
You manage two parallel execution engines on the same account:
1. Engine A (Magic 123456 - Autonomous Scalper):
   - Strict risk control with hard Stop Loss (100 to 500 points) and Take Profit (100 to 500 points).
   - If in trade, managed via "HOLD_POSITION", "MODIFY_SLTP" (tighten SL / Breakeven), or "CLOSE_POSITION".
2. Engine B (Magic 1212 - Dynamic Full Recovery / "ระบบแก้ไม้เต็มรูปแบบ - ห้ามคัทลอส"):
   - Opened simultaneously with Engine A upon entry, but has NO Hard Stop Loss (sl = 0).
   - NEVER CUT LOSS AT A DRAWDOWN. Even if Engine A gets stopped out, Engine B remains alive and in play.
   - Magic 1212 is a dedicated recovery basket capable of holding up to 20 recovery layers (Layer 1 to 20).
   - At each closed M5 bar, evaluate whether the market structure justifies opening a recovery order ("เปิดไม้แก้") and whether to increase lot size ("เพิ่ม lot" up to 0.05).
   - STRICT MANDATE: NEVER surrender or close Magic 1212 at a loss! Your mission is to guide the basket until it turns profitable.

CORE METHODOLOGY:
1. Macro Context (100-bar Normalized Curve [0.00 to 1.00]):
   - Overextended Extremes (>0.85 or <0.15): Look for liquidity sweep exhaustion or sharp mean reversion.
   - Equilibrium Band (0.40 - 0.60): Trend continuation if backed by high-volume displacement, else avoid low-volume chop.
2. Micro Price Action (20-bar Detailed OHLCV):
   - Liquidity Sweeps & Wick Rejections: Wicks >= 40% of candle range piercing swing highs/lows and closing back inside.
   - Market Structure Shift (MSS/CHoCH): Aggressive displacement breaking opposing swing points.
   - Volume Confirmation: High tick volume confirming institutional order flow vs low-volume fakeouts.

DECISION PROTOCOLS:

A. When FLAT (No Active Positions):
   - If clear confluence emerges: Output "BUY" or "SELL".
   - sl_points: 100 to 500 points beyond structural invalidation (applied to Magic 123456).
   - tp_points: 100 to 500 points targeting opposing liquidity (applied to both 123456 and 1212).
   - recovery_action: "NONE".

B. When Magic 123456 is IN POSITION:
   - "HOLD_POSITION": Thesis remains intact; normal pullback or progressing toward TP.
   - "MODIFY_SLTP": Active risk protection:
     * If floating profit >= 150 points: Move SL to Breakeven (+20 pts).
     * If new swing pivot formed: Trail SL behind newest M5 swing low (BUY) or swing high (SELL).
     * Rule: ONLY tighten SL (lower risk). NEVER widen SL.
   - "CLOSE_POSITION": Early emergency exit for 123456 upon adverse liquidity sweep / opposing CHoCH.

C. When Magic 1212 Recovery Basket is ACTIVE (Evaluate "แก้ไม้เต็มรูปแบบ - ห้ามคัทลอส"):
   - "NONE": When no 1212 basket exists.
   - "HOLD": 1212 basket is running and in negative drawdown; wait patiently. Do NOT add a layer if adverse momentum is still explosive without structural exhaustion. NEVER close 1212 at a loss!
   - "ADD_RECOVERY_LAYER": High-confluence reversal zone reached (e.g. key liquidity sweep, opposing order block, rejection pin bar).
     * System supports up to 20 recovery layers (Layer 1 to 20).
     * recovery_lot: Evaluate whether to keep same lot (0.01) or increase lot (0.02 to 0.05) to pull the basket break-even price closer.
   - "CLOSE_RECOVERY_BASKET": PROFIT TAKE ONLY! Only permitted if basket_pnl_points > 0 (net profit) and you want to lock in basket profit early. STRICTLY FORBIDDEN to use when basket is at a loss!

OUTPUT FORMAT REQUIREMENTS:
Return PURE JSON ONLY (no markdown code blocks, no backticks, no conversational filler):
{
  "action": "BUY" | "SELL" | "HOLD" | "HOLD_POSITION" | "MODIFY_SLTP" | "CLOSE_POSITION",
  "confidence": <float 0.0 to 1.0>,
  "sl_points": <int 100 to 500 or null>,
  "tp_points": <int 100 to 500 or null>,
  "new_sl": <float or null>,
  "new_tp": <float or null>,
  "recovery_action": "NONE" | "HOLD" | "ADD_RECOVERY_LAYER" | "CLOSE_RECOVERY_BASKET",
  "recovery_lot": <float 0.01 to 0.05 or null>,
  "market_regime": "<concise regime description>",
  "reasoning": "<structured quantitative rationale: 1) 100-bar macro, 2) 20-bar candle action, 3) 123456 scalp management, 4) 1212 recovery evaluation (up to 20 layers, zero cut-loss policy) and lot sizing>"
}"""

        active_trade_str = json.dumps(ctx.get("active_trade"), indent=1) if ctx.get("active_trade") else "FLAT (No Open Positions)"
        recovery_basket_str = json.dumps(ctx.get("recovery_basket"), indent=1) if ctx.get("recovery_basket") else "FLAT (No Open Basket)"

        user_prompt = f"""Asset: {self.symbol} (M5)
Completed Candle Timestamp: {ctx['candle_time']}
Latest Closed Price: {ctx['current_price']:.2f}

[PORTFOLIO & POSITION STATUS]
- Magic 123456 (Autonomous Scalp):
{active_trade_str}

- Magic 1212 (Recovery Engine - No SL):
{recovery_basket_str}

[100-Bar Macro Context]
- 100-Bar Low: {ctx['min_100']:.2f}
- 100-Bar High: {ctx['max_100']:.2f}
- 100-Bar Normalized Closes (0.00 to 1.00, oldest to newest):
{ctx['norm_closes']}

[20-Bar Detailed M5 OHLCV (Bars -20 to -1)]
{json.dumps(ctx['detailed_20'], separators=(',', ':'))}

Analyze the market structure, evaluate trade health (if in position), setup confluence (if flat), and assess Magic 1212 recovery action & lot sizing. Output your pure JSON decision:"""

        # -------------------------------------------------------------
        # STEP 1: ลองเรียก Primary Model (gpt-6-sol) ก่อน
        # -------------------------------------------------------------
        logging.info(f"⏳ [Primary Model] เรียก {self.model} (Dynamic Timeout: {timeout_seconds}s)...")
        sol6_decision = self._call_single_model(
            model_name=self.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout_seconds=max(60, min(timeout_seconds, 90)),
            reasoning_effort=self.reasoning_effort,
            tag=f"Primary-{self.model}"
        )

        if sol6_decision:
            sol6_decision["model_used"] = self.model
            sol6_decision["tier"] = 1
            logging.info(f"✅ สำเร็จ: ได้รับคำตอบจาก Primary Model ({self.model})")
            return sol6_decision

        # -------------------------------------------------------------
        # STEP 2: หาก 6 sol ขัดข้อง สลับไปเรียก Secondary Model (gpt-6.1-sol)
        # -------------------------------------------------------------
        logging.warning("=" * 65)
        logging.warning(f"⚠️ [PRIMARY FAILED] {self.model} ขัดข้องหรือไม่ตอบสนอง!")
        logging.warning(f"🔄 [Secondary Fallback] สลับไปเรียก {self.secondary_model}...")
        logging.warning("=" * 65)

        now_t2 = datetime.now()
        seconds_left_t2 = max(45, int((target_time - now_t2).total_seconds()) - 10)

        sol61_decision = self._call_single_model(
            model_name=self.secondary_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout_seconds=max(45, min(seconds_left_t2, 75)),
            reasoning_effort=self.reasoning_effort,
            tag=f"Secondary-{self.secondary_model}"
        )

        if sol61_decision:
            sol61_decision["model_used"] = f"{self.secondary_model} (Fallback)"
            sol61_decision["tier"] = 2
            logging.info(f"✅ สำเร็จ: ได้รับคำตอบจาก Secondary Model ({self.secondary_model})")
            return sol61_decision

        # -------------------------------------------------------------
        # STEP 3: หากทั้งสองตัวใช้ไม่ได้ -> SKIP ทันทีตามคำสั่งผู้ใช้
        # -------------------------------------------------------------
        logging.warning("=" * 65)
        logging.warning(f"🚨 [ALL TIERS FAILED] ทั้ง {self.model} และ {self.secondary_model} ใช้งานไม่ได้!")
        logging.warning("⏭️ มาตรการระบุ: หากทั้ง 6 sol และ 6.1 sol ใช้ไม่ได้ ให้ SKIP รอบนี้ทันที ไม่เปิดออเดอร์ใหม่")
        logging.warning("=" * 65)

        is_in_trade = bool(ctx.get("active_trade"))
        safe_action = "HOLD_POSITION" if is_in_trade else "HOLD"
        return {
            "action": safe_action,
            "confidence": 0.0,
            "sl_points": None,
            "tp_points": None,
            "new_sl": None,
            "new_tp": None,
            "market_regime": "All Models Unavailable - Cycle Skipped",
            "reasoning": f"Both primary ({self.model}) and secondary ({self.secondary_model}) models failed to respond. Protocol skipped this cycle to ensure account safety.",
            "model_used": "All Models Failed -> Skipped",
            "tier": 0
        }

    def execute_decision(self, decision: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> bool:
        """
        ตรวจสอบเงื่อนไขความปลอดภัยและส่งออเดอร์/จัดการออเดอร์ไปยัง MT5 ผ่าน Remote Bridge:
        - Engine A: Magic 123456 (Autonomous Scalp พร้อม SL/TP)
        - Engine B: Magic 1212 (Recovery Engine ไม่มี SL บริหารแก้ไม้/เพิ่ม lot โดย LLM)
        """
        action = decision.get("action", "HOLD").upper()
        confidence = decision.get("confidence", 0.0)
        reasoning = decision.get("reasoning", "")
        model_used = decision.get("model_used", self.model)
        active_trade = ctx.get("active_trade") if ctx else None
        recovery_basket = ctx.get("recovery_basket") if ctx else None

        rec_act_log = f" | RecAction: {decision.get('recovery_action')} (Lot: {decision.get('recovery_lot')})" if decision.get("recovery_action") else ""
        logging.info(f"📊 AI Decision: {action} (Conf: {confidence:.2f}){rec_act_log} | Model: {model_used} | Regime: {decision.get('market_regime')}")
        logging.info(f"💡 Rationale: {reasoning}")

        # -------------------------------------------------------------
        # STEP 1: จัดการคำสั่ง CLOSE_ALL หรือ CLOSE_POSITION
        # -------------------------------------------------------------
        if action == "CLOSE_ALL":
            logging.info("🧹 ได้รับคำสั่ง CLOSE_ALL -> ปิดทุกออเดอร์ที่เปิดอยู่ทั้งหมด...")
            all_pos = ctx.get("all_positions", []) if ctx else []
            closed_count = 0
            for p in all_pos:
                p_ticket = p.get("ticket") or p.get("id")
                if p_ticket:
                    res = self.bridge.close_position(ticket=p_ticket, symbol=self.symbol)
                    if res and res.get("status") == "SUCCESS":
                        closed_count += 1
            logging.info(f"✅ สั่งปิดเรียบร้อย {closed_count} ออเดอร์")
            return True

        if action == "CLOSE_POSITION":
            if active_trade:
                t_id = active_trade.get("ticket")
                logging.info(f"🧹 AI สั่งปิดออเดอร์ Magic 123456 (Ticket #{t_id}) เพื่อรักษากำไร/ตัดความเสี่ยง...")
                if self.dry_run:
                    logging.info(f"🧪 [DRY RUN] จำลองปิดออเดอร์ #{t_id} สำเร็จ")
                else:
                    res = self.bridge.close_position(ticket=t_id, symbol=self.symbol)
                    if res and res.get("status") == "SUCCESS":
                        logging.info(f"✅ ปิดออเดอร์ Magic 123456 #{t_id} สำเร็จ!")
                    else:
                        logging.error(f"❌ ปิดออเดอร์ Magic 123456 ล้มเหลว: {res}")
            else:
                logging.info("ℹ️ ไม่มีออเดอร์ Magic 123456 เปิดอยู่ จึงไม่ต้องปิด")

        # -------------------------------------------------------------
        # STEP 2: จัดการคำสั่ง MODIFY_SLTP สำหรับ Magic 123456
        # -------------------------------------------------------------
        if action == "MODIFY_SLTP":
            if not active_trade:
                logging.warning("⚠️ AI สั่ง MODIFY_SLTP แต่ไม่มีออเดอร์ Magic 123456 เปิดอยู่")
            else:
                t_id = active_trade.get("ticket")
                dir_pos = active_trade.get("direction")
                cur_sl = active_trade.get("current_sl")
                cur_tp = active_trade.get("current_tp")
                new_sl = decision.get("new_sl")
                new_tp = decision.get("new_tp") or cur_tp

                if new_sl is None:
                    logging.warning("⚠️ AI สั่ง MODIFY_SLTP แต่ไม่ได้ระบุ new_sl")
                else:
                    new_sl = round(float(new_sl), 2)
                    if new_tp is not None:
                        new_tp = round(float(new_tp), 2)

                    # Safety Guard: ห้ามขยาย Stop Loss (ขยับได้เฉพาะทางที่ลดความเสี่ยงเท่านั้น)
                    blocked = False
                    if cur_sl is not None and cur_sl > 0:
                        if dir_pos == "BUY" and new_sl < cur_sl:
                            logging.warning(f"🛡️ Guard Blocked: ห้ามเลื่อน SL กว้างขึ้นสำหรับ BUY ({new_sl} < {cur_sl})")
                            blocked = True
                        elif dir_pos == "SELL" and new_sl > cur_sl:
                            logging.warning(f"🛡️ Guard Blocked: ห้ามเลื่อน SL กว้างขึ้นสำหรับ SELL ({new_sl} > {cur_sl})")
                            blocked = True

                    if not blocked:
                        logging.info(f"🛡️ [Magic 123456 LLM] ปรับ SL/TP ออเดอร์ #{t_id} -> SL ใหม่: {new_sl} (เดิม: {cur_sl}) | TP ใหม่: {new_tp}")
                        if self.dry_run:
                            logging.info(f"🧪 [DRY RUN] จำลองแก้ไข SL/TP สำเร็จ: #{t_id}")
                        else:
                            res = self.bridge.modify_position(ticket=t_id, new_sl=new_sl, new_tp=new_tp)
                            if res and res.get("status") == "SUCCESS":
                                logging.info(f"✅ แก้ไข SL/TP ออเดอร์ #{t_id} สำเร็จ!")
                            else:
                                logging.error(f"❌ แก้ไข SL/TP ล้มเหลว: {res}")

        # -------------------------------------------------------------
        # STEP 3: เปิดออเดอร์เริ่มต้นคู่ (Dual Entry: Magic 123456 + Magic 1212)
        # -------------------------------------------------------------
        if action in ["BUY", "SELL"]:
            if active_trade or recovery_basket:
                logging.info(f"⚠️ มีออเดอร์ในระบบเปิดอยู่แล้ว (123456: {bool(active_trade)}, 1212: {bool(recovery_basket)}) -> ข้ามการเปิดใหม่เพื่อคุมความเสี่ยง")
            else:
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

                comment_123456 = f"Scalp-{action[:1]}-{sl_points}/{tp_points}"[:27]
                comment_1212 = f"Rec-{action[:1]}-L1-0/{tp_points}"[:27]

                if self.dry_run:
                    logging.info(f"🧪 [DRY RUN] จำลองส่งออเดอร์คู่: Magic 123456 (SL: {sl_price}) + Magic 1212 (NO SL | TP: {tp_price})")
                    return True

                # ส่งคำสั่งไม้ 1: Magic 123456 (Scalp ปกติ มี SL)
                res_123456 = self.bridge.send_order(
                    action=action,
                    symbol=self.symbol,
                    volume=self.lot_size,
                    sl=sl_price,
                    tp=tp_price,
                    magic=123456,
                    comment=comment_123456
                )

                # ส่งคำสั่งไม้ 2: Magic 1212 (Duplicate ไม้ 123456 แต่ไม่มี SL - แก้ไม้โดย LLM)
                res_1212 = self.bridge.send_order(
                    action=action,
                    symbol=self.symbol,
                    volume=self.lot_size,
                    sl=None,
                    tp=tp_price,
                    magic=1212,
                    comment=comment_1212
                )

                t1 = res_123456.get("ticket") if res_123456 else "FAILED"
                t2 = res_1212.get("ticket") if res_1212 else "FAILED"
                logging.info(f"🎯 [Dual Entry Success] Magic 123456 Ticket #{t1} (SL={sl_price}) | Magic 1212 Ticket #{t2} (NO SL | TP={tp_price})")
                return True

        if action in ["HOLD", "HOLD_POSITION"]:
            if active_trade:
                logging.info(f"⏸️ AI สั่ง HOLD_POSITION -> ถือออเดอร์ Magic 123456 #{active_trade.get('ticket')} (PnL: {active_trade.get('pnl_points')} pts)")
            else:
                logging.info("⏸️ Action เป็น HOLD สำหรับ Magic 123456")

        # -------------------------------------------------------------
        # STEP 4: ระบบประเมินและบริหารแก้ไม้สำหรับ Magic 1212 (Recovery Engine)
        # -------------------------------------------------------------
        rec_action = str(decision.get("recovery_action", "NONE")).upper()

        if rec_action == "CLOSE_RECOVERY_BASKET":
            if recovery_basket:
                basket_pnl_pts = float(recovery_basket.get("basket_pnl_points", 0.0))
                # 🛡️ HARD SAFETY GUARD: Magic 1212 คือระบบแก้ไม้เต็มรูปแบบ ห้ามคัทลอสตอนติดลบเด็ดขาด!
                if basket_pnl_pts <= 0:
                    logging.warning(
                        f"🛡️ Safety Guard Blocked: AI สั่ง CLOSE_RECOVERY_BASKET แต่ตระกร้า Magic 1212 ยังติดลบ ({basket_pnl_pts} pts) "
                        f"-> ระบบแก้ไม้เต็มรูปแบบไม่อนุญาตให้คัทลอส! บังคับ HOLD เพื่อรอจังหวะแก้ไม้ทำกำไร"
                    )
                else:
                    layers = recovery_basket.get("layers", [])
                    logging.info(f"💰 AI สั่ง CLOSE_RECOVERY_BASKET เพื่อเก็บกำไรรวม {basket_pnl_pts} pts -> กำลังปิดรวบ Magic 1212 ทั้งหมด {len(layers)} ไม้...")
                    closed_count = 0
                    for lyr in layers:
                        t_id = lyr.get("ticket")
                        if t_id:
                            if self.dry_run:
                                logging.info(f"🧪 [DRY RUN] จำลองปิดไม้แก้ Magic 1212 Ticket #{t_id}")
                                closed_count += 1
                            else:
                                res = self.bridge.close_position(ticket=t_id, symbol=self.symbol)
                                if res and res.get("status") == "SUCCESS":
                                    closed_count += 1
                    logging.info(f"✅ ปิดรวบกำไร Magic 1212 สำเร็จ: {closed_count}/{len(layers)} ไม้ (Net PnL: +{basket_pnl_pts} pts)")
            else:
                logging.info("ℹ️ ไม่มีออเดอร์ Magic 1212 ค้างอยู่ จึงไม่ต้องปิด")

        elif rec_action == "ADD_RECOVERY_LAYER":
            if not recovery_basket:
                logging.warning("⚠️ AI สั่ง ADD_RECOVERY_LAYER แต่ไม่มีตะกร้าไม้ Magic 1212 เปิดอยู่")
            else:
                layers = recovery_basket.get("layers", [])
                curr_layers = len(layers)
                if curr_layers >= self.max_recovery_layers:
                    logging.warning(f"🛡️ Safety Guard Blocked: ตะกร้า Magic 1212 มีไม้แก้เต็มโควตาแล้ว ({curr_layers}/{self.max_recovery_layers}) ห้ามเปิดเพิ่ม")
                else:
                    last_layer_entry = layers[-1]["entry_price"]
                    price_info = self.bridge.get_current_price(symbol=self.symbol)
                    bid = price_info.get("bid", 0.0) if price_info else 0.0
                    ask = price_info.get("ask", 0.0) if price_info else 0.0
                    basket_dir = recovery_basket.get("direction", "BUY").upper()
                    exec_price = ask if basket_dir == "BUY" else bid

                    dist_points = abs(exec_price - last_layer_entry) / 0.01
                    if dist_points < self.min_recovery_distance_points:
                        logging.warning(f"🛡️ Safety Guard Blocked: ราคาห่างจากไม้แก้ล่าสุดไม่ถึงเกณฑ์ ({dist_points:.1f} pts < {self.min_recovery_distance_points} pts) -> ชะลอการเปิดไม้แก้")
                    else:
                        rec_lot_req = decision.get("recovery_lot")
                        try:
                            rec_lot_val = float(rec_lot_req) if rec_lot_req is not None else self.lot_size
                        except Exception:
                            rec_lot_val = self.lot_size
                        
                        final_rec_lot = round(max(self.lot_size, min(self.max_recovery_lot, rec_lot_val)), 2)
                        next_layer_num = curr_layers + 1
                        comment = f"Rec-{basket_dir[:1]}-L{next_layer_num}-{final_rec_lot}"[:27]
                        basket_tp = layers[0].get("current_tp")

                        logging.info(f"💡 [LLM Recovery Execution] เปิดไม้แก้ Layer {next_layer_num}: {basket_dir} {final_rec_lot} lot @ {exec_price} (NO SL | TP: {basket_tp})")
                        if self.dry_run:
                            logging.info(f"🧪 [DRY RUN] จำลองเปิดไม้แก้สำเร็จ: Layer {next_layer_num} ({final_rec_lot} lot)")
                        else:
                            res = self.bridge.send_order(
                                action=basket_dir,
                                symbol=self.symbol,
                                volume=final_rec_lot,
                                sl=None,
                                tp=basket_tp,
                                magic=1212,
                                comment=comment
                            )
                            if res and res.get("status") == "SUCCESS":
                                logging.info(f"✅ เปิดไม้แก้ Magic 1212 Layer {next_layer_num} สำเร็จ! Ticket #{res.get('ticket')}")
                            else:
                                logging.error(f"❌ เปิดไม้แก้ Magic 1212 ล้มเหลว: {res}")

        elif rec_action == "HOLD":
            if recovery_basket:
                logging.info(f"⏸️ AI สั่ง HOLD ไม้แก้ Magic 1212 ({recovery_basket.get('layers_count')} layers, PnL: {recovery_basket.get('basket_pnl_points')} pts) -> รอโครงสร้างราคาคลี่คลาย")

        return True

    def run_single_cycle(self) -> bool:
        """
        ทำงาน 1 รอบวัฏจักร (Single Cycle)
        """
        local_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info("=" * 60)
        logging.info(f"🔄 เริ่มรอบการทำงาน: {local_time_str} (เวลาเครื่อง)")

        # 1. ตรวจสอบว่าตลาดเปิดทำการอยู่หรือไม่
        is_open, market_reason = self.is_market_open()
        if not is_open:
            logging.info(f"⏸️ [MARKET CLOSED] ตลาด {self.symbol} ปิดทำการ ({market_reason})")
            logging.info("💤 ข้ามการเรียก AI ในรอบนี้เพื่อประหยัดเครดิตและป้องกันข้อผิดพลาด")
            return False

        # 2. เตรียม Context ข้อมูลตลาด
        ctx = self.prepare_market_context()
        if not ctx:
            logging.warning("⚠️ ข้อมูลไม่พร้อม ข้ามรอบนี้")
            return False

        if ctx["candle_time"] == self.last_processed_candle_time:
            logging.info(f"ℹ️ แท่งเทียน {ctx['candle_time']} ได้รับการประมวลผลไปแล้ว")
            return False

        trade_status_str = f"In Trade #{ctx['active_trade']['ticket']} ({ctx['active_trade']['direction']} {ctx['active_trade']['pnl_points']} pts)" if ctx.get("active_trade") else "FLAT"
        rec_status_str = f"Basket {ctx['recovery_basket']['direction']} ({ctx['recovery_basket']['layers_count']}L, {ctx['recovery_basket']['total_lot']} lot, avg: {ctx['recovery_basket']['avg_entry']}, PnL: {ctx['recovery_basket']['basket_pnl_points']} pts)" if ctx.get("recovery_basket") else "FLAT"
        logging.info(f"📈 วิเคราะห์แท่ง M5 ปิด: {ctx['candle_time']} | ราคาปิด: {ctx['current_price']:.2f}")
        logging.info(f"💼 สถานะพอร์ต: [Magic 123456: {trade_status_str}] | [Magic 1212: {rec_status_str}]")
        logging.info(f"📊 100-Bar Macro: [{ctx['min_100']:.2f} - {ctx['max_100']:.2f}] (Range: {ctx['range_100']:.2f})")

        decision = self.call_ai_decision(ctx)
        if not decision:
            logging.error("❌ ไม่ได้รับคำตอบจาก AI")
            return False

        self.last_processed_candle_time = ctx["candle_time"]
        return self.execute_decision(decision, ctx)

    def run_forever(self, run_immediately: bool = True):
        """
        รันระบบอัตโนมัติต่อเนื่อง 24/7 โดยซิงค์ตามเวลาเครื่องโทรศัพท์ทุก 5 นาทีเป๊ะ
        เช่น 01:00, 01:05, 01:10, 01:15, ... (+ 1 วินาที buffer ให้แท่งปิดสมบูรณ์)
        """
        logging.info("🎯 เข้าสู่โหมดรันอัตโนมัติ 24/7 (ซิงค์เวลาโทรศัพท์ทุก 5 นาที | Dual-Engine: 123456 + 1212)")
        
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
