# 虚拟股市训练器（Windows）

这是一个可在 Windows 运行的 Python 桌面程序：
- 实时拉取真实市场行情（默认美股、A股/港股、日股、欧股示例）
- 支持账户创建与登录（每个账户独立组合）
- 模拟买入/卖出与持仓管理（练习盘）
- 自动记录并持久化操作历史，可随时在软件内查看
- 通过 **OpenAI 会员订阅模式** 接入 AI 训练（不走 API 按量计费）
- 内置投资限制（单笔上限 + 总投资上限），避免练习结果偏离现实

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

## 3. 会员订阅模式 AI 训练

点击按钮“会员AI训练”后：
1. 程序自动生成训练提示词
2. 自动打开 ChatGPT 网页（`chatgpt.com`）
3. 使用你的会员账号继续训练

> 该方式不调用 API，不需要 `OPENAI_API_KEY`，可减少实际 token 消耗。

## 4. 风险与现实约束

程序默认限制：
- 单笔交易上限：`200,000`
- 总投资上限：`1,000,000`

你可以在 `app.py` 中修改：
- `MAX_SINGLE_TRADE`
- `MAX_TOTAL_EXPOSURE`

## 5. 打包为 Windows 可执行文件（可选）

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed app.py
```

生成的 exe 在 `dist/app.exe`。

## 6. 说明

- 数据源使用 `yfinance`，不同市场代码格式不同（例如 `7203.T`、`0700.HK`）。
- 默认每 5 秒尝试刷新一次当前关注标的价格。
- 这是训练软件，不构成投资建议。


## 7. 账户与历史记录

- 首次启动可直接创建账户，随后登录。
- 每次买入、卖出、发起 AI 训练都会写入历史记录。
- 用户数据保存在 `users.json`（包含密码、组合、历史记录）。
