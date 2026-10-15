"""
Remote MT5 Bridge Client (รันบนเครื่องโทรศัพท์มือถือ หรือ Main Engine)
เชื่อมต่อไปยัง VPS MT5 Bridge Server ผ่าน REST API
ให้ Interface เหมือนกับ MT5Integration ใน mt5_integration.py แบบ 1:1
ทำให้บอท LLM หรือสคริปต์เทรดรันบนมือถือได้โดยตรง ไม่ต้องเปิดรีโมทเข้า VPS
"""

import logging
import time
import requests
import pandas as pd
from typing import Optional, Dict, List, Any

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class RemoteMT5Bridge:
    """
    คลาส Client สำหรับเชื่อมต่อกับ Windows VPS ผ่าน HTTP API
    จำลองการทำงานเหมือนต่อตรงกับ MT5 บนเครื่อง แต่ข้อมูลทุกอย่างคุยผ่าน VPS
    """
    def __init__(self, vps_url: str = "http://127.0.0.1:8000", api_key: str = "llm-trade-secret-key-2026", timeout: int = 15):
        # ลบ trailing slash ถ้ามี
        self.vps_url = vps_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json"
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.initialized = False

    def connect(self) -> bool:
        """ตรวจสอบและเชื่อมต่อกับ VPS MT5 Bridge"""
        try:
            resp = self.session.get(f"{self.vps_url}/health", timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "HEALTHY":
                    self.initialized = True
                    login = data.get("account_login")
                    server = data.get("account_server")
                    balance = data.get("balance", 0.0)
                    logging.info(f"✅ เชื่อมต่อ VPS สำเร็จ! MT5 บัญชี: {login} @ {server} (Balance: ${balance:.2f})")
                    return True
                else:
                    logging.warning(f"⚠️ VPS ออนไลน์แต่ MT5 ยังไม่พร้อม: {data.get('message')}")
                    return False
            elif resp.status_code == 401:
                logging.error("❌ เข้าถึง VPS ไม่สำเร็จ: API Key (X-API-Key) ไม่ถูกต้อง!")
                return False
            else:
                logging.error(f"❌ VPS ส่งสถานะผิดพลาด: HTTP {resp.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            logging.error(f"❌ ไม่สามารถเชื่อมต่อ VPS ที่ {self.vps_url} ได้: {e}")
            self.initialized = False
            return False

    def check_health(self) -> Dict[str, Any]:
        """เรียกดูสถานะเซิร์ฟเวอร์แบบละเอียด"""
        try:
            resp = self.session.get(f"{self.vps_url}/health", timeout=self.timeout)
            return resp.json()
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def get_current_price(self, symbol: str = "XAUUSD") -> Optional[Dict[str, float]]:
        """
        ดึงราคา Bid / Ask / Spread ปัจจุบันจาก MT5 ผ่าน VPS
        คืนค่า: {'bid': float, 'ask': float, 'price': float, 'spread': float}
        """
        try:
            resp = self.session.get(
                f"{self.vps_url}/api/price",
                params={"symbol": symbol},
                timeout=self.timeout
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "bid": data["bid"],
                    "ask": data["ask"],
                    "price": data["price"],
                    "spread": data["spread"]
                }
            logging.error(f"❌ ดึงราคา {symbol} ล้มเหลว: {resp.text}")
            return None
        except Exception as e:
            logging.error(f"❌ ไม่สามารถเชื่อมต่อดึงราคาได้: {e}")
            return None

    def get_historical_data(self, symbol: str = "XAUUSD", timeframe: str = "15m", num_candles: int = 100) -> pd.DataFrame:
        """
        ดึงแท่งเทียน OHLC ย้อนหลังจาก MT5 ผ่าน VPS
        - timeframe: '1m', '5m', '15m', '30m', '1h', '4h', '1d'
        - num_candles: จำนวนแท่ง
        คืนค่า: pd.DataFrame ที่มีคอลัมน์ ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        """
        try:
            resp = self.session.get(
                f"{self.vps_url}/api/ohlc",
                params={
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "count": num_candles
                },
                timeout=self.timeout
            )
            if resp.status_code == 200:
                json_data = resp.json()
                candles = json_data.get("data", [])
                if not candles:
                    return pd.DataFrame()
                df = pd.DataFrame(candles)
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                return df[["timestamp", "open", "high", "low", "close", "volume"]]
            logging.error(f"❌ ดึง OHLC {symbol} ({timeframe}) ล้มเหลว: {resp.text}")
            return pd.DataFrame()
        except Exception as e:
            logging.error(f"❌ เกิดข้อผิดพลาดในการดึง OHLC: {e}")
            return pd.DataFrame()

    def get_account_status(self) -> Optional[Dict[str, float]]:
        """
        ดึงข้อมูลเงินทุนและสถานะพอร์ต
        คืนค่า: {'balance': float, 'equity': float, 'margin': float, 'free_margin': float, 'floating_pnl': float}
        """
        try:
            resp = self.session.get(f"{self.vps_url}/api/account", timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json()
            logging.error(f"❌ ดึงข้อมูลบัญชีล้มเหลว: {resp.text}")
            return None
        except Exception as e:
            logging.error(f"❌ ไม่สามารถเชื่อมต่อดึงข้อมูลบัญชีได้: {e}")
            return None

    def get_open_positions(self, symbol: Optional[str] = "XAUUSD", magic: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        ดึงรายการออเดอร์ที่เปิดค้างอยู่
        """
        try:
            params = {}
            if symbol:
                params["symbol"] = symbol
            if magic is not None:
                params["magic"] = magic

            resp = self.session.get(f"{self.vps_url}/api/positions", params=params, timeout=self.timeout)
            if resp.status_code == 200:
                raw_positions = resp.json().get("positions", [])
                # แปลงให้ฟิลด์เข้ากันได้กับ MT5Integration เดิม
                positions_list = []
                for p in raw_positions:
                    positions_list.append({
                        "id": p["ticket"],
                        "direction": p["direction"],
                        "lot": p["volume"],
                        "entry_price": p["open_price"],
                        "sl": p["sl"],
                        "tp": p["tp"],
                        "pnl": p["pnl"],
                        "margin": 0.0,
                        "magic": p["magic"]
                    })
                return positions_list
            logging.error(f"❌ ดึงออเดอร์ค้างล้มเหลว: {resp.text}")
            return []
        except Exception as e:
            logging.error(f"❌ ไม่สามารถดึงรายการออเดอร์ได้: {e}")
            return []

    def open_position(
        self,
        direction: str,
        lot: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        entry: Optional[float] = None,
        symbol: str = "XAUUSD",
        magic: int = 123456,
        force_market: bool = True,
        comment: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        ส่งคำสั่งซื้อขายไปยัง MT5 บน VPS
        - direction: 'BUY' หรือ 'SELL'
        - lot: ขนาดสัญญา เช่น 0.01
        - sl: ราคา Stop Loss
        - tp: ราคา Take Profit
        - entry: ราคา Limit/Stop หากไม่ใช่ Market
        - force_market: True เพื่อเปิดทันทีที่ราคาตลาด
        """
        payload = {
            "direction": direction,
            "lot": float(lot),
            "sl": float(sl) if sl is not None else None,
            "tp": float(tp) if tp is not None else None,
            "entry": float(entry) if entry is not None else None,
            "symbol": symbol,
            "magic": int(magic),
            "force_market": bool(force_market),
            "comment": comment or "Phone LLM Engine"
        }
        try:
            logging.info(f"📱 [Phone Engine] กำลังส่งคำสั่งเทรด -> VPS: {direction} {lot} {symbol}...")
            resp = self.session.post(f"{self.vps_url}/api/order", json=payload, timeout=self.timeout)
            if resp.status_code == 200:
                result = resp.json()
                if result.get("status") == "SUCCESS":
                    logging.info(f"✅ คำสั่งสำเร็จ! Ticket: {result.get('ticket')}")
                else:
                    logging.warning(f"⚠️ คำสั่งไม่สำเร็จ: {result.get('message')}")
                return result
            else:
                err_msg = f"HTTP Error {resp.status_code}: {resp.text}"
                logging.error(f"❌ ยิงออเดอร์ล้มเหลว: {err_msg}")
                return {"status": "ERROR", "message": err_msg}
        except Exception as e:
            err_msg = f"Exception: {str(e)}"
            logging.error(f"❌ ไม่สามารถเชื่อมต่อไปยัง VPS ได้: {err_msg}")
            return {"status": "ERROR", "message": err_msg}

    def close_position(self, ticket: int, symbol: str = "XAUUSD") -> Dict[str, Any]:
        """ปิดออเดอร์ตาม Ticket ID"""
        try:
            payload = {"ticket": int(ticket), "symbol": symbol}
            resp = self.session.post(f"{self.vps_url}/api/close", json=payload, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json()
            return {"status": "ERROR", "message": resp.text}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def modify_position(self, ticket: int, new_sl: Optional[float] = None, new_tp: Optional[float] = None) -> Dict[str, Any]:
        """แก้ไข SL/TP ของออเดอร์"""
        try:
            payload = {
                "ticket": int(ticket),
                "sl": float(new_sl) if new_sl is not None else None,
                "tp": float(new_tp) if new_tp is not None else None
            }
            resp = self.session.post(f"{self.vps_url}/api/modify", json=payload, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json()
            return {"status": "ERROR", "message": resp.text}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def get_global_variable(self, name: str) -> Optional[float]:
        """ดึงค่าตัวแปร Global จาก MT5"""
        try:
            resp = self.session.get(f"{self.vps_url}/api/global_variable", params={"name": name}, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json().get("value")
            return None
        except Exception:
            return None
