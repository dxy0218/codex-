import csv
import hashlib
import json
import math
import os
import queue
import sqlite3
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import urllib.parse
import webbrowser

import yfinance as yf

APP_DIR = Path(__file__).resolve().parent
DATA_FILE = APP_DIR / "users.json"
DB_FILE = APP_DIR / "stock_trainer.sqlite3"
MAX_TOTAL_EXPOSURE = 1_000_000.0
MAX_SINGLE_TRADE = 200_000.0
INITIAL_CASH = 100_000.0
PRICE_REFRESH_SECONDS = 5

MARKETS = {
    "US": ["AAPL", "MSFT", "NVDA", "SPY"],
    "CN/HK": ["0700.HK", "9988.HK", "000001.SS", "399001.SZ"],
    "JP": ["7203.T", "6758.T", "9984.T", "^N225"],
    "EU": ["ASML.AS", "SAP.DE", "MC.PA", "^STOXX50E"],
}

MARKET_INDICES = {
    "上证指数": "000001.SS",
    "深证成指": "399001.SZ",
    "恒生指数": "^HSI",
    "日经225": "^N225",
    "标普500": "^GSPC",
    "纳斯达克": "^IXIC",
    "道琼斯": "^DJI",
}

APP_BG = "#0f1720"
PANEL_BG = "#151f2b"
CARD_BG = "#1b2836"
TEXT_FG = "#d7e2ee"
MUTED_FG = "#8ea0b5"
UP_COLOR = "#e84b4b"
DOWN_COLOR = "#21a67a"
ACCENT = "#2f80ed"

FUTURES_CONTRACTS = {
    "ES=F": {"name": "标普500 E-mini", "exchange": "CME", "multiplier": 50, "margin_rate": 0.12, "currency": "USD"},
    "NQ=F": {"name": "纳斯达克100 E-mini", "exchange": "CME", "multiplier": 20, "margin_rate": 0.14, "currency": "USD"},
    "YM=F": {"name": "道指 E-mini", "exchange": "CBOT", "multiplier": 5, "margin_rate": 0.10, "currency": "USD"},
    "GC=F": {"name": "COMEX黄金", "exchange": "COMEX", "multiplier": 100, "margin_rate": 0.10, "currency": "USD"},
    "CL=F": {"name": "NYMEX原油", "exchange": "NYMEX", "multiplier": 1000, "margin_rate": 0.12, "currency": "USD"},
}


def market_rule_for(symbol: str) -> dict:
    symbol = symbol.upper()
    if symbol in FUTURES_CONTRACTS:
        return {"asset_type": "期货", "market": FUTURES_CONTRACTS[symbol]["exchange"], "lot_size": 1, "short_allowed": True, "t_plus_one": False}
    if symbol.endswith((".SS", ".SZ")):
        return {"asset_type": "股票", "market": "A股", "lot_size": 100, "short_allowed": False, "t_plus_one": True}
    if symbol.endswith(".HK"):
        return {"asset_type": "股票", "market": "港股", "lot_size": 100, "short_allowed": False, "t_plus_one": False}
    if symbol.endswith(".T"):
        return {"asset_type": "股票", "market": "日股", "lot_size": 100, "short_allowed": False, "t_plus_one": False}
    if "." in symbol and not symbol.startswith("^"):
        return {"asset_type": "股票", "market": "国际", "lot_size": 1, "short_allowed": False, "t_plus_one": False}
    return {"asset_type": "股票", "market": "美股", "lot_size": 1, "short_allowed": False, "t_plus_one": False}


@dataclass
class Position:
    symbol: str
    shares: float
    avg_price: float
    asset_type: str = "股票"
    market: str = "美股"
    opened_at: str = ""


class Portfolio:
    def __init__(self, cash: float = INITIAL_CASH):
        self.cash = cash
        self.positions: Dict[str, Position] = {}

    def exposure(self) -> float:
        total = 0.0
        for p in self.positions.values():
            if p.asset_type == "期货" and p.symbol in FUTURES_CONTRACTS:
                total += abs(p.shares) * p.avg_price * FUTURES_CONTRACTS[p.symbol]["multiplier"]
            else:
                total += abs(p.shares) * p.avg_price
        return total

    def market_value(self, price_cache: Dict[str, float]) -> float:
        total = self.cash
        for pos in self.positions.values():
            latest = price_cache.get(pos.symbol, pos.avg_price)
            if pos.asset_type == "期货" and pos.symbol in FUTURES_CONTRACTS:
                total += (latest - pos.avg_price) * pos.shares * FUTURES_CONTRACTS[pos.symbol]["multiplier"]
            else:
                total += pos.shares * latest
        return total

    @staticmethod
    def _validate_positive_number(name: str, value: float):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name}必须是大于 0 的有效数字")

    @staticmethod
    def _validate_lot(symbol: str, shares: float, action: str):
        rule = market_rule_for(symbol)
        lot = rule["lot_size"]
        if rule["asset_type"] == "股票" and action == "buy" and lot > 1 and not math.isclose(shares % lot, 0, abs_tol=1e-9):
            raise ValueError(f"{rule['market']}买入需按 {lot} 股/手整数倍下单")
        if rule["asset_type"] == "期货" and not float(shares).is_integer():
            raise ValueError("期货按合约整数手交易")

    def buy(self, symbol: str, shares: float, price: float):
        symbol = symbol.upper()
        self._validate_positive_number("数量", shares)
        self._validate_positive_number("价格", price)
        self._validate_lot(symbol, shares, "buy")
        rule = market_rule_for(symbol)
        if rule["asset_type"] == "期货":
            self.open_future(symbol, shares, price, side="long")
            return
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
            self.positions[symbol] = Position(symbol, shares, price, rule["asset_type"], rule["market"], datetime.now().date().isoformat())
        self.cash -= cost

    def sell(self, symbol: str, shares: float, price: float):
        symbol = symbol.upper()
        self._validate_positive_number("数量", shares)
        self._validate_positive_number("价格", price)
        self._validate_lot(symbol, shares, "sell")
        rule = market_rule_for(symbol)
        pos = self.positions.get(symbol)
        if rule["asset_type"] == "期货":
            if pos and pos.shares > 0:
                self.close_future(symbol, shares, price)
            else:
                self.open_future(symbol, shares, price, side="short")
            return
        if not pos or pos.shares < shares:
            raise ValueError("持仓不足；股票模拟暂不允许裸卖空")
        if rule.get("t_plus_one") and pos.opened_at == datetime.now().date().isoformat():
            raise ValueError("A股执行 T+1：当日买入的股票不能当日卖出")
        pos.shares -= shares
        self.cash += shares * price
        if math.isclose(pos.shares, 0, abs_tol=1e-9):
            del self.positions[symbol]

    def open_future(self, symbol: str, contracts: float, price: float, side: str):
        spec = FUTURES_CONTRACTS.get(symbol)
        if not spec:
            raise ValueError("未配置该期货合约参数")
        signed = contracts if side == "long" else -contracts
        margin = abs(contracts) * price * spec["multiplier"] * spec["margin_rate"]
        if margin > self.cash:
            raise ValueError(f"保证金不足，需要约 {margin:.2f}")
        pos = self.positions.get(symbol)
        if pos and pos.shares * signed < 0:
            raise ValueError("已有反向期货持仓，请先平仓")
        if pos:
            total_contracts = abs(pos.shares) + contracts
            pos.avg_price = (abs(pos.shares) * pos.avg_price + contracts * price) / total_contracts
            pos.shares += signed
        else:
            rule = market_rule_for(symbol)
            self.positions[symbol] = Position(symbol, signed, price, "期货", rule["market"], datetime.now().date().isoformat())
        self.cash -= margin

    def close_future(self, symbol: str, contracts: float, price: float):
        pos = self.positions.get(symbol)
        spec = FUTURES_CONTRACTS.get(symbol)
        if not pos or pos.asset_type != "期货":
            raise ValueError("没有可平期货持仓")
        if contracts > abs(pos.shares):
            raise ValueError("平仓手数超过持仓")
        direction = 1 if pos.shares > 0 else -1
        pnl = (price - pos.avg_price) * contracts * spec["multiplier"] * direction
        released_margin = contracts * pos.avg_price * spec["multiplier"] * spec["margin_rate"]
        self.cash += released_margin + pnl
        pos.shares -= contracts * direction
        if math.isclose(pos.shares, 0, abs_tol=1e-9):
            del self.positions[symbol]

    def to_json(self):
        return {"cash": self.cash, "positions": [asdict(p) for p in self.positions.values()]}

    @staticmethod
    def from_json(raw):
        pf = Portfolio(cash=float(raw.get("cash", INITIAL_CASH)))
        for item in raw.get("positions", []):
            symbol = str(item["symbol"]).upper()
            rule = market_rule_for(symbol)
            pf.positions[symbol] = Position(
                symbol=symbol,
                shares=float(item["shares"]),
                avg_price=float(item["avg_price"]),
                asset_type=str(item.get("asset_type", rule["asset_type"])),
                market=str(item.get("market", rule["market"])),
                opened_at=str(item.get("opened_at", "")),
            )
        return pf

