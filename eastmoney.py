"""A-share data — mixed public endpoints (no token required).

Sources:
  - 涨停板池  https://push2ex.eastmoney.com/getTopicZTPool      (East Money — works globally)
  - 批量行情  https://qt.gtimg.cn/q=                              (Tencent Finance)
  - 日K线     https://web.ifzq.gtimg.cn/appstock/app/fqkline/get (Tencent Finance)

push2.eastmoney.com and push2his.eastmoney.com are blocked outside mainland China;
Tencent Finance endpoints are not.
"""
from __future__ import annotations
import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any
import httpx

from common import EM_HEADERS, market_of, secid, fmt_seal_time


# ── retry helper ─────────────────────────────────────────────────────────────

async def _get_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """GET with up to 3 retries on transient network errors."""
    last_exc: Exception = RuntimeError("unreachable")
    for attempt in range(3):
        try:
            r = await client.get(url, **kwargs)
            r.raise_for_status()
            return r
        except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadTimeout,
                httpx.ConnectTimeout) as exc:
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
    raise last_exc


# ── constants ─────────────────────────────────────────────────────────────────

ZT_POOL_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
TC_QUOTE_BASE = "https://qt.gtimg.cn/q="
TC_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

TC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://finance.qq.com/",
    "Accept": "*/*",
}


# ── Tencent secid helpers ─────────────────────────────────────────────────────

def _tc_secid(code: str) -> str:
    """Tencent secid: sh600519 / sz000001."""
    pfx = "sh" if market_of(code) == 1 else "sz"
    return f"{pfx}{str(code).zfill(6)}"


def _tc_date(d: str) -> str:
    """YYYYMMDD → YYYY-MM-DD."""
    d = d.replace("-", "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def _ts() -> str:
    return str(int(time.time() * 1000))


# ── 涨停板池 (East Money — unchanged) ─────────────────────────────────────────

async def fetch_zt_pool(client: httpx.AsyncClient, date: str) -> list[dict[str, Any]]:
    """Return the limit-up (涨停) pool for a trading day (date='YYYYMMDD')."""
    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": "0",
        "pagesize": "600",
        "sort": "fbt:asc",
        "date": date,
        "_": _ts(),
    }
    r = await _get_with_retry(client, ZT_POOL_URL, params=params, headers=EM_HEADERS, timeout=20)
    data = (r.json() or {}).get("data") or {}
    return data.get("pool") or []


def parse_zt_item(it: dict[str, Any]) -> dict[str, Any]:
    """Normalise one east-money 涨停池 item into a clean dict."""
    zttj = it.get("zttj") or {}
    try:
        lian_ban_days = int(zttj.get("days") or 0)
    except (TypeError, ValueError):
        lian_ban_days = 0
    return {
        "code": str(it.get("c", "")).zfill(6),
        "name": it.get("n", ""),
        "lian_ban_days": lian_ban_days,
        "industry": it.get("hybk", ""),
        "first_seal_time": fmt_seal_time(it.get("fbt")),
        "last_seal_time": fmt_seal_time(it.get("lbt")),
        "break_count": it.get("zbc"),
        "amount": it.get("amount"),
        "turnover_rate": it.get("hs"),
        "float_mktcap": it.get("ltsz"),
        "seal_fund": it.get("fund"),
        "change_pct": it.get("zdp"),
    }


# ── 批量行情 (Tencent) ─────────────────────────────────────────────────────────

def _parse_tc_quote_line(line: str) -> tuple[str, dict] | None:
    """Parse one line of Tencent qt.gtimg.cn response into (code, fields_dict)."""
    line = line.strip()
    if "=" not in line:
        return None
    val = line.split("=", 1)[1].strip().strip('";')
    fields = val.split("~")
    if len(fields) < 40:
        return None
    code = str(fields[2]).zfill(6)

    # amount in 元: field[35] is "price/vol/amount_yuan"
    amount_yuan = None
    try:
        parts = fields[35].split("/")
        if len(parts) >= 3:
            amount_yuan = float(parts[2])
    except (ValueError, IndexError):
        pass

    def fv(idx: int):
        try:
            v = fields[idx]
            return v if v not in ("", "-") else None
        except IndexError:
            return None

    def fv_scaled(idx: int, scale: float):
        """Return field value multiplied by scale (for unit conversion)."""
        v = fv(idx)
        try:
            return str(float(v) * scale) if v is not None else None
        except (TypeError, ValueError):
            return None

    return code, {
        "f2":  fv(3),                          # 最新价
        "f3":  fv(32),                         # 涨跌幅(%)
        "f6":  str(amount_yuan) if amount_yuan is not None else None,  # 成交额(元)
        "f8":  fv(38),                         # 换手率(%)
        "f12": code,                           # 代码
        "f14": fv(1),                          # 名称
        "f15": fv(33),                         # 最高
        "f16": fv(34),                         # 最低
        "f17": fv(5),                          # 今开
        "f18": fv(4),                          # 昨收
        "f20": fv_scaled(45, 1e8),             # 总市值(元) — Tencent [45] is 亿
        "f21": fv_scaled(44, 1e8),             # 流通市值(元) — Tencent [44] is 亿
        # f26 (上市日期) not available from Tencent — new-stock filter skipped
    }


