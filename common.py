"""Shared helpers: code/market detection, filters, formatting."""
from __future__ import annotations
from datetime import datetime

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}

THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://data.10jqka.com.cn/market/zdf排行/",
    "Accept": "application/json, text/plain, */*",
}


def market_of(code: str) -> int:
    """East-money secid market prefix: 1 = Shanghai, 0 = Shenzhen."""
    c = str(code).zfill(6)
    if c.startswith(("60", "68", "50", "51", "56", "58", "11", "90", "20")):
        return 1
    return 0


def secid(code: str) -> str:
    return f"{market_of(code)}.{str(code).zfill(6)}"


def is_bj(code: str) -> bool:
    """北交所: codes 4x/8x/92x (plus legacy 43/83/87/88)."""
    c = str(code).zfill(6)
    return c.startswith(("4", "8", "92"))


def is_st(name: str) -> bool:
    n = (name or "").upper().replace(" ", "")
    return "ST" in n or "退" in (name or "")


def limit_factor(code: str) -> float:
    """涨停比例: 创业板/科创板 20%, 其它(非ST,非北交所) 10%."""
    c = str(code).zfill(6)
    if c.startswith(("30", "688", "689")):
        return 1.20
    return 1.10


def limit_price(prev_close: float, code: str) -> float:
    return round(prev_close * limit_factor(code), 2)


def fmt_seal_time(t) -> str:
    """East-money fbt/lbt int like 93005 -> '09:30:05'."""
    try:
        s = str(int(t)).zfill(6)
        return f"{s[:2]}:{s[2:4]}:{s[4:6]}"
    except (TypeError, ValueError):
        return ""


def ymd(d: str) -> str:
    return str(d).replace("-", "")


def days_between(a: str, b: str) -> int:
    """Calendar days between YYYYMMDD strings (b - a)."""
    da = datetime.strptime(ymd(a), "%Y%m%d")
    db = datetime.strptime(ymd(b), "%Y%m%d")
    return (db - da).days


def round2(x):
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return None
