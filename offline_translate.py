#!/usr/bin/env python3
"""
本地化离线翻译引擎（示例）

特性：
- 完全离线运行（基于 Argos Translate 本地模型）
- 支持多语言自动识别（需安装对应模型）
- 提供命令行与交互模式
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable, List, Optional, Tuple

try:
    from argostranslate import package, translate
except ImportError as exc:
    raise SystemExit(
        "未安装 argostranslate。请先执行: pip install argostranslate"
    ) from exc


def list_installed_language_codes() -> List[str]:
    """列出已安装语言代码。"""
    return sorted({lang.code for lang in translate.get_installed_languages()})


def _find_language(lang_code: str):
    for lang in translate.get_installed_languages():
        if lang.code == lang_code:
            return lang
    return None


def _find_translation(from_code: str, to_code: str):
    from_lang = _find_language(from_code)
    if from_lang is None:
        return None
    for tr in from_lang.translations_from:
        if tr.to_lang.code == to_code:
            return tr
    return None


def _iter_downloadable_packages() -> Iterable[package.Package]:
    package.update_package_index()
    return package.get_available_packages()


def install_language_pair(from_code: str, to_code: str) -> bool:
    """安装指定语言对模型，成功返回 True。"""
    installed = _find_translation(from_code, to_code)
    if installed is not None:
        return True

    for pkg in _iter_downloadable_packages():
        if pkg.from_code == from_code and pkg.to_code == to_code:
            path = pkg.download()
            package.install_from_path(path)
            return True
    return False


def detect_source_language(text: str) -> Optional[str]:
    """尝试识别输入文本语言。"""
    candidates: List[Tuple[str, float]] = []
    for lang in translate.get_installed_languages():
        try:
            confidence = lang.get_confidence(text)
            candidates.append((lang.code, confidence))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    best = candidates[0]
    return best[0] if best[1] > 0 else None


def translate_text(text: str, to_code: str, from_code: Optional[str] = None) -> str:
    """翻译文本，支持自动检测源语言。"""
    from_code = from_code or detect_source_language(text)
    if not from_code:
        raise ValueError("无法自动识别源语言，请显式指定 --from")

    tr = _find_translation(from_code, to_code)
    if tr is None:
        raise ValueError(
            f"未找到翻译模型: {from_code}->{to_code}。"
            "请先运行 install-pair 子命令安装模型。"
        )
    return tr.translate(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线本地化翻译引擎")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-languages", help="列出已安装语言代码")

    p_install = sub.add_parser("install-pair", help="安装语言对模型")
    p_install.add_argument("--from", dest="from_code", required=True, help="源语言代码，如 en")
    p_install.add_argument("--to", dest="to_code", required=True, help="目标语言代码，如 zh")

    p_translate = sub.add_parser("translate", help="翻译文本")
    p_translate.add_argument("--text", required=True, help="待翻译文本")
    p_translate.add_argument("--to", dest="to_code", required=True, help="目标语言代码")
    p_translate.add_argument("--from", dest="from_code", help="源语言代码（可选）")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "list-languages":
        for code in list_installed_language_codes():
            print(code)
        return 0

    if args.cmd == "install-pair":
        ok = install_language_pair(args.from_code, args.to_code)
        if ok:
            print(f"已安装或已存在: {args.from_code}->{args.to_code}")
            return 0
        print(f"未找到可用模型: {args.from_code}->{args.to_code}")
        return 2

    if args.cmd == "translate":
        try:
            print(translate_text(args.text, args.to_code, args.from_code))
            return 0
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
