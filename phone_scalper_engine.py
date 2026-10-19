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

# ตารางนโยบายบริหารออเดอร์พร้อมกันหลาย Magic Number (Multi-Order Matrix A/B/C Testing)
ORDER_POLICIES = {
    123456: {
        "name": "LLM Autonomous",
        "tag": "LLM",
        "description": "คำสั่งอิสระโดย AI LLM ทั้งหมด (ถือ, ปรับ SL, ปิดฉุกเฉิน)",
        "tp_extended": False,
    },
    2222: {
        "name": "50% TP Breakeven",
        "tag": "BE50",
        "description": "เลื่อน SL ไป BE (+20 pts) เมื่อกำไร >= 50% ของ TP",
        "tp_extended": False,
    },
    3333: {
        "name": "Fast Step Trailing",
        "tag": "FastTrail",
        "description": "Step Trail เริ่มที่ 100 pts ขยับก้าวละ 50 pts",
        "tp_extended": False,
    },
    4444: {
        "name": "ATR Dynamic Volatility",
        "tag": "ATRTrail",
        "description": "Trailing SL ที่ระยะ 1.5x ATR(14) ของกรอบ M5",
        "tp_extended": False,
    },
    6666: {
        "name": "Structural Swing Pivot",
        "tag": "SwingTrail",
        "description": "Trailing SL ตามปลายไส้แท่ง M5 ล่าสุด (Low BUY / High SELL)",
        "tp_extended": False,
    },
    7777: {
        "name": "Asymmetric Big-Runner",
        "tag": "BigRunner",
        "description": "TP ขยาย 800 pts, เลื่อน BE ที่ 50% TP เดิม, ล็อค 200 pts ที่ 400 pts",
        "tp_extended": True,
    }
}

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
        self.dry_run = dry_run

        self.order_policies = ORDER_POLICIES
        self.last_context: Optional[Dict[str, Any]] = None
        self.last_processed_candle_time: Optional[str] = None
        logging.info("🚀 Price Action Scalper Engine Initialized")
        logging.info(f"Target: {self.symbol} | Primary: {self.model} | Secondary: {self.secondary_model} | Failover: Skip")
        logging.info(f"Scalp Bounds: [{self.min_sl_tp_points}, {self.max_sl_tp_points}] points | Dry Run: {self.dry_run}")
        logging.info(f"Multi-Order Matrix Enabled: {list(self.order_policies.keys())}")

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

            # คำนวณ ATR 14 บนแท่งเทียน M5 ที่ปิดแล้ว
            highs = completed_df["high"].values
            lows = completed_df["low"].values
            closes = completed_df["close"].values
            tr_list = []
            for i in range(1, len(completed_df)):
                tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                tr_list.append(tr)
            atr_14 = float(pd.Series(tr_list[-14:]).mean()) if len(tr_list) >= 14 else 1.5

            last_bar_info = {
                "high": float(highs[-1]),
                "low": float(lows[-1]),
                "close": float(closes[-1]),
                "time": str(completed_df.iloc[-1]["timestamp"])[-8:]
            }

            # 3. ตรวจสอบ Active Positions ทั้งหมดสำหรับ Multi-Order Matrix
            active_trade_info = None
            open_positions = []
            try:
                if hasattr(self.bridge, "get_positions"):
                    open_positions = self.bridge.get_positions(symbol=self.symbol)
                else:
                    open_positions = self.bridge.get_open_positions(symbol=self.symbol)
                
                # ค้นหา Position สำหรับ Magic 123456 (LLM Autonomous) ส่งให้ AI จัดการ
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
                "atr_14": round(atr_14, 2),
                "last_bar": last_bar_info,
                "all_positions": open_positions
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
        ตรวจสอบเงื่อนไขความปลอดภัยและส่งออเดอร์/จัดการออเดอร์ไปยัง MT5 ผ่าน Remote Bridge
        """
        action = decision.get("action", "HOLD").upper()
        confidence = decision.get("confidence", 0.0)
        reasoning = decision.get("reasoning", "")
        model_used = decision.get("model_used", self.model)
        active_trade = ctx.get("active_trade") if ctx else None

        logging.info(f"📊 AI Decision: {action} (Conf: {confidence:.2f}) | Model: {model_used} | Regime: {decision.get('market_regime')}")
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
            elif active_trade:
                t_id = active_trade.get("ticket")
                logging.info(f"🧹 AI สั่งปิดออเดอร์ Magic 123456 (Ticket #{t_id}) เพื่อรักษากำไร/ตัดความเสี่ยง...")
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
                logging.info("ℹ️ ไม่มีออเดอร์ Magic 123456 เปิดอยู่ จึงไม่ต้องปิด")
                return True

        # 3. MODIFY_SLTP (ปรับ Trailing Stop หรือเลื่อนไป Breakeven สำหรับ Magic 123456)
        if action == "MODIFY_SLTP":
            if not active_trade:
                logging.warning("⚠️ AI สั่ง MODIFY_SLTP แต่ไม่มีออเดอร์ Magic 123456 เปิดอยู่")
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

            logging.info(f"🛡️ [Magic 123456 LLM] ปรับ SL/TP ออเดอร์ #{t_id} -> SL ใหม่: {new_sl} (เดิม: {cur_sl}) | TP ใหม่: {new_tp}")
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

        # 4. BUY หรือ SELL (เปิดคำสั่งพร้อมกันหลาย Magic Number ใน Multi-Order Matrix)
        if action in ["BUY", "SELL"]:
            if active_trade:
                logging.info(f"⚠️ มีออเดอร์ Magic 123456 (#{active_trade.get('ticket')}) เปิดอยู่แล้ว -> ข้ามการเปิดใหม่เพื่อคุมความเสี่ยง")
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

            # ตรวจสอบว่า Magic ไหนเปิดค้างอยู่แล้วบ้าง
            all_pos = ctx.get("all_positions", []) if ctx else []
            open_magics = {p.get("magic") for p in all_pos if p.get("magic") is not None}

            opened_tickets = []
            for magic, policy in self.order_policies.items():
                if magic in open_magics:
                    logging.info(f"ℹ️ [Matrix Skip] Magic {magic} ({policy['name']}) มีออเดอร์เปิดค้างอยู่แล้ว")
                    continue

                if policy.get("tp_extended"):
                    policy_tp_points = 800
                    policy_tp_price = round(entry_price + (800 * point_value), 2) if action == "BUY" else round(entry_price - (800 * point_value), 2)
                else:
                    policy_tp_points = tp_points
                    policy_tp_price = tp_price

                comment = f"Scalp-{action[:1]}-{policy['tag']}-{sl_points}/{policy_tp_points}"

                if self.dry_run:
                    sim_ticket = 990000 + magic
                    logging.info(f"🧪 [DRY RUN] จำลองเปิดออเดอร์ Magic {magic} [{policy['name']}]: {action} {self.lot_size} lot @ {entry_price} | SL: {sl_price} | TP: {policy_tp_price} (#{sim_ticket})")
                    opened_tickets.append(sim_ticket)
                    continue

                res = self.bridge.send_order(
                    action=action,
                    symbol=self.symbol,
                    volume=self.lot_size,
                    sl=sl_price,
                    tp=policy_tp_price,
                    magic=magic,
                    comment=comment
                )
                if res and res.get("status") == "SUCCESS":
                    t_num = res.get("ticket")
                    opened_tickets.append(t_num)
                    logging.info(f"✅ เปิดออเดอร์สำเร็จ! Magic {magic} [{policy['name']}]: Ticket #{t_num} | SL: {sl_price} | TP: {policy_tp_price}")
                else:
                    logging.error(f"❌ เปิดออเดอร์ Magic {magic} [{policy['name']}] ล้มเหลว: {res}")

            if opened_tickets:
                logging.info(f"🎯 [Multi-Order Matrix] ส่งออเดอร์ทั้งหมด {len(opened_tickets)} ใบเรียบร้อย! Tickets: {opened_tickets}")
                return True
            else:
                logging.warning("⚠️ ไม่มีออเดอร์ใหม่ถูกเปิดในรอบนี้")
                return False

        logging.warning(f"⚠️ ไม่รู้จักคำสั่ง Action: {action}")
        return False

    def manage_rule_based_positions(self, ctx: Optional[Dict[str, Any]] = None) -> Tuple[int, int]:
        """
        ระบบบริหารจัดการออเดอร์ตามกฎเฉพาะของแต่ละ Magic Number (Matrix Lifecycle Management):
        - Magic 2222: 50% TP Breakeven (เลื่อน SL ไป Entry + 20 pts เมื่อกำไร >= 50% ของ TP)
        - Magic 3333: Fast Step Trailing Stop (เริ่มที่ 100 pts, ก้าวละ 50 pts)
        - Magic 4444: ATR Dynamic Volatility Trailing (Trailing SL ที่ระยะ 1.5x ATR(14))
        - Magic 6666: Structural Swing Pivot Trailing (Trailing SL ตามปลายไส้ M5 แท่งก่อนหน้า)
        - Magic 7777: Asymmetric Big-Runner (TP 800 pts, เลื่อน BE ที่ 50% TP เดิม, ล็อค 200 pts ที่ 400 pts)
        (หมายเหตุ: Magic 123456 จัดการโดยตรงผ่าน AI LLM ไม่ถูกปรับในฟังก์ชันนี้)
        คืนค่า: (จำนวนคำสั่งที่ถูก modify ในรอบนี้, จำนวนออเดอร์ Matrix ที่เปิดค้างอยู่)
        """
        try:
            if hasattr(self.bridge, "get_positions"):
                positions = self.bridge.get_positions(symbol=self.symbol)
            else:
                positions = self.bridge.get_open_positions(symbol=self.symbol)
        except Exception as e:
            logging.warning(f"⚠️ ไม่สามารถดึง Positions เพื่อจัดการ Trailing ได้: {e}")
            return (0, 0)

        if not positions:
            return (0, 0)

        # กรองเฉพาะออเดอร์ที่เป็น Matrix Rules เท่านั้น (Magic 2222, 3333, 4444, 6666, 7777)
        matrix_magics = {2222, 3333, 4444, 6666, 7777}
        matrix_positions = [p for p in positions if p.get("magic") in matrix_magics]
        if not matrix_positions:
            return (0, 0)

        point_value = 0.01
        atr = 1.5
        last_bar = None
        if ctx:
            atr = ctx.get("atr_14", 1.5)
            last_bar = ctx.get("last_bar")

        # ตรวจสอบว่าใน position dict มี current_price จาก MT5 ส่งมาพร้อมแล้วหรือไม่
        # เพื่อลดการเรียก HTTP Request ซ้ำซ้อน ประหยัด CPU และความเร็วตอบสนองระดับ Realtime
        has_embedded_price = any(p.get("current_price") and p.get("current_price") > 0 for p in matrix_positions)
        bid = 0.0
        ask = 0.0
        if not has_embedded_price:
            price_info = self.bridge.get_current_price(symbol=self.symbol)
            if price_info:
                bid = price_info.get("bid", 0.0)
                ask = price_info.get("ask", 0.0)

        modifications_count = 0

        for pos in matrix_positions:
            magic = pos.get("magic")
            ticket = pos.get("ticket") or pos.get("id")
            dir_pos = pos.get("direction", "BUY").upper()
            entry = float(pos.get("entry_price", 0.0))
            cur_sl = float(pos.get("sl", 0.0)) if pos.get("sl") is not None else 0.0
            cur_tp = float(pos.get("tp", 0.0)) if pos.get("tp") is not None else 0.0

            if not ticket or entry <= 0:
                continue

            # ใช้ current_price จาก MT5 position โดยตรง (BUY = Bid, SELL = Ask)
            current_p = float(pos.get("current_price", 0.0))
            if current_p <= 0:
                current_p = bid if dir_pos == "BUY" else ask

            if current_p <= 0:
                continue

            # คำนวณ Floating Profit ในหน่วย Points
            if dir_pos == "BUY":
                profit_points = (current_p - entry) / point_value
            else:
                profit_points = (entry - current_p) / point_value

            # ---------------------------------------------------------
            # 1. Magic 2222: 50% TP Breakeven
            # ---------------------------------------------------------
            if magic == 2222:
                tp_dist_pts = abs(cur_tp - entry) / point_value if cur_tp > 0 else 300.0
                if profit_points >= 0.5 * tp_dist_pts:
                    be_sl = round(entry + (20 * point_value), 2) if dir_pos == "BUY" else round(entry - (20 * point_value), 2)
                    should_modify = (cur_sl < be_sl - 0.05) if dir_pos == "BUY" else (cur_sl == 0.0 or cur_sl > be_sl + 0.05)
                    if should_modify:
                        logging.info(f"🎯 [Magic 2222 BE50] Ticket #{ticket} กำไรแตะ 50% TP ({profit_points:.1f} pts >= {0.5*tp_dist_pts:.1f} pts) -> ปรับ SL ไปยัง BE (+20 pts): {be_sl}")
                        if not self.dry_run:
                            res = self.bridge.modify_position(ticket=ticket, new_sl=be_sl, new_tp=cur_tp)
                            if res and res.get("status") == "SUCCESS":
                                modifications_count += 1

            # ---------------------------------------------------------
            # 2. Magic 3333: Fast Step Trailing Stop (Start 100 pts, Step 50 pts)
            # ---------------------------------------------------------
            elif magic == 3333:
                if profit_points >= 100.0:
                    steps = int(profit_points) // 50
                    locked_pts = (steps - 1) * 50
                    trail_sl = round(entry + (locked_pts * point_value), 2) if dir_pos == "BUY" else round(entry - (locked_pts * point_value), 2)
                    should_modify = (trail_sl > cur_sl + 0.05) if dir_pos == "BUY" else (cur_sl == 0.0 or trail_sl < cur_sl - 0.05)
                    if should_modify:
                        logging.info(f"⚡ [Magic 3333 FastTrail] Ticket #{ticket} กำไร {profit_points:.1f} pts -> Step Trail ขยับ SL ไปที่ {trail_sl} (ล็อคกำไร: +{locked_pts} pts)")
                        if not self.dry_run:
                            res = self.bridge.modify_position(ticket=ticket, new_sl=trail_sl, new_tp=cur_tp)
                            if res and res.get("status") == "SUCCESS":
                                modifications_count += 1

            # ---------------------------------------------------------
            # 3. Magic 4444: ATR Dynamic Volatility Trailing (1.5x ATR)
            # ---------------------------------------------------------
            elif magic == 4444:
                trail_dist = max(1.0, min(3.5, 1.5 * atr))
                if profit_points >= 80.0:
                    trail_sl = round(current_p - trail_dist, 2) if dir_pos == "BUY" else round(current_p + trail_dist, 2)
                    should_modify = (trail_sl > cur_sl + 0.15 and trail_sl > entry) if dir_pos == "BUY" else ((cur_sl == 0.0 or trail_sl < cur_sl - 0.15) and trail_sl < entry)
                    if should_modify:
                        logging.info(f"🌊 [Magic 4444 ATRTrail] Ticket #{ticket} กำไร {profit_points:.1f} pts -> Trail SL (1.5xATR: {trail_dist:.2f}) ไปที่ {trail_sl}")
                        if not self.dry_run:
                            res = self.bridge.modify_position(ticket=ticket, new_sl=trail_sl, new_tp=cur_tp)
                            if res and res.get("status") == "SUCCESS":
                                modifications_count += 1

            # ---------------------------------------------------------
            # 4. Magic 6666: Structural Swing Pivot Trailing (Wick of previous M5)
            # ---------------------------------------------------------
            elif magic == 6666 and last_bar:
                if dir_pos == "BUY":
                    swing_sl = round(last_bar["low"] - (10 * point_value), 2)
                    should_modify = (swing_sl > cur_sl + 0.05 and swing_sl > entry)
                else:
                    swing_sl = round(last_bar["high"] + (10 * point_value), 2)
                    should_modify = (cur_sl == 0.0 or swing_sl < cur_sl - 0.05) and (swing_sl < entry)

                if should_modify:
                    logging.info(f"📐 [Magic 6666 SwingTrail] Ticket #{ticket} -> Trail SL ตามไส้ M5 แท่งก่อนหน้า ({last_bar['time']}) ไปที่ {swing_sl}")
                    if not self.dry_run:
                        res = self.bridge.modify_position(ticket=ticket, new_sl=swing_sl, new_tp=cur_tp)
                        if res and res.get("status") == "SUCCESS":
                            modifications_count += 1

            # ---------------------------------------------------------
            # 5. Magic 7777: Asymmetric Big-Runner (800 TP, Tiered Locks)
            # ---------------------------------------------------------
            elif magic == 7777:
                target_sl = None
                tier_desc = ""
                if profit_points >= 600.0:
                    target_sl = round(entry + (400 * point_value), 2) if dir_pos == "BUY" else round(entry - (400 * point_value), 2)
                    tier_desc = "Tier 3 (+400 pts locked)"
                elif profit_points >= 400.0:
                    target_sl = round(entry + (200 * point_value), 2) if dir_pos == "BUY" else round(entry - (200 * point_value), 2)
                    tier_desc = "Tier 2 (+200 pts locked)"
                elif profit_points >= 150.0:
                    target_sl = round(entry + (20 * point_value), 2) if dir_pos == "BUY" else round(entry - (20 * point_value), 2)
                    tier_desc = "Tier 1 (Breakeven +20 pts)"

                if target_sl is not None:
                    should_modify = (target_sl > cur_sl + 0.05) if dir_pos == "BUY" else (cur_sl == 0.0 or target_sl < cur_sl - 0.05)
                    if should_modify:
                        logging.info(f"🚀 [Magic 7777 BigRunner] Ticket #{ticket} กำไร {profit_points:.1f} pts -> {tier_desc} ปรับ SL ไปที่ {target_sl}")
                        if not self.dry_run:
                            res = self.bridge.modify_position(ticket=ticket, new_sl=target_sl, new_tp=cur_tp)
                            if res and res.get("status") == "SUCCESS":
                                modifications_count += 1

        return (modifications_count, len(matrix_positions))

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
        logging.info(f"📈 วิเคราะห์แท่งเทียนที่เพิ่งปิด: {ctx['candle_time']} | ราคาปิด: {ctx['current_price']:.2f} | สถานะ: {trade_status_str}")
        logging.info(f"📊 100-Bar Macro: [{ctx['min_100']:.2f} - {ctx['max_100']:.2f}] (Range: {ctx['range_100']:.2f})")

        decision = self.call_ai_decision(ctx)
        if not decision:
            logging.error("❌ ไม่ได้รับคำตอบจาก AI")
            return False

        self.last_processed_candle_time = ctx["candle_time"]
        self.last_context = ctx
        success = self.execute_decision(decision, ctx)
        
        # บริหารจัดการ Trailing Stop & Breakeven สำหรับ Rule-Based Matrix
        self.manage_rule_based_positions(ctx=ctx)
        return success

    def run_forever(self, run_immediately: bool = True):
        """
        รันระบบอัตโนมัติต่อเนื่อง 24/7 โดยซิงค์ตามเวลาเครื่องโทรศัพท์ทุก 5 นาทีเป๊ะ
        เช่น 01:00, 01:05, 01:10, 01:15, ... (+ 1 วินาที buffer ให้แท่งปิดสมบูรณ์)
        พร้อม Sub-loop Trailing Stop / BE ความเร็วสูงระดับ Adaptive Turbo:
        - เมื่อมีออเดอร์เปิดค้าง: ตรวจสอบและ Trailing ทุก 1.0 วินาที (ตอบสนองไวที่สุดในตลาดทองคำ)
        - เมื่อไม่มีออเดอร์ค้าง: โหมดประหยัดพลังงาน ตรวจสอบทุก 3.0 วินาที (ไม่กิน CPU / แบตเตอรี่โทรศัพท์)
        """
        logging.info("🎯 เข้าสู่โหมดรันอัตโนมัติ 24/7 (Adaptive Turbo Trailing 1.0s / 3.0s)")
        
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
                
                active_matrix_count = 0
                # Sub-loop: ตรวจสอบและบริหารจัดการ Trailing / BE แบบ Adaptive Turbo ระหว่างรอแท่ง M5 ถัดไป
                while True:
                    curr_now = datetime.now()
                    rem_sec = (target_time - curr_now).total_seconds()
                    if rem_sec <= 0.5:
                        if rem_sec > 0:
                            time.sleep(rem_sec)
                        break
                    
                    # Adaptive Interval:
                    # ถ้ามีออเดอร์ Matrix -> 1.0 วินาที (Ultra-Fast Trailing ป้องกันการกระชากของราคาทอง)
                    # ถ้าไม่มีออเดอร์ -> 3.0 วินาที (Eco Mode เซฟแบตโทรศัพท์)
                    target_interval = 1.0 if active_matrix_count > 0 else 3.0
                    sleep_chunk = min(target_interval, max(0.1, rem_sec - 0.2))
                    time.sleep(sleep_chunk)
                    
                    is_open, _ = self.is_market_open()
                    if is_open:
                        _, active_matrix_count = self.manage_rule_based_positions(ctx=getattr(self, "last_context", None))
                    else:
                        active_matrix_count = 0

                self.run_single_cycle()

            except KeyboardInterrupt:
                logging.info("\n🛑 ผู้ใช้หยุดการทำงานของระบบ (Ctrl+C)")
                break
            except Exception as e:
                logging.error(f"❌ เกิดข้อผิดพลาดใน Loop: {e}")
                time.sleep(3)

if __name__ == "__main__":
    once = "--once" in sys.argv
    engine = PriceActionScalperEngine(dry_run=False)
    if once:
        engine.run_single_cycle()
    else:
        engine.run_forever(run_immediately=True)
