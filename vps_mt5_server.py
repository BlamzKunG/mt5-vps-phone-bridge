"""
VPS MT5 Bridge Server (FastAPI)
รันบน Windows VPS ที่ติดตั้งโปรแกรม MetaTrader 5 (MT5 Terminal)
ทำหน้าที่เป็น Gateway / Execution Server รับคำสั่งจากโทรศัพท์มือถือและส่งข้อมูล OHLC กลับไป
"""

import os
import sys
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
import pandas as pd
from fastapi import FastAPI, Header, HTTPException, Depends, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

# ติดตั้ง logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MT5BridgeServer")

# ตรวจสอบว่ามีแพ็กเกจ MetaTrader5 หรือไม่
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    logger.error("❌ ไม่พบแพ็กเกจ MetaTrader5 กรุณารัน 'pip install MetaTrader5' บน Windows VPS")

# คอนฟิกความปลอดภัย API Key
API_KEY = os.environ.get("BRIDGE_API_KEY", "llm-trade-secret-key-2026")

app = FastAPI(
    title="MT5 VPS Bridge for LLM Trading",
    description="Bridge API to connect Mobile Trading Engine with MetaTrader 5 on Windows VPS",
    version="1.0.0"
)

# อนุญาต CORS สำหรับการเรียกข้ามโฮสต์
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------- Security Dependency -----------------
def verify_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """ตรวจสอบความถูกต้องของ API Key"""
    if not x_api_key or x_api_key != API_KEY:
        logger.warning(f"⚠️ การเข้าถึงถูกปฏิเสธ: API Key ไม่ถูกต้อง ({x_api_key})")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header"
        )
    return x_api_key

# ----------------- MT5 Handler Class -----------------
class MT5Gateway:
    def __init__(self):
        self.initialized = False
        self.symbol_cache: Dict[str, str] = {}

    def ensure_connected(self) -> bool:
        if not MT5_AVAILABLE:
            return False
        if self.initialized:
            # ตรวจสอบสถานะการเชื่อมต่อ
            if mt5.terminal_info() is not None:
                return True
            logger.warning("⚠️ Terminal หลุดการเชื่อมต่อ กำลังลองเชื่อมต่อใหม่...")

        if not mt5.initialize():
            logger.error(f"❌ mt5.initialize() ล้มเหลว: {mt5.last_error()}")
            self.initialized = False
            return False

        self.initialized = True
        term_info = mt5.terminal_info()
        logger.info(f"✅ เชื่อมต่อ MT5 Terminal สำเร็จ: Connected={term_info.connected if term_info else False}")
        return True

    def resolve_symbol(self, symbol: str) -> str:
        """
        แปลงชื่อคู่เงินให้ตรงกับชื่อบนโบรกเกอร์แบบอัตโนมัติ
        รองรับ suffix ทุกรูปแบบ เช่น:
        - XAUUSD-xxx (XAUUSD-pro, XAUUSD-ecn, XAUUSD-raw)
        - Cent accounts: XAUUSDc, GOLDc, EURUSDc
        - Micro accounts: XAUUSDm, XAUUSD.m, GOLD.m
        - Custom suffixes: XAUUSD_i, XAUUSD+, #XAUUSD, GOLD.pro
        """
        if symbol in self.symbol_cache:
            return self.symbol_cache[symbol]

        if not self.ensure_connected():
            return symbol

        # 1. เช็คชื่อตรงก่อน
        if mt5.symbol_select(symbol, True):
            self.symbol_cache[symbol] = symbol
            return symbol

        # 2. จัดเตรียมคีย์ค้นหาหลัก (Aliases)
        sym_upper = symbol.upper()
        if "XAU" in sym_upper or "GOLD" in sym_upper:
            aliases = ["XAUUSD", "GOLD"]
        elif "BTC" in sym_upper or "BITCOIN" in sym_upper:
            aliases = ["BTCUSD", "BITCOIN"]
        else:
            aliases = [sym_upper]

        # 3. ดึงรายชื่อคู่เงินทั้งหมดจากโบรกเกอร์มาสแกน
        all_syms = mt5.symbols_get()
        if not all_syms:
            logger.warning(f"⚠️ mt5.symbols_get() คืนค่าว่าง กำลังลองค้นหาแบบกลุ่ม...")
            for alias in aliases:
                grouped = mt5.symbols_get(group=f"*{alias}*")
                if grouped:
                    all_syms = grouped
                    break

        if all_syms:
            import re
            candidates = []

            for alias in aliases:
                # Regex รองรับ prefix เช่น # หรือ r และ suffix เช่น -xxx, c, .m, _i, +
                pattern = re.compile(rf"^(#|r)?{re.escape(alias)}([-._+/#]?[a-zA-Z0-9]+)?$", re.IGNORECASE)

                for s in all_syms:
                    s_name = s.name
                    if pattern.match(s_name):
                        # จัดคะแนนความเหมาะสม (Priority Score)
                        score = 0
                        # ถ้าเปิดการเทรดแบบเต็มรูปแบบ (SYMBOL_TRADE_MODE_FULL = 4)
                        trade_mode = getattr(s, "trade_mode", 4)
                        if trade_mode == 4:
                            score += 10
                        elif trade_mode != 0: # ไม่ได้ถูกปิด (Disabled)
                            score += 5

                        # ถ้าเปิดมองเห็นใน Market Watch อยู่แล้ว
                        if getattr(s, "visible", False):
                            score += 3

                        # ให้ความสำคัญกับชื่อที่สั้นกว่า (ใกล้เคียงต้นฉบับมากกว่า)
                        score -= len(s_name) * 0.1

                        candidates.append((score, s_name))

            if candidates:
                # เรียงลำดับจากคะแนนสูงสุด
                candidates.sort(key=lambda x: x[0], reverse=True)
                best_match = candidates[0][1]

                if mt5.symbol_select(best_match, True):
                    logger.info(f"🔍 [Universal Symbol Resolver] จับคู่สำเร็จ: '{symbol}' -> '{best_match}' (เลือกจากผู้สมัคร {len(candidates)} รายการ)")
                    self.symbol_cache[symbol] = best_match
                    return best_match

        # 4. Fallback แบบเดิมกรณีค้นหาไม่พบ
        logger.warning(f"⚠️ ไม่พบสัญลักษณ์ตรงหรือ suffix สำหรับ '{symbol}' บนโบรกเกอร์ ใช้ชื่อเดิม '{symbol}'")
        self.symbol_cache[symbol] = symbol
        return symbol

