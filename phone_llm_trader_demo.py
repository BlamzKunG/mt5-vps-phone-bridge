"""
Phone LLM Trading Engine (Main Engine)
สคริปต์นี้รันบนโทรศัพท์มือถือ (Termux / Linux) ทำหน้าที่เป็นสมองกลหลัก:
1. ดึงข้อมูลแท่งเทียน OHLC จาก VPS ผ่าน RemoteMT5Bridge
2. สรุปข้อมูลส่งให้ LLM วิเคราะห์ (รองรับ Claude, Groq, OpenAI หรือ TradingAgents)
3. เมื่อ LLM สั่ง BUY/SELL จะส่งคำสั่งไปยิงที่ MT5 บน VPS ทันที
4. ตรวจสอบ PnL และสถานะพอร์ตได้จากหน้าจอมือถือโดยตรง ไม่ต้อง RDP เข้า VPS
"""

import os
import sys
import time
import logging
from datetime import datetime

# นำเข้า Client เชื่อมต่อ VPS
from vps_bridge_client import RemoteMT5Bridge

# ตรวจสอบว่ามีโมดูล TradingAgents เดิมในระบบหรือไม่
try:
    from trading_agents import TradingAgents
    TRADING_AGENTS_AVAILABLE = True
except ImportError:
    TRADING_AGENTS_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger("PhoneLLMEngine")

# คอนฟิกการเชื่อมต่อไปยัง VPS
VPS_HOST = os.environ.get("VPS_URL", "http://127.0.0.1:8000")  # เปลี่ยนเป็น IP ของ VPS เช่น http://100.xx.xx.xx:8000 (Tailscale) หรือ http://x.x.x.x:8000
VPS_API_KEY = os.environ.get("BRIDGE_API_KEY", "llm-trade-secret-key-2026")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "your-llm-api-key")

