# MT5 VPS Bridge for Mobile LLM Trading

ระบบสถาปัตยกรรมเชื่อมต่อระหว่าง **โทรศัพท์มือถือ (Main Engine / Brain)** กับ **Windows VPS (MT5 Execution & Data Gateway)** เพื่อให้คุณสามารถรันโมเดล AI / LLM วิเคราะห์กราฟบนมือถือได้โดยตรง **โดยไม่ต้องล็อกอินรีโมทเข้า VPS บ่อยๆ**

---

## 🏗️ โครงสร้างระบบ (Architecture Overview)

```
+-------------------------------------------------------------------+
|               📱 โทรศัพท์มือถือ (Main Engine / Brain)               |
|  - รัน Python Engine (Termux / Linux)                             |
|  - วิเคราะห์แท่งเทียนด้วย LLM (Claude / Groq / OpenAI)            |
|  - คลาส RemoteMT5Bridge (vps_bridge_client.py)                    |
+---------------------------------+---------------------------------+
                                  |
              📡 เรียกผ่าน HTTP REST API (X-API-Key)
           (แนะนำใช้ Tailscale หรือ Cloudflare Tunnel)
                                  |
                                  v
+-------------------------------------------------------------------+
|                 💻 Windows VPS (MT5 Gateway Server)               |
|  - รัน FastAPI Server (vps_mt5_server.py)                         |
|  - โปรแกรม MetaTrader 5 Terminal (เปิดทิ้งไว้)                    |
|  - ทำหน้าที่:                                                      |
|     1. ดึง OHLC และราคาส่งกลับให้มือถือ                           |
|     2. รับคำสั่ง BUY / SELL / CLOSE จากมือถือ แล้วยิงเข้า MT5     |
|     3. รายงาน Balance / Equity / PnL / ออเดอร์ค้าง                |
+-------------------------------------------------------------------+
```

---

## 🌐 วิธีเชื่อมต่อมือถือกับ VPS ให้เสถียรและปลอดภัย (ไม่ต้องปวดหัวเรื่อง IP)

เนื่องจากโทรศัพท์มือถือเชื่อมต่อเน็ต 4G/5G หรือ Wi-Fi ทั่วไป (ไม่มี Public IP) **แนะนำ 3 วิธีในการเชื่อมต่อ**:

### วิธีที่ 1: ใช้ Tailscale (⭐ แนะนำที่สุด - ง่าย ปลอดภัย ฟรี 100%)
1. โหลด **Tailscale** ติดตั้งบน Windows VPS และบนโทรศัพท์มือถือ (โหลดจาก Play Store / App Store)
2. ล็อกอินด้วยบัญชีเดียวกัน ทั้ง 2 เครื่องจะอยู่ในวง Virtual Private Network เดียวกัน
3. คุณจะได้ IP เสมือนของ VPS (เช่น `100.85.12.34`)
4. ในมือถือใส่ `VPS_URL = "http://100.85.12.34:8000"` เชื่อมต่อได้ทันที ไม่ต้องเปิดพอร์ตเราเตอร์ ไม่โดนแฮก

### วิธีที่ 2: ใช้ Cloudflare Tunnel หรือ Ngrok (ได้ URL แบบ HTTPS)
- บน VPS รันคำสั่ง `ngrok http 8000` คุณจะได้ URL เช่น `https://xxxx.ngrok-free.app`
- นำ URL นั้นมากรอกในฝั่งมือถือได้เลย ปลอดภัยและเข้ารหัส SSL

### วิธีที่ 3: ใช้ Public IP ตรงของ VPS
- หาก VPS มี Public IP อยู่แล้ว ให้เปิด Firewall บน Windows อนุญาต Inbound Port `8000`
- เชื่อมต่อไปที่ `http://<VPS_PUBLIC_IP>:8000`

---

## 🚀 ขั้นตอนการติดตั้งและใช้งาน

