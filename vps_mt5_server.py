"""
VPS MT5 Bridge Server (FastAPI)
รันบน Windows VPS ที่ติดตั้งโปรแกรม MetaTrader 5 (MT5 Terminal)
ทำหน้าที่เป็น Gateway / Execution Server รับคำสั่งจากโทรศัพท์มือถือและส่งข้อมูล OHLC กลับไป
"""

import os
import sys
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
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

class MT5Gateway:
    def __init__(self):
        self.initialized = False
        self.symbol_cache: Dict[str, str] = {}
        self.custom_mappings: Dict[str, str] = self._load_initial_mappings()

    def _load_initial_mappings(self) -> Dict[str, str]:
        """โหลดคู่แมปสัญลักษณ์แบบกำหนดเองจาก Environment Variable SYMBOL_MAP"""
        import json
        mappings = {}
        raw = os.environ.get("SYMBOL_MAP", "").strip()
        if not raw:
            return mappings
        try:
            if raw.startswith("{"):
                return json.loads(raw)
            for pair in raw.split(","):
                if ":" in pair:
                    k, v = pair.split(":", 1)
                    mappings[k.strip().upper()] = v.strip()
        except Exception as e:
            logger.warning(f"⚠️ ไม่สามารถแปลงค่า SYMBOL_MAP ({raw}): {e}")
        return mappings

    def ensure_connected(self) -> bool:
        if not MT5_AVAILABLE:
            return False
        if self.initialized:
            # ตรวจสอบสถานะการเชื่อมต่อ
            term = mt5.terminal_info()
            if term is not None and getattr(term, "connected", False):
                return True
            logger.warning("⚠️ Terminal หลุดการเชื่อมต่อหรือเน็ตหลุด กำลังลองเชื่อมต่อใหม่...")

        # ดึงค่าล็อกอินอัตโนมัติจาก Environment (หากมีกำหนดไว้)
        login = os.environ.get("MT5_LOGIN")
        password = os.environ.get("MT5_PASSWORD")
        server = os.environ.get("MT5_SERVER")
        path = os.environ.get("MT5_PATH")

        init_kwargs = {}
        if path:
            init_kwargs["path"] = path
        if login and password and server:
            try:
                init_kwargs["login"] = int(login)
                init_kwargs["password"] = password
                init_kwargs["server"] = server
                logger.info(f"🔑 กำลังล็อกอินเข้า MT5 อัตโนมัติ: บัญชี {login} @ {server}...")
            except ValueError:
                logger.warning(f"⚠️ MT5_LOGIN ไม่ถูกต้อง ({login}) ต้องเป็นตัวเลขเท่านั้น")

        if not mt5.initialize(**init_kwargs):
            logger.error(f"❌ mt5.initialize() ล้มเหลว: {mt5.last_error()}")
            self.initialized = False
            return False

        self.initialized = True
        term_info = mt5.terminal_info()
        logger.info(f"✅ เชื่อมต่อ MT5 Terminal สำเร็จ: Connected={term_info.connected if term_info else False}")
        return True

    def get_filling_mode(self, sym_info) -> int:
        """ตรวจหา Filling Mode ที่เหมาะสมจากคู่เงินของโบรกเกอร์ (IOC, FOK, RETURN)"""
        filling_mode = getattr(sym_info, "filling_mode", 0)
        # Bit 2 = IOC (Immediate Or Cancel)
        if filling_mode & 2:
            return mt5.ORDER_FILLING_IOC
        # Bit 1 = FOK (Fill Or Kill)
        elif filling_mode & 1:
            return mt5.ORDER_FILLING_FOK
        else:
            return mt5.ORDER_FILLING_RETURN

    def execute_with_fill_retry(self, request_dict: dict) -> Any:
        """
        ส่งคำสั่งซื้อขายและลองสลับ Filling Mode อัตโนมัติ
        ป้องกันข้อผิดพลาด 10030 (Unsupported filling mode) ซึ่งพบบ่อยในบัญชี ECN/Raw/Cent
        """
        initial_mode = request_dict.get("type_filling", mt5.ORDER_FILLING_IOC)
        modes_to_try = [initial_mode]
        for alt in [mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN, mt5.ORDER_FILLING_IOC]:
            if alt not in modes_to_try:
                modes_to_try.append(alt)

        last_res = None
        for mode in modes_to_try:
            request_dict["type_filling"] = mode
            last_res = mt5.order_send(request_dict)
            if last_res is not None and last_res.retcode == mt5.TRADE_RETCODE_DONE:
                return last_res
            # retcode 10030 = TRADE_RETCODE_INVALID_FILL
            if last_res is not None and last_res.retcode == 10030:
                logger.warning(f"⚠️ โบรกเกอร์ไม่รองรับ Filling Mode {mode} กำลังลองโหมดสำรองถัดไป...")
                continue
            # หากติด Error อื่น (เช่น Margin ไม่พอ, ตลาดปิด) ให้หยุดทันที ไม่ส่งซ้ำ
            break
        return last_res

    def resolve_symbol(self, symbol: str) -> str:
        """
        แปลงชื่อคู่เงินให้ตรงกับชื่อบนโบรกเกอร์แบบอัตโนมัติ
        ลำดับความสำคัญ:
        0. ตรวจสอบ Custom Mapping ที่ผู้ใช้กำหนดเอง (เช่น XAUUSD -> XAUUSD247 หรือ XAUUSD.crp)
        1. ตรวจสอบชื่อตรง (หากผู้ใช้ส่ง XAUUSD247 หรือ XAUUSD-ECN มาตรงๆ จะใช้ชื่อนั้นทันที)
        2. Auto-Discovery สแกน Suffix ทั้งหมด (-ECN, .kio, c, m, pro)
        3. Priority Scoring คัดกรองคู่เงินที่ trade_mode=FULL
        """
        sym_clean = symbol.strip()
        sym_upper = sym_clean.upper()

        # 0. ตรวจสอบ Custom Mapping ที่กำหนดเองก่อนเป็นอันดับแรก
        if sym_upper in self.custom_mappings:
            target = self.custom_mappings[sym_upper]
            if not self.ensure_connected():
                return target
            if mt5.symbol_select(target, True):
                logger.info(f"🎯 [Explicit Mapping] ใช้การจับคู่แบบกำหนดเอง: '{symbol}' -> '{target}'")
                return target
            logger.warning(f"⚠️ สัญลักษณ์ที่กำหนดเอง '{target}' ไม่พบใน MT5 กำลัง fallback สแกนอัตโนมัติ...")

        # ตรวจสอบจาก Cache
        if sym_clean in self.symbol_cache:
            return self.symbol_cache[sym_clean]

        if not self.ensure_connected():
            return sym_clean

        # 1. เช็คชื่อตรงก่อน (ถ้าส่ง XAUUSD247 หรือ XAUUSD.crp หรือ XAUUSD-ECN มาตรงๆ)
        if mt5.symbol_select(sym_clean, True):
            self.symbol_cache[sym_clean] = sym_clean
            return sym_clean

        # 2. จัดเตรียมคีย์ค้นหาหลัก (Aliases)
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
                # Regex รองรับ prefix (#, r) และ suffix ทุกชนิด เช่น -ECN, .kio, c, m, 247, .crp
                pattern = re.compile(rf"^(#|r)?{re.escape(alias)}([-._+/#]?[a-zA-Z0-9]+)?$", re.IGNORECASE)

                for s in all_syms:
                    s_name = s.name
                    if pattern.match(s_name):
                        score = 0
                        # 3.1 สถานะการเทรด (SYMBOL_TRADE_MODE_FULL = 4)
                        trade_mode = getattr(s, "trade_mode", 4)
                        if trade_mode == 4:
                            score += 10
                        elif trade_mode != 0: # ไม่ได้ Disabled
                            score += 5

                        # 3.2 เปิดมองเห็นใน Market Watch อยู่แล้ว
                        if getattr(s, "visible", False):
                            score += 3

                        # 3.3 ตรวจสอบความสอดคล้องกับเจตนาของผู้ใช้:
                        # หากผู้ใช้ขอ XAUUSD ธรรมดา (ไม่ได้ระบุ 247 หรือ crp)
                        # ให้คะแนนคู่ Spot (-ECN, .kio, .pro, c, m) สูงกว่า Synthetic/Crypto
                        s_name_upper = s_name.upper()
                        is_exotic = any(k in s_name_upper for k in ["247", "CRP", "CRYPTO", "SYN"])
                        user_wants_exotic = any(k in sym_upper for k in ["247", "CRP", "CRYPTO", "SYN"])
                        if is_exotic and not user_wants_exotic:
                            score -= 8  # หักคะแนนคู่ 247/crp หากมีคู่ spot ปกติเปิดให้เทรด

                        # 3.4 ให้ความสำคัญกับชื่อที่สั้นกว่า
                        score -= len(s_name) * 0.1

                        candidates.append((score, s_name))

            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                best_match = candidates[0][1]

                if mt5.symbol_select(best_match, True):
                    logger.info(f"🔍 [Universal Symbol Resolver] จับคู่สำเร็จ: '{symbol}' -> '{best_match}' (เลือกจากผู้สมัคร {len(candidates)} รายการ)")
                    self.symbol_cache[sym_clean] = best_match
                    return best_match

        # 4. Fallback แบบเดิมกรณีค้นหาไม่พบ
        logger.warning(f"⚠️ ไม่พบสัญลักษณ์ตรงหรือ suffix สำหรับ '{symbol}' บนโบรกเกอร์ ใช้ชื่อเดิม '{symbol}'")
        self.symbol_cache[sym_clean] = sym_clean
        return sym_clean

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

