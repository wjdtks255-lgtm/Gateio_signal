#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
주간 리뷰 위원회
- trades.csv(+ trades_audit.csv) 와 ai_trades.csv 를 읽어 현재 전략 버전 거래를 조건별로 집계합니다.
- 숫자 집계는 코드가 하고, AI(Gemini, GPT-OSS)는 그 요약표만 보고 "자주 지는 조건"과 "조일 만한 기준"을 제안합니다.
- 결과는 텔레그램으로 보냅니다. 봇 설정이나 파일은 아무것도 바꾸지 않습니다.
"""

import csv
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Tuple

import ai_committee as ai
import bot

MIN_TRADES_FOR_AI = 10   # 이보다 적으면 AI 제안 생략 (표본이 너무 적으면 우연을 패턴으로 착각함)

REVIEW_PROMPT = """너는 선물 단기매매 봇의 성과를 검토하는 퀀트 리뷰어다.
아래는 규칙 기반 시그널 봇의 현재 파라미터와, 종료된 거래를 조건별로 집계한 표다.
R은 손절폭 1단위 대비 손익이다 (+1R = 손절폭만큼 이익).

한국어로, 아래 세 부분만 간결하게 써라. 마크다운 표나 제목 기호(#, **)는 쓰지 마라.
1) 자주 지는 조건: 표본 수가 어느 정도 있는 구간 중 승률이나 평균 R이 뚜렷이 나쁜 곳 2~3개.
2) 조정 제안: 위 결과를 근거로 바꿔볼 만한 파라미터 최대 3개. 각각 "이름: 현재값 → 제안값, 이유" 형식.
   표본이 적어 확신할 수 없으면 솔직히 그렇게 말하고 "더 지켜보기"를 제안해도 된다.
