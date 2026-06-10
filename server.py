"""A-share 首板 (first limit-up board) MCP server.

Free, token-less. Data: 东方财富 (hard public fields) + 同花顺 (涨停原因, best-effort).
Transport: Streamable HTTP (default) and SSE — both exposed by FastMCP.

Tools
  1. get_today_first_boards               今日首板
  2. get_yesterday_first_board_today      昨日首板今日表现
  3. get_prev_two_days_followup           前两个交易日首板 T+1 / T+2
  4. get_recent_first_board_5d_stats      最近N交易日首板 后5日最高涨幅/最大回撤

Run:
  python server.py                # Streamable HTTP at http://127.0.0.1:8000/mcp
  TRANSPORT=sse python server.py  # SSE at http://127.0.0.1:8000/sse
  HOST/PORT env vars override bind address.
"""
from __future__ import annotations
import asyncio
import os
from datetime import datetime
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

import eastmoney as em
import ths
from common import (
    is_bj, is_st, limit_price, days_between, round2, market_of,
)

mcp = FastMCP("astock-first-board")

_SEM = asyncio.Semaphore(8)            # cap concurrent kline calls
_NEW_STOCK_DAYS_DEFAULT = 90           # 次新股过滤阈值(自然日), 可调


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={}, follow_redirects=True)


def _ipo_date(quote: dict) -> Optional[str]:
    v = quote.get("f26")
    if v in (None, "-", "", 0, "0"):
        return None
    return str(v)


async def _first_board_items(client, date: str, new_stock_days: int,
                             with_reason: bool = False) -> list[dict]:
    """Filtered 首板 list for a trading day: 非ST, 非北交所, 非新股."""
    pool = await em.fetch_zt_pool(client, date)
    cand = []
    for raw in pool:
        it = em.parse_zt_item(raw)
        if it["lian_ban_days"] != 1:        # only 首板
            continue
        if is_st(it["name"]) or is_bj(it["code"]):
            continue
        cand.append(it)

    if not cand:
        return []

    # 次新股过滤: drop names whose IPO is within new_stock_days of `date`.
    quotes = await em.fetch_quotes(client, [c["code"] for c in cand])
    kept = []
    for it in cand:
        q = quotes.get(it["code"], {})
        ipo = _ipo_date(q)
        if ipo and days_between(ipo, date) < new_stock_days:
            continue
        kept.append(it)

    if with_reason and kept:
        reasons = await ths.fetch_reasons(client, date)
        for it in kept:
            r = reasons.get(it["code"])
            it["reason"] = (r or {}).get("reason") or "未获取(同花顺/编辑性字段)"
            it["concept"] = (r or {}).get("concept") or it["industry"] or "未获取"
    return kept


# ── Tool 1 ────────────────────────────────────────────────────────────────
@mcp.tool()
async def get_today_first_boards(date: Optional[str] = None,
                                 new_stock_days: int = _NEW_STOCK_DAYS_DEFAULT) -> dict:
    """今日首板 (今天涨停且为首板、非ST、非新股、非北交所)，按封板时间从早到晚排序。

    Args:
        date: 交易日 YYYYMMDD; 缺省取最近交易日。
        new_stock_days: 上市不足该自然天数视为新股并剔除 (默认90)。
    Returns 每条: 股票代码/股票名称/所属概念/涨停原因/封板时间/炸板次数/
                  成交额/换手率/流通市值/涨停封单金额。
    """
    async with _client() as client:
        if not date:
            tds = await em.trading_days(client, datetime.now().strftime("%Y%m%d"), 1)
            date = tds[-1] if tds else datetime.now().strftime("%Y%m%d")
        items = await _first_board_items(client, date, new_stock_days, with_reason=True)

    items.sort(key=lambda x: x["first_seal_time"] or "99:99:99")
    rows = [{
        "股票代码": it["code"],
        "股票名称": it["name"],
        "所属概念": it.get("concept", ""),
        "涨停原因": it.get("reason", ""),
        "封板时间": it["first_seal_time"],
        "炸板次数": it["break_count"],
        "成交额": it["amount"],
        "换手率(%)": it["turnover_rate"],
        "流通市值": it["float_mktcap"],
        "涨停封单金额": it["seal_fund"],
    } for it in items]
    return {"date": date, "count": len(rows), "rows": rows,
            "note": "涨停原因/所属概念来自同花顺人工归类(编辑性，可能缺失);其余为东财公开硬数据。"}