### 1. ฝั่ง Windows VPS (ทำครั้งเดียวแล้วปล่อยรันยาว)
1. นำไฟล์ในโฟลเดอร์นี้ไปไว้ที่ Windows VPS:
   - `vps_mt5_server.py`
   - `requirements_vps.txt`
   - `start_server.bat`
2. เปิดโปรแกรม **MetaTrader 5** และล็อกอินบัญชีเทรดให้เรียบร้อย (ติ๊กถูก Allow Algo Trading)
3. ดับเบิ้ลคลิกไฟล์ `start_server.bat`
   - ระบบจะติดตั้งไลบรารีที่จำเป็นอัตโนมัติ
   - เซิร์ฟเวอร์จะเริ่มทำงานที่พอร์ต `8000` พร้อมแสดงสถานะการเชื่อมต่อ MT5

*(สามารถเปลี่ยนรหัสผ่าน API Key ได้ที่ตัวแปร `BRIDGE_API_KEY` ในไฟล์ `start_server.bat` หรือ `vps_mt5_server.py`)*

---

### 2. ฝั่งโทรศัพท์มือถือ (Main Engine)
1. ใช้ไฟล์ `vps_bridge_client.py` เป็นตัวเชื่อมต่อ
2. ตัวอย่างการดึงแท่งเทียนและยิงคำสั่งในโค้ด Python:

```python
from vps_bridge_client import RemoteMT5Bridge

# ตั้งค่า URL ของ VPS และ API Key
bridge = RemoteMT5Bridge(
    vps_url="http://100.85.12.34:8000",   # ใส่ IP Tailscale หรือ IP ของ VPS
    api_key="llm-trade-secret-key-2026"
)

# 1. ตรวจสอบการเชื่อมต่อ & พอร์ต
if bridge.connect():
    acc = bridge.get_account_status()
    print(f"Balance: ${acc['balance']} | Equity: ${acc['equity']}")

    # 2. ดึงข้อมูลแท่งเทียน OHLC M5 (ได้ผลลัพธ์เป็น pandas DataFrame ทันที)
    df_ohlc = bridge.get_historical_data(symbol="XAUUSD", timeframe="5m", num_candles=50)
    print(df_ohlc.tail())

    # 3. ส่งข้อมูลให้ LLM วิเคราะห์... เมื่อได้สัญญาณ BUY/SELL ก็ยิงคำสั่ง:
    order_result = bridge.open_position(
        direction="BUY",
        lot=0.01,
        sl=2650.0,
        tp=2680.0,
        symbol="XAUUSD",
        magic=123456,
        force_market=True,
        comment="LLM Mobile Engine"
    )
    print("ผลการส่งคำสั่ง:", order_result)
```

3. รันตัวอย่างระบบเทรดแบบเต็มได้ที่:
```bash
python phone_llm_trader_demo.py
```

---

## 🔌 การนำไปใช้กับ `bot_orchestrator_mt5.py` เดิม

หากคุณมีสคริปต์ `bot_orchestrator_mt5.py` อยู่แล้วและอยากย้ายมารันบนมือถือโดยไม่ต้องแก้โค้ดด้านใน:
เพียงเปลี่ยนบรรทัด Import ใน `bot_orchestrator_mt5.py`:

```python
# ของเดิม (รันได้เฉพาะบน Windows):
# from mt5_integration import MT5Integration
# self.mt5_bridge = MT5Integration(...)

# ของใหม่ (รันบนมือถือ คุยผ่าน VPS):
from vps_bridge_client import RemoteMT5Bridge
self.mt5_bridge = RemoteMT5Bridge(vps_url="http://<VPS_IP>:8000", api_key="llm-trade-secret-key-2026")
```
ฟังก์ชันทั้งหมด (`get_historical_data`, `get_current_price`, `open_position`, `close_position`, `get_account_status`) ใช้งานแทนกันได้ทันที 100%!

---

## 💎 ระบบ Auto-Discovery Suffix ของโบรกเกอร์ (Universal Symbol Resolver)

