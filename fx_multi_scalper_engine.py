#!/usr/bin/env python3
"""
Multi-Currency Forex Price Action Scalper Engine (M5)
----------------------------------------------------
- Asset Universe: 7 Major Currency Pairs (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD)
- Architecture: Multi-Pair Snapshot (Single LLM Call)
- Position Management:
  * Strict Portfolio Limit: Maximum 3 concurrent open orders total.
  * Zero-Interference Rule: When any position is open, SKIP LLM calls completely.
  * Server-Side Risk: Every order enters MT5 with hard Stop Loss and Take Profit.
- Magic Number: 888888 (Completely isolated from XAUUSD 123456)
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

# Setup logging (clean, zero emojis)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("fx_multi_scalper.log", encoding="utf-8")
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


class FXMultiCurrencyScalperEngine:
    DEFAULT_PAIRS = [
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "AUDUSD",
        "USDCAD",
        "USDCHF",
        "NZDUSD"
    ]

    def __init__(
        self,
        vps_url: Optional[str] = None,
        bridge_api_key: Optional[str] = None,
        maxplus_api_key: Optional[str] = None,
        pairs: Optional[List[str]] = None,
        model: str = "gpt-6-sol",
        secondary_model: str = "gpt-6.1-sol",
        reasoning_effort: str = "low",
        lot_size: float = 0.01,
        max_concurrent_orders: int = 3,
        max_spread_points: float = 25.0,
        magic: int = 888888,
        dry_run: bool = False
    ):
        self.vps_url = vps_url or os.getenv("VPS_URL", "http://154.197.116.121:8000")
        bridge_key = bridge_api_key or os.getenv("BRIDGE_API_KEY", "llm-trade-secret-key-2026")
        self.bridge = RemoteMT5Bridge(vps_url=self.vps_url, api_key=bridge_key)
        self.maxplus_api_key = maxplus_api_key or os.getenv("MAXPLUS_API_KEY", "")
        self.pairs = pairs or self.DEFAULT_PAIRS
        self.model = model
        self.secondary_model = secondary_model
        self.reasoning_effort = reasoning_effort
        self.lot_size = lot_size
        self.max_concurrent_orders = max_concurrent_orders
        self.max_spread_points = max_spread_points
        self.magic = magic
        self.dry_run = dry_run

        self.last_processed_candle_time: Optional[str] = None

        logging.info("[INIT] FX Multi-Currency Scalper Engine Initialized")
        logging.info(f"Target Pairs: {self.pairs}")
        logging.info(f"Model Primary: {self.model} | Secondary: {self.secondary_model}")
        logging.info(f"Portfolio Rules: Max Concurrent Orders = {self.max_concurrent_orders} | Lot = {self.lot_size}")
        logging.info(f"Isolation: Magic = {self.magic} (XAUUSD unaffected) | Dry Run = {self.dry_run}")
        logging.info("Zero-Interference: If any order is active, LLM calls are skipped 100%.")

    @staticmethod
    def get_symbol_scale(symbol: str, price: float) -> Tuple[float, int]:
        """
        คำนวณ Point Size และจำนวนทศนิยม
        - คู่ JPY (เช่น USDJPY) หรือราคา > 50: point = 0.001 (ทศนิยม 3 ตำแหน่ง, 1 pip = 10 pts = 0.010)
        - คู่ปกติ (EURUSD, GBPUSD ฯลฯ): point = 0.00001 (ทศนิยม 5 ตำแหน่ง, 1 pip = 10 pts = 0.00010)
        """
        if "JPY" in symbol.upper() or price > 50.0:
            return 0.001, 3
        return 0.00001, 5

    def is_market_open(self) -> Tuple[bool, str]:
        """
        ตรวจสอบปฏิทินเวลาทำการของตลาด Forex
        """
        now_utc = datetime.now(timezone.utc)
        weekday = now_utc.weekday()
        hour = now_utc.hour

        if weekday == 5:
            return False, "Saturday - Market Closed"
        if weekday == 6 and hour < 22:
            return False, "Sunday - Market opens at ~22:00 UTC"
        if weekday == 4 and hour >= 22:
            return False, "Friday - Market closed for weekend at 22:00 UTC"
        if weekday in range(0, 5) and hour == 21:
            return False, "Daily Rollover Break (~21:00-22:00 UTC)"

        return True, "Market Open"

    def fetch_pair_snapshot(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        ดึงข้อมูล M5 OHLCV 20 แท่งล่าสุดและค่าสเปรดของคู่เงิน
        """
        try:
            p_info = self.bridge.get_current_price(symbol=symbol)
            if not p_info:
                return None

            bid = float(p_info.get("bid", 0.0))
            ask = float(p_info.get("ask", 0.0))
            spread_pts = float(p_info.get("spread", 0.0))
            current_price = float(p_info.get("price", bid))

            df = self.bridge.get_historical_data(symbol=symbol, timeframe="5m", num_candles=25)
            if df is None or len(df) < 20:
                return None

            completed_df = df.iloc[:-1].copy() if len(df) >= 20 else df.copy()

            point_size, digits = self.get_symbol_scale(symbol, current_price)

            min_20 = float(completed_df["low"].tail(20).min())
            max_20 = float(completed_df["high"].tail(20).max())
            range_pts = round((max_20 - min_20) / point_size, 1) if point_size > 0 else 0.0

            pos_in_range_pct = 50.0
            if max_20 > min_20:
                pos_in_range_pct = round(((current_price - min_20) / (max_20 - min_20)) * 100.0, 1)

            last_row = completed_df.iloc[-1]
            c_open = float(last_row["open"])
            c_close = float(last_row["close"])
            c_high = float(last_row["high"])
            c_low = float(last_row["low"])

            c_dir = "BULL" if c_close >= c_open else "BEAR"
            body_pts = round(abs(c_close - c_open) / point_size, 1)
            upper_wick_pts = round((c_high - max(c_open, c_close)) / point_size, 1)
            lower_wick_pts = round((min(c_open, c_close) - c_low) / point_size, 1)

            first_20_close = float(completed_df["close"].iloc[-20])
            net_change_20_pts = round((current_price - first_20_close) / point_size, 1)

            return {
                "symbol": symbol,
                "current_price": round(current_price, digits),
                "bid": round(bid, digits),
                "ask": round(ask, digits),
                "spread_pts": spread_pts,
                "point_size": point_size,
                "digits": digits,
                "candle_time": str(last_row.get("timestamp", last_row.get("time"))),
                "range_20_pts": range_pts,
                "pos_in_range_pct": pos_in_range_pct,
                "net_change_20_pts": net_change_20_pts,
                "last_candle": {
                    "dir": c_dir,
                    "body_pts": body_pts,
                    "upper_wick_pts": upper_wick_pts,
                    "lower_wick_pts": lower_wick_pts,
                    "vol": int(last_row.get("volume", 0))
                }
            }
        except Exception as e:
            logging.warning(f"[SNAPSHOT FAIL] Error fetching {symbol}: {e}")
            return None

    def build_multi_pair_snapshot(self) -> Dict[str, Any]:
        """
        รวบรวมข้อมูล M5 ล่าสุดของทั้ง 7 คู่เงิน
        """
        snapshot = {}
        for sym in self.pairs:
            data = self.fetch_pair_snapshot(sym)
            if data:
                snapshot[sym] = data
        return snapshot

    def _call_single_model(
        self,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: int = 60
    ) -> Optional[Dict[str, Any]]:
        """
        ส่งคำสั่งเรียก LLM ผ่าน MaxPlus API
        """
        if not self.maxplus_api_key:
            logging.error("[CONFIG ERROR] MAXPLUS_API_KEY is not set.")
            return None

        url = "https://api.maxplus-ai.cc/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.maxplus_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.1,
            "reasoning_effort": self.reasoning_effort
        }

        try:
            t0 = time.time()
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
            elapsed = time.time() - t0

            if resp.status_code != 200:
                logging.warning(f"[API WARN] {model_name} HTTP {resp.status_code}: {resp.text[:150]}")
                return None

            data = resp.json()
            raw_content = data["choices"][0]["message"]["content"].strip()
            clean_json = raw_content.strip("```json").strip("```").strip()
            decision = json.loads(clean_json)
            decision["latency_seconds"] = round(elapsed, 2)
            decision["model_used"] = model_name
            logging.info(f"[API OK] Received response from {model_name} ({elapsed:.2f}s)")
            return decision
        except Exception as e:
            logging.warning(f"[API ERROR] {model_name} failed: {e}")
            return None

    def call_ai_decision(self, snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        เรียก LLM เพื่อเลือกคู่เงินที่คมที่สุด (0 ถึง 3 คู่)
        """
        system_prompt = """You are an Institutional FX Quantitative Portfolio Scalper managing a multi-currency book of 7 Major Pairs (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD) on M5 timeframe.

PORTFOLIO CONSTRAINTS:
1. Capital Allocation & Risk:
   - You can propose a maximum of 3 concurrent trade orders total.
   - If market conditions are choppy, unconvincing, or lacking high confluence, output "action": "HOLD" and "orders": [].
2. Currency Correlation Discipline:
   - Do NOT over-concentrate correlated USD exposure (e.g. avoid buying 3 USD-base pairs or selling 3 USD-quote pairs simultaneously).
   - Only select pairs demonstrating independent, textbook Price Action setups.
3. Execution Criteria (M5 Price Action):
   - Liquidity Sweeps: Deep rejection wicks piercing 20-bar extremes (pos_in_range_pct near 0% or 100%).
   - Market Structure Shift (MSS): Displacement breaking swing points with high tick volume.
   - Risk/Reward: Target SL between 80 - 200 points (8 - 20 pips) and TP between 120 - 350 points (12 - 35 pips).
   - Spread Check: Prefer pairs with low spread_pts relative to their range.

OUTPUT FORMAT REQUIREMENTS:
Output pure JSON only without markdown or code blocks:
{
  "portfolio_regime": "<concise macro regime summary across USD pairs>",
  "action": "TRADE" | "HOLD",
  "orders": [
    {
      "symbol": "<symbol name, e.g. EURUSD>",
      "action": "BUY" | "SELL",
      "sl_points": <integer 80 to 200>,
      "tp_points": <integer 120 to 350>,
      "confidence": <float 0.0 to 1.0>,
      "reasoning": "<concise entry thesis: liquidity sweep, MSS, range position>"
    }
  ],
  "rationale": "<brief explanation of portfolio selection>"
}"""

        user_prompt = f"""[M5 MULTI-CURRENCY SNAPSHOT]
Pairs evaluated: {list(snapshot.keys())}
Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}