# ── Tool 2 ────────────────────────────────────────────────────────────────
@mcp.tool()
async def get_yesterday_first_board_today(yesterday: Optional[str] = None,
                                          today: Optional[str] = None,
                                          new_stock_days: int = _NEW_STOCK_DAYS_DEFAULT) -> dict:
    """昨日首板股票的今日表现。

    Args:
        yesterday: 首板交易日 YYYYMMDD; 缺省取最近交易日的前一交易日。
        today: 表现交易日 YYYYMMDD; 缺省取最近交易日。
    Returns 每条: 股票代码/股票名称/今日涨跌幅/开盘涨跌幅/最高涨幅/是否涨停/
                  是否冲击涨停/成交额/换手率。
    """
    async with _client() as client:
        if not today or not yesterday:
            tds = await em.trading_days(client, datetime.now().strftime("%Y%m%d"), 2)
            today = today or tds[-1]
            yesterday = yesterday or tds[-2]
        items = await _first_board_items(client, yesterday, new_stock_days)
        codes = [it["code"] for it in items]
        quotes = await em.fetch_quotes(client, codes) if codes else {}

    rows = []
    name_by = {it["code"]: it["name"] for it in items}
    for code in codes:
        q = quotes.get(code, {})
        def g(k):
            v = q.get(k)
            return float(v) if v not in (None, "-", "") else None
        last, high, open_, prev = g("f2"), g("f15"), g("f17"), g("f18")
        chg = g("f3")
        amount, turn = g("f6"), g("f8")
        open_pct = round2((open_ - prev) / prev * 100) if open_ and prev else None
        high_pct = round2((high - prev) / prev * 100) if high and prev else None
        lp = limit_price(prev, code) if prev else None
        is_zt = bool(last and lp and abs(last - lp) < 0.005)
        # 冲击涨停: 盘中触及涨停价但收盘未封住 (确定性计算)
        hit_zt = bool(high and lp and high >= lp - 0.005 and not is_zt)
        rows.append({
            "股票代码": code,
            "股票名称": name_by.get(code, q.get("f14", "")),
            "首板日期": yesterday,
            "今日涨跌幅(%)": chg,
            "开盘涨跌幅(%)": open_pct,
            "最高涨幅(%)": high_pct,
            "是否涨停": is_zt,
            "是否冲击涨停": hit_zt,
            "成交额": amount,
            "换手率(%)": turn,
        })
    return {"yesterday": yesterday, "today": today, "count": len(rows), "rows": rows,
            "note": "是否涨停/是否冲击涨停为确定性计算(现价/最高价 vs 涨停价),非主观推理。"}


# ── Tool 3 ────────────────────────────────────────────────────────────────
async def _followup_for_day(client, board_day: str, td_after: list[str],
                            new_stock_days: int) -> list[dict]:
    items = await _first_board_items(client, board_day, new_stock_days)
    if not items or len(td_after) < 2:
        return []
    t1, t2 = td_after[0], td_after[1]
    # fetch up to 5 days for 后5日最高涨幅/最大回撤
    end = td_after[4] if len(td_after) >= 5 else td_after[-1]

    async def one(it):
        async with _SEM:
            kl = await em.fetch_kline(client, it["code"], board_day, end)
        by_date = {r["date"]: r for r in kl}
        board_row = by_date.get(board_day, {})
        r1, r2 = by_date.get(t1), by_date.get(t2)
        p1 = r1["change_pct"] if r1 else None
        p2 = r2["change_pct"] if r2 else None
        # 后5日窗口
        window = [by_date[d] for d in td_after if d in by_date]
        base_close = board_row.get("close")
        tg, dd = _five_day_stats(base_close, window)
        # T+2是否站上涨停价: T+2收盘价 >= T+2涨停价(以T+1收盘为基准)
        t2_above_zt = None
        if r2 and r1:
            lp = limit_price(r1["close"], it["code"])
            t2_above_zt = bool(r2["close"] >= lp - 0.005)
        return {
            "股票代码": it["code"], "股票名称": it["name"],
            "首板日期": board_day,
            "T+1日期": t1, "T+1涨跌幅(%)": p1,
            "T+2日期": t2, "T+2涨跌幅(%)": p2,
            "是否两日红盘": bool(p1 is not None and p2 is not None and p1 > 0 and p2 > 0),
            "后5日最高涨幅(%)": tg, "后5日最大回撤(%)": dd,
            "T+2是否站上涨停价": t2_above_zt,
        }
    return list(await asyncio.gather(*[one(it) for it in items]))