class SymbolMapRequest(BaseModel):
    mappings: Dict[str, str] = Field(..., description="พจนานุกรมจับคู่สัญลักษณ์ เช่น {'XAUUSD': 'XAUUSD247'}")

# ----------------- API Endpoints -----------------

@app.get("/")
def index():
    return {
        "status": "online",
        "service": "MT5 VPS Bridge for LLM Trading",
        "docs": "/docs",
        "mt5_connected": gateway.ensure_connected(),
        "custom_mappings": gateway.custom_mappings
    }

@app.get("/api/symbols")
def list_available_symbols(search: Optional[str] = Query("XAU"), auth: str = Depends(verify_api_key)):
    """
    ค้นหารายชื่อคู่เงินบนโบรกเกอร์ (เช่น ค้นหา XAU จะเห็น XAUUSD, XAUUSD-ECN, XAUUSD.crp, XAUUSD247 ทั้งหมด)
    ส่งกลับมาให้มือถือดูได้เลยว่าโบรกเกอร์มีคู่เงินอะไรบ้าง
    """
    if not gateway.ensure_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    all_syms = mt5.symbols_get()
    if not all_syms:
        return {"symbols": []}

    search_upper = search.upper() if search else ""
    trade_mode_map = {0: "DISABLED", 1: "LONGONLY", 2: "SHORTONLY", 3: "CLOSEONLY", 4: "FULL"}

    results = []
    for s in all_syms:
        s_name_upper = s.name.upper()
        s_desc_upper = (s.description or "").upper()
        if not search_upper or search_upper in s_name_upper or search_upper in s_desc_upper:
            results.append({
                "name": s.name,
                "description": s.description,
                "trade_mode": trade_mode_map.get(s.trade_mode, str(s.trade_mode)),
                "is_tradeable": (s.trade_mode == 4),
                "visible": s.visible,
                "digits": s.digits,
                "spread": s.spread,
                "point": s.point
            })
    return {
        "search": search,
        "count": len(results),
        "symbols": results
    }