gateway = MT5Gateway()

# ----------------- Pydantic Models -----------------
class OrderRequest(BaseModel):
    direction: str = Field(..., description="BUY หรือ SELL")
    lot: float = Field(..., gt=0, description="ขนาดล็อต เช่น 0.01")
    sl: Optional[float] = Field(None, description="Stop Loss Price")
    tp: Optional[float] = Field(None, description="Take Profit Price")
    entry: Optional[float] = Field(None, description="ราคา Entry ล่วงหน้ากรณี Pending")
    symbol: str = Field("XAUUSD", description="ชื่อคู่เงิน")
    magic: int = Field(123456, description="Magic Number เพื่อแยกกลยุทธ์")
    force_market: bool = Field(True, description="True เพื่อยิง Market Order ทันที")
    comment: Optional[str] = Field("LLM Trade", description="Comment ออเดอร์")

class CloseRequest(BaseModel):
    ticket: int = Field(..., description="Order Ticket ID ที่ต้องการปิด")
    symbol: Optional[str] = Field("XAUUSD", description="ชื่อคู่เงิน")

class ModifyRequest(BaseModel):
    ticket: int = Field(..., description="Order Ticket ID")
    sl: Optional[float] = Field(None, description="Stop Loss ใหม่")
    tp: Optional[float] = Field(None, description="Take Profit ใหม่")

# ----------------- API Endpoints -----------------

@app.get("/")
def index():
    return {
        "status": "online",
        "service": "MT5 VPS Bridge for LLM Trading",
        "docs": "/docs",
        "mt5_connected": gateway.ensure_connected()
    }