class UserStore:
    """SQLite-backed user store with JSON-file migration compatibility."""

    JSON_FIELDS = ["portfolio", "history", "watchlist", "equity_history", "backtests", "strategy_params"]

    def __init__(self, path: Path):
        self.legacy_path = path
        self.db_path = DB_FILE
        self.data = {"users": {}}
        self._init_db()
        self._migrate_legacy_json()
        self.load()

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.db_path)
        try:
            yield con
        finally:
            con.close()

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    portfolio TEXT NOT NULL,
                    history TEXT NOT NULL,
                    watchlist TEXT NOT NULL,
                    equity_history TEXT NOT NULL,
                    backtests TEXT NOT NULL,
                    strategy_params TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            con.commit()

    def _migrate_legacy_json(self):
        if not self.legacy_path.exists():
            return
        with self._connect() as con:
            existing_count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if existing_count:
            return
        try:
            raw = json.loads(self.legacy_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            backup = self.legacy_path.with_suffix(f".corrupt-{int(time.time())}.json")
            self.legacy_path.replace(backup)
            messagebox.showwarning("数据文件损坏", f"已备份损坏文件到:\n{backup}\n将使用新的 SQLite 数据库。\n错误: {exc}")
            return
        if not isinstance(raw, dict) or not isinstance(raw.get("users"), dict):
            return
        self.data = raw
        for user in self.data.get("users", {}).values():
            self.ensure_user_shape(user)
        self.save()
        migrated = self.legacy_path.with_suffix(f".migrated-{int(time.time())}.json")
        try:
            self.legacy_path.replace(migrated)
        except OSError:
            pass

    def load(self):
        self.data = {"users": {}}
        with self._connect() as con:
            rows = con.execute(
                "SELECT username,password_hash,portfolio,history,watchlist,equity_history,backtests,strategy_params FROM users"
            ).fetchall()
        for username, password_hash, portfolio, history, watchlist, equity_history, backtests, strategy_params in rows:
            user = {
                "password_hash": json.loads(password_hash),
                "portfolio": json.loads(portfolio),
                "history": json.loads(history),
                "watchlist": json.loads(watchlist),
                "equity_history": json.loads(equity_history),
                "backtests": json.loads(backtests),
                "strategy_params": json.loads(strategy_params),
            }
            self.ensure_user_shape(user)
            self.data["users"][username] = user

    def save(self):
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as con:
            for username, user in self.data.get("users", {}).items():
                self.ensure_user_shape(user)
                con.execute(
                    """
                    INSERT INTO users(username,password_hash,portfolio,history,watchlist,equity_history,backtests,strategy_params,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(username) DO UPDATE SET
                        password_hash=excluded.password_hash,
                        portfolio=excluded.portfolio,
                        history=excluded.history,
                        watchlist=excluded.watchlist,
                        equity_history=excluded.equity_history,
                        backtests=excluded.backtests,
                        strategy_params=excluded.strategy_params,
                        updated_at=excluded.updated_at
                    """,
                    (
                        username,
                        json.dumps(user["password_hash"], ensure_ascii=False),
                        json.dumps(user["portfolio"], ensure_ascii=False),
                        json.dumps(user["history"], ensure_ascii=False),
                        json.dumps(user["watchlist"], ensure_ascii=False),
                        json.dumps(user["equity_history"], ensure_ascii=False),
                        json.dumps(user["backtests"], ensure_ascii=False),
                        json.dumps(user["strategy_params"], ensure_ascii=False),
                        now,
                    ),
                )
            con.commit()

    @staticmethod
    def _hash_password(password: str, salt: Optional[str] = None) -> Dict[str, str]:
        salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000).hex()
        return {"salt": salt, "hash": digest, "algo": "pbkdf2_sha256"}

    @classmethod
    def _verify_password(cls, user: dict, password: str) -> bool:
        if "password_hash" in user:
            meta = user["password_hash"]
            expected = cls._hash_password(password, meta["salt"])["hash"]
            return secrets.compare_digest(expected, meta["hash"])
        if user.get("password") == password:
            user["password_hash"] = cls._hash_password(password)
            user.pop("password", None)
            return True
        return False

    @staticmethod
    def ensure_user_shape(user: dict):
        user.setdefault("portfolio", Portfolio().to_json())
        user.setdefault("history", [])
        user.setdefault("watchlist", [])
        user.setdefault("equity_history", [])
        user.setdefault("backtests", [])
        user.setdefault("strategy_params", {"symbol": "AAPL", "period": "1y", "fast": 20, "slow": 60})

    def register(self, username: str, password: str):
        username = username.strip()
        if len(username) < 2:
            raise ValueError("用户名至少需要 2 个字符")
        if len(password) < 6:
            raise ValueError("密码至少需要 6 个字符")
        users = self.data["users"]
        if username in users:
            raise ValueError("用户名已存在")
        users[username] = {
            "password_hash": self._hash_password(password),
            "portfolio": Portfolio().to_json(),
            "history": [],
            "watchlist": [],
            "equity_history": [],
            "backtests": [],
            "strategy_params": {"symbol": "AAPL", "period": "1y", "fast": 20, "slow": 60},
        }
        self.save()

    def login(self, username: str, password: str):
        username = username.strip()
        user = self.data["users"].get(username)
        if not user or not self._verify_password(user, password):
            raise ValueError("用户名或密码错误")
        self.ensure_user_shape(user)
        self.save()
        return user

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("虚拟股市训练器 (Windows)")
        self.geometry("1220x820")

        self.store = UserStore(DATA_FILE)
        self.username = None
        self.user_data = None

        self.market = tk.StringVar(value="US")
        self.asset_class = tk.StringVar(value="股票")
        self.symbol = tk.StringVar(value=MARKETS["US"][0])
        self.search_var = tk.StringVar()
        self.price_var = tk.StringVar(value="--")
        self.cash_var = tk.StringVar()
        self.equity_var = tk.StringVar(value="--")
        self.status_var = tk.StringVar(value="就绪")
        self.ai_text = tk.StringVar(value="AI 训练建议会显示在这里（会员订阅模式）")
        self.backtest_symbol_var = tk.StringVar(value="AAPL")
        self.backtest_period_var = tk.StringVar(value="1y")
        self.backtest_fast_var = tk.IntVar(value=20)
        self.backtest_slow_var = tk.IntVar(value=60)

        self.portfolio = Portfolio()
        self.price_cache: Dict[str, float] = {}
        self.quote_cache: Dict[str, dict] = {}
        self.tick_queue: queue.Queue = queue.Queue()
        self._last_requested_symbol: Optional[str] = None
        self._last_equity_snapshot_at = 0.0

        self._show_auth_dialog()
        self._load_strategy_params()
        self._build_ui()
        self._refresh_all_views()
        self._start_ticker_loop()
        self.after(300, self._consume_ticks)

    def _show_auth_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title("账户登录 / 创建")
        dlg.geometry("340x230")
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
                username = user_var.get().strip()
                self.user_data = self.store.login(username, pass_var.get())
                self.username = username
                self.portfolio = Portfolio.from_json(self.user_data["portfolio"])
                dlg.destroy()
            except Exception as e:
                messagebox.showerror("登录失败", str(e))

        def do_register():
            try:
                self.store.register(user_var.get(), pass_var.get())
                messagebox.showinfo("成功", "账户创建成功，请登录")
            except Exception as e:
                messagebox.showerror("创建失败", str(e))

        ttk.Button(dlg, text="登录", command=do_login).pack(pady=(14, 4))
        ttk.Button(dlg, text="创建账户", command=do_register).pack()
        self.wait_window(dlg)

    def _build_ui(self):
        self.configure(bg=APP_BG)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=APP_BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("Card.TFrame", background=CARD_BG)
        style.configure("TLabel", background=APP_BG, foreground=TEXT_FG)
        style.configure("Muted.TLabel", background=APP_BG, foreground=MUTED_FG)
        style.configure("TButton", padding=(10, 5))
        style.configure("TNotebook", background=APP_BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 7))
        style.configure("Treeview", background="#101923", fieldbackground="#101923", foreground=TEXT_FG, rowheight=25, borderwidth=0)
        style.configure("Treeview.Heading", background="#223246", foreground=TEXT_FG, relief="flat")
        style.map("Treeview", background=[("selected", "#315a86")])

        self._build_market_header()

        main = ttk.Frame(self, style="TFrame")
        main.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.sidebar = ttk.Frame(main, width=270, style="Panel.TFrame")
        self.sidebar.pack(side="left", fill="y", padx=(0, 10))
        self.sidebar.pack_propagate(False)
        self.content = ttk.Frame(main, style="TFrame")
        self.content.pack(side="left", fill="both", expand=True)
        self._build_sidebar()

        trade = ttk.Frame(self.content, style="Card.TFrame")
        trade.pack(fill="x", pady=(0, 8))
        ttk.Label(trade, text="品种", background=CARD_BG, foreground=MUTED_FG).grid(row=0, column=0, padx=(12, 4), pady=10)
        asset_box = ttk.Combobox(trade, textvariable=self.asset_class, values=["股票", "期货"], width=7, state="readonly")
        asset_box.grid(row=0, column=1, padx=4)
        asset_box.bind("<<ComboboxSelected>>", self._on_asset_class_change)
        ttk.Label(trade, text="市场", background=CARD_BG, foreground=MUTED_FG).grid(row=0, column=2, padx=(12, 4), pady=10)
        market_box = ttk.Combobox(trade, textvariable=self.market, values=list(MARKETS.keys()), width=8, state="readonly")
        market_box.grid(row=0, column=3, padx=4)
        market_box.bind("<<ComboboxSelected>>", self._on_market_change)
        ttk.Label(trade, text="代码", background=CARD_BG, foreground=MUTED_FG).grid(row=0, column=4, padx=(12, 4))
        self.symbol_box = ttk.Combobox(trade, textvariable=self.symbol, values=MARKETS[self.market.get()], width=14)
        self.symbol_box.grid(row=0, column=5, padx=4)
        self.symbol_box.bind("<<ComboboxSelected>>", lambda _event: self._request_price_refresh(force=True))
        ttk.Button(trade, text="刷新", command=lambda: self._request_price_refresh(force=True)).grid(row=0, column=6, padx=4)
        ttk.Button(trade, text="加入自选", command=self._add_current_to_watchlist).grid(row=0, column=7, padx=4)
        ttk.Label(trade, text="价", background=CARD_BG, foreground=MUTED_FG).grid(row=0, column=8, padx=(14, 4))
        ttk.Label(trade, textvariable=self.price_var, background=CARD_BG, foreground=UP_COLOR, font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=9, padx=4)
        ttk.Label(trade, text="现金", background=CARD_BG, foreground=MUTED_FG).grid(row=0, column=10, padx=(14, 4))
        ttk.Label(trade, textvariable=self.cash_var, background=CARD_BG, foreground="#7ee787").grid(row=0, column=11, padx=4)
        ttk.Label(trade, text="总资产", background=CARD_BG, foreground=MUTED_FG).grid(row=0, column=12, padx=(14, 4))
        ttk.Label(trade, textvariable=self.equity_var, background=CARD_BG, foreground="#f0b429").grid(row=0, column=13, padx=4)

        order = ttk.Frame(self.content, style="Card.TFrame")
        order.pack(fill="x", pady=(0, 8))
        self.shares_entry = ttk.Entry(order, width=12)
        self.shares_entry.insert(0, "1")
        ttk.Label(order, text="数量", background=CARD_BG, foreground=MUTED_FG).grid(row=0, column=0, padx=(12, 4), pady=8)
        self.shares_entry.grid(row=0, column=1)
        ttk.Button(order, text="买入/做多", command=self._buy).grid(row=0, column=2, padx=8)
        ttk.Button(order, text="卖出/做空", command=self._sell).grid(row=0, column=3, padx=8)
        ttk.Button(order, text="保存组合", command=self._save_user_state).grid(row=0, column=4, padx=8)
        ttk.Button(order, text="导出交易CSV", command=self._export_history_csv).grid(row=0, column=5, padx=8)
        ttk.Button(order, text="会员AI训练", command=self._ask_ai_subscription).grid(row=0, column=6, padx=8)

        self.tabs = ttk.Notebook(self.content)
        self.tabs.pack(fill="both", expand=True)
        self.home_tab = ttk.Frame(self.tabs)
        self.trade_tab = ttk.Frame(self.tabs)
        self.watch_tab = ttk.Frame(self.tabs)
        self.chart_tab = ttk.Frame(self.tabs)
        self.backtest_tab = ttk.Frame(self.tabs)
        self.tabs.add(self.home_tab, text="行情首页")
        self.tabs.add(self.trade_tab, text="交易与持仓")
        self.tabs.add(self.watch_tab, text="股票搜索 / 自选股")
        self.tabs.add(self.chart_tab, text="收益曲线")
        self.tabs.add(self.backtest_tab, text="回测")

        self._build_home_tab()
        self._build_trade_tab()
        self._build_watch_tab()
        self._build_chart_tab()
        self._build_backtest_tab()

        status = ttk.Frame(self, style="TFrame")
        status.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(status, textvariable=self.status_var, foreground=MUTED_FG, background=APP_BG).pack(anchor="w")

    def _build_market_header(self):
        header = tk.Frame(self, bg="#07111d", height=74)
        header.pack(fill="x")
        header.pack_propagate(False)
        left = tk.Frame(header, bg="#07111d")
        left.pack(side="left", fill="y", padx=14)
        tk.Label(left, text="StockTrainer Pro", bg="#07111d", fg="#ffffff", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", pady=(9, 0))
        tk.Label(left, text=f"用户 {self.username} · 实时行情训练终端", bg="#07111d", fg=MUTED_FG).pack(anchor="w")
        self.market_strip = tk.Frame(header, bg="#07111d")
        self.market_strip.pack(side="left", fill="both", expand=True, padx=10)
        self.index_labels: Dict[str, Tuple[tk.Label, tk.Label]] = {}
        for name, symbol in MARKET_INDICES.items():
            box = tk.Frame(self.market_strip, bg="#0d1b2a", padx=10, pady=7)
            box.pack(side="left", padx=5, pady=9)
            title = tk.Label(box, text=name, bg="#0d1b2a", fg=MUTED_FG, font=("Microsoft YaHei UI", 9))
            title.pack(anchor="w")
            val = tk.Label(box, text="--", bg="#0d1b2a", fg=TEXT_FG, font=("Consolas", 11, "bold"))
            val.pack(anchor="w")
            self.index_labels[symbol] = (title, val)

    def _build_sidebar(self):
        tk.Label(self.sidebar, text="大盘指数", bg=PANEL_BG, fg=TEXT_FG, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=10, pady=(12, 4))
        self.index_view = ttk.Treeview(self.sidebar, columns=("name", "price", "chg"), show="headings", height=8)
        for col, title, width in [("name", "指数", 82), ("price", "最新", 78), ("chg", "涨跌", 72)]:
            self.index_view.heading(col, text=title)
            self.index_view.column(col, width=width, anchor="center")
        self.index_view.pack(fill="x", padx=8, pady=(0, 10))
        self.index_view.tag_configure("up", foreground=UP_COLOR)
        self.index_view.tag_configure("down", foreground=DOWN_COLOR)

        tk.Label(self.sidebar, text="自选股票", bg=PANEL_BG, fg=TEXT_FG, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=10, pady=(8, 4))
        quick = tk.Frame(self.sidebar, bg=PANEL_BG)
        quick.pack(fill="x", padx=8, pady=(0, 6))
        self.sidebar_add_entry = ttk.Entry(quick, width=14)
        self.sidebar_add_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(quick, text="添加", command=self._add_sidebar_symbol).pack(side="left", padx=(5, 0))
        self.sidebar_watch_view = ttk.Treeview(self.sidebar, columns=("symbol", "price", "chg"), show="headings", height=19)
        for col, title, width in [("symbol", "代码", 82), ("price", "最新", 78), ("chg", "涨跌", 72)]:
            self.sidebar_watch_view.heading(col, text=title)
            self.sidebar_watch_view.column(col, width=width, anchor="center")
        self.sidebar_watch_view.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.sidebar_watch_view.tag_configure("up", foreground=UP_COLOR)
        self.sidebar_watch_view.tag_configure("down", foreground=DOWN_COLOR)
        self.sidebar_watch_view.bind("<Double-1>", lambda _event: self._use_sidebar_watchlist())
        ttk.Button(self.sidebar, text="刷新自选", command=self._refresh_watchlist_prices).pack(fill="x", padx=8, pady=(0, 10))

    def _load_strategy_params(self):
        params = self.user_data.get("strategy_params", {}) if self.user_data else {}
        self.backtest_symbol_var.set(str(params.get("symbol", "AAPL")))
        self.backtest_period_var.set(str(params.get("period", "1y")))
        self.backtest_fast_var.set(int(params.get("fast", 20)))
        self.backtest_slow_var.set(int(params.get("slow", 60)))

    def _save_strategy_params(self):
        self.user_data["strategy_params"] = {
            "symbol": self.backtest_symbol_var.get().strip().upper() or "AAPL",
            "period": self.backtest_period_var.get().strip() or "1y",
            "fast": int(self.backtest_fast_var.get()),
            "slow": int(self.backtest_slow_var.get()),
        }
        self._save_user_state(silent=True)
        self.status_var.set("策略参数已保存")

    def _build_home_tab(self):
        wrap = ttk.Frame(self.home_tab, padding=18)
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="虚拟股市训练器", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        ttk.Label(wrap, text="练习交易、观察自选股、查看收益曲线，并用均线策略做快速回测。", foreground="#555").pack(anchor="w", pady=(4, 16))
        cards = ttk.Frame(wrap)
        cards.pack(fill="x")
        self.home_summary_var = tk.StringVar()
        ttk.Label(cards, textvariable=self.home_summary_var, justify="left", font=("Microsoft YaHei UI", 11)).pack(side="left", anchor="n", padx=(0, 40))
        actions = ttk.LabelFrame(cards, text="快捷入口", padding=10)
        actions.pack(side="left", fill="x", expand=True)
        ttk.Button(actions, text="去交易", command=lambda: self.tabs.select(self.trade_tab)).grid(row=0, column=0, padx=6, pady=6, sticky="we")
        ttk.Button(actions, text="管理自选股", command=lambda: self.tabs.select(self.watch_tab)).grid(row=0, column=1, padx=6, pady=6, sticky="we")
        ttk.Button(actions, text="查看收益曲线", command=lambda: self.tabs.select(self.chart_tab)).grid(row=1, column=0, padx=6, pady=6, sticky="we")
        ttk.Button(actions, text="运行回测", command=lambda: self.tabs.select(self.backtest_tab)).grid(row=1, column=1, padx=6, pady=6, sticky="we")
        for i in range(2):
            actions.columnconfigure(i, weight=1)
        ttk.Separator(wrap).pack(fill="x", pady=18)
        ttk.Label(wrap, text="产品化更新", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        notes = (
            "• 数据已迁移到 SQLite，更适合长期使用和后续扩展。\n"
            "• 回测参数会自动保存，下次登录继续使用。\n"
            "• 支持K线图、回测报告导出和 Windows exe 打包。\n"
            "• 本软件仅用于交易训练，不构成投资建议。"
        )
        ttk.Label(wrap, text=notes, justify="left").pack(anchor="w", pady=8)

    def _refresh_home_summary(self):
        if not hasattr(self, "home_summary_var"):
            return
        equity = self.portfolio.market_value(self.price_cache)
        watch_count = len(self.user_data.get("watchlist", []))
        position_count = len(self.portfolio.positions)
        backtest_count = len(self.user_data.get("backtests", []))
        self.home_summary_var.set(
            f"当前用户：{self.username}\n"
            f"现金：{self.portfolio.cash:.2f}\n"
            f"估算总资产：{equity:.2f}\n"
            f"持仓数量：{position_count}\n"
            f"自选股数量：{watch_count}\n"
            f"回测次数：{backtest_count}"
        )

    def _build_trade_tab(self):
        middle = ttk.PanedWindow(self.trade_tab, orient="horizontal")
        middle.pack(fill="both", expand=True)
        left = ttk.Frame(middle)
        right = ttk.Frame(middle)
        middle.add(left, weight=3)
        middle.add(right, weight=2)

        self.holding_view = ttk.Treeview(left, columns=("type", "symbol", "shares", "avg", "last", "market", "pnl"), show="headings", height=12)
        for col, title, width in [
            ("type", "类型", 70), ("symbol", "代码", 90), ("shares", "持仓/手", 90), ("avg", "成本价", 90),
            ("last", "现价", 90), ("market", "市值/保证金", 110), ("pnl", "浮动盈亏", 100),
        ]:
            self.holding_view.heading(col, text=title)
            self.holding_view.column(col, width=width, anchor="center")
        self.holding_view.pack(fill="x", expand=False, padx=6, pady=6)

        ttk.Label(left, text="操作历史").pack(anchor="w", padx=6, pady=(8, 2))
        self.history_list = tk.Listbox(left, height=14)
        self.history_list.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        ttk.Label(right, text="AI 投资训练反馈").pack(anchor="w", padx=6, pady=6)
        ttk.Label(right, textvariable=self.ai_text, wraplength=430, justify="left").pack(fill="both", expand=True, padx=6, pady=10)

    def _build_watch_tab(self):
        top = ttk.Frame(self.watch_tab)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="搜索/输入代码").pack(side="left")
        ttk.Entry(top, textvariable=self.search_var, width=24).pack(side="left", padx=6)
        ttk.Button(top, text="查询", command=self._search_symbol).pack(side="left", padx=4)
        ttk.Button(top, text="设为当前代码", command=self._set_symbol_from_search).pack(side="left", padx=4)
        ttk.Button(top, text="加入自选", command=self._add_watchlist_from_search).pack(side="left", padx=4)
        ttk.Button(top, text="删除自选", command=self._remove_selected_watchlist).pack(side="left", padx=4)
        ttk.Button(top, text="刷新自选价格", command=self._refresh_watchlist_prices).pack(side="left", padx=4)
        ttk.Button(top, text="绘制K线", command=self._draw_kline_from_search).pack(side="left", padx=4)

        body = ttk.PanedWindow(self.watch_tab, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=8)
        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=2)
        body.add(right, weight=3)

        self.watch_view = ttk.Treeview(left, columns=("symbol", "price", "note"), show="headings", height=18)
        for col, title, width in [("symbol", "自选代码", 120), ("price", "最新价", 100), ("note", "备注", 180)]:
            self.watch_view.heading(col, text=title)
            self.watch_view.column(col, width=width, anchor="center")
        self.watch_view.pack(fill="both", expand=True)
        self.watch_view.bind("<Double-1>", lambda _event: self._use_selected_watchlist())

        ttk.Label(right, text="查询结果 / 股票摘要").pack(anchor="w")
        self.search_text = tk.Text(right, height=18, wrap="word")
        self.search_text.pack(fill="both", expand=True, pady=(4, 8))
        self.kline_canvas = tk.Canvas(right, bg="white", height=260)
        self.kline_canvas.pack(fill="both", expand=True)

    def _build_chart_tab(self):
        bar = ttk.Frame(self.chart_tab)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(bar, text="记录当前总资产", command=lambda: self._snapshot_equity(force=True)).pack(side="left", padx=4)
        ttk.Button(bar, text="刷新收益曲线", command=self._draw_equity_curve).pack(side="left", padx=4)
        ttk.Button(bar, text="导出收益CSV", command=self._export_equity_csv).pack(side="left", padx=4)
        ttk.Label(bar, text="提示：交易后会自动记录一次资产快照。").pack(side="left", padx=12)
        self.equity_canvas = tk.Canvas(self.chart_tab, bg="white", height=420)
        self.equity_canvas.pack(fill="both", expand=True, padx=8, pady=8)

    def _build_backtest_tab(self):
        top = ttk.Frame(self.backtest_tab)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="代码").pack(side="left")
        ttk.Entry(top, textvariable=self.backtest_symbol_var, width=14).pack(side="left", padx=4)
        ttk.Label(top, text="周期").pack(side="left", padx=(10, 0))
        ttk.Combobox(top, textvariable=self.backtest_period_var, values=["3mo", "6mo", "1y", "2y", "5y"], width=8, state="readonly").pack(side="left", padx=4)
        ttk.Label(top, text="快均线").pack(side="left", padx=(10, 0))
        ttk.Spinbox(top, from_=3, to=120, textvariable=self.backtest_fast_var, width=6).pack(side="left", padx=4)
        ttk.Label(top, text="慢均线").pack(side="left", padx=(10, 0))
        ttk.Spinbox(top, from_=5, to=240, textvariable=self.backtest_slow_var, width=6).pack(side="left", padx=4)
        ttk.Button(top, text="保存参数", command=self._save_strategy_params).pack(side="left", padx=4)
        ttk.Button(top, text="运行均线回测", command=self._run_backtest_async).pack(side="left", padx=10)
        ttk.Button(top, text="导出回测CSV", command=self._export_backtest_csv).pack(side="left", padx=4)
        ttk.Button(top, text="导出报告", command=self._export_backtest_report).pack(side="left", padx=4)

        self.backtest_text = tk.Text(self.backtest_tab, height=12, wrap="word")
        self.backtest_text.pack(fill="x", padx=8, pady=(0, 8))
        self.backtest_canvas = tk.Canvas(self.backtest_tab, bg="white", height=300)
        self.backtest_canvas.pack(fill="both", expand=True, padx=8, pady=8)

    def _record_history(self, action: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {action}"
        self.user_data.setdefault("history", []).append(line)
        self.history_list.insert("end", line)
        self._snapshot_equity(force=True, save=False)
        self._save_user_state(silent=True)

    def _save_user_state(self, silent=False):
        self.user_data["portfolio"] = self.portfolio.to_json()
        self.store.data["users"][self.username] = self.user_data
        self.store.save()
        if not silent:
            messagebox.showinfo("提示", "已保存用户数据")

    def _refresh_all_views(self):
        self._refresh_portfolio_view()
        self._refresh_watchlist_view()
        self._refresh_market_views()
        self._draw_equity_curve()
        self._refresh_home_summary()

    def _refresh_history(self):
        self.history_list.delete(0, "end")
        for row in self.user_data.get("history", []):
            self.history_list.insert("end", row)

    def _on_asset_class_change(self, _event=None):
        if self.asset_class.get() == "期货":
            symbols = list(FUTURES_CONTRACTS.keys())
            self.symbol_box.configure(values=symbols)
            self.symbol.set(symbols[0])
        else:
            self._on_market_change()
        self.price_var.set("--")
        self._request_price_refresh(force=True)

    def _on_market_change(self, _event=None):
        if self.asset_class.get() == "期货":
            self._on_asset_class_change()
            return
        symbols = MARKETS[self.market.get()]
        merged = sorted(set(symbols + self.user_data.get("watchlist", [])))
        self.symbol_box.configure(values=merged)
        self.symbol.set(symbols[0])
        self.price_var.set("--")
        self._request_price_refresh(force=True)

    def _request_price_refresh(self, force=False):
        symbol = self.symbol.get().strip().upper()
        if not symbol:
            return
        if force or symbol != self._last_requested_symbol:
            self.status_var.set(f"正在刷新 {symbol} 行情...")
            self._last_requested_symbol = symbol

    def _start_ticker_loop(self):
        def run():
            while True:
                symbols = {self.symbol.get().strip().upper()}
                symbols.update(self.user_data.get("watchlist", [])[:30])
                symbols.update(MARKET_INDICES.values())
                symbols.update(FUTURES_CONTRACTS.keys())
                for sym in sorted(s for s in symbols if s):
                    try:
                        data = yf.Ticker(sym).history(period="1d", interval="1m")
                        if not data.empty:
                            closes = [float(x) for x in data["Close"].tolist() if math.isfinite(float(x))]
                            if closes:
                                price = closes[-1]
                                base = closes[-2] if len(closes) > 1 else closes[0]
                                change = price - base
                                pct = (change / base * 100) if base else 0.0
                                self.tick_queue.put(("quote", sym, {"price": price, "change": change, "pct": pct}))
                        elif sym == self.symbol.get().strip().upper():
                            self.tick_queue.put(("status", sym, "未获取到行情数据"))
                    except Exception as exc:
                        if sym == self.symbol.get().strip().upper():
                            self.tick_queue.put(("status", sym, f"行情刷新失败: {exc}"))
                time.sleep(PRICE_REFRESH_SECONDS)

        threading.Thread(target=run, daemon=True).start()

    def _consume_ticks(self):
        try:
            while True:
                kind, sym, payload = self.tick_queue.get_nowait()
                if kind == "quote":
                    quote = dict(payload)
                    price = float(quote.get("price", 0))
                    self.price_cache[sym] = price
                    self.quote_cache[sym] = quote
                    if sym == self.symbol.get().strip().upper():
                        self.price_var.set(f"{price:.2f}")
                        self.status_var.set(f"{sym} 行情已更新")
                    self._refresh_portfolio_view(refresh_history=False)
                    self._refresh_watchlist_view()
                    self._refresh_market_views()
                elif kind == "price":
                    price = float(payload)
                    old = self.price_cache.get(sym, price)
                    change = price - old
                    pct = (change / old * 100) if old else 0.0
                    self.price_cache[sym] = price
                    self.quote_cache[sym] = {"price": price, "change": change, "pct": pct}
                    if sym == self.symbol.get().strip().upper():
                        self.price_var.set(f"{price:.2f}")
                        self.status_var.set(f"{sym} 行情已更新")
                    self._refresh_portfolio_view(refresh_history=False)
                    self._refresh_watchlist_view()
                    self._refresh_market_views()
                elif kind == "status" and sym == self.symbol.get().strip().upper():
                    self.status_var.set(str(payload))
        except queue.Empty:
            pass
        self.after(300, self._consume_ticks)

    def _current_price(self, symbol: str) -> float:
        p = self.price_cache.get(symbol.upper())
        if p is None:
            raise ValueError("暂无最新价格，请稍后再试或点击刷新价格")
        if not math.isfinite(p) or p <= 0:
            raise ValueError("价格数据异常，请刷新后再试")
        return p

    def _parse_shares(self) -> float:
        try:
            shares = float(self.shares_entry.get().strip())
        except ValueError as exc:
            raise ValueError("数量必须是数字") from exc
        if not math.isfinite(shares) or shares <= 0:
            raise ValueError("数量必须大于 0")
        return shares

    def _buy(self):
        try:
            symbol = self.symbol.get().strip().upper()
            shares = self._parse_shares()
            price = self._current_price(symbol)
            self.portfolio.buy(symbol, shares, price)
            self._refresh_portfolio_view()
            self._record_history(f"买入 {symbol} x {shares:.2f} @ {price:.2f}")
        except Exception as e:
            messagebox.showwarning("交易限制", str(e))

    def _sell(self):
        try:
            symbol = self.symbol.get().strip().upper()
            shares = self._parse_shares()
            price = self._current_price(symbol)
            self.portfolio.sell(symbol, shares, price)
            self._refresh_portfolio_view()
            self._record_history(f"卖出 {symbol} x {shares:.2f} @ {price:.2f}")
        except Exception as e:
            messagebox.showwarning("交易限制", str(e))

    def _refresh_portfolio_view(self, refresh_history=True):
        self.cash_var.set(f"{self.portfolio.cash:.2f}")
        equity = self.portfolio.market_value(self.price_cache)
        self.equity_var.set(f"{equity:.2f}")
        for i in self.holding_view.get_children():
            self.holding_view.delete(i)
        for pos in self.portfolio.positions.values():
            latest = self.price_cache.get(pos.symbol)
            if pos.asset_type == "期货" and pos.symbol in FUTURES_CONTRACTS:
                spec = FUTURES_CONTRACTS[pos.symbol]
                margin = abs(pos.shares) * pos.avg_price * spec["multiplier"] * spec["margin_rate"]
                pnl = None if latest is None else (latest - pos.avg_price) * pos.shares * spec["multiplier"]
                market_value = margin
            else:
                market_value = pos.shares * latest if latest else None
                pnl = (latest - pos.avg_price) * pos.shares if latest else None
            self.holding_view.insert("", "end", values=(
                pos.asset_type, pos.symbol, f"{pos.shares:.2f}", f"{pos.avg_price:.2f}",
                "--" if latest is None else f"{latest:.2f}",
                "--" if market_value is None else f"{market_value:.2f}",
                "--" if pnl is None else f"{pnl:+.2f}",
            ))
        if refresh_history:
            self._refresh_history()

    def _search_symbol(self):
        query = self.search_var.get().strip().upper()
        if not query:
            messagebox.showinfo("提示", "请输入股票代码，例如 AAPL、MSFT、0700.HK")
            return
        self.search_text.delete("1.0", "end")
        self.search_text.insert("end", f"正在查询 {query}...\n")
        self.status_var.set(f"正在查询 {query}...")
        threading.Thread(target=self._search_symbol_worker, args=(query,), daemon=True).start()

    def _search_symbol_worker(self, query: str):
        try:
            ticker = yf.Ticker(query)
            hist = ticker.history(period="5d")
            info = {}
            try:
                info = ticker.fast_info or {}
            except Exception:
                info = {}
            last_price = None
            if not hist.empty:
                last_price = float(hist["Close"].iloc[-1])
                self.tick_queue.put(("price", query, last_price))
            summary = [f"代码: {query}"]
            if last_price:
                summary.append(f"最近收盘价: {last_price:.2f}")
            for key, label in [("currency", "货币"), ("market_cap", "市值"), ("exchange", "交易所"), ("timezone", "时区")]:
                val = getattr(info, key, None) if not isinstance(info, dict) else info.get(key)
                if val:
                    summary.append(f"{label}: {val}")
            if hist.empty:
                summary.append("未获取到历史价格。请确认代码格式，例如港股 0700.HK、日股 7203.T。")
            self.after(0, lambda: self._show_search_result("\n".join(summary)))
        except Exception as exc:
            self.after(0, lambda: self._show_search_result(f"查询失败: {exc}"))

    def _show_search_result(self, text: str):
        self.search_text.delete("1.0", "end")
        self.search_text.insert("end", text)
        self.status_var.set("查询完成")

    def _set_symbol_from_search(self):
        symbol = self.search_var.get().strip().upper()
        if not symbol:
            return
        self.symbol.set(symbol)
        self._extend_symbol_box(symbol)
        self._request_price_refresh(force=True)
        self.tabs.select(self.trade_tab)

    def _extend_symbol_box(self, symbol: str):
        values = list(self.symbol_box.cget("values"))
        if symbol not in values:
            values.append(symbol)
            self.symbol_box.configure(values=values)

    def _add_watchlist_from_search(self):
        symbol = self.search_var.get().strip().upper()
        self._add_symbol_to_watchlist(symbol)

    def _add_current_to_watchlist(self):
        self._add_symbol_to_watchlist(self.symbol.get().strip().upper())

    def _add_sidebar_symbol(self):
        self._add_symbol_to_watchlist(self.sidebar_add_entry.get().strip().upper())
        self.sidebar_add_entry.delete(0, "end")

    def _add_symbol_to_watchlist(self, symbol: str):
        if not symbol:
            return
        watch = self.user_data.setdefault("watchlist", [])
        if symbol not in watch:
            watch.append(symbol)
            watch.sort()
            self._save_user_state(silent=True)
            self._refresh_watchlist_view()
            self._extend_symbol_box(symbol)
            self.status_var.set(f"已加入自选: {symbol}")
        self._request_price_refresh(force=True)

    def _format_quote(self, symbol: str):
        quote = self.quote_cache.get(symbol, {})
        price = quote.get("price", self.price_cache.get(symbol))
        change = quote.get("change")
        pct = quote.get("pct")
        price_text = "--" if price is None else f"{float(price):.2f}"
        chg_text = "--" if change is None or pct is None else f"{float(change):+.2f} {float(pct):+.2f}%"
        tag = "up" if (change or 0) >= 0 else "down"
        return price_text, chg_text, tag

    def _refresh_market_views(self):
        if hasattr(self, "index_view"):
            for i in self.index_view.get_children():
                self.index_view.delete(i)
            for name, symbol in MARKET_INDICES.items():
                price_text, chg_text, tag = self._format_quote(symbol)
                self.index_view.insert("", "end", values=(name, price_text, chg_text), tags=(tag,))
                if hasattr(self, "index_labels") and symbol in self.index_labels:
                    _, val = self.index_labels[symbol]
                    val.configure(text=f"{price_text}  {chg_text}", fg=UP_COLOR if tag == "up" else DOWN_COLOR)

    def _selected_watch_symbol(self) -> Optional[str]:
        sel = self.watch_view.selection()
        if not sel:
            return None
        return str(self.watch_view.item(sel[0], "values")[0])

    def _use_selected_watchlist(self):
        symbol = self._selected_watch_symbol()
        if symbol:
            self.search_var.set(symbol)
            self.symbol.set(symbol)
            self._extend_symbol_box(symbol)
            self._request_price_refresh(force=True)
            self.tabs.select(self.trade_tab)

    def _remove_selected_watchlist(self):
        symbol = self._selected_watch_symbol()
        if not symbol:
            messagebox.showinfo("提示", "请先选择一条自选股")
            return
        watch = self.user_data.setdefault("watchlist", [])
        if symbol in watch:
            watch.remove(symbol)
            self._save_user_state(silent=True)
            self._refresh_watchlist_view()
            self.status_var.set(f"已删除自选: {symbol}")

    def _refresh_watchlist_prices(self):
        watch = self.user_data.get("watchlist", [])
        if not watch:
            return
        self.status_var.set("正在刷新自选股价格...")
        def worker():
            for sym in watch:
                try:
                    hist = yf.Ticker(sym).history(period="1d", interval="1m")
                    if not hist.empty:
                        self.tick_queue.put(("price", sym, float(hist["Close"].iloc[-1])))
                except Exception:
                    continue
        threading.Thread(target=worker, daemon=True).start()

    def _use_sidebar_watchlist(self):
        sel = self.sidebar_watch_view.selection() if hasattr(self, "sidebar_watch_view") else []
        if not sel:
            return
        symbol = str(self.sidebar_watch_view.item(sel[0], "values")[0])
        self.search_var.set(symbol)
        self.symbol.set(symbol)
        self._extend_symbol_box(symbol)
        self._request_price_refresh(force=True)
        self.tabs.select(self.trade_tab)

    def _refresh_watchlist_view(self):
        watch = self.user_data.get("watchlist", [])
        if hasattr(self, "watch_view"):
            for i in self.watch_view.get_children():
                self.watch_view.delete(i)
            for sym in watch:
                price_text, chg_text, tag = self._format_quote(sym)
                held = "持仓" if sym in self.portfolio.positions else chg_text
                self.watch_view.insert("", "end", values=(sym, price_text, held), tags=(tag,))
        if hasattr(self, "sidebar_watch_view"):
            for i in self.sidebar_watch_view.get_children():
                self.sidebar_watch_view.delete(i)
            for sym in watch:
                price_text, chg_text, tag = self._format_quote(sym)
                self.sidebar_watch_view.insert("", "end", values=(sym, price_text, chg_text), tags=(tag,))


    def _draw_kline_from_search(self):
        symbol = (self.search_var.get().strip().upper() or self.symbol.get().strip().upper())
        if not symbol:
            messagebox.showinfo("提示", "请输入股票代码")
            return
        self.search_var.set(symbol)
        self.status_var.set(f"正在绘制 {symbol} K线...")
        threading.Thread(target=self._draw_kline_worker, args=(symbol,), daemon=True).start()

    def _draw_kline_worker(self, symbol: str):
        try:
            hist = yf.Ticker(symbol).history(period="3mo", interval="1d")
            if hist.empty:
                raise ValueError("未获取到K线数据")
            rows = []
            for _, row in hist.tail(60).iterrows():
                rows.append((float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])))
            self.after(0, lambda: self._draw_candles(self.kline_canvas, rows, f"{symbol} 最近60日K线"))
            self.after(0, lambda: self.status_var.set("K线绘制完成"))
        except Exception as exc:
            self.after(0, lambda: self.status_var.set(f"K线绘制失败: {exc}"))

    def _draw_candles(self, canvas: tk.Canvas, rows: List[Tuple[float, float, float, float]], title: str):
        canvas.delete("all")
        width = max(canvas.winfo_width(), 520)
        height = max(canvas.winfo_height(), 240)
        pad = 35
        canvas.create_text(width / 2, 18, text=title, font=("Microsoft YaHei UI", 11, "bold"))
        if not rows:
            canvas.create_text(width / 2, height / 2, text="暂无K线数据", fill="#777")
            return
        hi = max(r[1] for r in rows)
        lo = min(r[2] for r in rows)
        if math.isclose(hi, lo):
            hi *= 1.01
            lo *= 0.99
        def y(v):
            return height - pad - (v - lo) * (height - 2 * pad) / (hi - lo)
        step = (width - 2 * pad) / max(len(rows), 1)
        body_w = max(3, step * 0.58)
        canvas.create_line(pad, height - pad, width - pad, height - pad, fill="#ddd")
        canvas.create_line(pad, pad, pad, height - pad, fill="#ddd")
        for i, (op, high, low, close) in enumerate(rows):
            x = pad + i * step + step / 2
            color = "#d64545" if close >= op else "#18864b"
            canvas.create_line(x, y(high), x, y(low), fill=color)
            top, bottom = y(max(op, close)), y(min(op, close))
            if abs(top - bottom) < 1:
                bottom = top + 1
            canvas.create_rectangle(x - body_w / 2, top, x + body_w / 2, bottom, outline=color, fill=color)
        canvas.create_text(pad + 4, pad, text=f"高 {hi:.2f}", anchor="w", fill="#555")
        canvas.create_text(pad + 4, height - pad - 14, text=f"低 {lo:.2f}", anchor="w", fill="#555")

    def _snapshot_equity(self, force=False, save=True):
        now = time.time()
        if not force and now - self._last_equity_snapshot_at < 60:
            return
        self._last_equity_snapshot_at = now
        equity = self.portfolio.market_value(self.price_cache)
        row = {"ts": datetime.now().isoformat(timespec="seconds"), "equity": round(equity, 4), "cash": round(self.portfolio.cash, 4)}
        history = self.user_data.setdefault("equity_history", [])
        if not history or history[-1].get("equity") != row["equity"] or force:
            history.append(row)
            if len(history) > 1000:
                del history[:-1000]
        self.equity_var.set(f"{equity:.2f}")
        self._draw_equity_curve()
        self._refresh_home_summary()
        if save:
            self._save_user_state(silent=True)

    def _draw_equity_curve(self):
        if not hasattr(self, "equity_canvas"):
            return
        rows = self.user_data.get("equity_history", [])
        self._draw_line_chart(self.equity_canvas, [float(r.get("equity", 0)) for r in rows], "总资产收益曲线", empty_text="暂无收益曲线数据，点击“记录当前总资产”或进行交易后生成。")

    def _draw_line_chart(self, canvas: tk.Canvas, values: List[float], title: str, empty_text: str = "暂无数据"):
        canvas.delete("all")
        width = max(canvas.winfo_width(), 600)
        height = max(canvas.winfo_height(), 260)
        pad = 45
        canvas.create_text(width / 2, 22, text=title, font=("Microsoft YaHei UI", 12, "bold"))
        if len(values) < 2:
            canvas.create_text(width / 2, height / 2, text=empty_text, fill="#777")
            return
        vals = [v for v in values if math.isfinite(v)]
        if len(vals) < 2:
            canvas.create_text(width / 2, height / 2, text=empty_text, fill="#777")
            return
        lo, hi = min(vals), max(vals)
        if math.isclose(lo, hi):
            lo *= 0.99
            hi *= 1.01
        canvas.create_line(pad, height - pad, width - pad, height - pad, fill="#aaa")
        canvas.create_line(pad, pad, pad, height - pad, fill="#aaa")
        points = []
        for idx, val in enumerate(vals):
            x = pad + idx * (width - 2 * pad) / (len(vals) - 1)
            y = height - pad - (val - lo) * (height - 2 * pad) / (hi - lo)
            points.extend([x, y])
        canvas.create_line(*points, fill="#2f80ed", width=2, smooth=True)
        canvas.create_text(pad + 4, pad, text=f"高 {hi:.2f}", anchor="w", fill="#555")
        canvas.create_text(pad + 4, height - pad - 16, text=f"低 {lo:.2f}", anchor="w", fill="#555")
        canvas.create_text(width - pad, height - pad + 20, text=f"最新 {vals[-1]:.2f}", anchor="e", fill="#333")

    def _export_history_csv(self):
        path = filedialog.asksaveasfilename(title="导出交易记录", defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile=f"{self.username}_trades.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["record"])
            for row in self.user_data.get("history", []):
                writer.writerow([row])
        messagebox.showinfo("导出完成", f"已导出到:\n{path}")

    def _export_equity_csv(self):
        path = filedialog.asksaveasfilename(title="导出收益曲线", defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile=f"{self.username}_equity.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["ts", "equity", "cash"])
            writer.writeheader()
            writer.writerows(self.user_data.get("equity_history", []))
        messagebox.showinfo("导出完成", f"已导出到:\n{path}")

    def _run_backtest_async(self):
        symbol = self.backtest_symbol_var.get().strip().upper()
        period = self.backtest_period_var.get().strip()
        fast = int(self.backtest_fast_var.get())
        slow = int(self.backtest_slow_var.get())
        if not symbol:
            messagebox.showinfo("提示", "请输入回测代码")
            return
        if fast >= slow:
            messagebox.showwarning("参数错误", "快均线必须小于慢均线")
            return
        self._save_strategy_params()
        self.backtest_text.delete("1.0", "end")
        self.backtest_text.insert("end", f"正在回测 {symbol}，周期 {period}，均线 {fast}/{slow}...\n")
        self.status_var.set("正在运行回测...")
        threading.Thread(target=self._run_backtest_worker, args=(symbol, period, fast, slow), daemon=True).start()

    def _run_backtest_worker(self, symbol: str, period: str, fast: int, slow: int):
        try:
            hist = yf.Ticker(symbol).history(period=period, interval="1d")
            if hist.empty or len(hist) < slow + 5:
                raise ValueError("历史数据不足，无法回测")
            closes = [float(x) for x in hist["Close"].tolist()]
            dates = [str(idx.date()) for idx in hist.index]
            cash = INITIAL_CASH
            shares = 0.0
            equity_curve = []
            trades: List[Tuple[str, str, float]] = []
            for i, price in enumerate(closes):
                if i < slow:
                    equity_curve.append(cash + shares * price)
                    continue
                fast_ma = sum(closes[i-fast+1:i+1]) / fast
                slow_ma = sum(closes[i-slow+1:i+1]) / slow
                prev_fast = sum(closes[i-fast:i]) / fast
                prev_slow = sum(closes[i-slow:i]) / slow
                if prev_fast <= prev_slow and fast_ma > slow_ma and cash > 0:
                    shares = cash / price
                    cash = 0.0
                    trades.append((dates[i], "BUY", price))
                elif prev_fast >= prev_slow and fast_ma < slow_ma and shares > 0:
                    cash = shares * price
                    shares = 0.0
                    trades.append((dates[i], "SELL", price))
                equity_curve.append(cash + shares * price)
            final_equity = equity_curve[-1]
            buy_hold = INITIAL_CASH / closes[slow] * closes[-1]
            ret = (final_equity / INITIAL_CASH - 1) * 100
            bh_ret = (buy_hold / INITIAL_CASH - 1) * 100
            result = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "symbol": symbol,
                "period": period,
                "fast": fast,
                "slow": slow,
                "final_equity": round(final_equity, 2),
                "return_pct": round(ret, 2),
                "buy_hold_return_pct": round(bh_ret, 2),
                "trades": trades,
                "equity_curve": [round(v, 2) for v in equity_curve],
            }
            self.user_data.setdefault("backtests", []).append(result)
            self.after(0, lambda: self._show_backtest_result(result))
        except Exception as exc:
            self.after(0, lambda: self._show_backtest_error(str(exc)))

    def _show_backtest_result(self, result: dict):
        trades = result["trades"]
        text = (
            f"代码: {result['symbol']}\n周期: {result['period']}\n策略: {result['fast']}日 / {result['slow']}日均线金叉买入、死叉卖出\n"
            f"最终资产: {result['final_equity']:.2f}\n策略收益: {result['return_pct']:+.2f}%\n买入持有收益: {result['buy_hold_return_pct']:+.2f}%\n交易次数: {len(trades)}\n"
        )
        if trades:
            text += "\n最近交易:\n" + "\n".join([f"{d} {side} @ {price:.2f}" for d, side, price in trades[-8:]])
        self.backtest_text.delete("1.0", "end")
        self.backtest_text.insert("end", text)
        self._draw_line_chart(self.backtest_canvas, result["equity_curve"], "回测资产曲线")
        self._save_user_state(silent=True)
        self._refresh_home_summary()
        self.status_var.set("回测完成")

    def _show_backtest_error(self, text: str):
        self.backtest_text.delete("1.0", "end")
        self.backtest_text.insert("end", f"回测失败: {text}")
        self.status_var.set("回测失败")

    def _export_backtest_csv(self):
        tests = self.user_data.get("backtests", [])
        if not tests:
            messagebox.showinfo("提示", "暂无回测结果")
            return
        latest = tests[-1]
        path = filedialog.asksaveasfilename(title="导出最近回测", defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile=f"{latest['symbol']}_backtest.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["index", "equity"])
            for i, val in enumerate(latest.get("equity_curve", [])):
                writer.writerow([i, val])
            writer.writerow([])
            writer.writerow(["date", "side", "price"])
            for row in latest.get("trades", []):
                writer.writerow(row)
        messagebox.showinfo("导出完成", f"已导出到:\n{path}")


    def _export_backtest_report(self):
        tests = self.user_data.get("backtests", [])
        if not tests:
            messagebox.showinfo("提示", "暂无回测结果")
            return
        latest = tests[-1]
        path = filedialog.asksaveasfilename(
            title="导出回测报告",
            defaultextension=".html",
            filetypes=[("HTML", "*.html"), ("Text", "*.txt")],
            initialfile=f"{latest['symbol']}_backtest_report.html",
        )
        if not path:
            return
        trades = latest.get("trades", [])
        trade_rows = "".join(f"<tr><td>{d}</td><td>{side}</td><td>{price:.2f}</td></tr>" for d, side, price in trades)
        html = f"""<!doctype html><html><head><meta charset='utf-8'><title>回测报告</title>
<style>body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:32px}}table{{border-collapse:collapse}}td,th{{border:1px solid #ddd;padding:6px 10px}}</style></head><body>
<h1>回测报告 - {latest['symbol']}</h1>
<p>生成时间：{datetime.now().isoformat(timespec='seconds')}</p>
<ul>
<li>周期：{latest['period']}</li><li>策略：{latest['fast']} / {latest['slow']} 日均线交叉</li>
<li>最终资产：{latest['final_equity']:.2f}</li><li>策略收益：{latest['return_pct']:+.2f}%</li>
<li>买入持有收益：{latest['buy_hold_return_pct']:+.2f}%</li><li>交易次数：{len(trades)}</li>
</ul><h2>交易记录</h2><table><tr><th>日期</th><th>方向</th><th>价格</th></tr>{trade_rows}</table>
<p><em>仅用于虚拟训练，不构成投资建议。</em></p></body></html>"""
        Path(path).write_text(html, encoding="utf-8")
        messagebox.showinfo("导出完成", f"已导出到:\n{path}")

    def _ask_ai_subscription(self):
        snapshot = self.portfolio.to_json()
        symbol = self.symbol.get().strip().upper()
        price = self.price_cache.get(symbol, None)
        training_prompt = (
            "你是投资教练。这是虚拟交易训练，不构成真实投资建议。"
            "请基于我的虚拟账户快照，输出："
            "1) 风险提示 2) 仓位建议 3) 今日复盘任务 4) 下一步练习计划。"
            f"账户={json.dumps(snapshot, ensure_ascii=False)}；关注股票={symbol}；当前价={price}；"
            f"自选股={self.user_data.get('watchlist', [])}"
        )
        self.ai_text.set("已生成训练提示词（会员订阅模式）。\n将自动打开 ChatGPT 网页，请确认提示词后继续训练；本程序不调用 API。")
        webbrowser.open(f"https://chatgpt.com/?q={urllib.parse.quote(training_prompt)}")
        self._record_history(f"发起AI训练: {symbol} @ {price}")


if __name__ == "__main__":
    app = App()
    app.mainloop()
