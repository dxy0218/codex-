import json
import queue
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List

import tkinter as tk
from tkinter import ttk, messagebox

import yfinance as yf

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

DATA_FILE = Path("portfolio.json")

MARKETS = {
    "US": ["AAPL", "MSFT", "NVDA", "SPY"],
    "CN": ["0700.HK", "9988.HK", "000001.SS", "399001.SZ"],
    "JP": ["7203.T", "6758.T", "9984.T", "^N225"],
    "EU": ["ASML.AS", "SAP.DE", "MC.PA", "^STOXX50E"],
}


@dataclass
class Position:
    symbol: str
    shares: float
    avg_price: float


class Portfolio:
    def __init__(self, cash: float = 100000.0):
        self.cash = cash
        self.positions: Dict[str, Position] = {}

    def buy(self, symbol: str, shares: float, price: float):
        cost = shares * price
        if cost > self.cash:
            raise ValueError("现金不足")
        pos = self.positions.get(symbol)
        if pos:
            total_cost = pos.shares * pos.avg_price + cost
            total_shares = pos.shares + shares
            pos.avg_price = total_cost / total_shares
            pos.shares = total_shares
        else:
            self.positions[symbol] = Position(symbol, shares, price)
        self.cash -= cost

    def sell(self, symbol: str, shares: float, price: float):
        pos = self.positions.get(symbol)
        if not pos or pos.shares < shares:
            raise ValueError("持仓不足")
        pos.shares -= shares
        self.cash += shares * price
        if pos.shares == 0:
            del self.positions[symbol]

    def to_json(self):
        return {
            "cash": self.cash,
            "positions": [asdict(p) for p in self.positions.values()],
        }

    @staticmethod
    def from_file(path: Path):
        if not path.exists():
            return Portfolio()
        raw = json.loads(path.read_text(encoding="utf-8"))
        pf = Portfolio(cash=raw.get("cash", 100000.0))
        for item in raw.get("positions", []):
            pf.positions[item["symbol"]] = Position(**item)
        return pf


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("虚拟股市训练器 (Windows)")
        self.geometry("920x620")

        self.market = tk.StringVar(value="US")
        self.symbol = tk.StringVar(value=MARKETS["US"][0])
        self.price_var = tk.StringVar(value="--")
        self.cash_var = tk.StringVar()
        self.ai_text = tk.StringVar(value="AI 建议会显示在这里")

        self.portfolio = Portfolio.from_file(DATA_FILE)
        self.price_cache: Dict[str, float] = {}
        self.tick_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._refresh_portfolio_view()
        self._start_ticker_loop()
        self.after(300, self._consume_ticks)

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=10)

        ttk.Label(top, text="市场").pack(side="left")
        market_box = ttk.Combobox(top, textvariable=self.market, values=list(MARKETS.keys()), width=8, state="readonly")
        market_box.pack(side="left", padx=6)
        market_box.bind("<<ComboboxSelected>>", self._on_market_change)

        ttk.Label(top, text="代码").pack(side="left")
        self.symbol_box = ttk.Combobox(top, textvariable=self.symbol, values=MARKETS[self.market.get()], width=14)
        self.symbol_box.pack(side="left", padx=6)

        ttk.Label(top, text="实时价:").pack(side="left", padx=(12, 4))
        ttk.Label(top, textvariable=self.price_var, foreground="blue").pack(side="left")

        ttk.Label(top, text="现金:").pack(side="left", padx=(16, 4))
        ttk.Label(top, textvariable=self.cash_var, foreground="green").pack(side="left")

        trade = ttk.LabelFrame(self, text="模拟交易")
        trade.pack(fill="x", padx=12, pady=6)

        self.shares_entry = ttk.Entry(trade, width=12)
        self.shares_entry.insert(0, "1")
        ttk.Label(trade, text="数量").grid(row=0, column=0, padx=6, pady=8)
        self.shares_entry.grid(row=0, column=1)

        ttk.Button(trade, text="买入", command=self._buy).grid(row=0, column=2, padx=8)
        ttk.Button(trade, text="卖出", command=self._sell).grid(row=0, column=3, padx=8)
        ttk.Button(trade, text="保存组合", command=self._save).grid(row=0, column=4, padx=8)
        ttk.Button(trade, text="AI训练建议", command=self._ask_ai).grid(row=0, column=5, padx=8)

        middle = ttk.PanedWindow(self, orient="horizontal")
        middle.pack(fill="both", expand=True, padx=12, pady=8)

        left = ttk.Frame(middle)
        right = ttk.Frame(middle)
        middle.add(left, weight=2)
        middle.add(right, weight=3)

        self.holding_view = ttk.Treeview(left, columns=("symbol", "shares", "avg"), show="headings", height=16)
        for col, title in [("symbol", "代码"), ("shares", "持仓"), ("avg", "成本价")]:
            self.holding_view.heading(col, text=title)
            self.holding_view.column(col, width=90)
        self.holding_view.pack(fill="both", expand=True)

        ttk.Label(right, text="AI 投资训练反馈").pack(anchor="w")
        ttk.Label(right, textvariable=self.ai_text, wraplength=430, justify="left").pack(fill="both", expand=True, pady=10)

    def _on_market_change(self, _event=None):
        symbols = MARKETS[self.market.get()]
        self.symbol_box.configure(values=symbols)
        self.symbol.set(symbols[0])

    def _start_ticker_loop(self):
        def run():
            while True:
                sym = self.symbol.get().strip().upper()
                try:
                    data = yf.Ticker(sym).history(period="1d", interval="1m")
                    if not data.empty:
                        latest = float(data["Close"].iloc[-1])
                        self.tick_queue.put((sym, latest))
                except Exception:
                    pass
                time.sleep(5)

        threading.Thread(target=run, daemon=True).start()

    def _consume_ticks(self):
        try:
            while True:
                sym, price = self.tick_queue.get_nowait()
                self.price_cache[sym] = price
                if sym == self.symbol.get().strip().upper():
                    self.price_var.set(f"{price:.2f}")
        except queue.Empty:
            pass
        self.after(300, self._consume_ticks)

    def _current_price(self, symbol: str) -> float:
        p = self.price_cache.get(symbol.upper())
        if p is None:
            raise ValueError("暂无最新价格，请稍后再试")
        return p

    def _buy(self):
        symbol = self.symbol.get().strip().upper()
        shares = float(self.shares_entry.get())
        price = self._current_price(symbol)
        self.portfolio.buy(symbol, shares, price)
        self._refresh_portfolio_view()

    def _sell(self):
        symbol = self.symbol.get().strip().upper()
        shares = float(self.shares_entry.get())
        price = self._current_price(symbol)
        self.portfolio.sell(symbol, shares, price)
        self._refresh_portfolio_view()

    def _save(self):
        DATA_FILE.write_text(json.dumps(self.portfolio.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        messagebox.showinfo("提示", "组合已保存")

    def _refresh_portfolio_view(self):
        self.cash_var.set(f"{self.portfolio.cash:.2f}")
        for i in self.holding_view.get_children():
            self.holding_view.delete(i)
        for pos in self.portfolio.positions.values():
            self.holding_view.insert("", "end", values=(pos.symbol, f"{pos.shares:.2f}", f"{pos.avg_price:.2f}"))

    def _ask_ai(self):
        if OpenAI is None:
            self.ai_text.set("未安装 openai 包，无法启用 AI 建议。")
            return
        api_key = __import__("os").environ.get("OPENAI_API_KEY")
        if not api_key:
            self.ai_text.set("请先设置 OPENAI_API_KEY 环境变量。")
            return

        snapshot = self.portfolio.to_json()
        symbol = self.symbol.get().strip().upper()
        price = self.price_cache.get(symbol, None)

        try:
            client = OpenAI(api_key=api_key)
            prompt = (
                "你是投资教练。请基于以下虚拟账户快照，给出风险提示、仓位建议、复盘训练任务。"
                f"账户: {json.dumps(snapshot, ensure_ascii=False)}; 当前关注股票: {symbol}; 价格: {price}"
            )
            resp = client.responses.create(
                model="gpt-4.1-mini",
                input=prompt,
                temperature=0.3,
            )
            self.ai_text.set(resp.output_text[:800])
        except Exception as e:
            self.ai_text.set(f"AI 请求失败: {e}")


if __name__ == "__main__":
    app = App()
    app.mainloop()