@app.get("/health")
def health_check(auth: str = Depends(verify_api_key)):
    """ตรวจเช็คสุขภาพการเชื่อมต่อ MT5 และข้อมูลบัญชี"""
    if not gateway.ensure_connected():
        return {
            "status": "DISCONNECTED",
            "message": "ไม่สามารถเชื่อมต่อ MT5 Terminal บน VPS ได้",
            "last_error": str(mt5.last_error()) if MT5_AVAILABLE else "No MT5 Library"
        }
    
    acc = mt5.account_info()
    term = mt5.terminal_info()
    return {
        "status": "HEALTHY",
        "terminal_connected": term.connected if term else False,
        "account_login": acc.login if acc else None,
        "account_server": acc.server if acc else None,
        "balance": acc.balance if acc else 0.0,
        "equity": acc.equity if acc else 0.0,
        "server_time": datetime.now().isoformat()
    }

@app.get("/api/account")
def get_account(auth: str = Depends(verify_api_key)):
    """ดึงข้อมูลสถานะพอร์ต Balance, Equity, Margin, Free Margin"""
    if not gateway.ensure_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")
    acc = mt5.account_info()
    if not acc:
        raise HTTPException(status_code=500, detail="Cannot read account info")
    return {
        "login": acc.login,
        "server": acc.server,
        "currency": acc.currency,
        "balance": float(acc.balance),
        "equity": float(acc.equity),
        "margin": float(acc.margin),
        "free_margin": float(acc.margin_free),
        "margin_level": float(acc.margin_level) if acc.margin > 0 else 0.0,
        "floating_pnl": float(acc.profit)
    }

@app.get("/api/price")
def get_price(symbol: str = Query("XAUUSD"), auth: str = Depends(verify_api_key)):
    """ดึงราคา Tick ปัจจุบัน (Bid / Ask / Spread)"""
    if not gateway.ensure_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")
    
    real_sym = gateway.resolve_symbol(symbol)
    tick = mt5.symbol_info_tick(real_sym)
    if not tick:
        raise HTTPException(status_code=404, detail=f"Cannot fetch price for {symbol} ({real_sym})")
    
    sym_info = mt5.symbol_info(real_sym)
    spread = sym_info.spread if sym_info else 0.0
    return {
        "symbol": symbol,
        "resolved_symbol": real_sym,
        "bid": float(tick.bid),
        "ask": float(tick.ask),
        "price": float((tick.bid + tick.ask) / 2.0),
        "spread": float(spread),
        "time": tick.time
    }

@app.get("/api/ohlc")
def get_ohlc(
    symbol: str = Query("XAUUSD"),
    timeframe: str = Query("15m", description="1m, 5m, 15m, 30m, 1h, 4h, 1d"),
    count: int = Query(100, ge=1, le=1000, description="จำนวนแท่งเทียน"),
    auth: str = Depends(verify_api_key)
):
    """
    ดึงแท่งเทียนประวัติศาสตร์ (OHLC) ส่งกลับให้เครื่องโทรศัพท์นำไปวิเคราะห์ด้วย LLM
    """
    if not gateway.ensure_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")
    
    tf_map = {
        "1m": mt5.TIMEFRAME_M1,
        "5m": mt5.TIMEFRAME_M5,
        "15m": mt5.TIMEFRAME_M15,
        "30m": mt5.TIMEFRAME_M30,
        "1h": mt5.TIMEFRAME_H1,
        "4h": mt5.TIMEFRAME_H4,
        "1d": mt5.TIMEFRAME_D1,
    }
    mt5_tf = tf_map.get(timeframe.lower(), mt5.TIMEFRAME_M15)
    real_sym = gateway.resolve_symbol(symbol)

    rates = mt5.copy_rates_from_pos(real_sym, mt5_tf, 0, count)
    if rates is None or len(rates) == 0:
        logger.error(f"❌ ดึง OHLC ล้มเหลว: {mt5.last_error()}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch rates: {mt5.last_error()}")

    # แปลงเป็น JSON ที่คลีนและพร้อมใช้งาน
    df = pd.DataFrame(rates)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s").dt.strftime("%Y-%m-%d %H:%M:%S")
    df = df.rename(columns={"tick_volume": "volume"})
    
    candles = df[["timestamp", "open", "high", "low", "close", "volume"]].to_dict(orient="records")
    return {
        "symbol": symbol,
        "resolved_symbol": real_sym,
        "timeframe": timeframe,
        "count": len(candles),
        "data": candles
    }

