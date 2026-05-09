# codex-

测试用：本地化离线翻译引擎。

## 功能目标
- 覆盖大部分语言（通过下载不同语言模型扩展）。
- 支持完全离线翻译（模型安装后无需联网翻译）。
- 支持自动识别源语言（基于本地已安装语言包）。

## 安装
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 使用方法
### 1) 查看本地已安装语言
```bash
python offline_translate.py list-languages
```

### 2) 安装语言模型（示例：英文 -> 中文）
> 首次安装模型需要联网下载；安装完成后翻译可离线。
```bash
python offline_translate.py install-pair --from en --to zh
```

### 3) 执行翻译
```bash
python offline_translate.py translate --text "Hello world" --to zh
```

### 4) 指定源语言翻译
```bash
python offline_translate.py translate --text "Bonjour" --from fr --to en
```

## 说明
- 本项目使用 `Argos Translate` 本地模型实现离线翻译。
- 想覆盖更多语言，只需持续安装更多语种对模型。
- 不同语种对质量不同，建议按业务场景评估模型效果。
