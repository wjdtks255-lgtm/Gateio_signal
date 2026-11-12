#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 위원회 (1단계: 참고·기록 전용)
- bot.py 의 규칙 기반 시그널을 Gemini, Groq 두 AI가 각각 검토해 "진입/보류 + 확신도 + 근거"를 냅니다.
- 결과는 텔레그램 메시지에 붙고, 포지션 종료 시 ai_trades.csv 에 기록됩니다.
- 시그널 진입 여부에는 영향을 주지 않습니다. (나중에 성과를 비교해 필터로 쓸지 결정)
- 이 파일에서 무슨 오류가 나도 bot.py 는 평소처럼 동작하도록 모든 함수가 예외를 삼킵니다.
"""

import csv
import html
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
# 모델 이름은 서비스 쪽에서 바뀔 수 있어 환경변수로 덮어쓸 수 있게 해 둠
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

AI_TIMEOUT_SEC = 20          # AI 하나당 응답 대기 한도
AI_TIME_BUDGET_SEC = 220     # 실행 시작 후 이 시간이 지나면 AI 검토 생략 (워크플로 5분 제한 보호)
DRAW_BAND_R = 0.05           # bot.py 와 같은 무승부 기준

AI_LOG_FILE = Path(os.getenv("AI_LOG_FILE", "ai_trades.csv"))
AI_LOG_FIELDS = [
    "closed_at", "symbol", "direction", "entry_time", "exit_reason", "net_r", "outcome",
    "ai_verdict", "ai_enter_votes", "ai_valid_votes", "ai_avg_confidence",
    "gemini_decision", "gemini_confidence", "groq_decision", "groq_confidence",
]

SYSTEM_PROMPT = """너는 선물 단기매매 시그널의 리스크 심사위원이다.
규칙 기반 봇이 잡은 진입 후보 하나를 받는다. 이 자리에 지금 진입할 가치가 있는지 판단하라.
- 기본 태도는 회의적이다. 근거가 애매하면 SKIP.
- 경계할 것: 24시간 급등락 뒤 추격 진입, 롱인데 RSI 과열/숏인데 RSI 과매도, EMA 이격 과다,
  약한 추세(ADX 낮음), 거래량 증가 없음, BTC 방향과의 충돌, 해당 방향의 최근 전략 성과 부진.
