#!/usr/bin/env python3
"""本地化离线翻译引擎（集成语言包版）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

try:
    from argostranslate import package, translate
except ImportError as exc:
    raise SystemExit("未安装 argostranslate。请先执行: pip install -r requirements.txt") from exc

MODEL_DIR = Path.home() / ".local_offline_translator" / "models"


def list_installed_language_codes() -> List[str]:
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


def _pkg_file(pkg: package.Package) -> Path:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    return MODEL_DIR / f"{pkg.from_code}_{pkg.to_code}.argosmodel"


def _install_pkg(pkg: package.Package) -> bool:
    target = _pkg_file(pkg)
    if not target.exists():
        downloaded = Path(pkg.download())
        downloaded.replace(target)
    package.install_from_path(str(target))
    return True


def install_language_pair(from_code: str, to_code: str) -> bool:
    if _find_translation(from_code, to_code) is not None:
        return True
    for pkg in _iter_downloadable_packages():
        if pkg.from_code == from_code and pkg.to_code == to_code:
            return _install_pkg(pkg)
    return False


def install_direct_packages(target_languages: Sequence[str], pivot_language: str = "en") -> int:
    """将语言包直接下载并整合到引擎。

    默认以英文做中转，安装：
    - 目标语言 -> 英文
    - 英文 -> 目标语言
    """
    targets = set(target_languages)
    if pivot_language in targets:
        targets.remove(pivot_language)

    installed_count = 0
    for pkg in _iter_downloadable_packages():
        cond1 = pkg.from_code == pivot_language and pkg.to_code in targets
        cond2 = pkg.to_code == pivot_language and pkg.from_code in targets
        if cond1 or cond2:
            if _find_translation(pkg.from_code, pkg.to_code) is None:
                _install_pkg(pkg)
                installed_count += 1
    return installed_count


def detect_source_language(text: str) -> Optional[str]:
    candidates: List[Tuple[str, float]] = []
    for lang in translate.get_installed_languages():
        try:
            candidates.append((lang.code, lang.get_confidence(text)))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0] if candidates[0][1] > 0 else None


def translate_text(text: str, to_code: str, from_code: Optional[str] = None) -> str:
    from_code = from_code or detect_source_language(text)
    if not from_code:
        raise ValueError("无法自动识别源语言，请显式指定 --from")
    tr = _find_translation(from_code, to_code)
    if tr is None:
        raise ValueError(f"未找到翻译模型: {from_code}->{to_code}。请先运行 sync-models 或 install-pair。")
    return tr.translate(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线本地化翻译引擎")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-languages", help="列出已安装语言代码")

    p_install = sub.add_parser("install-pair", help="安装单个语言对模型")
    p_install.add_argument("--from", dest="from_code", required=True)
    p_install.add_argument("--to", dest="to_code", required=True)

    p_sync = sub.add_parser("sync-models", help="直接下载并整合语言包")
    p_sync.add_argument("--langs", nargs="+", required=True, help="目标语言代码列表，如 zh ja fr de")
    p_sync.add_argument("--pivot", default="en", help="中转语言，默认 en")

    p_translate = sub.add_parser("translate", help="翻译文本")
    p_translate.add_argument("--text", required=True)
    p_translate.add_argument("--to", dest="to_code", required=True)
    p_translate.add_argument("--from", dest="from_code")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "list-languages":
        for code in list_installed_language_codes():
            print(code)
        return 0
    if args.cmd == "install-pair":
        ok = install_language_pair(args.from_code, args.to_code)
        print(f"已安装或已存在: {args.from_code}->{args.to_code}" if ok else f"未找到可用模型: {args.from_code}->{args.to_code}")
        return 0 if ok else 2
    if args.cmd == "sync-models":
        count = install_direct_packages(args.langs, args.pivot)
        print(f"已下载并整合模型数量: {count}")
        return 0
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
