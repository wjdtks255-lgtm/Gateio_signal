#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 위원회 v2 (1단계: 참고·기록 전용) - 5명 위원회
- Gemini Flash(구글) / GPT-OSS 120B(OpenAI, Groq) / Qwen(알리바바, Groq)
  / OpenRouter 무료 모델(DeepSeek 우선, 없으면 다른 회사 무료 모델 자동 선택)
  / 반론 위원(Gemini Flash-Lite, 약점 찾기 전담)
- 각 위원은 "진입/보류 + 확신도 + 근거"를 내고, 결과는 텔레그램 메시지와 ai_trades.csv 에 기록됩니다.
- 시그널 진입 여부에는 영향을 주지 않습니다.
- 이 파일에서 무슨 오류가 나도 bot.py 는 평소처럼 동작하도록 모든 공개 함수가 예외를 삼킵니다.
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
from typing import Any, Dict, List, Optional

import requests

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

AI_TIMEOUT_SEC = 45          # 위원 한 명당 응답 대기 한도 (추론형 모델은 생각하는 시간이 길어 넉넉히)
AI_TIME_BUDGET_SEC = 170     # 실행 시작 후 이 시간이 지나면 AI 검토 생략 (워크플로 시간 제한 보호)
DRAW_BAND_R = 0.05           # bot.py 와 같은 무승부 기준

NORMAL_PROMPT = """너는 선물 단기매매 시그널의 리스크 심사위원이다.
규칙 기반 봇이 잡은 진입 후보 하나를 받는다. 이 자리에 지금 진입할 가치가 있는지 판단하라.
- 기본 태도는 회의적이다. 근거가 애매하면 SKIP.
- 경계할 것: 24시간 급등락 뒤 추격 진입, 롱인데 RSI 과열/숏인데 RSI 과매도, EMA 이격 과다,
  약한 추세(ADX 낮음), 거래량 증가 없음, BTC 방향과의 충돌, 해당 방향의 최근 전략 성과 부진.
- 손절폭(sl_pct) 대비 목표(tp1~tp3) 구조도 고려하라.
반드시 JSON 객체 하나만 출력하라. 다른 글자는 쓰지 마라.
{"decision": "ENTER" 또는 "SKIP", "confidence": 0~100 정수, "reason": "한국어 한 문장, 60자 이내"}"""

SKEPTIC_PROMPT = """너는 선물 단기매매 위원회의 '반론 담당' 위원이다.
다른 위원들은 이 시그널을 긍정적으로 볼 수 있다. 너의 임무는 이 진입 후보의 가장 큰 약점을 찾는 것이다.
- 먼저 손실로 끝날 수 있는 가장 그럴듯한 시나리오를 떠올려라.
  (추격 진입, 과열/과매도, 약한 추세, 거래량 부족, BTC 역풍, 최근 성과 부진, 불리한 손익 구조 등)
- 그 약점이 진입을 막을 만큼 결정적이면 SKIP, 결정적 약점이 없을 때만 ENTER.
- reason 에는 네가 찾은 가장 큰 약점(또는 약점이 약한 이유)을 적어라.
반드시 JSON 객체 하나만 출력하라. 다른 글자는 쓰지 마라.
{"decision": "ENTER" 또는 "SKIP", "confidence": 0~100 정수, "reason": "한국어 한 문장, 60자 이내"}"""