- 손절폭(sl_pct) 대비 목표(tp1~tp3) 구조도 고려하라.
반드시 JSON 객체 하나만 출력하라. 다른 글자는 쓰지 마라.
{"decision": "ENTER" 또는 "SKIP", "confidence": 0~100 정수, "reason": "한국어 한 문장, 60자 이내"}"""


# ───────────────────────────── AI 호출 ─────────────────────────────
def _parse_vote(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    decision = str(data.get("decision", "")).strip().upper()
    if decision not in ("ENTER", "SKIP"):
        return None
    try:
        confidence = int(float(data.get("confidence", 0)))
    except (TypeError, ValueError):
        confidence = 0
    reason = str(data.get("reason", "")).strip()[:120]
    return {"decision": decision, "confidence": max(0, min(100, confidence)), "reason": reason}


def _ask_gemini(prompt: str) -> Optional[Dict[str, Any]]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    response = requests.post(
        url, headers={"x-goog-api-key": GEMINI_API_KEY}, json=body, timeout=AI_TIMEOUT_SEC,
    )
    if response.status_code != 200:
        logging.warning("Gemini 응답 오류: %s %s", response.status_code, response.text[:300])
        return None
    parts = response.json()["candidates"][0]["content"]["parts"]
    return _parse_vote("".join(part.get("text", "") for part in parts))


def _ask_groq(prompt: str) -> Optional[Dict[str, Any]]:
    body = {
        "model": GROQ_MODEL,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json=body, timeout=AI_TIMEOUT_SEC,
    )
    if response.status_code != 200:
        logging.warning("Groq 응답 오류: %s %s", response.status_code, response.text[:300])
        return None
    return _parse_vote(response.json()["choices"][0]["message"]["content"])


def _safe_call(func: Callable[[str], Optional[Dict[str, Any]]], prompt: str) -> Optional[Dict[str, Any]]:
    try:
        return func(prompt)
    except Exception as exc:  # 네트워크·형식 오류 등은 '응답 없음'으로 처리
        logging.warning("AI 호출 실패 (%s): %s", func.__name__, exc)
        return None


def _review(signal: Dict[str, Any]) -> Dict[str, Any]:
    voters = []
    if GEMINI_API_KEY:
        voters.append(("Gemini", _ask_gemini))
    if GROQ_API_KEY:
        voters.append(("Groq", _ask_groq))
    if not voters:
        return {"verdict": "UNAVAILABLE", "enter": 0, "valid": 0, "total": 0,
                "avg_confidence": 0, "votes": {}}

    prompt = "아래 진입 후보를 심사하라.\n" + json.dumps(signal, ensure_ascii=False, indent=1)
    with ThreadPoolExecutor(max_workers=len(voters)) as pool:
        futures = {name: pool.submit(_safe_call, func, prompt) for name, func in voters}
        votes = {name: future.result() for name, future in futures.items()}

    valid = [vote for vote in votes.values() if vote]
    enter = sum(1 for vote in valid if vote["decision"] == "ENTER")
    if not valid:
        verdict = "UNAVAILABLE"
    elif enter == len(valid):
        verdict = "APPROVE"
    elif enter == 0:
        verdict = "REJECT"
    else:
        verdict = "SPLIT"
    avg_conf = round(sum(vote["confidence"] for vote in valid) / len(valid)) if valid else 0
    return {"verdict": verdict, "enter": enter, "valid": len(valid), "total": len(voters),
            "avg_confidence": avg_conf, "votes": votes}


# ───────────────────────────── 메시지 ─────────────────────────────
VERDICT_LABEL = {
    "APPROVE": "✅ 찬성",
    "SPLIT": "⚖️ 의견 갈림",
    "REJECT": "❌ 반대",
    "UNAVAILABLE": "⚪ 응답 없음",
}


def _format_block(review: Dict[str, Any]) -> str:
    verdict = review.get("verdict", "UNAVAILABLE")
    head = f"🤖 <b>AI 위원회: {VERDICT_LABEL.get(verdict, verdict)}"
    if review.get("valid"):
        head += f" {review['enter']}/{review['valid']}</b> (평균 확신도 {review['avg_confidence']})"
    else:
        head += "</b>"
    lines = [head]
    for name, vote in review.get("votes", {}).items():
        if not vote:
            lines.append(f"• {name}: 응답 없음")
            continue
        label = "진입" if vote["decision"] == "ENTER" else "보류"
        lines.append(f"• {name}: {label} {vote['confidence']} — {html.escape(vote['reason'])}")
    lines.append("<i>AI 의견은 참고·기록용이며 시그널에는 영향 없음</i>")
    return "\n".join(lines)


def review_and_format(slot: Dict[str, Any], symbol: str, category: str, change: float,
                      btc_bias: str, analysis: Dict[str, Any], state: Dict[str, Any],
                      start_time: float) -> str:
    """bot.py 에서 신규 시그널마다 호출. 결과를 slot['ai'] 에 저장하고 메시지용 텍스트를 돌려줌."""
    try:
        if time.time() - start_time > AI_TIME_BUDGET_SEC:
            logging.info("AI 검토 생략 (시간 예산 초과): %s", symbol)
            return ""
        direction = slot["direction"]
        price = float(slot["entry_price"])

        def pct(target: float) -> float:
            move = (target - price) / price * 100.0
            return round(move if direction == "LONG" else -move, 2)

        signal = {
            "symbol": symbol,
            "category": category,
            "direction": direction,
            "price": price,
            "change_24h_pct": round(change, 2),
            "btc_1h_bias": btc_bias,
            "rule_score": analysis["score"],
            "required_score": analysis["required_score"],
            "max_score": analysis.get("max_score"),
            "rule_reasons": analysis["reasons"],
            "rsi_1h": round(analysis["rsi1h"], 1),
            "rsi_15m": round(analysis["rsi15"], 1),
            "adx_1h": round(analysis["adx"], 1),
            "volume_surge": round(analysis["volume_surge"], 2),
            "body_ratio_15m": round(analysis["body_ratio"], 2),
            "ema21_distance_atr": round(analysis["ema_distance_atr"], 2),
            "sl_pct": pct(slot["stop_loss"]),
            "tp1_pct": pct(slot["tp1"]),
            "tp2_pct": pct(slot["tp2"]),
            "tp3_pct": pct(slot["tp3"]),
            "recent_strategy_stats_same_direction": state.get("stats", {})
                .get("directional", {}).get(direction, {}),
        }
        review = _review(signal)
        slot["ai"] = review
        logging.info("AI review %s %s: %s", symbol, direction, review["verdict"])
        return "\n\n" + _format_block(review)
    except Exception:
        logging.exception("AI review failed for %s", symbol)
        return ""


# ───────────────────────────── 기록과 성과 ─────────────────────────────
def log_closed_trade(symbol: str, info: Dict[str, Any], net_r: float, outcome: str,
                     reason: str, now: datetime) -> None:
    """포지션 종료 시 AI 판정과 실제 결과를 함께 기록. AI 검토가 없던 거래도 NONE 으로 남김."""
    try:
        review = info.get("ai") or {}
        votes = review.get("votes", {})
        gemini = votes.get("Gemini") or {}
        groq = votes.get("Groq") or {}
        row = {
            "closed_at": now.isoformat(),
            "symbol": symbol,
            "direction": info.get("direction", ""),
            "entry_time": info.get("entry_time", ""),
            "exit_reason": reason,
            "net_r": round(net_r, 3),
            "outcome": outcome,
            "ai_verdict": review.get("verdict", "NONE"),
            "ai_enter_votes": review.get("enter", ""),
            "ai_valid_votes": review.get("valid", ""),
            "ai_avg_confidence": review.get("avg_confidence", ""),
            "gemini_decision": gemini.get("decision", ""),
            "gemini_confidence": gemini.get("confidence", ""),
            "groq_decision": groq.get("decision", ""),
            "groq_confidence": groq.get("confidence", ""),
        }
        exists = AI_LOG_FILE.exists() and AI_LOG_FILE.stat().st_size > 0
        with AI_LOG_FILE.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=AI_LOG_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception:
        logging.exception("AI trade log failed for %s", symbol)


def build_ai_stats_line() -> str:
    """AI 판정별(찬성/갈림/반대) 실제 성과. 슬롯 현황 메시지 아래에 붙음."""
    try:
        if not AI_LOG_FILE.exists():
            return ""
        groups: Dict[str, List[float]] = {"APPROVE": [], "SPLIT": [], "REJECT": []}
        with AI_LOG_FILE.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("ai_verdict") in groups:
                    groups[row["ai_verdict"]].append(float(row.get("net_r") or 0))
        if not any(groups.values()):
            return ""
        lines = ["🤖 <b>AI 판정별 실제 성과</b>"]
        for key, name in (("APPROVE", "찬성"), ("SPLIT", "갈림"), ("REJECT", "반대")):
            values = groups[key]
            if not values:
                continue
            wins = sum(1 for v in values if v > DRAW_BAND_R)
            losses = sum(1 for v in values if v < -DRAW_BAND_R)
            win_rate = wins / (wins + losses) * 100.0 if wins + losses else 0.0
            avg_r = sum(values) / len(values)
            lines.append(
                f"<code>{name} {len(values)}건 | 승률 {win_rate:.1f}% | 평균 {avg_r:+.2f}R | 합계 {sum(values):+.1f}R</code>"
            )
        return "\n".join(lines)
    except Exception:
        logging.exception("AI stats line failed")
        return ""