async def fetch_quotes(client: httpx.AsyncClient, codes: list[str]) -> dict[str, dict]:
    """Real-time snapshot keyed by 6-digit code. Uses Tencent qt.gtimg.cn."""
    out: dict[str, dict] = {}
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        q = ",".join(_tc_secid(c) for c in chunk)
        r = await _get_with_retry(client, TC_QUOTE_BASE + q,
                                  headers=TC_HEADERS, timeout=20)
        for line in r.text.splitlines():
            parsed = _parse_tc_quote_line(line)
            if parsed:
                code, d = parsed
                out[code] = d
    return out


# ── 日K线 (Tencent) ───────────────────────────────────────────────────────────

async def _tc_kline_raw(client: httpx.AsyncClient, tc_code: str,
                        fetch_beg: str, fetch_end: str,
                        adj: str = "qfq", lmt: int = 300) -> list[list]:
    """Fetch Tencent kline. Returns raw list of [date, open, close, high, low, vol]."""
    param = f"{tc_code},day,{fetch_beg},{fetch_end},{lmt},{adj}" if adj else \
            f"{tc_code},day,{fetch_beg},{fetch_end},{lmt}"
    r = await _get_with_retry(client, TC_KLINE_URL,
                              params={"_var": "kline_dayqfq", "param": param,
                                      "r": str(time.time())},
                              headers=TC_HEADERS, timeout=20)
    txt = r.text
    if "=" in txt:
        txt = txt.split("=", 1)[1].strip()
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        return []
    stock = (data.get("data") or {}).get(tc_code) or {}
    # adjusted data may be under qfqday/hfqday; unadjusted under day
    return stock.get("qfqday") or stock.get("hfqday") or stock.get("day") or []


async def fetch_kline(client: httpx.AsyncClient, code: str, beg: str, end: str,
                      fqt: int = 1) -> list[dict]:
    """Daily candles between beg/end (YYYYMMDD). fqt: 0 none, 1 前复权, 2 后复权.

    Returns rows: {date, open, close, high, low, amount, change_pct, turnover_rate}
    """
    tc_code = _tc_secid(code)
    adj = {1: "qfq", 2: "hfq"}.get(fqt, "")
    beg_ymd = beg.replace("-", "")
    end_ymd = end.replace("-", "")
    # fetch one extra week before beg to get the prev-close for change_pct of first bar
    fetch_beg_dt = datetime.strptime(beg_ymd, "%Y%m%d") - timedelta(days=10)
    fetch_beg = fetch_beg_dt.strftime("%Y-%m-%d")
    fetch_end = _tc_date(end_ymd)

    raw = await _tc_kline_raw(client, tc_code, fetch_beg, fetch_end, adj)

    rows = []
    prev_close: float | None = None
    for item in raw:
        try:
            d = str(item[0]).replace("-", "")
            o = float(item[1])
            c = float(item[2])
            h = float(item[3])
            lo = float(item[4])
        except (IndexError, ValueError, TypeError):
            continue
        chg = round((c - prev_close) / prev_close * 100, 2) if prev_close else None
        prev_close = c
        if beg_ymd <= d <= end_ymd:
            rows.append({
                "date": d,
                "open": o, "close": c,
                "high": h, "low": lo,
                "amount": None,
                "change_pct": chg,
                "turnover_rate": None,
            })
    return rows


# ── 交易日历 (Tencent, sh000001) ──────────────────────────────────────────────

async def trading_days(client: httpx.AsyncClient, end: str, count: int) -> list[str]:
    """Last `count` trading days (ascending) up to and including `end` if it trades.

    Derived from Shanghai Composite index (sh000001) via Tencent Finance.
    """
    end_ymd = end.replace("-", "")
    fetch_end = _tc_date(end_ymd)
    # sh000001 has no qfq adjustment — use empty adj or try both
    raw = await _tc_kline_raw(client, "sh000001", "2020-01-01", fetch_end, adj="", lmt=2000)
    if not raw:
        # fallback: try with qfq (some Tencent endpoints accept it for indexes)
        raw = await _tc_kline_raw(client, "sh000001", "2020-01-01", fetch_end, adj="qfq", lmt=2000)
    dates = [str(item[0]).replace("-", "") for item in raw
             if str(item[0]).replace("-", "") <= end_ymd]
    return dates[-count:] if count <= len(dates) else dates