# 위원 명단: (표시 이름, 기록용 이름, 제공처, 모델 후보들, 역할)
# 모델 이름은 서비스 쪽에서 바뀔 수 있어 환경변수로 덮어쓸 수 있게 해 둠. 후보가 여러 개면 앞에서부터 시도.
VOTERS = [
    ("Gemini", "gemini", "gemini",
     os.getenv("GEMINI_MODEL", "gemini-flash-latest,gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash").split(","),
     "normal"),
    ("GPT-OSS", "gptoss", "groq",
     [os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")], "normal"),
    ("Qwen", "qwen", "groq",
     [os.getenv("QWEN_MODEL", "qwen/qwen3.8-27b")], "normal"),
    # "auto" = OpenRouter 의 현재 무료 모델 목록에서 자동 선택 (무료 모델이 자주 바뀌어서)
    ("OpenRouter", "deepseek", "openrouter",
     os.getenv("OPENROUTER_MODELS", "auto").split(","), "normal"),
    ("반론위원", "skeptic", "gemini",
     [os.getenv("SKEPTIC_MODEL", "gemini-flash-lite-latest")], "skeptic"),
]
PROVIDER_KEYS = {"gemini": GEMINI_API_KEY, "groq": GROQ_API_KEY, "openrouter": OPENROUTER_API_KEY}

AI_LOG_FILE = Path(os.getenv("AI_LOG_FILE", "ai_trades.csv"))
AI_LOG_FIELDS = [
    "closed_at", "symbol", "direction", "entry_time", "exit_reason", "net_r", "outcome",
    "ai_verdict", "ai_enter_votes", "ai_valid_votes", "ai_avg_confidence",
]
for _name, _slug, *_ in VOTERS:
    AI_LOG_FIELDS += [f"{_slug}_decision", f"{_slug}_confidence"]


# ───────────────────────────── 응답 해석 ─────────────────────────────
def _parse_vote(text: str) -> Optional[Dict[str, Any]]:
    """추론 모델의 <think> 부분을 지우고, decision 이 들어 있는 마지막 JSON 객체를 찾음."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    for chunk in reversed(re.findall(r"\{[^{}]*\}", text)):
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        decision = str(data.get("decision", "")).strip().upper()
        if decision not in ("ENTER", "SKIP"):
            continue
        try:
            confidence = int(float(data.get("confidence", 0)))
        except (TypeError, ValueError):
            confidence = 0
        reason = str(data.get("reason", "")).strip()[:120]
        return {"decision": decision, "confidence": max(0, min(100, confidence)), "reason": reason}
    return None


# ───────────────────────────── 제공처별 호출 ─────────────────────────────
class AIError(Exception):
    """텔레그램에 짧게 보여줄 실패 이유를 담는 예외."""


def _http_error(status: int) -> AIError:
    labels = {400: "요청 형식 오류", 401: "키 오류", 402: "크레딧 부족", 403: "권한 없음",
              404: "모델 없음", 429: "한도 초과", 503: "서버 혼잡"}
    return AIError(f"{status} {labels.get(status, '오류')}")


def _call_gemini(model: str, system: str, prompt: str) -> Optional[str]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    response = requests.post(url, headers={"x-goog-api-key": GEMINI_API_KEY},
                             json=body, timeout=AI_TIMEOUT_SEC)
    if response.status_code != 200:
        logging.warning("Gemini(%s) 응답 오류: %s %s", model, response.status_code, response.text[:300])
        raise _http_error(response.status_code)
    parts = response.json()["candidates"][0]["content"]["parts"]
    return "".join(part.get("text", "") for part in parts if not part.get("thought"))


def _call_openai_style(url: str, key: str, label: str, model: str,
                       system: str, prompt: str) -> Optional[str]:
    body = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    response = requests.post(url, headers={"Authorization": f"Bearer {key}"},
                             json=body, timeout=AI_TIMEOUT_SEC)
    if response.status_code != 200:
        logging.warning("%s(%s) 응답 오류: %s %s", label, model, response.status_code, response.text[:300])
        raise _http_error(response.status_code)
    return response.json()["choices"][0]["message"].get("content") or ""


# 위원회에 이미 있는 회사(OpenAI·알리바바·구글)는 피하고, 다른 회사 모델을 우선
OPENROUTER_PREFERENCE = ["deepseek/", "mistralai/", "meta-llama/", "z-ai/", "moonshotai/",
                         "minimax/", "nvidia/", "microsoft/"]
OPENROUTER_AVOID = ("openai/", "qwen/", "google/")
_openrouter_cache: Optional[List[str]] = None


def _openrouter_free_models() -> List[str]:
    """OpenRouter 의 지금 살아 있는 무료(:free) 모델 중 위원회에 없는 회사 모델을 최대 3개 고름."""
    global _openrouter_cache
    if _openrouter_cache is not None:
        return _openrouter_cache
    found: List[str] = []
    try:
        response = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        ids = [str(m.get("id", "")) for m in response.json().get("data", [])]
        free = [i for i in ids if i.endswith(":free") and not i.startswith(OPENROUTER_AVOID)]
        for prefix in OPENROUTER_PREFERENCE:
            found += [i for i in free if i.startswith(prefix) and i not in found]
        found += [i for i in free if i not in found]
    except Exception as exc:
        logging.warning("OpenRouter 무료 모델 목록 조회 실패: %s", exc)
    _openrouter_cache = found[:3]
    logging.info("OpenRouter 무료 모델 후보: %s", _openrouter_cache)
    return _openrouter_cache


def _ask(provider: str, models: List[str], role: str, prompt: str) -> Dict[str, Any]:
    """성공하면 투표, 실패하면 {"error": 이유} 를 돌려줌."""
    system = SKEPTIC_PROMPT if role == "skeptic" else NORMAL_PROMPT
    error = "응답 없음"
    candidates = [m.strip() for m in models if m.strip()]
    if provider == "openrouter" and candidates == ["auto"]:
        candidates = _openrouter_free_models()
        if not candidates:
            return {"error": "무료 모델 없음"}
    queue = [(model, 0) for model in candidates]
    while queue:
        model, attempt = queue.pop(0)
        try:
            if provider == "gemini":
                text = _call_gemini(model, system, prompt)
            elif provider == "groq":
                text = _call_openai_style("https://api.groq.com/openai/v1/chat/completions",
                                          GROQ_API_KEY, "Groq", model, system, prompt)
            else:
                text = _call_openai_style("https://openrouter.ai/api/v1/chat/completions",
                                          OPENROUTER_API_KEY, "OpenRouter", model, system, prompt)
        except AIError as exc:
            error = str(exc)
            if error.startswith("503") and attempt == 0:   # 일시적 혼잡이면 잠깐 쉬고 한 번 더
                time.sleep(3)
                queue.insert(0, (model, 1))
            continue
        except requests.Timeout:
            logging.warning("AI 시간 초과 (%s %s, %s초)", provider, model, AI_TIMEOUT_SEC)
            error = "시간 초과"
            continue
        except Exception as exc:  # 네트워크·형식 오류 등은 다음 후보로
            logging.warning("AI 호출 실패 (%s %s): %s", provider, model, exc)
            error = "연결 오류"
            continue
        vote = _parse_vote(text or "")
        if vote:
            vote["model"] = model
            return vote
        logging.warning("AI 응답 해석 실패 (%s %s): %s", provider, model, (text or "")[:200])
        error = "답변 형식 오류" if text else "빈 답변"
    return {"error": error}


def _review(signal: Dict[str, Any]) -> Dict[str, Any]:
    active = [(name, provider, models, role) for name, _slug, provider, models, role in VOTERS
              if PROVIDER_KEYS.get(provider)]
    if not active:
        return {"verdict": "UNAVAILABLE", "enter": 0, "valid": 0, "total": 0,
                "avg_confidence": 0, "votes": {}}

    prompt = "아래 진입 후보를 심사하라.\n" + json.dumps(signal, ensure_ascii=False, indent=1)
    with ThreadPoolExecutor(max_workers=len(active)) as pool:
        futures = {name: pool.submit(_ask, provider, models, role, prompt)
                   for name, provider, models, role in active}
        votes = {name: future.result() for name, future in futures.items()}
    for name, provider, _models, _role in active:
        vote = votes.get(name) or {}
        if provider == "openrouter" and vote.get("model"):
            short = vote["model"].split("/")[-1].replace(":free", "")
            vote["label"] = f"{name}({short})"

    valid = [vote for vote in votes.values() if vote and "decision" in vote]
    enter = sum(1 for vote in valid if vote["decision"] == "ENTER")
    if not valid:
        verdict = "UNAVAILABLE"
    elif enter * 2 > len(valid):
        verdict = "APPROVE"      # 응답한 위원의 과반 찬성
    elif enter * 2 < len(valid):
        verdict = "REJECT"       # 과반 반대
    else:
        verdict = "SPLIT"        # 정확히 반반 (응답 수가 짝수일 때)
    avg_conf = round(sum(vote["confidence"] for vote in valid) / len(valid)) if valid else 0
    return {"verdict": verdict, "enter": enter, "valid": len(valid), "total": len(active),
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
        if not vote or "decision" not in vote:
            reason = (vote or {}).get("error", "")
            lines.append(f"• {name}: 응답 없음" + (f" ({html.escape(reason)})" if reason else ""))
            continue
        label = "진입" if vote["decision"] == "ENTER" else "보류"
        shown = html.escape(vote.get("label", name))
        lines.append(f"• {shown}: {label} {vote['confidence']} — {html.escape(vote['reason'])}")
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
        logging.info("AI review %s %s: %s %s/%s", symbol, direction,
                     review["verdict"], review["enter"], review["valid"])
        return "\n\n" + _format_block(review)
    except Exception:
        logging.exception("AI review failed for %s", symbol)
        return ""


# ───────────────────────────── 기록과 성과 ─────────────────────────────
def _ensure_log_header() -> bool:
    """예전 형식(위원 2명)의 기록 파일이 있으면 옆으로 옮기고 새 형식으로 시작. 파일이 이미 맞으면 True."""
    if not AI_LOG_FILE.exists() or AI_LOG_FILE.stat().st_size == 0:
        return False
    with AI_LOG_FILE.open("r", newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle), [])
    if header == AI_LOG_FIELDS:
        return True
    os.replace(AI_LOG_FILE, AI_LOG_FILE.with_name(AI_LOG_FILE.stem + "_old.csv"))
    return False


def log_closed_trade(symbol: str, info: Dict[str, Any], net_r: float, outcome: str,
                     reason: str, now: datetime) -> None:
    """포지션 종료 시 AI 판정과 실제 결과를 함께 기록. AI 검토가 없던 거래도 NONE 으로 남김."""
    try:
        review = info.get("ai") or {}
        votes = review.get("votes", {})
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
        }
        for name, slug, *_ in VOTERS:
            vote = votes.get(name) or {}
            row[f"{slug}_decision"] = vote.get("decision", "")
            row[f"{slug}_confidence"] = vote.get("confidence", "")
        exists = _ensure_log_header()
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