{json.dumps(snapshot, indent=2)}

Analyze the multi-currency matrix, verify correlation sanity, and select at most 3 highest-confluence trades (or HOLD all). Return pure JSON:"""

        logging.info(f"[LLM] Calling Primary Model ({self.model}) with 7-pair snapshot...")
        decision = self._call_single_model(
            model_name=self.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout_seconds=75
        )

        if not decision and self.secondary_model:
            logging.warning(f"[LLM FAILOVER] Calling Secondary Model ({self.secondary_model})...")
            decision = self._call_single_model(
                model_name=self.secondary_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_seconds=75
            )

        return decision

    def execute_decision(self, decision: Dict[str, Any], snapshot: Dict[str, Any]) -> bool:
        """
        ดำเนินการส่งคำสั่งซื้อขายเข้าสู่ MT5 โดยตรง
        """
        action = str(decision.get("action", "HOLD")).upper()
        if action != "TRADE":
            logging.info(f"[HOLD] LLM selected HOLD for all pairs. Rationale: {decision.get('rationale', 'N/A')}")
            return True

        orders = decision.get("orders", [])
        if not orders:
            logging.info("[HOLD] Action was TRADE but orders list is empty.")
            return True

        # จำกัดจำนวนออเดอร์ไม่เกินโควตาสูงสุด
        allowed_orders = orders[:self.max_concurrent_orders]
        logging.info(f"[TRADE EXECUTION] Processing {len(allowed_orders)} trade(s) from LLM...")

        executed_count = 0
        for order in allowed_orders:
            sym = str(order.get("symbol", "")).upper()
            o_action = str(order.get("action", "")).upper()
            sl_pts = int(order.get("sl_points", 120))
            tp_pts = int(order.get("tp_points", 200))
            conf = float(order.get("confidence", 0.0))

            if o_action not in ["BUY", "SELL"]:
                logging.warning(f"[ORDER SKIP] Invalid action '{o_action}' for {sym}")
                continue

            pair_snap = snapshot.get(sym)
            if not pair_snap:
                logging.warning(f"[ORDER SKIP] No snapshot data available for {sym}")
                continue

            spread = pair_snap.get("spread_pts", 0.0)
            if spread > self.max_spread_points:
                logging.warning(f"[SPREAD GUARD] {sym} spread {spread} pts exceeds limit {self.max_spread_points} pts. Skipped.")
                continue

            point_size = pair_snap.get("point_size", 0.00001)
            digits = pair_snap.get("digits", 5)

            # ดึงราคา Ask/Bid ล่าสุด
            price_info = self.bridge.get_current_price(symbol=sym)
            if not price_info:
                logging.warning(f"[PRICE ERROR] Failed to fetch live price for {sym}")
                continue

            bid = float(price_info.get("bid", 0.0))
            ask = float(price_info.get("ask", 0.0))
            exec_price = ask if o_action == "BUY" else bid

            if o_action == "BUY":
                sl_price = round(exec_price - (sl_pts * point_size), digits)
                tp_price = round(exec_price + (tp_pts * point_size), digits)
            else:
                sl_price = round(exec_price + (sl_pts * point_size), digits)
                tp_price = round(exec_price - (tp_pts * point_size), digits)

            comment = f"FX-{sym[:3]}-{conf:.2f}"[:27]

            logging.info(f"[ORDER SEND] {o_action} {self.lot_size} lot on {sym} @ {exec_price} | SL: {sl_price} ({sl_pts} pts) | TP: {tp_price} ({tp_pts} pts) | Magic: {self.magic}")

            if self.dry_run:
                logging.info(f"[DRY RUN] Simulated order success for {sym} {o_action}")
                executed_count += 1
            else:
                res = self.bridge.send_order(
                    action=o_action,
                    symbol=sym,
                    volume=self.lot_size,
                    sl=sl_price,
                    tp=tp_price,
                    magic=self.magic,
                    comment=comment
                )
                if res and res.get("status") == "SUCCESS":
                    logging.info(f"[SUCCESS] Order placed on MT5! Ticket #{res.get('ticket')} ({sym})")
                    executed_count += 1
                else:
                    logging.error(f"[FAILED] Failed to place order on MT5 for {sym}: {res}")

        logging.info(f"[PORTFOLIO UPDATE] Successfully executed {executed_count}/{len(allowed_orders)} order(s).")
        return True

    def run_single_cycle(self) -> bool:
        """
        รอบการทำงาน 1 รอบ (M5 Closed Bar)
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info("=" * 60)
        logging.info(f"[CYCLE START] FX Multi-Currency Scalper: {now_str}")

        # 1. ตรวจสอบสถานะตลาด Forex
        is_open, market_reason = self.is_market_open()
        if not is_open:
            logging.info(f"[MARKET CLOSED] {market_reason}. Skipping cycle.")
            return False

        # 2. ตรวจสอบ Active Positions ของ Magic 888888 (FX Portfolio เท่านั้น)
        open_positions = []
        try:
            open_positions = self.bridge.get_positions(symbol=None, magic=self.magic)
        except Exception as e:
            logging.warning(f"[POSITION CHECK WARN] Failed to query MT5 positions: {e}")

        # 3. ZERO-INTERFERENCE RULE:
        # หากมีออเดอร์เปิดค้างอยู่ ไม่ว่าจะกี่ไม้ ห้ามเรียก LLM เด็ดขาด
        if len(open_positions) > 0:
            syms = [f"#{p.get('ticket')} {p.get('symbol', 'FX')} ({p.get('direction')} {p.get('lot')}L)" for p in open_positions]
            logging.info(f"[PORTFOLIO BUSY] Active positions: {len(open_positions)}/{self.max_concurrent_orders} -> {syms}")
            logging.info("[ZERO-INTERFERENCE ACTIVE] Open order exists. SKIPPING LLM call completely. MT5 hardware SL/TP managing trade.")
            return False

        # 4. เมื่อพอร์ตว่าง (Flat 0 positions) -> รวบรวม Snapshot ของ 7 คู่เงิน
        logging.info(f"[PORTFOLIO FLAT] No active orders for Magic {self.magic}. Building 7-pair M5 snapshot...")
        snapshot = self.build_multi_pair_snapshot()
        if len(snapshot) < 4:
            logging.warning(f"[SNAPSHOT INCOMPLETE] Only {len(snapshot)}/7 pairs returned data. Skipping cycle.")
            return False

        # ป้องกันการวิเคราะห์แท่งซ้ำ
        reference_candle_time = next(iter(snapshot.values())).get("candle_time")
        if reference_candle_time and reference_candle_time == self.last_processed_candle_time:
            logging.info(f"[BAR ALREADY PROCESSED] Candle {reference_candle_time} already analyzed. Skipping.")
            return False

        # 5. เรียก LLM 1 Call วิเคราะห์พร้อมกันทั้ง 7 คู่
        decision = self.call_ai_decision(snapshot)
        if not decision:
            logging.error("[LLM FAIL] No valid response received from LLM.")
            return False

        self.last_processed_candle_time = reference_candle_time

        # 6. ส่งคำสั่งเข้า MT5 ตรง
        return self.execute_decision(decision, snapshot)

    def run_forever(self, run_immediately: bool = True):
        """
        ลูปทำงานอัตโนมัติ ซิงค์เวลากับแท่ง M5 (ทุก 5 นาที)
        """
        logging.info(f"[DAEMON STARTED] Multi-Currency Scalper Daemon running (Magic: {self.magic})")
        
        if run_immediately:
            logging.info("[INITIAL RUN] Running first evaluation immediately...")
            self.run_single_cycle()

        while True:
            try:
                now = datetime.now()
                minutes_to_add = 5 - (now.minute % 5)
                target_time = (now + timedelta(minutes=minutes_to_add)).replace(second=2, microsecond=0)
                
                wait_seconds = (target_time - now).total_seconds()
                if wait_seconds <= 0:
                    wait_seconds += 300
                    target_time += timedelta(minutes=5)

                wait_m = int(wait_seconds) // 60
                wait_s = int(wait_seconds) % 60
                logging.info(f"[WAIT] Next M5 check at {target_time.strftime('%H:%M:%S')} (Waiting {wait_m}m {wait_s}s)...")
                
                time.sleep(wait_seconds)
                self.run_single_cycle()

            except KeyboardInterrupt:
                logging.info("[SHUTDOWN] Engine stopped by user (Ctrl+C).")
                break
            except Exception as e:
                logging.error(f"[LOOP ERROR] Unexpected error: {e}")
                time.sleep(5)


if __name__ == "__main__":
    once = "--once" in sys.argv
    dry = "--dry-run" in sys.argv
    engine = FXMultiCurrencyScalperEngine(dry_run=dry)
    if once:
        engine.run_single_cycle()
    else:
        engine.run_forever(run_immediately=True)
