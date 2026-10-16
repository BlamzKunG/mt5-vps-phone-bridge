#!/usr/bin/env python3
"""
M5 Price Action Scalping Engine - Max Cache Read Edition
-------------------------------------------------------
- Architecture: Prefix-Aligned KV Cache Optimization (Max Cache Read)
  * โครงสร้าง Prompt ถูกจัดเรียงใหม่แบบ Prefix-First:
    1. Static System Guidelines & Execution Rules (100% Immutable)
    2. Historical Structure Context & 100-Bar Curve (Semi-Static Prefix)
    3. Preceding Candles (Bars -20 to -2)
    4. Realtime Trigger Suffix (Bar -1, Timestamp, Active Position) อยู่ล่างสุดเท่านั้น
  * ช่วยให้ GPU ดึง KV Cache เดิมได้เกือบ 100% ลด Latency และประหยัดค่า API ถึง 95%
- 3-Tier Multi-Model Failover Architecture:
  * Tier 1 (Primary): gpt-6.1-sol (MaxPlus AI, reasoning_effort: xhigh)
  * Tier 2 (Secondary Fallback): gpt-6-sol
  * Tier 3 (Emergency Consensus): gpt-6-luna x 2 (Parallel Dual-Verification)
- Target Asset: XAUUSD (M5) Scalp 100 - 500 points
- Zero Balance Leakage, Protective Trailing Guard, Phone Local Clock Sync
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

from vps_bridge_client import RemoteMT5Bridge

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scalper_cache_engine.log", encoding="utf-8")
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

class PriceActionScalperCacheEngine:
    def __init__(
        self,
        vps_url: Optional[str] = None,
        bridge_api_key: Optional[str] = None,
        maxplus_api_key: Optional[str] = None,
        model: str = "gpt-6.1-sol",
        secondary_model: str = "gpt-6-sol",
        emergency_model: str = "gpt-6-luna",
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
        self.secondary_model = secondary_model
        self.emergency_model = emergency_model
        self.reasoning_effort = reasoning_effort
        self.symbol = symbol
        self.lot_size = lot_size
        self.max_spread_points = max_spread_points
        self.min_sl_tp_points = min_sl_tp_points
        self.max_sl_tp_points = max_sl_tp_points
        self.dry_run = dry_run

        self.last_processed_candle_time: Optional[str] = None
        logging.info("⚡ [CACHE EDITION] Price Action Scalper Engine Initialized")
        logging.info(f"Architecture: Prefix-Aligned KV Cache Read | Symbol: {self.symbol}")
        logging.info(f"Tier 1: {self.model} | Tier 2: {self.secondary_model} | Tier 3: {self.emergency_model} x2")

    def prepare_market_context(self) -> Optional[Dict[str, Any]]:
        """
        ดึง 105 แท่ง M5 ล่าสุด แยกข้อมูลออกเป็น 2 กลุ่ม:
        1. Context Prefix (Semi-Static): 100 แท่ง Normalized + 19 แท่งประวัติศาสตร์ (-20 ถึง -2)
        2. Dynamic Suffix (Realtime Trigger): แท่ง -1 ที่เพิ่งปิด + สถานะออเดอร์ปัจจุบัน
        """
        try:
            df = self.bridge.get_historical_data(symbol=self.symbol, timeframe="5m", num_candles=105)
            if df.empty or len(df) < 100:
                logging.error(f"❌ ดึงข้อมูลแท่งเทียนไม่พอ: {len(df)} แท่ง")
                return None

            completed_df = df.iloc[-101:-1].copy() if len(df) > 100 else df.iloc[-100:].copy()
            last_candle_time = str(completed_df.iloc[-1]["timestamp"])

            # 1. รายละเอียด 20 แท่งล่าสุด แยกเป็น Preceding (19 แท่ง) และ Trigger (1 แท่งล่าสุด)
            detailed_20 = completed_df.iloc[-20:].copy()
            preceding_19 = []
            for _, row in detailed_20.iloc[:-1].iterrows():
                preceding_19.append({
                    "bar": len(preceding_19) - 20,
                    "time": str(row["timestamp"])[-8:],
                    "open": round(float(row["open"]), 2),
                    "high": round(float(row["high"]), 2),
                    "low": round(float(row["low"]), 2),
                    "close": round(float(row["close"]), 2),
                    "vol": int(row["volume"])
                })

            trigger_row = detailed_20.iloc[-1]
            trigger_candle = {
                "bar": -1,
                "time": str(trigger_row["timestamp"])[-8:],
                "open": round(float(trigger_row["open"]), 2),
                "high": round(float(trigger_row["high"]), 2),
                "low": round(float(trigger_row["low"]), 2),
                "close": round(float(trigger_row["close"]), 2),
                "vol": int(trigger_row["volume"])
            }

            # 2. Normalized 100 แท่ง
            min_100 = float(completed_df["low"].min())
            max_100 = float(completed_df["high"].max())
            rng_100 = max_100 - min_100 if max_100 > min_100 else 1.0
            norm_closes = [round((float(c) - min_100) / rng_100, 3) for c in completed_df["close"]]
            current_close = float(completed_df.iloc[-1]["close"])

            # 3. ตรวจสอบ Active Position
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
                "preceding_19": preceding_19,
                "trigger_candle": trigger_candle,
                "active_trade": active_trade_info
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
            
            # ดึงสถิติ Cache Read จาก API
            usage = data.get("usage", {})
            prompt_details = usage.get("prompt_tokens_details", {})
            cached_tokens = prompt_details.get("cached_tokens", 0)
            prompt_tokens = usage.get("prompt_tokens", 0)
            cache_rate = (cached_tokens / prompt_tokens * 100) if prompt_tokens > 0 else 0.0

            decision["cache_stats"] = {
                "cached_tokens": cached_tokens,
                "prompt_tokens": prompt_tokens,
                "cache_hit_rate": round(cache_rate, 1)
            }
            decision["raw_model"] = model_name

            logging.info(f"⚡ [CACHE METRICS] {tag or model_name}: {cached_tokens}/{prompt_tokens} cached tokens ({cache_rate:.1f}% Hit Rate) | Latency: {elapsed:.2f}s")
            return decision
        except Exception as e:
            logging.warning(f"⚠️ {tag or model_name} call/parse failed: {e}")
            return None

    def call_ai_decision(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        สร้าง Prompt ในสถาปัตยกรรม Prefix-Aligned KV Cache:
        - Immutable System Rules & Protocols (100% Cacheable Prefix)
        - Historical Structure Context (Semi-Static Prefix)
        - Dynamic Realtime Trigger (Suffix ท้ายสุดจุดเดียวที่เปลี่ยน)
        """
        now = datetime.now()
        minutes_to_add = 5 - (now.minute % 5)
        target_time = (now + timedelta(minutes=minutes_to_add)).replace(second=1, microsecond=0)
        seconds_remaining = (target_time - now).total_seconds()
        timeout_seconds = max(90, min(270, int(seconds_remaining) - 15))

        # -------------------------------------------------------------
        # 1. IMMUTABLE SYSTEM PROMPT (100% Stable Prefix)
        # -------------------------------------------------------------
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

        # -------------------------------------------------------------
        # 2. USER PROMPT: จัดเรียงแบบ Semi-Static Prefix -> Dynamic Suffix
        # -------------------------------------------------------------
        active_trade_str = json.dumps(ctx.get("active_trade"), indent=1) if ctx.get("active_trade") else "FLAT (No Open Positions)"

        user_prompt = f"""[MARKET SPECIFICATION]
Asset: {self.symbol}
Timeframe: M5 (5 Minutes)

[100-BAR MACRO STRUCTURAL CONTEXT]
- 100-Bar Macro Range: [{ctx['min_100']:.2f} - {ctx['max_100']:.2f}] (Range: {ctx['range_100']:.2f})
- 100-Bar Normalized Closes (0.00 to 1.00, oldest to newest):
{ctx['norm_closes']}

[PREVIOUS HISTORICAL CANDLES (Bars -20 to -2)]
{json.dumps(ctx['preceding_19'], separators=(',', ':'))}

============================================================
[REALTIME TRIGGER DATA - NEWLY COMPLETED BAR -1]
============================================================
Completed Candle Timestamp: {ctx['candle_time']}
Latest Closed Price: {ctx['current_price']:.2f}
Latest Completed M5 Candle: {json.dumps(ctx['trigger_candle'], separators=(',', ':'))}

[CURRENT PORTFOLIO / TRADE STATUS]
{active_trade_str}

Synthesize the immutable methodology, historical structure, and the newly closed M5 bar to output your pure JSON decision:"""

        # -------------------------------------------------------------
        # STEP 1: ลองเรียก Tier-1 Primary Model (gpt-6.1-sol)
        # -------------------------------------------------------------
        logging.info(f"⏳ [Tier 1: Primary] เรียก {self.model} (Max Cache Read Architecture)...")
        sol61_decision = self._call_single_model(
            model_name=self.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout_seconds=max(60, min(timeout_seconds, 90)),
            reasoning_effort=self.reasoning_effort,
            tag=f"Primary-{self.model}"
        )

        if sol61_decision:
            sol61_decision["model_used"] = self.model
            sol61_decision["tier"] = 1
            sol61_decision["emergency_mode"] = False
            logging.info(f"✅ สำเร็จ: ได้รับคำตอบจาก Tier-1 Primary Model ({self.model})")
            return sol61_decision

        # -------------------------------------------------------------
        # STEP 2: Tier-2 Secondary Model (gpt-6-sol)
        # -------------------------------------------------------------
        logging.warning("=" * 65)
        logging.warning(f"⚠️ [TIER 1 FAILED] {self.model} ขัดข้องหรือไม่ตอบสนอง!")
        logging.warning(f"🔄 [Tier 2: Fallback] สลับไปเรียก {self.secondary_model}...")
        logging.warning("=" * 65)

        now_t2 = datetime.now()
        seconds_left_t2 = max(45, int((target_time - now_t2).total_seconds()) - 10)

        sol6_decision = self._call_single_model(
            model_name=self.secondary_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout_seconds=max(45, min(seconds_left_t2, 75)),
            reasoning_effort=self.reasoning_effort,
            tag=f"Secondary-{self.secondary_model}"
        )

        if sol6_decision:
            sol6_decision["model_used"] = f"{self.secondary_model} (Tier 2 Fallback)"
            sol6_decision["tier"] = 2
            sol6_decision["emergency_mode"] = False
            logging.info(f"✅ สำเร็จ: ได้รับคำตอบจาก Tier-2 Secondary Model ({self.secondary_model})")
            return sol6_decision

        # -------------------------------------------------------------
        # STEP 3: Tier-3 Emergency Consensus (gpt-6-luna x 2)
        # -------------------------------------------------------------
        logging.warning("=" * 65)
        logging.warning(f"🚨 [EMERGENCY PROTOCOL ACTIVATED] ทั้ง {self.model} และ {self.secondary_model} ใช้งานไม่ได้!")
        logging.warning(f"👥 [Tier 3: Emergency] เรียกใช้งาน {self.emergency_model} แบบคู่ขนาน 2 ตัวเพื่อทำ Consensus Verification...")
        logging.warning("=" * 65)

        now3 = datetime.now()
        seconds_left_t3 = max(30, int((target_time - now3).total_seconds()) - 10)

        def _worker(instance_id: int):
            return instance_id, self._call_single_model(
                model_name=self.emergency_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_seconds=seconds_left_t3,
                reasoning_effort="xhigh",
                tag=f"Luna-#{instance_id}"
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(_worker, 1), executor.submit(_worker, 2)]
            results = {}
            for fut in as_completed(futures):
                try:
                    i_id, dec = fut.result()
                    results[i_id] = dec
                except Exception as e:
                    logging.error(f"❌ Luna worker error: {e}")

        dec1 = results.get(1)
        dec2 = results.get(2)

        if not dec1 or not dec2:
            logging.error(f"❌ [EMERGENCY FAILED] gpt-6-luna ตอบกลับไม่ครบทั้งสองตัว")
            is_in_trade = bool(ctx.get("active_trade"))
            safe_action = "HOLD_POSITION" if is_in_trade else "HOLD"
            return {
                "action": safe_action,
                "confidence": 0.0,
                "sl_points": None,
                "tp_points": None,
                "new_sl": None,
                "new_tp": None,
                "market_regime": "Emergency Instances Incomplete",
                "reasoning": "All upstream tiers failed and emergency Luna instances did not complete both responses. Safeguard maintains safe hold stance.",
                "model_used": "Emergency Failed -> Safe Hold",
                "tier": 3,
                "emergency_mode": True
            }

        act1 = dec1.get("action", "").upper()
        act2 = dec2.get("action", "").upper()
        conf1 = dec1.get("confidence", 0.0)
        conf2 = dec2.get("confidence", 0.0)

        logging.info(f"🔍 [Consensus Evaluation] Luna #1: {act1} (Conf: {conf1:.2f}) vs Luna #2: {act2} (Conf: {conf2:.2f})")

        if act1 == act2:
            logging.info(f"🤝 [CONSENSUS AGREED] AI ทั้ง 2 ตัวเห็นตรงกันเป็นเอกฉันท์: {act1}")
            final_decision = dec1.copy()
            final_decision["model_used"] = f"{self.emergency_model} x2 (Tier 3 Consensus: {act1})"
            final_decision["tier"] = 3
            final_decision["emergency_mode"] = True
            final_decision["consensus_agreed"] = True
            final_decision["luna1_action"] = act1
            final_decision["luna2_action"] = act2
            final_decision["luna1_conf"] = conf1
            final_decision["luna2_conf"] = conf2
            final_decision["reasoning"] = f"[Luna-1 (Leader)]: {dec1.get('reasoning')} | [Luna-2 (Consensus Verified)]: {dec2.get('reasoning')}"
            return final_decision
        else:
            logging.warning(f"⚠️ [CONSENSUS DIVERGENCE] ความเห็นไม่ตรงกัน ({act1} != {act2}) -> สั่งระงับการเทรดเพื่อความปลอดภัย")
            is_in_trade = bool(ctx.get("active_trade"))
            safe_action = "HOLD_POSITION" if is_in_trade else "HOLD"
            return {
                "action": safe_action,
                "confidence": 0.0,
                "sl_points": None,
                "tp_points": None,
                "new_sl": None,
                "new_tp": None,
                "market_regime": f"Consensus Split: Luna1={act1} vs Luna2={act2}",
                "reasoning": f"Emergency consensus divergence: Luna #1 decided {act1}, whereas Luna #2 decided {act2}. As both instances did not reach unanimous agreement, the system holds position / flat.",
                "model_used": f"{self.emergency_model} x2 (Tier 3 Split: {act1}/{act2} -> Safe Hold)",
                "tier": 3,
                "emergency_mode": True,
                "consensus_agreed": False
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

        cache_info = ""
        if "cache_stats" in decision:
            cs = decision["cache_stats"]
            cache_info = f" | Cache Hit: {cs.get('cache_hit_rate')}% ({cs.get('cached_tokens')}/{cs.get('prompt_tokens')} tokens)"

        logging.info(f"📊 AI Decision: {action} (Conf: {confidence:.2f}) | Model: {model_used}{cache_info} | Regime: {decision.get('market_regime')}")
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
                    comment=f"ScalpCache-{action[:1]}-{sl_points}/{tp_points}"
                )
            else:
                result = self.bridge.open_position(
                    direction=action,
                    lot=self.lot_size,
                    sl=sl_price,
                    tp=tp_price,
                    symbol=self.symbol,
                    comment=f"ScalpCache-{action[:1]}-{sl_points}/{tp_points}"
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
        local_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info("=" * 60)
        logging.info(f"🔄 เริ่มรอบการทำงาน (Cache Edition): {local_time_str} (เวลาเครื่อง)")
        
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
        logging.info("🎯 เข้าสู่โหมดรันอัตโนมัติ 24/7 (Cache Edition - ซิงค์เวลาโทรศัพท์ทุก 5 นาที)")
        
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
    engine = PriceActionScalperCacheEngine(dry_run=False)
    if once:
        engine.run_single_cycle()
    else:
        engine.run_forever(run_immediately=True)