@app.get("/api/config/symbol_map")
def get_symbol_map(auth: str = Depends(verify_api_key)):
    """ดูตารางการจับคู่สัญลักษณ์แบบกำหนดเองในปัจจุบัน"""
    return {
        "custom_mappings": gateway.custom_mappings,
        "cached_symbols": gateway.symbol_cache
    }

@app.post("/api/config/symbol_map")
def set_symbol_map(req: SymbolMapRequest, auth: str = Depends(verify_api_key)):
    """กำหนดหรืออัปเดตการจับคู่สัญลักษณ์แบบเฉพาะเจาะจงจากโทรศัพท์มือถือ"""
    for k, v in req.mappings.items():
        clean_k = k.strip().upper()
        clean_v = v.strip()
        gateway.custom_mappings[clean_k] = clean_v
        # เคลียร์แคชเพื่อให้เริ่มใช้การจับคู่ใหม่ทันที
        if clean_k in gateway.symbol_cache:
            del gateway.symbol_cache[clean_k]
    logger.info(f"🔄 [Config] อัปเดต Custom Symbol Map: {gateway.custom_mappings}")
    return {
        "status": "SUCCESS",
        "custom_mappings": gateway.custom_mappings
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

@app.get("/api/history")
@app.get("/api/deals")
def get_history_deals(
    symbol: Optional[str] = Query(None),
    magic: Optional[int] = Query(None),
    days: int = Query(7),
    ticket: Optional[int] = Query(None),
    position_id: Optional[int] = Query(None),
    auth: str = Depends(verify_api_key)
):
    """
    ดึงประวัติ Deals (การซื้อขาย/ปิดออเดอร์ที่เกิดขึ้นแล้ว) จาก MT5 Terminal
    """
    if not gateway.ensure_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")
    
    now = datetime.now()
    date_from = now - timedelta(days=days)
    date_to = now + timedelta(days=1)
    
    if position_id is not None:
        deals = mt5.history_deals_get(position=position_id)
    elif ticket is not None:
        deals = mt5.history_deals_get(ticket=ticket)
    else:
        deals = mt5.history_deals_get(date_from, date_to)
        
    if deals is None:
        return {"deals": []}
        
    real_sym = gateway.resolve_symbol(symbol) if symbol else None
    res = []
    for d in deals:
        if real_sym and d.symbol and d.symbol != real_sym:
            continue
        if magic is not None and d.magic != magic:
            continue
            
        entry_type = "IN" if d.entry == 0 else ("OUT" if d.entry == 1 else "INOUT")
        deal_dir = "BUY" if d.type == 0 else ("SELL" if d.type == 1 else "BALANCE")
        
        res.append({
            "ticket": d.ticket,
            "order": d.order,
            "position_id": d.position_id,
            "time": datetime.fromtimestamp(d.time).strftime("%Y-%m-%d %H:%M:%S") if d.time else None,
            "timestamp": d.time,
            "direction": deal_dir,
            "entry_type": entry_type,
            "symbol": d.symbol,
            "volume": float(d.volume),
            "price": float(d.price),
            "profit": float(d.profit),
            "commission": float(d.commission),
            "swap": float(d.swap),
            "fee": float(d.fee),
            "magic": d.magic,
            "comment": d.comment
        })
    return {"deals": res}

@app.get("/api/history_orders")
def get_history_orders(
    symbol: Optional[str] = Query(None),
    magic: Optional[int] = Query(None),
    days: int = Query(7),
    ticket: Optional[int] = Query(None),
    position_id: Optional[int] = Query(None),
    auth: str = Depends(verify_api_key)
):
    """
    ดึงประวัติ Orders ในอดีตจาก MT5 Terminal
    """
    if not gateway.ensure_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")
        
    now = datetime.now()
    date_from = now - timedelta(days=days)
    date_to = now + timedelta(days=1)
    
    if position_id is not None:
        orders = mt5.history_orders_get(position=position_id)
    elif ticket is not None:
        orders = mt5.history_orders_get(ticket=ticket)
    else:
        orders = mt5.history_orders_get(date_from, date_to)
        
    if orders is None:
        return {"orders": []}
        
    real_sym = gateway.resolve_symbol(symbol) if symbol else None
    res = []
    for o in orders:
        if real_sym and o.symbol and o.symbol != real_sym:
            continue
        if magic is not None and o.magic != magic:
            continue
            
        res.append({
            "ticket": o.ticket,
            "position_id": o.position_id,
            "symbol": o.symbol,
            "type": "BUY" if o.type == 0 else ("SELL" if o.type == 1 else str(o.type)),
            "state": o.state,
            "volume": float(o.volume_initial),
            "price_open": float(o.price_open),
            "sl": float(o.sl),
            "tp": float(o.tp),
            "magic": o.magic,
            "comment": o.comment,
            "time_setup": datetime.fromtimestamp(o.time_setup).strftime("%Y-%m-%d %H:%M:%S") if o.time_setup else None,
            "time_done": datetime.fromtimestamp(o.time_done).strftime("%Y-%m-%d %H:%M:%S") if o.time_done else None
        })
    return {"orders": res}

@app.get("/api/closed_trades")
def get_closed_trades(
    symbol: Optional[str] = Query(None),
    magic: Optional[int] = Query(None),
    days: int = Query(7),
    auth: str = Depends(verify_api_key)
):
    """
    ดึงสรุปออเดอร์ที่ปิดเรียบร้อยแล้ว (Closed Positions) จาก Deals
    """
    if not gateway.ensure_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")
        
    now = datetime.now()
    date_from = now - timedelta(days=days)
    date_to = now + timedelta(days=1)
    
    deals = mt5.history_deals_get(date_from, date_to)
    if deals is None:
        return {"trades": []}
        
    real_sym = gateway.resolve_symbol(symbol) if symbol else None
    
    pos_map: Dict[int, Dict[str, Any]] = {}
    for d in deals:
        if real_sym and d.symbol and d.symbol != real_sym:
            continue
        if magic is not None and d.magic != magic:
            continue
            
        pos_id = d.position_id
        if not pos_id:
            continue
            
        if pos_id not in pos_map:
            pos_map[pos_id] = {
                "ticket": pos_id,
                "symbol": d.symbol,
                "direction": "BUY" if d.type == 0 else "SELL",
                "volume": float(d.volume),
                "open_price": None,
                "close_price": None,
                "open_time": None,
                "close_time": None,
                "profit": 0.0,
                "commission": 0.0,
                "swap": 0.0,
                "magic": d.magic,
                "comment": d.comment
            }
            
        rec = pos_map[pos_id]
        rec["commission"] += float(d.commission)
        rec["swap"] += float(d.swap)
        rec["profit"] += float(d.profit)
        
        if d.entry == 0:
            rec["open_price"] = float(d.price)
            rec["open_time"] = datetime.fromtimestamp(d.time).strftime("%Y-%m-%d %H:%M:%S")
            rec["direction"] = "BUY" if d.type == 0 else "SELL"
        elif d.entry == 1:
            rec["close_price"] = float(d.price)
            rec["close_time"] = datetime.fromtimestamp(d.time).strftime("%Y-%m-%d %H:%M:%S")

    trades = []
    for pos_id, t in pos_map.items():
        if t["close_time"] is not None or t["close_price"] is not None:
            t["net_pnl"] = round(t["profit"] + t["commission"] + t["swap"], 2)
            trades.append(t)
            
    trades.sort(key=lambda x: x["close_time"] or "", reverse=True)
    return {"trades": trades}

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

        initial_filling = gateway.get_filling_mode(sym_info)
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
            "type_filling": initial_filling,
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
    result = gateway.execute_with_fill_retry(trade_request)

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
    try:
        if not gateway.ensure_connected():
            raise HTTPException(status_code=503, detail="MT5 not connected")

        # ค้นหา Position ด้วย Ticket ID (มี Fallback สแกนทั้งหมดหาก MT5 ticket filter ไม่ตอบสนอง)
        pos = None
        positions = mt5.positions_get(ticket=int(req.ticket))
        if positions and len(positions) > 0:
            pos = positions[0]
        else:
            all_positions = mt5.positions_get()
            if all_positions:
                for p in all_positions:
                    if p.ticket == int(req.ticket):
                        pos = p
                        break

        if not pos:
            return {"status": "ERROR", "message": f"ไม่พบออเดอร์ Ticket {req.ticket} ในระบบ (อาจถูกปิดหรือชน SL/TP ไปแล้ว)"}

        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        sym_info = mt5.symbol_info(pos.symbol)
        initial_filling = gateway.get_filling_mode(sym_info) if sym_info else mt5.ORDER_FILLING_IOC

        tick = mt5.symbol_info_tick(pos.symbol)
        if not tick:
            return {"status": "ERROR", "message": f"ไม่สามารถดึงราคาปัจจุบันของ {pos.symbol} ได้"}
        close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

        close_req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": int(pos.ticket),
            "symbol": pos.symbol,
            "volume": float(pos.volume),
            "type": close_type,
            "price": float(close_price),
            "deviation": 20,
            "magic": int(pos.magic),
            "comment": "LLM Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": initial_filling,
        }

        result = gateway.execute_with_fill_retry(close_req)
        if result is None:
            err_msg = f"mt5.order_send() คืนค่า None รหัสข้อผิดพลาด: {mt5.last_error()}"
            logger.error(err_msg)
            return {"status": "FAILED", "message": err_msg}

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            err_msg = f"ปิดออเดอร์ {req.ticket} ไม่สำเร็จ: {result.comment} (code: {result.retcode})"
            logger.error(err_msg)
            return {"status": "FAILED", "message": err_msg}

        logger.info(f"✅ ปิดออเดอร์ Ticket {req.ticket} สำเร็จ")
        return {"status": "SUCCESS", "ticket": req.ticket, "message": "Position closed successfully"}
    except Exception as e:
        logger.exception(f"❌ เกิดข้อผิดพลาดใน close_order: {e}")
        return {"status": "ERROR", "message": str(e)}

