"""站名规范化：全站统一英文，去掉 INTL 这类纯类型后缀。

上游 stationinfo 的 site 字段是航空写法「城市/机场 + 类型词」，实测样本：

    Shanghai/Pudong Intl       → Shanghai Pudong
    Guangzhou/Baiyun Intl      → Guangzhou Baiyun
    Beijing Intl               → Beijing
    Frankfurt/Main Arpt        → Frankfurt Main

只做两件可预期的事，不翻译、不缩写、不猜城市：

1) 斜杠换空格 —— 「城市/机场」的层级在卡片和表头里没有意义，还占宽度
2) 丢掉 INTL / INTERNATIONAL / ARPT / AIRPORT 这类纯类型词 —— 它们不含
   任何地理信息，留着只让名字变长

所以同一个站点每次清理的结果都一样，出问题能直接拿原始 site 核对。
"""

from __future__ import annotations

import re

# 纯类型后缀（比较时已去掉标点并转大写，所以 "Intl." "INT'L" 都能命中）
_TYPE_WORDS = frozenset({
    "INTL",        # 同时覆盖 "Intl." 和 "INT'L"（判断前已去标点）
    "INTERNATIONAL",
    "ARPT",
    "AIRPORT",
    "APT",
})

_PUNCT_TAIL = " -–—,;:/"


def clean_site_name(raw: str | None, fallback: str = "") -> str:
    """把上游 site 洗成看板显示名。拿不到有效内容时回落到 fallback。"""
    text = (raw or "").strip()
    if not text:
        return (fallback or "").strip() or "Unknown"

    # 斜杠是「城市/机场」的分隔，换成空格更自然，也避免看起来像路径
    text = text.replace("/", " ")

    kept: list[str] = []
    for token in text.split():
        # 去掉首尾标点后再判断，才能命中 "Intl." 这种写法
        core = token.strip(".,;:()'\"")
        key = re.sub(r"[^A-Z]", "", core.upper())
        if key and key in _TYPE_WORDS:
            continue
        if core:
            kept.append(core)

    name = re.sub(r"\s{2,}", " ", " ".join(kept)).strip(_PUNCT_TAIL)
    # 整个名字都是类型词（例如只有 "Intl"）时不能返回空串
    return name or (fallback or "").strip() or "Unknown"
