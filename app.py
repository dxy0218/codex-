import json
import queue
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

import tkinter as tk
from tkinter import ttk, messagebox

import urllib.parse
import webbrowser

import yfinance as yf

DATA_FILE = Path("portfolio.json")
MAX_TOTAL_EXPOSURE = 1_000_000.0
MAX_SINGLE_TRADE = 200_000.0
REFRESH_SECONDS = 3
STALE_SECONDS = 20

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

    def exposure(self) -> float:
        return sum(p.shares * p.avg_price for p in self.positions.values())

    def buy(self, symbol: str, shares: float, price: float):
        cost = shares * price
        if cost > self.cash:
            raise ValueError("现金不足")
        if cost > MAX_SINGLE_TRADE:
            raise ValueError(f"单笔交易上限为 {MAX_SINGLE_TRADE:.0f}")
        if self.exposure() + cost > MAX_TOTAL_EXPOSURE:
            raise ValueError(f"总投资上限为 {MAX_TOTAL_EXPOSURE:.0f}")
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
        return {"cash": self.cash, "positions": [asdict(p) for p in self.positions.values()]}

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
        self.geometry("980x650")

        self.market = tk.StringVar(value="US")
        self.symbol = tk.StringVar(value=MARKETS["US"][0])
        self.price_var = tk.StringVar(value="--")
        self.cash_var = tk.StringVar()
        self.feed_var = tk.StringVar(value="数据流状态：初始化中")
        self.ai_text = tk.StringVar(value="AI 训练建议会显示在这里（会员订阅模式）")

        self.portfolio = Portfolio.from_file(DATA_FILE)
        self.price_cache: Dict[str, float] = {}
        self.last_tick_ts: Dict[str, float] = {}
        self.tick_queue: queue.Queue = queue.Queue()

        self._active_market = self.market.get()
        self._symbols_snapshot = MARKETS[self._active_market][:]

        self._build_ui()
        self._refresh_portfolio_view()
        self._start_ticker_loop()
        self.after(300, self._consume_ticks)
        self.after(1000, self._check_staleness)

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

        ttk.Label(self, textvariable=self.feed_var, foreground="#666").pack(anchor="w", padx=14)

        trade = ttk.LabelFrame(self, text="模拟交易")
        trade.pack(fill="x", padx=12, pady=6)

        self.shares_entry = ttk.Entry(trade, width=12)
        self.shares_entry.insert(0, "1")
        ttk.Label(trade, text="数量").grid(row=0, column=0, padx=6, pady=8)
        self.shares_entry.grid(row=0, column=1)

        ttk.Button(trade, text="买入", command=self._buy).grid(row=0, column=2, padx=8)
        ttk.Button(trade, text="卖出", command=self._sell).grid(row=0, column=3, padx=8)
        ttk.Button(trade, text="保存组合", command=self._save).grid(row=0, column=4, padx=8)
        ttk.Button(trade, text="会员AI训练", command=self._ask_ai_subscription).grid(row=0, column=5, padx=8)

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
        self._active_market = self.market.get()
        symbols = MARKETS[self._active_market]
        self._symbols_snapshot = symbols[:]
        self.symbol_box.configure(values=symbols)
        self.symbol.set(symbols[0])

    def _fetch_symbol_price(self, symbol: str):
        try:
            info = yf.Ticker(symbol).fast_info
            p = info.get("lastPrice")
            if p:
                return float(p)
        except Exception:
            return None
        return None

    def _start_ticker_loop(self):
        def run():
            fail_count = 0
            while True:
                symbols = self._symbols_snapshot[:]
                try:
                    df = yf.download(
                        tickers=" ".join(symbols),
                        period="1d",
                        interval="1m",
                        group_by="ticker",
                        auto_adjust=False,
                        progress=False,
                        threads=True,
                    )
                    now_ts = time.time()
                    updates = []
                    for sym in symbols:
                        price = None
                        if sym in df:
                            part = df[sym]
                            if not part.empty and "Close" in part.columns:
                                price = float(part["Close"].dropna().iloc[-1]) if not part["Close"].dropna().empty else None
                        if price is None:
                            price = self._fetch_symbol_price(sym)
                        if price is not None:
                            updates.append((sym, price, now_ts))

                    if updates:
                        fail_count = 0
                        self.tick_queue.put(("batch", updates))
                    else:
                        fail_count += 1
                        self.tick_queue.put(("status", f"数据流状态：无新价格，重试中({fail_count})"))
                except Exception as e:
                    fail_count += 1
                    self.tick_queue.put(("status", f"数据流异常：{e}; 重试({fail_count})"))

                sleep_s = min(REFRESH_SECONDS + fail_count, 10)
                time.sleep(sleep_s)

        threading.Thread(target=run, daemon=True).start()

    def _consume_ticks(self):
        try:
            while True:
                kind, payload = self.tick_queue.get_nowait()
                if kind == "batch":
                    for sym, price, ts in payload:
                        self.price_cache[sym] = price
                        self.last_tick_ts[sym] = ts
                    current = self.symbol.get().strip().upper()
                    if current in self.price_cache:
                        self.price_var.set(f"{self.price_cache[current]:.2f}")
                        ts = datetime.fromtimestamp(self.last_tick_ts[current], tz=timezone.utc).astimezone()
                        self.feed_var.set(f"数据流状态：正常 | {current} 更新时间 {ts.strftime('%H:%M:%S')}")
                elif kind == "status":
                    self.feed_var.set(payload)
        except queue.Empty:
            pass
        self.after(300, self._consume_ticks)

    def _check_staleness(self):
        sym = self.symbol.get().strip().upper()
        ts = self.last_tick_ts.get(sym)
        if ts and (time.time() - ts > STALE_SECONDS):
            self.feed_var.set(f"数据流状态：{sym} 行情延迟>{STALE_SECONDS}s，自动重连中")
        self.after(1000, self._check_staleness)

    def _current_price(self, symbol: str) -> float:
        p = self.price_cache.get(symbol.upper())
        if p is None:
            raise ValueError("暂无最新价格，请稍后再试")
        return p

    def _buy(self):
        symbol = self.symbol.get().strip().upper()
        shares = float(self.shares_entry.get())
        price = self._current_price(symbol)
        try:
            self.portfolio.buy(symbol, shares, price)
            self._refresh_portfolio_view()
        except Exception as e:
            messagebox.showwarning("交易限制", str(e))

    def _sell(self):
        symbol = self.symbol.get().strip().upper()
        shares = float(self.shares_entry.get())
        price = self._current_price(symbol)
        try:
            self.portfolio.sell(symbol, shares, price)
            self._refresh_portfolio_view()
        except Exception as e:
            messagebox.showwarning("交易限制", str(e))

    def _save(self):
        DATA_FILE.write_text(json.dumps(self.portfolio.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        messagebox.showinfo("提示", "组合已保存")

    def _refresh_portfolio_view(self):
        self.cash_var.set(f"{self.portfolio.cash:.2f}")
        for i in self.holding_view.get_children():
            self.holding_view.delete(i)
        for pos in self.portfolio.positions.values():
            self.holding_view.insert("", "end", values=(pos.symbol, f"{pos.shares:.2f}", f"{pos.avg_price:.2f}"))

    def _ask_ai_subscription(self):
        snapshot = self.portfolio.to_json()
        symbol = self.symbol.get().strip().upper()
        price = self.price_cache.get(symbol, None)
        training_prompt = (
            "你是投资教练。请基于我的虚拟账户快照，输出："
            "1) 风险提示 2) 仓位建议 3) 今日复盘任务。"
            f"账户={json.dumps(snapshot, ensure_ascii=False)}；关注股票={symbol}；当前价={price}"
        )
        self.ai_text.set("已生成训练提示词（会员订阅模式）。\n将自动打开 ChatGPT 网页，请粘贴提示词进行训练，不调用 API。")
        encoded = urllib.parse.quote(training_prompt)
        webbrowser.open(f"https://chatgpt.com/?q={encoded}")


if __name__ == "__main__":
    app = App()
    app.mainloop()
