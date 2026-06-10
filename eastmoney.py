"""East Money (东方财富) free, token-less HTTP endpoints.

Endpoints used (all public, no registration):
  - 涨停板池  https://push2ex.eastmoney.com/getTopicZTPool        (per-day limit-up pool)
  - 批量行情  https://push2.eastmoney.com/api/qt/ulist.np/get      (real-time snapshot, many secids)
  - 日K线     https://push2his.eastmoney.com/api/qt/stock/kline/get (daily candles, full history)
"""
from __future__ import annotations
import asyncio
import time
from typing import Any
import httpx

from common import EM_HEADERS, secid, market_of, fmt_seal_time


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

ZT_POOL_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def _ts() -> str:
    return str(int(time.time() * 1000))


async def fetch_zt_pool(client: httpx.AsyncClient, date: str) -> list[dict[str, Any]]:
    """Return the limit-up (涨停) pool for a trading day (date='YYYYMMDD').

    Each raw item carries:
      c name n, p price, zdp 涨跌幅, amount 成交额, ltsz 流通市值, hs 换手率,
      fbt 首次封板时间, lbt 最后封板时间, zbc 炸板次数, fund 封板资金,
      hybk 行业板块, zttj={days:连板天数, ct:统计内涨停次数}
    """
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
        "lian_ban_days": lian_ban_days,              # 1 == 首板
        "industry": it.get("hybk", ""),             # 行业板块 (非概念)
        "first_seal_time": fmt_seal_time(it.get("fbt")),
        "last_seal_time": fmt_seal_time(it.get("lbt")),
        "break_count": it.get("zbc"),               # 炸板次数
        "amount": it.get("amount"),                 # 成交额 (元)
        "turnover_rate": it.get("hs"),              # 换手率 (%)
        "float_mktcap": it.get("ltsz"),             # 流通市值 (元)
        "seal_fund": it.get("fund"),                # 涨停封单金额 (元)
        "change_pct": it.get("zdp"),                # 涨跌幅 (%)
    }


async def fetch_quotes(client: httpx.AsyncClient, codes: list[str]) -> dict[str, dict]:
    """Real-time snapshot keyed by code. Chunks to stay under URL limits.

    Fields: f2 最新价, f3 涨跌幅, f6 成交额, f8 换手率, f12 代码, f14 名称,
            f15 最高, f16 最低, f17 今开, f18 昨收, f20 总市值, f21 流通市值,
            f26 上市日期(YYYYMMDD).
    """
    out: dict[str, dict] = {}
    fields = "f2,f3,f6,f8,f12,f14,f15,f16,f17,f18,f20,f21,f26"
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        secids = ",".join(secid(c) for c in chunk)
        params = {"fltt": "2", "secids": secids, "fields": fields, "_": _ts()}
        r = await _get_with_retry(client, ULIST_URL, params=params, headers=EM_HEADERS, timeout=20)
        diff = ((r.json() or {}).get("data") or {}).get("diff") or []
        # diff may be a list or a dict depending on endpoint mood; handle both
        items = diff.values() if isinstance(diff, dict) else diff
        for it in items:
            out[str(it.get("f12", "")).zfill(6)] = it
    return out


async def fetch_kline(client: httpx.AsyncClient, code: str, beg: str, end: str,
                      fqt: int = 1) -> list[dict]:
    """Daily candles between beg/end (YYYYMMDD). fqt: 0 none, 1 前复权, 2 后复权.

    Returns rows: {date, open, close, high, low, amount, change_pct, turnover_rate}
    """
    params = {
        "secid": secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": str(fqt),
        "beg": beg, "end": end, "_": _ts(),
    }
    r = await _get_with_retry(client, KLINE_URL, params=params, headers=EM_HEADERS, timeout=20)
    data = (r.json() or {}).get("data") or {}
    rows = []
    for line in data.get("klines") or []:
        p = line.split(",")
        # date,open,close,high,low,vol,amount,amplitude,pct,chg,turnover
        rows.append({
            "date": p[0].replace("-", ""),
            "open": float(p[1]), "close": float(p[2]),
            "high": float(p[3]), "low": float(p[4]),
            "amount": float(p[6]),
            "change_pct": float(p[8]),
            "turnover_rate": float(p[10]) if len(p) > 10 else None,
        })
    return rows


async def trading_days(client: httpx.AsyncClient, end: str, count: int) -> list[str]:
    """Last `count` trading days (ascending) up to and including `end` if it trades.

    Derived from the Shanghai Composite index calendar (secid 1.000001).
    """
    # Pull generously to cover holidays.
    params = {
        "secid": "1.000001",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51",
        "klt": "101", "fqt": "1",
        "beg": "20200101", "end": end, "_": _ts(),
    }
    r = await _get_with_retry(client, KLINE_URL, params=params, headers=EM_HEADERS, timeout=20)
    data = (r.json() or {}).get("data") or {}
    dates = [ln.split(",")[0].replace("-", "") for ln in (data.get("klines") or [])]
    return dates[-count:] if count <= len(dates) else dates
