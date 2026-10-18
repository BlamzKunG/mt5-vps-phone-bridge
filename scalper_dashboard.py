#!/usr/bin/env python3
"""
M5 Price Action Scalper - Rich CLI Dashboard
--------------------------------------------
Realtime Terminal Dashboard for Termux / Linux
- Market Status Check: ตรวจสอบสถานะตลาดเปิด/ปิดอัตโนมัติ
- Live second-by-second countdown to next M5 candle close
- Live Bid/Ask/Spread tracker from VPS MT5
- AI Decision display (Action, Confidence, Regime, Reasoning)
- Model Failover Display: Primary (gpt-6-sol) / Fallback (gpt-6.1-sol) / Failover (Skip)
- Active Trade Management (HOLD_POSITION, MODIFY_SLTP, CLOSE_POSITION)
- 100-Bar Normalized level bar & recent candles
- Active Position with SL/TP & Floating PnL
- Recent Trade Journal
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich import box
from rich.align import Align

# Import our robust scalper engine
from phone_scalper_engine import PriceActionScalperEngine

# Redirect root logging so it doesn't corrupt the Rich TUI screen
logging.getLogger().handlers = [logging.FileHandler("scalper_dashboard.log", encoding="utf-8")]

class ScalperCLIDashboard:
    def __init__(self, dry_run: bool = False):
        self.console = Console()
        self.engine = PriceActionScalperEngine(dry_run=dry_run)
        self.dry_run = dry_run

        self.last_decision: Optional[Dict[str, Any]] = None
        self.last_context: Optional[Dict[str, Any]] = None
        self.trade_history: List[Dict[str, Any]] = []
        self.active_position: Optional[Dict[str, Any]] = None
        self.live_price: Optional[Dict[str, Any]] = None
        self.is_market_open: bool = True
        self.market_status_desc: str = "กำลังตรวจสอบ..."
        self.is_analyzing: bool = False
        self.status_message: str = "พร้อมทำงาน - กำลังรอแท่งเทียนถัดไป..."
        self.last_processed_slot: Optional[str] = None

    def fetch_live_status(self):
        """ดึงราคา, สถานะตลาด และ Position ล่าสุดจาก MT5"""
        try:
            self.is_market_open, self.market_status_desc = self.engine.is_market_open()
            self.live_price = self.engine.bridge.get_current_price(symbol=self.engine.symbol)
            if hasattr(self.engine.bridge, "get_positions"):
                positions = self.engine.bridge.get_positions(symbol=self.engine.symbol)
            else:
                positions = self.engine.bridge.get_open_positions(symbol=self.engine.symbol)
            self.active_position = positions[0] if positions else None
        except Exception as e:
            self.status_message = f"เชื่อมต่อ VPS ขัดข้อง: {e}"

    def trigger_ai_cycle(self):
        """เรียก AI วิเคราะห์แท่งเทียนรอบใหม่ (เฉพาะเมื่อตลาดเปิดทำการ)"""
        # 1. ตรวจสอบสถานะตลาดก่อนเรียก AI
        is_open, market_reason = self.engine.is_market_open()
        self.is_market_open = is_open
        self.market_status_desc = market_reason

        if not is_open:
            self.status_message = f"⏸️ ตลาดปิดทำการ ({market_reason}) - ข้ามการเรียก AI เพื่อประหยัด Token"
            return

        self.is_analyzing = True
        self.status_message = f"🤖 กำลังวิเคราะห์แท่งเทียน (Primary: {self.engine.model} / Fallback: {self.engine.secondary_model})..."
        try:
            ctx = self.engine.prepare_market_context()
            if not ctx:
                self.status_message = "❌ ข้อมูลไม่พร้อม ข้ามรอบนี้"
                self.is_analyzing = False
                return

            self.last_context = ctx
            decision = self.engine.call_ai_decision(ctx)
            if decision:
                self.last_decision = decision
                action = decision.get("action", "HOLD").upper()
                model_used = decision.get("model_used", self.engine.model)
                self.status_message = f"✅ AI ตัดสินใจ: {action} ({model_used})"
                
                # ส่งคำสั่งผ่าน Safety Guard
                executed = self.engine.execute_decision(decision, ctx)

                if action in ["BUY", "SELL"]:
                    sl_tp_desc = f"{decision.get('sl_points', 0)}/{decision.get('tp_points', 0)}"
                elif action == "MODIFY_SLTP":
                    sl_tp_desc = f"SL->{decision.get('new_sl')}"
                elif action in ["CLOSE_POSITION", "CLOSE_ALL"]:
                    sl_tp_desc = "EXIT"
                elif action == "HOLD_POSITION":
                    sl_tp_desc = "IN_TRADE"
                else:
                    sl_tp_desc = "-"
                
                self.trade_history.insert(0, {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "action": action,
                    "confidence": decision.get("confidence", 0),
                    "price": ctx.get("current_price", 0),
                    "sl_tp": sl_tp_desc,
                    "status": "EXECUTED" if executed else "SKIPPED/HOLD"
                })
                if len(self.trade_history) > 6:
                    self.trade_history.pop()
            else:
                self.status_message = "❌ ไม่ได้รับคำตอบจาก AI (รอบนี้ถูกข้าม)"
        except Exception as e:
            self.status_message = f"❌ Error ระหว่าง AI Cycle: {e}"
        finally:
            self.is_analyzing = False

    def build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="top_metrics", size=6),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=3)
        )
        layout["main"].split_row(
            Layout(name="ai_panel", ratio=6),
            Layout(name="market_panel", ratio=4)
        )
        return layout

    def render(self) -> Layout:
        layout = self.build_layout()
        now = datetime.now()

        # 1. Header
        header_text = Text()
        header_text.append(" ⚡ AI QUANTITATIVE SCALPER (M5) ", style="bold white on blue")
        header_text.append("  |  ", style="dim")

        if self.last_decision and self.last_decision.get("tier") == 2:
            header_text.append(f"Model: {self.last_decision.get('model_used')} (effort: {self.engine.reasoning_effort})", style="bold yellow")
        else:
            model_name = self.last_decision.get("model_used", self.engine.model) if self.last_decision else self.engine.model
            header_text.append(f"Model: {model_name} (effort: {self.engine.reasoning_effort})", style="bold cyan")

        header_text.append("  |  ", style="dim")
        header_text.append(f"Asset: {self.engine.symbol}", style="bold yellow")
        header_text.append("  |  ", style="dim")

        if self.is_market_open:
            header_text.append("● MARKET OPEN", style="bold green")
        else:
            header_text.append("● MARKET CLOSED", style="bold red blink")

        layout["header"].update(Panel(Align.center(header_text), box=box.ROUNDED, style="blue"))

        # 2. Top Metrics (Price, Spread, Countdown, Position)
        metrics_table = Table.grid(expand=True)
        metrics_table.add_column(ratio=2)
        metrics_table.add_column(ratio=2)
        metrics_table.add_column(ratio=3)
        metrics_table.add_column(ratio=3)

        # Price & Spread
        bid = self.live_price.get("bid", 0.0) if self.live_price else 0.0
        ask = self.live_price.get("ask", 0.0) if self.live_price else 0.0
        spread = self.live_price.get("spread", 0.0) if self.live_price else 0.0
        spread_color = "green" if spread < 30 else ("yellow" if spread <= 40 else "red")

        p_price = Panel(
            f"[bold white]{bid:.2f}[/] / [dim]{ask:.2f}[/]\nSpread: [{spread_color}]{spread:.1f} pts[/]",
            title="[bold]💵 LIVE MARKET[/]", box=box.ROUNDED, border_style="cyan"
        )

        # 100-Bar Macro Level Bar
        min_100 = self.last_context.get("min_100", 0) if self.last_context else 0
        max_100 = self.last_context.get("max_100", 0) if self.last_context else 0
        cur_price = self.last_context.get("current_price", bid) if self.last_context else bid
        if max_100 > min_100 and cur_price > 0:
            norm_val = max(0.0, min(1.0, (cur_price - min_100) / (max_100 - min_100)))
            bars_filled = int(norm_val * 15)
            bar_graph = "█" * bars_filled + "░" * (15 - bars_filled)
            macro_desc = f"[{bar_graph}] {norm_val:.2f}\nLow: {min_100:.1f} | High: {max_100:.1f}"
        else:
            macro_desc = "กำลังประมวลผลข้อมูล..."

        p_macro = Panel(macro_desc, title="[bold]📊 100-BAR LEVEL[/]", box=box.ROUNDED, border_style="magenta")

        # Countdown to next M5
        minutes_to_add = 5 - (now.minute % 5)
        target_time = (now + timedelta(minutes=minutes_to_add)).replace(second=1, microsecond=0)
        wait_seconds = max(0, int((target_time - now).total_seconds()))
        wait_m = wait_seconds // 60
        wait_s = wait_seconds % 60
        timer_color = "bold green" if wait_seconds > 30 else "bold red blink"

        if not self.is_market_open:
            timer_text = f"[bold red]ตลาดปิดทำการ[/]\n[dim]{self.market_status_desc[:25]}[/]"
        elif self.is_analyzing:
            timer_text = "[bold yellow blink]⚡ กำลังประมวลผล AI...[/]\nรอการวิเคราะห์ (~20s)"
        else:
            timer_text = f"[{timer_color}]{wait_m:02d}:{wait_s:02d}[/] น.\nรอบถัดไป: {target_time.strftime('%H:%M:%S')}"

        p_timer = Panel(timer_text, title="[bold]⏰ NEXT M5 CANDLE[/]", box=box.ROUNDED, border_style="yellow")

        # Active Position
        if self.active_position:
            p_dir = self.active_position.get("direction", "N/A")
            p_lot = self.active_position.get("lot", 0.01)
            p_entry = self.active_position.get("entry_price", 0.0)
            p_pnl = self.active_position.get("pnl", 0.0)
            p_sl = self.active_position.get("sl", 0.0)
            pnl_color = "bold green" if p_pnl >= 0 else "bold red"
            sl_info = f" | SL: {p_sl:.2f}" if p_sl else ""
            pos_text = f"[bold]{p_dir} {p_lot} lot[/] @ {p_entry:.2f}{sl_info}\nFloating PnL: [{pnl_color}]{p_pnl:+.2f} USD[/]"
            pos_border = "green" if p_pnl >= 0 else "red"
        else:
            pos_text = "[dim]ไม่มีออเดอร์เปิดค้าง[/]\n(Scalp 100-500 pts)"
            pos_border = "dim white"

        p_pos = Panel(pos_text, title="[bold]💼 ACTIVE POSITION[/]", box=box.ROUNDED, border_style=pos_border)

        metrics_table.add_row(p_price, p_macro, p_timer, p_pos)
        layout["top_metrics"].update(metrics_table)

        # 3. Main - Left: AI Decision Panel
        if not self.is_market_open:
            decision_text = Text()
            decision_text.append(f"\n   🛑 ตลาดปิดทำการ ({self.market_status_desc})\n\n", style="bold red")
            decision_text.append("   ระบบทำการหยุดพักการวิเคราะห์ AI ชั่วคราวอัตโนมัติ เพื่อประหยัด Token เครดิต\n", style="yellow")
            decision_text.append("   ระบบจะกลับมาทำงานต่อทันทีเมื่อตลาดเปิดทำการครับ", style="dim white")
            ai_panel_border = "red"
            panel_title = "[bold red]⏸️ MARKET CLOSED (AI PAUSED)[/]"
        elif self.last_decision:
            action = self.last_decision.get("action", "HOLD").upper()
            conf = self.last_decision.get("confidence", 0.0)
            sl_pts = self.last_decision.get("sl_points", 0)
            tp_pts = self.last_decision.get("tp_points", 0)
            new_sl = self.last_decision.get("new_sl")
            new_tp = self.last_decision.get("new_tp")
            regime = self.last_decision.get("market_regime", "N/A")
            reasoning = self.last_decision.get("reasoning", "No rationale provided")
            tier = self.last_decision.get("tier", 1)

            if action == "BUY":
                action_color = "bold white on green"
                ai_panel_border = "green"
                order_details = f"  SL: {sl_pts} pts ({sl_pts*0.01:.2f} USD)  |  TP: {tp_pts} pts ({tp_pts*0.01:.2f} USD)\n\n"
            elif action == "SELL":
                action_color = "bold white on red"
                ai_panel_border = "red"
                order_details = f"  SL: {sl_pts} pts ({sl_pts*0.01:.2f} USD)  |  TP: {tp_pts} pts ({tp_pts*0.01:.2f} USD)\n\n"
            elif action == "HOLD_POSITION":
                action_color = "bold white on blue"
                ai_panel_border = "cyan"
                order_details = "  Trade Thesis Intact: Holding Active Position\n\n"
            elif action == "MODIFY_SLTP":
                action_color = "bold white on magenta"
                ai_panel_border = "magenta"
                order_details = f"  Risk Guard: Tightening SL -> {new_sl} (TP: {new_tp or 'Unchanged'})\n\n"
            elif action in ["CLOSE_POSITION", "CLOSE_ALL"]:
                action_color = "bold white on dark_red"
                ai_panel_border = "red"
                order_details = "  Emergency / Structural Invalidation Exit at Market\n\n"
            else:
                action_color = "bold black on yellow"
                ai_panel_border = "yellow"
                order_details = "  No Setup: Maintaining Flat Stance\n\n"

            decision_text = Text()
            decision_text.append(f" ACTION: {action} ", style=action_color)
            decision_text.append(f"  Confidence: {conf:.0%}  |  Regime: {regime}\n", style="bold cyan")
            decision_text.append(order_details, style="bold yellow")
            decision_text.append("🧠 Deep Quantitative Rationale:\n", style="bold underline")
            decision_text.append(reasoning, style="italic text")

            if tier == 2:
                panel_title = "[bold yellow]🔄 SECONDARY FALLBACK: GPT-6.1-SOL[/]"
            else:
                panel_title = "[bold]🧠 PRIMARY MODEL: GPT-6-SOL[/]"
        else:
            decision_text = Text("\n\n   กำลังเตรียมข้อมูลเพื่อเริ่มการวิเคราะห์รอบแรก...", style="dim")
            ai_panel_border = "blue"
            panel_title = "[bold]🧠 AI INTELLIGENCE[/]"

        layout["ai_panel"].update(Panel(decision_text, title=panel_title, box=box.ROUNDED, border_style=ai_panel_border))

        # 4. Main - Right: Recent M5 Candles & Trade History
        right_table = Table(box=box.SIMPLE_HEAD, expand=True, show_edge=False)
        right_table.add_column("Time", justify="center", style="cyan")
        right_table.add_column("Action", justify="center")
        right_table.add_column("Price", justify="right")
        right_table.add_column("SL/TP", justify="center")
        right_table.add_column("Status", justify="right")

        for h in self.trade_history:
            act = h["action"]
            if act == "BUY":
                act_style = "green"
            elif act == "SELL":
                act_style = "red"
            elif act == "HOLD_POSITION":
                act_style = "cyan"
            elif act == "MODIFY_SLTP":
                act_style = "magenta"
            elif act in ["CLOSE_POSITION", "CLOSE_ALL"]:
                act_style = "bold red"
            else:
                act_style = "yellow"

            right_table.add_row(
                h["time"],
                f"[{act_style}]{act}[/]",
                f"{h['price']:.2f}",
                f"{h.get('sl_tp', '-')}",
                f"[dim]{h['status']}[/]"
            )

        if not self.trade_history:
            right_table.add_row("-", "[dim]รอประวัติรอบแรก[/]", "-", "-", "-")

        layout["market_panel"].update(Panel(right_table, title="[bold]📜 RECENT TRADE JOURNAL[/]", box=box.ROUNDED, border_style="cyan"))

        # 5. Footer (Status Bar & Hotkeys)
        footer_text = Text()
        footer_text.append(f" ℹ️  สถานะ: {self.status_message} ", style="bold white")
        footer_text.append("  |  ", style="dim")
        footer_text.append("กด Ctrl+C เพื่อออกจาก Dashboard", style="dim italic")
        layout["footer"].update(Panel(footer_text, box=box.ROUNDED, style="dim white"))

        return layout

    def start(self):
        """ลูปหลักของ Live Dashboard"""
        self.console.clear()
        self.fetch_live_status()

        # รันรอบแรกทันทีเมื่อสตาร์ท
        self.trigger_ai_cycle()

        with Live(self.render(), console=self.console, refresh_per_second=2, screen=True) as live:
            tick_counter = 0
            while True:
                try:
                    time.sleep(0.5)
                    tick_counter += 1
                    now = datetime.now()

                    # ดึงสถานะราคาและพอร์ตทุก 3 วินาที
                    if tick_counter % 6 == 0 and not self.is_analyzing:
                        self.fetch_live_status()

                    # ตรวจสอบว่าถึงเวลาแท่ง M5 ปิดหรือยัง (:00, :05, :10, :15, ...)
                    current_slot = f"{now.strftime('%H')}:{(now.minute // 5) * 5:02d}"
                    is_exact_5min = (now.minute % 5 == 0) and (now.second in [1, 2, 3])

                    if is_exact_5min and current_slot != self.last_processed_slot and not self.is_analyzing:
                        self.last_processed_slot = current_slot
                        live.update(self.render())
                        self.trigger_ai_cycle()

                    live.update(self.render())

                except KeyboardInterrupt:
                    break
                except Exception as e:
                    self.status_message = f"Error: {e}"

        self.console.clear()
        self.console.print("\n[bold yellow]🛑 ปิด Dashboard เรียบร้อยแล้ว[/]\n")

if __name__ == "__main__":
    dry = "--dry" in sys.argv
    dashboard = ScalperCLIDashboard(dry_run=dry)
    dashboard.start()
