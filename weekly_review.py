#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
주간 리뷰 위원회
- trades.csv(+ trades_audit.csv) 와 ai_trades.csv 를 읽어 현재 전략 버전 거래를 조건별로 집계합니다.
- 숫자 집계는 코드가 하고, AI(Gemini, GPT-OSS)는 그 요약표만 보고 "자주 지는 조건"과 "조일 만한 기준"을 제안합니다.
- AI 가 파라미터 역할을 오해하지 않도록 각 파라미터가 필터인지 점수 기준인지 함께 전달합니다.
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
아래는 규칙 기반 시그널 봇의 현재 파라미터(각각의 실제 역할 포함)와, 종료된 거래를 조건별로 집계한 표다.
R은 손절폭 1단위 대비 손익이다 (+1R = 손절폭만큼 이익).

중요한 규칙:
- 각 파라미터의 [역할]을 반드시 확인하라. [점수]로 표시된 파라미터는 품질 점수만 바꾸고 진입을 막지 않는다.
  최소 점수 기준이 낮아서 점수 기준을 올려도 해당 구간 거래는 대부분 그대로 진입한다.
- 진입을 실제로 막는 것은 [필터]로 표시된 파라미터뿐이다.
- 원하는 효과를 내는 파라미터가 목록에 없으면 기존 숫자를 억지로 바꾸지 말고
  "새 필터 추가: 내용" 형식으로 제안하라. (예: 1H ADX 상한, 특정 자산군 제외 등)
- 건수가 20건 미만인 구간은 근거로 삼지 말고 "표본 부족"이라고만 언급하라.
- 손절(SL) 청산이 손실인 것은 정상이다. 청산 이유 자체를 "지는 조건"으로 꼽지 마라.

한국어로, 아래 세 부분만 간결하게 써라. 마크다운 표나 제목 기호(#, **)는 쓰지 마라.
1) 자주 지는 조건: 표본이 20건 이상인 구간 중 승률이나 평균 R이 뚜렷이 나쁜 곳 2~3개.
2) 조정 제안: 최대 3개. 기존 파라미터는 "이름: 현재값 → 제안값, 이유", 새 필터는 "새 필터 추가: 내용, 이유" 형식.
   각 제안마다 그 변경이 실제로 어떤 거래를 막는지 한 줄로 확인하라.
   표본이 적어 확신할 수 없으면 솔직히 그렇게 말하고 "더 지켜보기"를 제안해도 된다.
3) AI 위원회 평가: AI 판정(찬성/반대)과 위원별 기록을 보고, AI 반대 시그널을 걸러내는 필터가 쓸모 있어 보이는지.
전체 25줄 이내."""

# 파라미터 이름 → 실제 역할. AI 가 점수 기준을 필터로 착각하지 않도록 함께 보낸다.
PARAMETER_ROLES = [
    ("MIN_CHANGE_PCT", "[후보] 24시간 변동률 절댓값이 이 % 이상인 종목만 후보. +면 롱, -면 숏 후보"),
    ("ADX_MIN_1H", "[필터] 1H ADX 가 이 값 미만이면 진입 안 함. 상한은 없음"),
    ("RSI_LONG_1H", "[필터] 롱일 때 1H RSI 허용 범위"),
    ("RSI_SHORT_1H", "[필터] 숏일 때 1H RSI 허용 범위"),
    ("RSI_LONG_15M", "[필터] 롱일 때 15M RSI 허용 범위"),
    ("RSI_SHORT_15M", "[필터] 숏일 때 15M RSI 허용 범위"),
    ("BODY_RATIO_MIN", "[필터] 15M 캔들 몸통 비율이 이 값 미만이면 진입 안 함"),
    ("MAX_EMA_DISTANCE_ATR", "[필터] 현재가와 15M EMA21 거리가 이 ATR 배수를 넘으면 진입 안 함"),
    ("MIN_SCORE_LONG", "[필터] 롱 품질 점수가 이 값 미만이면 진입 안 함 (점수 0~6)"),
    ("MIN_SCORE_SHORT", "[필터] 숏 품질 점수가 이 값 미만이면 진입 안 함 (점수 0~6)"),
    ("ADX_GOOD_1H", "[점수] 1H ADX 가 이 값 이상이면 +1점. 진입을 막지 않음"),
    ("ADX_STRONG_1H", "[점수] 1H ADX 가 이 값 이상이면 +2점. 진입을 막지 않음"),
    ("MIN_VOLUME_SURGE", "[점수] 15M 거래량 배수가 이 값 이상이면 +1점. 진입을 막지 않음"),
    ("VOLUME_STRONG", "[점수] 15M 거래량 배수가 이 값 이상이면 +2점. 진입을 막지 않음"),
    ("SLOPE_GOOD_PCT", "[점수] 1H EMA20 기울기 절댓값이 이 % 이상이면 +1점. 진입을 막지 않음"),
    ("BODY_STRONG", "[점수] 15M 캔들 몸통 비율이 이 값 이상이면 +1점. 진입을 막지 않음"),
    ("ATR_SL", "[손익] 손절폭 = 1H ATR x 이 값 (= 1R)"),
    ("ATR_TP_MULTS", "[손익] TP1/TP2/TP3 = 1H ATR x 각 값. TP1 에서 50% 익절 후 15M EMA21 트레일링"),
    ("TIME_STOP_HOURS", "[청산] 이 시간 보유하면 무조건 청산"),
    ("MAX_SLOTS", "[슬롯] 동시에 추적하는 최대 시그널 수"),
    ("MAX_CRYPTO_SLOTS", "[슬롯] 그중 코인 최대 개수. 주식·지수·원자재 개수는 제한하지 않음"),
]


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
    return "\n".join(f"{name} = {getattr(bot, name)}  {role}"
                     for name, role in PARAMETER_ROLES if hasattr(bot, name))


def ask_reviewers(summary: str) -> List[Tuple[str, str]]:
    prompt = f"[현재 파라미터와 역할]\n{current_parameters()}\n\n[집계]\n{summary}"
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
