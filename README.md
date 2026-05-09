# codex-

测试用：本地化离线翻译引擎（支持语言包直接下载整合）。

## 功能目标
- 覆盖大部分语言（按需批量下载语言包）。
- 支持完全离线翻译（模型安装后无需联网）。
- 支持自动识别源语言。

## 安装
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 直接下载整合语言包
> 首次下载模型需要联网，下载后翻译过程完全离线。

示例：一键整合中英日法德西与英文互译模型。
```bash
python offline_translate.py sync-models --langs zh ja fr de es --pivot en
```

## 使用翻译
```bash
python offline_translate.py translate --text "Hello world" --to zh
python offline_translate.py translate --text "Bonjour" --from fr --to en
```

## 其他命令
```bash
python offline_translate.py list-languages
python offline_translate.py install-pair --from en --to zh
```