### 1. รองรับ Suffix แปลกๆ ได้หมดหรือไม่? (`XAUUSD-ECN`, `XAUUSD.kio`, ฯลฯ)
**คำตอบ: ไม่มีปัญหาแน่นอน 100%!** 
ระบบใช้ Regular Expression แบบ Dynamic Match:
`^(#|r)?(XAUUSD|GOLD)([-._+/#]?[a-zA-Z0-9]+)?$`
ดังนั้น ไม่ว่าโบรกเกอร์จะตั้งชื่อแปลกแค่ไหน เช่น:
- มีขีดคั่น: `XAUUSD-ECN`, `XAUUSD-PRO`, `XAUUSD-RAW`, `XAUUSD-std`
- มีจุดคั่น: `XAUUSD.kio`, `XAUUSD.m`, `XAUUSD.pro`, `GOLD.pro`
- มีต่อท้ายติดกัน: `XAUUSDc`, `GOLDc`, `XAUUSD247`, `XAUUSDm`
- มี Prefix นำหน้า: `#XAUUSD`, `rXAUUSD`
ระบบจะตรวจจับและสแกนเจอทั้งหมด พร้อมทั้งจัดลำดับความสำคัญ (Priority Scoring) เลือกตัวที่เปิดให้เทรดจริง (`trade_mode=FULL`) ให้อัตโนมัติ

---

### 2. กรณีโบรกเกอร์มีหลายตัวเลือกพร้อมกัน (เช่น `XAUUSD-ECN`, `XAUUSD.crp`, `XAUUSD247`)
หากโบรกเกอร์มีทั้งทอง Spot ปกติ (`XAUUSD-ECN`), ทอง Crypto (`XAUUSD.crp`), และทองเสาร์-อาทิตย์ (`XAUUSD247`):
- **หากส่ง `XAUUSD` ธรรมดามา**: ระบบจะฉลาดพอที่จะเลือกคู่ Spot มาตรฐานก่อน (ไม่แย่งไปเปิด 247 หรือ crp ในวันธรรมดา)
- **หากต้องการระบุเจาะจง**: คุณสามารถเลือกทำได้ 3 วิธี:

#### วิธีที่ A: ระบุชื่อคู่เงินตรงๆ ในโค้ดบนมือถือได้เลย (Direct Pass-Through)
```python
# ส่งชื่อตรงๆ เข้าไปได้ทันที MT5 จะเปิดคู่นั้นตรงๆ ไม่แปลง
bridge.get_historical_data(symbol="XAUUSD247", timeframe="5m")
bridge.open_position(direction="BUY", lot=0.01, symbol="XAUUSD247")
```

#### วิธีที่ B: ตั้งค่า Override Mapping จากมือถือได้เลย (สะดวกที่สุด)
```python
# สั่งครั้งเดียว บอทที่ส่ง 'XAUUSD' จะวิ่งเข้า 'XAUUSD247' อัตโนมัติ
bridge.set_symbol_mapping("XAUUSD", "XAUUSD247")

# หรือถ้าจะเทรดคริปโตโกลด์:
bridge.set_symbol_mapping("XAUUSD", "XAUUSD.crp")
```

#### วิธีที่ C: ตั้งค่าที่ไฟล์ `start_server.bat` บน VPS
```bat
:: ในไฟล์ start_server.bat
set SYMBOL_MAP=XAUUSD:XAUUSD247,EURUSD:EURUSD-ECN
```

---

### 3. ตรวจสอบดูว่าโบรกเกอร์มีคู่เงินทองคำตัวไหนบ้างจากมือถือ
คุณสามารถสั่งจากโทรศัพท์เครื่องนี้เพื่อดูรายชื่อคู่เงินทั้งหมดบนโบรกเกอร์ได้ทันที:
```python
symbols = bridge.get_available_symbols("XAU")
for s in symbols:
    print(f"ชื่อ: {s['name']:15} | เทรดได้: {s['is_tradeable']} | Spread: {s['spread']}")
```


