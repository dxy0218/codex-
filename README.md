# 虚拟股市训练器（Windows）

这是一个可在 Windows 运行的 Python 桌面程序：
- 实时拉取真实市场行情（默认美股、A股/港股、日股、欧股示例）
- 模拟买入/卖出与持仓管理（练习盘）
- 可接入 OpenAI 做投资训练复盘建议

## 1. 环境准备

1. 安装 Python 3.10+
2. 在项目目录执行：

```bash
pip install -r requirements.txt
```

## 2. 运行

```bash
python app.py
```

## 3. AI 训练功能（可选）

设置环境变量后，点击按钮“AI训练建议”：

```bash
set OPENAI_API_KEY=你的key
```

> PowerShell:

```powershell
$env:OPENAI_API_KEY="你的key"
```

## 4. 打包为 Windows 可执行文件（可选）

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed app.py
```

生成的 exe 在 `dist/app.exe`。

## 5. 说明

- 数据源使用 `yfinance`，不同市场代码格式不同（例如 `7203.T`、`0700.HK`）。
- 默认每 5 秒尝试刷新一次当前关注标的价格。
- 这是训练软件，不构成投资建议。