@mcp.tool()
async def get_prev_two_days_followup(new_stock_days: int = _NEW_STOCK_DAYS_DEFAULT) -> dict:
    """前两个交易日的首板股票，统计首板后第二、三个交易日(T+1/T+2)涨跌幅与后5日最高涨幅/最大回撤。

    取最近两个"已具备完整T+1、T+2行情"的首板日作为统计对象。
    Returns 每条: 股票代码/股票名称/首板日期/T+1日期与涨跌幅/T+2日期与涨跌幅/
                  是否两日红盘/后5日最高涨幅/后5日最大回撤/T+2是否站上涨停价。
    """
    async with _client() as client:
        tds = await em.trading_days(client, datetime.now().strftime("%Y%m%d"), 15)
        if len(tds) < 4:
            return {"rows": [], "note": "交易日不足。"}
        # 候选首板日: 最近两个已具备完整T+2行情的交易日 => tds[-4], tds[-3]
        board_days = [tds[-4], tds[-3]]
        results = []
        for bd in board_days:
            idx = tds.index(bd)
            after = tds[idx + 1: idx + 6]   # up to 5 days; may be < 5 near current date
            results += await _followup_for_day(client, bd, after, new_stock_days)
    return {"board_days": board_days, "count": len(results), "rows": results,
            "note": "T+1/T+2为对应交易日日线涨跌幅;后5日最高涨幅/最大回撤相对首板日收盘(前复权);近期首板日后5日数据可能不足。"}


# ── Tool 4 ────────────────────────────────────────────────────────────────
def _five_day_stats(base_close: float, window: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """后5日最高涨幅 & 最大回撤 (相对首板日收盘价, %)。"""
    if not base_close or not window:
        return None, None
    max_high = max(r["high"] for r in window)
    top_gain = (max_high - base_close) / base_close * 100
    # 标准最大回撤: 从入场后任一峰值到其后低点的最大跌幅
    peak = base_close
    max_dd = 0.0
    for r in window:
        peak = max(peak, r["high"])
        dd = (r["low"] - peak) / peak * 100
        max_dd = min(max_dd, dd)
    return round2(top_gain), round2(max_dd)


@mcp.tool()
async def get_recent_first_board_5d_stats(lookback: int = 10,
                                          new_stock_days: int = _NEW_STOCK_DAYS_DEFAULT) -> dict:
    """最近 lookback 个交易日的首板股票，统计首板后5个交易日最高涨幅与最大回撤。

    若首板日距今不足5个交易日，按已有交易日给出部分统计并标注 days_used。
    Returns 每条: 股票代码/股票名称/首板日期/后5日最高涨幅/后5日最大回撤/已用天数。
    """
    async with _client() as client:
        tds_all = await em.trading_days(client, datetime.now().strftime("%Y%m%d"), lookback + 8)
        board_days = tds_all[-lookback:]
        idx_of = {d: i for i, d in enumerate(tds_all)}

        rows: list[dict] = []
        for bd in board_days:
            items = await _first_board_items(client, bd, new_stock_days)
            i = idx_of[bd]
            after = tds_all[i + 1: i + 6]            # up to 5 trading days
            if not after or not items:
                for it in items:
                    rows.append({"股票代码": it["code"], "股票名称": it["name"],
                                 "首板日期": bd, "后5日最高涨幅(%)": None,
                                 "后5日最大回撤(%)": None, "已用天数": 0})
                continue
            end = after[-1]

            async def one(it, after=after, bd=bd, end=end):
                async with _SEM:
                    kl = await em.fetch_kline(client, it["code"], bd, end)
                by_date = {r["date"]: r for r in kl}
                base = by_date.get(bd, {}).get("close")
                window = [by_date[d] for d in after if d in by_date]
                tg, dd = _five_day_stats(base, window)
                return {"股票代码": it["code"], "股票名称": it["name"],
                        "首板日期": bd, "后5日最高涨幅(%)": tg,
                        "后5日最大回撤(%)": dd, "已用天数": len(window)}
            rows += list(await asyncio.gather(*[one(it) for it in items]))
    return {"lookback": lookback, "board_days": board_days, "count": len(rows), "rows": rows,
            "note": "最高涨幅/最大回撤相对首板日收盘价(前复权日线),确定性计算。"}


# ── health endpoint (for Render / load-balancer checks) ─────────────────────
from starlette.requests import Request
from starlette.responses import JSONResponse

@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ── entrypoint ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    transport = os.getenv("TRANSPORT", "streamable-http").lower()
    mcp.settings.host = os.getenv("HOST", "127.0.0.1")
    mcp.settings.port = int(os.getenv("PORT", "8000"))
    # Allow all hosts so Railway/Render reverse-proxy domains work
    mcp.settings.transport_security.enable_dns_rebinding_protection = False
    if transport in ("sse",):
        mcp.run(transport="sse")            # http://host:port/sse
    else:
        mcp.run(transport="streamable-http")  # http://host:port/mcp