@app.get("/api/positions")
def get_positions(
    symbol: Optional[str] = Query(None),
    magic: Optional[int] = Query(None),
    auth: str = Depends(verify_api_key)
):
    """ดึงรายการออเดอร์ที่ถือครองอยู่ ณ ปัจจุบัน"""
    if not gateway.ensure_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")
    
    if symbol:
        real_sym = gateway.resolve_symbol(symbol)
        positions = mt5.positions_get(symbol=real_sym)
    else:
        positions = mt5.positions_get()

    if positions is None:
        return {"positions": []}

    res = []
    for pos in positions:
        if magic is not None and pos.magic != magic:
            continue
        res.append({
            "ticket": pos.ticket,
            "symbol": pos.symbol,
            "direction": "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL",
            "volume": float(pos.volume),
            "open_price": float(pos.price_open),
            "current_price": float(pos.price_current),
            "sl": float(pos.sl),
            "tp": float(pos.tp),
            "pnl": float(pos.profit),
            "magic": pos.magic,
            "comment": pos.comment,
            "open_time": datetime.fromtimestamp(pos.time).strftime("%Y-%m-%d %H:%M:%S")
        })
    return {"positions": res}

@app.post("/api/order")
def send_order(req: OrderRequest, auth: str = Depends(verify_api_key)):
    """
    รับคำสั่งเปิดออเดอร์จากโทรศัพท์มือถือ แล้วส่งเข้า MT5 Terminal บน VPS
    """
    if not gateway.ensure_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    direction = req.direction.upper()
    if direction not in ["BUY", "SELL"]:
        raise HTTPException(status_code=400, detail="Direction must be BUY or SELL")

    real_sym = gateway.resolve_symbol(req.symbol)
    sym_info = mt5.symbol_info(real_sym)
    if not sym_info:
        raise HTTPException(status_code=400, detail=f"Symbol {req.symbol} not found")

    digits = sym_info.digits
    tick_size = sym_info.trade_tick_size or 0.01

    tick = mt5.symbol_info_tick(real_sym)
    if not tick:
        raise HTTPException(status_code=500, detail=f"Cannot fetch price for {real_sym}")

    bid = tick.bid
    ask = tick.ask
    market_price = ask if direction == "BUY" else bid

    # ปรับปัดทศนิยม SL / TP ให้ตรงกับ Tick Size ของโบรกเกอร์
    final_sl = round(round(float(req.sl) / tick_size) * tick_size, digits) if req.sl else 0.0
    final_tp = round(round(float(req.tp) / tick_size) * tick_size, digits) if req.tp else 0.0

    if req.force_market or req.entry is None:
        # Market Order
        target_price = market_price
        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL

        # ป้องกัน SL/TP ผิดฝั่ง
        if direction == "BUY":
            if final_sl > 0.0 and final_sl >= target_price:
                final_sl = 0.0
            if final_tp > 0.0 and final_tp <= target_price:
                final_tp = 0.0
        else: # SELL
            if final_sl > 0.0 and final_sl <= target_price:
                final_sl = 0.0
            if final_tp > 0.0 and final_tp >= target_price:
                final_tp = 0.0

        trade_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": real_sym,
            "volume": float(req.lot),
            "type": order_type,
            "price": target_price,
            "sl": final_sl,
            "tp": final_tp,
            "deviation": 20,
            "magic": int(req.magic),
            "comment": req.comment or "LLM Trade",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
    else:
        # Pending Order
        target_price = round(round(float(req.entry) / tick_size) * tick_size, digits)
        if direction == "BUY":
            order_type = mt5.ORDER_TYPE_BUY_LIMIT if target_price < ask else mt5.ORDER_TYPE_BUY_STOP
        else:
            order_type = mt5.ORDER_TYPE_SELL_LIMIT if target_price > bid else mt5.ORDER_TYPE_SELL_STOP

        trade_request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": real_sym,
            "volume": float(req.lot),
            "type": order_type,
            "price": target_price,
            "sl": final_sl,
            "tp": final_tp,
            "deviation": 20,
            "magic": int(req.magic),
            "comment": req.comment or "LLM Pending",
            "type_time": mt5.ORDER_TIME_GTC,
        }

    logger.info(f"📤 กำลังส่งคำสั่งไป MT5: {direction} {req.lot} lot บน {real_sym} (Price={target_price}, SL={final_sl}, TP={final_tp})")
    result = mt5.order_send(trade_request)

    if result is None:
        err_msg = f"mt5.order_send() คืนค่า None รหัสข้อผิดพลาด: {mt5.last_error()}"
        logger.error(err_msg)
        return {"status": "FAILED", "message": err_msg}

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        err_msg = f"คำสั่งไม่สำเร็จ: {result.comment} (retcode: {result.retcode})"
        logger.error(err_msg)
        return {"status": "FAILED", "retcode": result.retcode, "message": err_msg}

    logger.info(f"🎉 ส่งคำสั่งสำเร็จ! Ticket ID: {result.order}")
    return {
        "status": "SUCCESS",
        "ticket": result.order,
        "volume": result.volume,
        "price": result.price,
        "comment": result.comment
    }

