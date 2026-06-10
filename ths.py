"""同花顺 (10jqka) limit-up pool — best-effort source for 涨停原因 / 概念.

NOTE: This endpoint is editorial (人工归类) and is occasionally protected by an
anti-crawl cookie (hexin-v). It is used ONLY to enrich 涨停原因/所属概念. If it
fails, the MCP tools still return every hard public field and mark the reason as
unavailable. Treat 涨停原因/所属概念 as non-guaranteed, source-dependent fields.
"""
from __future__ import annotations
import time
from typing import Any
import httpx

from common import THS_HEADERS

THS_POOL_URL = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"


async def fetch_reasons(client: httpx.AsyncClient, date: str) -> dict[str, dict]:
    """Map code -> {reason, concept, high_days}. Empty dict on any failure."""
    params = {
        "page": "1", "limit": "300",
        "field": "199112,10,9001,330329,330325,133971,133970,1968584,3475914,"
                 "9004,1378761,480536,9002,599874,564535,560864,580017,581235,"
                 "5400,9003,1149846,1646768,9005,9006,9007",
        "filter": "HS,GEM2STAR",
        "order_field": "330329", "order_type": "0",
        "date": date, "_": str(int(time.time() * 1000)),
    }
    try:
        r = await client.get(THS_POOL_URL, params=params, headers=THS_HEADERS, timeout=20)
        r.raise_for_status()
        info = ((r.json() or {}).get("data") or {}).get("info") or []
    except (httpx.HTTPError, ValueError, KeyError):
        return {}

    out: dict[str, dict] = {}
    for it in info:
        code = str(it.get("code", "")).zfill(6)
        if not code.strip("0"):
            continue
        out[code] = {
            "reason": it.get("reason_type") or "",        # 涨停原因(类别)
            "concept": it.get("reason_type") or "",       # 概念 = 同口径
            "high_days": it.get("high_days") or "",        # 几天几板
        }
    return out