@app.post("/api/modify")
def modify_order(req: ModifyRequest, auth: str = Depends(verify_api_key)):
    """ปรับแก้ Stop Loss หรือ Take Profit"""
    try:
        if not gateway.ensure_connected():
            raise HTTPException(status_code=503, detail="MT5 not connected")

        # ค้นหา Position ด้วย Ticket ID (มี Fallback สแกนทั้งหมด)
        pos = None
        positions = mt5.positions_get(ticket=int(req.ticket))
        if positions and len(positions) > 0:
            pos = positions[0]
        else:
            all_positions = mt5.positions_get()
            if all_positions:
                for p in all_positions:
                    if p.ticket == int(req.ticket):
                        pos = p
                        break

        if not pos:
            return {"status": "ERROR", "message": f"ไม่พบออเดอร์ Ticket {req.ticket} ในระบบ"}

        sym_info = mt5.symbol_info(pos.symbol)
        digits = sym_info.digits if sym_info else 2
        tick_size = sym_info.trade_tick_size if sym_info else 0.01

        new_sl = round(round(float(req.sl) / tick_size) * tick_size, digits) if req.sl is not None else float(pos.sl)
        new_tp = round(round(float(req.tp) / tick_size) * tick_size, digits) if req.tp is not None else float(pos.tp)

        mod_req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": int(pos.ticket),
            "symbol": pos.symbol,
            "sl": float(new_sl),
            "tp": float(new_tp),
        }

        result = mt5.order_send(mod_req)
        # หากบางโบรกเกอร์ปฏิเสธคำสั่งที่มีฟิลด์ 'symbol' ให้ลองส่งแบบไม่มี 'symbol'
        if result is None or (result.retcode != mt5.TRADE_RETCODE_DONE and "symbol" in mod_req):
            mod_req_fallback = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": int(pos.ticket),
                "sl": float(new_sl),
                "tp": float(new_tp),
            }
            res_alt = mt5.order_send(mod_req_fallback)
            if res_alt is not None and res_alt.retcode == mt5.TRADE_RETCODE_DONE:
                result = res_alt

        if result is None:
            err_msg = f"mt5.order_send() คืนค่า None รหัสข้อผิดพลาด: {mt5.last_error()}"
            logger.error(err_msg)
            return {"status": "FAILED", "message": err_msg}

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            err_msg = f"แก้ไข SL/TP {req.ticket} ไม่สำเร็จ: {result.comment} (code: {result.retcode})"
            logger.error(err_msg)
            return {"status": "FAILED", "message": err_msg}

        logger.info(f"✅ แก้ไข SL/TP Ticket {req.ticket} สำเร็จ: SL={new_sl}, TP={new_tp}")
        return {"status": "SUCCESS", "ticket": req.ticket, "sl": new_sl, "tp": new_tp}
    except Exception as e:
        logger.exception(f"❌ เกิดข้อผิดพลาดใน modify_order: {e}")
        return {"status": "ERROR", "message": str(e)}

@app.get("/api/global_variable")
def get_global_variable(name: str = Query(...), auth: str = Depends(verify_api_key)):
    """ดึงค่า MT5 Global Variable (เช่น ค่าจาก Quantum TrendPulse หรือ Custom EA)"""
    if not gateway.ensure_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")
    val = mt5.global_variable_get(name)
    return {"name": name, "value": val}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("vps_mt5_server:app", host="0.0.0.0", port=port, reload=False, timeout_keep_alive=65)