3) AI 위원회 평가: AI 판정(찬성/반대)과 위원별 기록을 보고, AI 반대 시그널을 걸러내는 필터가 쓸모 있어 보이는지.
전체 25줄 이내."""


def _bucket_table(title: str, rows: List[Dict[str, Any]],
                  key: Callable[[Dict[str, Any]], str], order: List[str] = None) -> str:
    groups: Dict[str, List[float]] = {}
    for row in rows:
        label = key(row)
        if label:
            groups.setdefault(label, []).append(float(row["net_r"]))
    if not groups:
        return ""
    labels = order and [l for l in order if l in groups] or sorted(groups)
    lines = [f"[{title}]"]
    for label in labels:
        values = groups[label]
        wins = sum(1 for v in values if v > bot.DRAW_BAND_R)
        losses = sum(1 for v in values if v < -bot.DRAW_BAND_R)
        win_rate = wins / (wins + losses) * 100.0 if wins + losses else 0.0
        lines.append(f"{label}: {len(values)}건 | 승률 {win_rate:.0f}% | 평균 {sum(values)/len(values):+.2f}R"
                     f" | 합계 {sum(values):+.1f}R")
    return "\n".join(lines)


def _num(row: Dict[str, Any], field: str) -> float:
    return bot.safe_float(row.get(field), float("nan"))


def _range_label(value: float, edges: List[Tuple[float, str]], last: str) -> str:
    if value != value:  # NaN
        return ""
    for edge, label in edges:
        if value < edge:
            return label
    return last


def build_summary() -> Tuple[str, int]:
    records = [r for r in bot.read_trade_csv()
               if int(bot.safe_float(r.get("strategy_version"), 0)) == bot.STRATEGY_VERSION]
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    this_week = [r for r in records if bot.parse_iso(r.get("closed_at", ""), week_ago) >= week_ago]

    # AI 판정 붙이기 (closed_at + symbol 로 매칭)
    ai_rows: Dict[Tuple[str, str], Dict[str, str]] = {}
    if ai.AI_LOG_FILE.exists():
        with ai.AI_LOG_FILE.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                ai_rows[(row.get("closed_at", ""), row.get("symbol", ""))] = row
    for record in records:
        record["ai"] = ai_rows.get((record.get("closed_at", ""), record.get("symbol", "")), {})

    stats = bot.calculate_stats(records)
    decisive = stats["wins"] + stats["losses"]
    head = [
        f"전략 v{bot.STRATEGY_VERSION} 누적 {stats['total']}건 (이번 주 {len(this_week)}건)",
        f"승률 {stats['wins'] / decisive * 100 if decisive else 0:.0f}% (무 제외) | "
        f"평균 {stats['sum_r'] / stats['total'] if stats['total'] else 0:+.2f}R | 합계 {stats['sum_r']:+.1f}R",
    ]

    def rsi_extreme(row: Dict[str, Any]) -> str:
        rsi = _num(row, "rsi_15m")
        if rsi != rsi:
            return ""
        gap = abs(rsi - 50.0)
        return "50 근처(±8 미만)" if gap < 8 else ("중간(±8~15)" if gap < 15 else "과열·과매도 근접(±15 이상)")

    tables = [
        _bucket_table("방향", records, lambda r: r.get("direction", "")),
        _bucket_table("자산", records, lambda r: r.get("category", "")),
        _bucket_table("청산 이유", records, lambda r: r.get("exit_reason", "")),
        _bucket_table("품질 점수", records, lambda r: str(int(_num(r, "score"))) + "점"
                      if _num(r, "score") == _num(r, "score") else ""),
        _bucket_table("15M RSI 극단도", records, rsi_extreme,
                      ["50 근처(±8 미만)", "중간(±8~15)", "과열·과매도 근접(±15 이상)"]),
        _bucket_table("1H ADX", records,
                      lambda r: _range_label(_num(r, "adx_1h"), [(20, "20 미만"), (25, "20~25"), (35, "25~35")], "35 이상"),
                      ["20 미만", "20~25", "25~35", "35 이상"]),
        _bucket_table("거래량 배수", records,
                      lambda r: _range_label(_num(r, "volume_surge"), [(1.05, "1.05 미만"), (1.5, "1.05~1.5"), (3, "1.5~3")], "3 이상"),
                      ["1.05 미만", "1.05~1.5", "1.5~3", "3 이상"]),
        _bucket_table("AI 판정", records,
                      lambda r: {"APPROVE": "찬성", "REJECT": "반대", "SPLIT": "갈림"}.get(r["ai"].get("ai_verdict", ""), ""),
                      ["찬성", "갈림", "반대"]),
    ]
    for name, slug, *_ in ai.VOTERS:
        tables.append(_bucket_table(f"위원 {name}", records,
                                    lambda r, s=slug: {"ENTER": "진입 의견", "SKIP": "보류 의견"}.get(
                                        r["ai"].get(f"{s}_decision", ""), ""),
                                    ["진입 의견", "보류 의견"]))
    body = "\n\n".join(t for t in tables if t)
    return "\n".join(head) + ("\n\n" + body if body else ""), stats["total"]


def current_parameters() -> str:
    names = ["MIN_SCORE_LONG", "MIN_SCORE_SHORT", "ADX_MIN_1H", "ADX_GOOD_1H", "ADX_STRONG_1H",
             "MIN_VOLUME_SURGE", "VOLUME_STRONG", "BODY_RATIO_MIN", "BODY_STRONG", "MAX_EMA_DISTANCE_ATR",
             "RSI_LONG_15M", "RSI_SHORT_15M", "RSI_LONG_1H", "RSI_SHORT_1H", "MIN_CHANGE_PCT",
             "ATR_SL", "ATR_TP_MULTS", "TIME_STOP_HOURS", "MAX_SLOTS", "MAX_CRYPTO_SLOTS"]
    return "\n".join(f"{n} = {getattr(bot, n)}" for n in names if hasattr(bot, n))


def ask_reviewers(summary: str) -> List[Tuple[str, str]]:
    prompt = f"[현재 파라미터]\n{current_parameters()}\n\n[집계]\n{summary}"
    reviewers = []
    if ai.GEMINI_API_KEY:
        reviewers.append(("Gemini", "gemini", ai.VOTERS[0][3]))
    if ai.GROQ_API_KEY:
        reviewers.append(("GPT-OSS", "groq", ai.VOTERS[1][3]))
    results = []
    for name, provider, models in reviewers:
        text, error = "", "응답 없음"
        for model in [m.strip() for m in models if m.strip()]:
            try:
                if provider == "gemini":
                    text = ai._call_gemini_text(model, REVIEW_PROMPT, prompt)
                else:
                    text = ai._call_openai_style("https://api.groq.com/openai/v1/chat/completions",
                                                 ai.GROQ_API_KEY, "Groq", model, REVIEW_PROMPT, prompt)
                if text:
                    break
            except ai.AIError as exc:
                error = str(exc)
            except Exception as exc:
                logging.warning("주간 리뷰 AI 호출 실패 (%s %s): %s", provider, model, exc)
                error = "연결 오류"
        results.append((name, text.strip() if text else f"응답 없음 ({error})"))
    return results


def main() -> None:
    summary, total = build_summary()
    bot.send_telegram_message(f"🗓️ <b>주간 리뷰 · 집계</b>\n<pre>{html.escape(summary[:3500])}</pre>")
    if total < MIN_TRADES_FOR_AI:
        bot.send_telegram_message(
            f"🗓️ <b>주간 리뷰 · AI 제안 생략</b>\n종료된 v{bot.STRATEGY_VERSION} 거래가 {total}건이라 "
            f"{MIN_TRADES_FOR_AI}건 이상 쌓이면 AI 제안을 붙입니다. 표본이 적을 때의 패턴은 우연일 가능성이 커요."
        )
        return
    for name, text in ask_reviewers(summary):
        bot.send_telegram_message(f"🗓️ <b>주간 리뷰 · {html.escape(name)} 의견</b>\n{html.escape(text[:3500])}")
    bot.send_telegram_message("ℹ️ 제안은 참고용입니다. 파라미터는 한 번에 하나씩, 충분히 지켜본 뒤 바꾸세요.")


if __name__ == "__main__":
    main()
