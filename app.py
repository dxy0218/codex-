import json
import queue
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict

import tkinter as tk
from tkinter import ttk, messagebox

import yfinance as yf

import webbrowser
import urllib.parse

DATA_FILE = Path("users.json")
MAX_TOTAL_EXPOSURE = 1_000_000.0
MAX_SINGLE_TRADE = 200_000.0

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
    def from_json(raw):
        pf = Portfolio(cash=raw.get("cash", 100000.0))
        for item in raw.get("positions", []):
            pf.positions[item["symbol"]] = Position(**item)
        return pf


class UserStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"users": {}}
        self.load()

    def load(self):
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self):
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def register(self, username: str, password: str):
        users = self.data["users"]
        if username in users:
            raise ValueError("用户名已存在")
        users[username] = {
            "password": password,
            "portfolio": Portfolio().to_json(),
            "history": [],
        }
        self.save()

    def login(self, username: str, password: str):
        user = self.data["users"].get(username)
        if not user or user["password"] != password:
            raise ValueError("用户名或密码错误")
        return user


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("虚拟股市训练器 (Windows)")
        self.geometry("980x680")

        self.store = UserStore(DATA_FILE)
        self.username = None
        self.user_data = None

        self.market = tk.StringVar(value="US")
        self.symbol = tk.StringVar(value=MARKETS["US"][0])
        self.price_var = tk.StringVar(value="--")
        self.cash_var = tk.StringVar()
        self.ai_text = tk.StringVar(value="AI 训练建议会显示在这里（会员订阅模式）")

        self.portfolio = Portfolio()
        self.price_cache: Dict[str, float] = {}
        self.tick_queue: queue.Queue = queue.Queue()

        self._show_auth_dialog()
        self._build_ui()
        self._refresh_portfolio_view()
        self._start_ticker_loop()
        self.after(300, self._consume_ticks)

    def _show_auth_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title("账户登录 / 创建")
        dlg.geometry("340x220")
        dlg.grab_set()
        dlg.protocol("WM_DELETE_WINDOW", self.destroy)

        user_var = tk.StringVar()
        pass_var = tk.StringVar()

        ttk.Label(dlg, text="用户名").pack(pady=(20, 4))
        ttk.Entry(dlg, textvariable=user_var).pack()
        ttk.Label(dlg, text="密码").pack(pady=(10, 4))
        ttk.Entry(dlg, textvariable=pass_var, show="*").pack()

        def do_login():
            try:
                self.user_data = self.store.login(user_var.get().strip(), pass_var.get().strip())
                self.username = user_var.get().strip()
                self.portfolio = Portfolio.from_json(self.user_data["portfolio"])
                dlg.destroy()
            except Exception as e:
                messagebox.showerror("登录失败", str(e))

        def do_register():
            try:
                self.store.register(user_var.get().strip(), pass_var.get().strip())
                messagebox.showinfo("成功", "账户创建成功，请登录")
            except Exception as e:
                messagebox.showerror("创建失败", str(e))

        ttk.Button(dlg, text="登录", command=do_login).pack(pady=(14, 4))
        ttk.Button(dlg, text="创建账户", command=do_register).pack()
        self.wait_window(dlg)

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=10)
        ttk.Label(top, text=f"当前用户: {self.username}", foreground="purple").pack(side="left", padx=(0, 10))
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
        ttk.Button(trade, text="保存组合", command=self._save_user_state).grid(row=0, column=4, padx=8)
        ttk.Button(trade, text="会员AI训练", command=self._ask_ai_subscription).grid(row=0, column=5, padx=8)

        middle = ttk.PanedWindow(self, orient="horizontal")
        middle.pack(fill="both", expand=True, padx=12, pady=8)
        left = ttk.Frame(middle)
        right = ttk.Frame(middle)
        middle.add(left, weight=2)
        middle.add(right, weight=3)

        self.holding_view = ttk.Treeview(left, columns=("symbol", "shares", "avg"), show="headings", height=12)
        for col, title in [("symbol", "代码"), ("shares", "持仓"), ("avg", "成本价")]:
            self.holding_view.heading(col, text=title)
            self.holding_view.column(col, width=90)
        self.holding_view.pack(fill="x", expand=False)

        ttk.Label(left, text="操作历史").pack(anchor="w", pady=(8, 2))
        self.history_list = tk.Listbox(left, height=12)
        self.history_list.pack(fill="both", expand=True)

        ttk.Label(right, text="AI 投资训练反馈").pack(anchor="w")
        ttk.Label(right, textvariable=self.ai_text, wraplength=430, justify="left").pack(fill="both", expand=True, pady=10)

    def _record_history(self, action: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {action}"
        self.user_data.setdefault("history", []).append(line)
        self.history_list.insert("end", line)
        self._save_user_state(silent=True)

    def _save_user_state(self, silent=False):
        self.user_data["portfolio"] = self.portfolio.to_json()
        self.store.data["users"][self.username] = self.user_data
        self.store.save()
        if not silent:
            messagebox.showinfo("提示", "已保存用户数据")

    def _refresh_history(self):
        self.history_list.delete(0, "end")
        for row in self.user_data.get("history", []):
            self.history_list.insert("end", row)

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
                        self.tick_queue.put((sym, float(data["Close"].iloc[-1])))
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
        try:
            self.portfolio.buy(symbol, shares, price)
            self._refresh_portfolio_view()
            self._record_history(f"买入 {symbol} x {shares:.2f} @ {price:.2f}")
        except Exception as e:
            messagebox.showwarning("交易限制", str(e))

    def _sell(self):
        symbol = self.symbol.get().strip().upper()
        shares = float(self.shares_entry.get())
        price = self._current_price(symbol)
        try:
            self.portfolio.sell(symbol, shares, price)
            self._refresh_portfolio_view()
            self._record_history(f"卖出 {symbol} x {shares:.2f} @ {price:.2f}")
        except Exception as e:
            messagebox.showwarning("交易限制", str(e))

    def _refresh_portfolio_view(self):
        self.cash_var.set(f"{self.portfolio.cash:.2f}")
        for i in self.holding_view.get_children():
            self.holding_view.delete(i)
        for pos in self.portfolio.positions.values():
            self.holding_view.insert("", "end", values=(pos.symbol, f"{pos.shares:.2f}", f"{pos.avg_price:.2f}"))
        self._refresh_history()

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
        webbrowser.open(f"https://chatgpt.com/?q={urllib.parse.quote(training_prompt)}")
        self._record_history(f"发起AI训练: {symbol} @ {price}")


if __name__ == "__main__":
    app = App()
    app.mainloop()