class PhoneLLMTrader:
    def __init__(self, vps_url: str, api_key: str, symbol: str = "XAUUSD"):
        self.symbol = symbol
        self.bridge = RemoteMT5Bridge(vps_url=vps_url, api_key=api_key)
        self.magic_number = 888999  # Magic สำหรับ Phone LLM Trader
        
        # เตรียมตัวแทน TradingAgents (ถ้ามี API Key)
        if TRADING_AGENTS_AVAILABLE and LLM_API_KEY and LLM_API_KEY != "your-llm-api-key":
            self.agents = TradingAgents(api_key=LLM_API_KEY)
            logger.info("🧠 โหลดโมดูล TradingAgents (Multi-Agent System) สำเร็จ")
        else:
            self.agents = None
            logger.info("ℹ️ ทำงานในโหมด Standalone / Custom Prompt")

    def run_check_and_trade(self, timeframe: str = "5m", count: int = 50, lot_size: float = 0.01):
        """
        ทำงาน 1 รอบวงจร:
        ดึงราคา -> ดึง OHLC -> ส่ง LLM วิเคราะห์ -> ส่งคำสั่งถ้ามีสัญญาณ
        """
        logger.info(f"\n{'='*50}\n📱 [Phone Engine] เริ่มต้นรอบการวิเคราะห์ {self.symbol} ({timeframe})\n{'='*50}")

        # 1. ตรวจสอบการเชื่อมต่อ VPS & ดึงข้อมูลพอร์ต
        acc = self.bridge.get_account_status()
        if not acc:
            logger.error("❌ ไม่สามารถดึงข้อมูลพอร์ตจาก VPS ได้ กรุณาตรวจสอบว่า VPS เปิดสคริปต์อยู่หรือไม่")
            return

        balance = acc["balance"]
        equity = acc["equity"]
        floating_pnl = acc["floating_pnl"]
        logger.info(f"💰 พอร์ตปัจจุบัน: Balance=${balance:.2f} | Equity=${equity:.2f} | PnL=${floating_pnl:+.2f}")

        # 2. ดึงราคาปัจจุบัน
        price_info = self.bridge.get_current_price(self.symbol)
        if not price_info:
            logger.error(f"❌ ดึงราคาปัจจุบันของ {self.symbol} ไม่สำเร็จ")
            return
        
        current_price = price_info["price"]
        spread = price_info["spread"]
        logger.info(f"📊 ราคา {self.symbol}: {current_price:.2f} (Spread: {spread})")

        # 3. ดึงแท่งเทียน OHLC ย้อนหลังจาก VPS
        logger.info(f"📥 กำลังขอข้อมูล OHLC ({timeframe}, {count} แท่ง) จาก VPS...")
        df = self.bridge.get_historical_data(symbol=self.symbol, timeframe=timeframe, num_candles=count)
        if df.empty:
            logger.error("❌ ไม่ได้รับข้อมูลแท่งเทียนจาก VPS")
            return

        last_candle = df.iloc[-1]
        logger.info(f"✅ ได้รับ OHLC สำเร็จ {len(df)} แท่ง | แท่งล่าสุด: {last_candle['timestamp']} C={last_candle['close']}")

        # 4. ตรวจสอบว่ามี Position ค้างอยู่หรือไม่
        open_positions = self.bridge.get_open_positions(symbol=self.symbol, magic=self.magic_number)
        if open_positions:
            logger.info(f"🔒 มีออเดอร์เปิดค้างอยู่ {len(open_positions)} ออเดอร์ (ข้ามการเปิดเพิ่มเพื่อคุมความเสี่ยง)")
            for p in open_positions:
                logger.info(f"   -> Ticket: {p['id']} | ฝั่ง: {p['direction']} {p['lot']} lot | กำไร: ${p['pnl']:+.2f}")
            return

        # 5. ส่งข้อมูลให้ LLM วิเคราะห์
        decision = self.ask_llm_decision(df, current_price, balance)
        action = decision.get("action", "HOLD").upper()
        confidence = decision.get("confidence", 0)
        reason = decision.get("reasoning", "ไม่มีเหตุผล")

        logger.info(f"\n🧠 ผลลัพธ์จาก LLM: Action={action} (ความมั่นใจ: {confidence}%)")
        logger.info(f"💡 เหตุผล: {reason}\n")

        # 6. ส่งคำสั่งไปยัง MT5 บน VPS ตามผลลัพธ์ LLM
        if action in ["BUY", "SELL"] and confidence >= 65:
            # คำนวณ SL / TP อิงโครงสร้างราคา (เช่น SL 500 จุด, TP 800 จุด)
            is_gold = "XAU" in self.symbol.upper() or "GOLD" in self.symbol.upper()
            point_val = 0.1 if is_gold else 0.0001
            sl_distance = 50.0 if is_gold else 0.0050
            tp_distance = 80.0 if is_gold else 0.0080

            sl_price = current_price - sl_distance if action == "BUY" else current_price + sl_distance
            tp_price = current_price + tp_distance if action == "BUY" else current_price - tp_distance

            logger.info(f"🚀 เตรียมส่งคำสั่ง {action} {lot_size} lot (SL: {sl_price:.2f}, TP: {tp_price:.2f})...")
            
            result = self.bridge.open_position(
                direction=action,
                lot=lot_size,
                sl=sl_price,
                tp=tp_price,
                symbol=self.symbol,
                magic=self.magic_number,
                force_market=True,
                comment=f"LLM Conf:{confidence}%"
            )
            
            if result.get("status") == "SUCCESS":
                logger.info(f"🎉 ออเดอร์เปิดสำเร็จใน MT5! Ticket: {result.get('ticket')}")
            else:
                logger.error(f"❌ ส่งออเดอร์ล้มเหลว: {result.get('message')}")
        else:
            logger.info("⏸️ สัญญาณเป็น HOLD หรือความมั่นใจต่ำกว่าเกณฑ์ พักดูเชิงตลาด")

    def ask_llm_decision(self, df, current_price, balance) -> dict:
        """ส่งแท่งเทียนให้ LLM ประมวลผล"""
        if self.agents:
            try:
                # เรียกใช้ Custom Agent ของ TradingAgents
                return self.agents.analyze_custom_agent(
                    df_5m=df,
                    balance=balance,
                    symbol=self.symbol,
                    nohold_mode=False
                )
            except Exception as e:
                logger.warning(f"เรียก TradingAgents ล้มเหลว สลับไปใช้โหมด Rule-based fallback: {e}")

        # Fallback Simulation / Simple Logic หากยังไม่ได้ผูก LLM API Key
        # ตัวอย่างการคำนวณ EMA ง่ายๆ เป็นตัวอย่าง
        ma20 = df['close'].tail(20).mean()
        if current_price > ma20 * 1.001:
            return {"action": "BUY", "confidence": 75, "reasoning": "ราคาปิดยืนเหนือ MA20 มีโมเมนตัมขาขึ้น"}
        elif current_price < ma20 * 0.999:
            return {"action": "SELL", "confidence": 75, "reasoning": "ราคาปิดหลุดต่ำกว่า MA20 มีโมเมนตัมขาลง"}
        else:
            return {"action": "HOLD", "confidence": 50, "reasoning": "ราคาวิ่งไซด์เวย์ติดกรอบกลาง"}

if __name__ == "__main__":
    print("""
    =======================================================
       📱 PHONE LLM TRADING ENGINE -> VPS MT5 GATEWAY
    =======================================================
    """)
    trader = PhoneLLMTrader(vps_url=VPS_HOST, api_key=VPS_API_KEY, symbol="XAUUSD")

    # ทดสอบการเชื่อมต่อ
    if trader.bridge.connect():
        print("\n🚀 เริ่มต้นรันรอบแรกทันที...")
        trader.run_check_and_trade(timeframe="5m", count=50, lot_size=0.01)
    else:
        print("\n❌ ไม่สามารถเชื่อมต่อ VPS ได้! โปรดตรวจสอบ URL และ API Key ในตัวแปร VPS_URL")