@app.post("/api/close")
def close_order(req: CloseRequest, auth: str = Depends(verify_api_key)):
    """ปิดออเดอร์ตาม Ticket ID"""
    if not gateway.ensure_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    positions = mt5.positions_get(ticket=req.ticket)
    if not positions:
        return {"status": "ERROR", "message": f"ไม่พบออเดอร์ Ticket {req.ticket} (อาจปิดไปแล้ว)"}

    pos = positions[0]
    close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(pos.symbol)
    close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

    close_req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": pos.ticket,
        "symbol": pos.symbol,
        "volume": pos.volume,
        "type": close_type,
        "price": close_price,
        "deviation": 20,
        "magic": pos.magic,
        "comment": "LLM Close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(close_req)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        err_msg = f"ปิดออเดอร์ {req.ticket} ไม่สำเร็จ: {result.comment} (code: {result.retcode})"
        logger.error(err_msg)
        return {"status": "FAILED", "message": err_msg}

    logger.info(f"✅ ปิดออเดอร์ Ticket {req.ticket} สำเร็จ")
    return {"status": "SUCCESS", "ticket": req.ticket, "message": "Position closed successfully"}

@app.post("/api/modify")
def modify_order(req: ModifyRequest, auth: str = Depends(verify_api_key)):
    """ปรับแก้ Stop Loss หรือ Take Profit"""
    if not gateway.ensure_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    positions = mt5.positions_get(ticket=req.ticket)
    if not positions:
        return {"status": "ERROR", "message": f"ไม่พบออเดอร์ Ticket {req.ticket}"}

    pos = positions[0]
    sym_info = mt5.symbol_info(pos.symbol)
    digits = sym_info.digits if sym_info else 2
    tick_size = sym_info.trade_tick_size if sym_info else 0.01

    new_sl = round(round(float(req.sl) / tick_size) * tick_size, digits) if req.sl is not None else pos.sl
    new_tp = round(round(float(req.tp) / tick_size) * tick_size, digits) if req.tp is not None else pos.tp

    mod_req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": pos.ticket,
        "symbol": pos.symbol,
        "sl": new_sl,
        "tp": new_tp,
    }

    result = mt5.order_send(mod_req)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        err_msg = f"แก้ไข SL/TP {req.ticket} ไม่สำเร็จ: {result.comment} (code: {result.retcode})"
        logger.error(err_msg)
        return {"status": "FAILED", "message": err_msg}

    return {"status": "SUCCESS", "ticket": req.ticket, "sl": new_sl, "tp": new_tp}

@app.get("/api/global_variable")
def get_global_variable(name: str = Query(...), auth: str = Depends(verify_api_key)):
    """ดึงค่า MT5 Global Variable (เช่น ค่าจาก Quantum TrendPulse หรือ Custom EA)"""
    if not gateway.ensure_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")
    val = mt5.global_variable_get(name)
    return {"name": name, "value": val}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"🚀 เริ่มต้น VPS MT5 Bridge Server บน Port {port}...")
    uvicorn.run("vps_mt5_server:app", host="0.0.0.0", port=port, reload=False)
