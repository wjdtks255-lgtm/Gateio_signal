#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io 선물 계좌 실시간 확인 텔레그램 봇
- 버튼/명령어: 💰 잔고, 📊 포지션, 📅 오늘 손익
- 자동 알림: 포지션 진입·추가·부분청산·종료, 청산가 근접 경고, 계좌 평가금 큰 변동, 매일 아침 요약
- 계좌 '읽기'만 함. 조회 전용 API 키(선물 읽기 권한)만 있어도 동작.

환경변수
  ACCOUNT_TELEGRAM_TOKEN   새로 만든 텔레그램 봇 토큰 (@BotFather)
  ACCOUNT_TELEGRAM_CHAT_ID 내 채팅 ID (이 채팅에만 응답)
  GATE_API_KEY / GATE_API_SECRET
  GATE_LIVE=1              실계좌 (없으면 테스트넷)
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

os.environ.setdefault("LOG_FILE", "account_bot.log")
import requests  # noqa: E402

import auto_trader as gt  # noqa: E402  (Gate.io 서명 API 재사용)

TOKEN = os.getenv("ACCOUNT_TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("ACCOUNT_TELEGRAM_CHAT_ID", "").strip()
TG = f"https://api.telegram.org/bot{TOKEN}"

WATCH_SECONDS = 30                                          # 계좌 변화 확인 주기
EQUITY_ALERT_PCT = float(os.getenv("EQUITY_ALERT_PCT", "3"))  # 평가금이 이만큼(%) 움직이면 알림
LIQ_WARN_PCT = float(os.getenv("LIQ_WARN_PCT", "3"))          # 청산가까지 남은 거리(%)가 이 이하이면 경고
DAILY_SUMMARY_HOUR_KST = 9
KST = timezone(timedelta(hours=9))

KEYBOARD = {"keyboard": [[{"text": "💰 잔고"}, {"text": "📊 포지션"}, {"text": "📅 오늘 손익"}]],
            "resize_keyboard": True}


# ───────────────────────────── 텔레그램 ─────────────────────────────
def send(text: str) -> None:
    try:
        requests.post(f"{TG}/sendMessage", json={
            "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True, "reply_markup": KEYBOARD}, timeout=15)
    except requests.RequestException:
        logging.exception("telegram send failed")


def get_updates(offset: int) -> List[Dict[str, Any]]:
    try:
        r = requests.get(f"{TG}/getUpdates", params={"offset": offset, "timeout": 20}, timeout=30)
        return r.json().get("result", []) if r.ok else []
    except requests.RequestException:
        return []


# ───────────────────────────── 계좌 조회 ─────────────────────────────
def f(value: Any) -> float:
    return gt.bot.safe_float(value)


def account() -> Dict[str, Any]:
    return gt.get_account()


def positions() -> Dict[str, Dict[str, Any]]:
    data = gt.gate("GET", f"/futures/{gt.SETTLE}/positions") or []
    return {p["contract"]: p for p in data if int(f(p.get("size"))) != 0}


def equity_of(acc: Dict[str, Any]) -> float:
    return f(acc.get("total")) + f(acc.get("unrealised_pnl"))


def pnl_since(start: datetime) -> Dict[str, float]:
    """기간 내 실현손익·수수료·펀딩비 합계 (account_book)."""
    sums = {"pnl": 0.0, "fee": 0.0, "fund": 0.0}
    try:
        rows = gt.gate("GET", f"/futures/{gt.SETTLE}/account_book",
                       {"from": int(start.timestamp()), "limit": 1000}) or []
        for row in rows:
            kind = row.get("type")
            if kind in sums:
                sums[kind] += f(row.get("change"))
    except gt.GateError:
        logging.exception("account_book failed")
    return sums


def side(size: float) -> str:
    return "🟢 롱" if size > 0 else "🔴 숏"


def liq_distance_pct(p: Dict[str, Any]) -> Optional[float]:
    mark, liq = f(p.get("mark_price")), f(p.get("liq_price"))
    if mark <= 0 or liq <= 0:
        return None
    return abs(mark - liq) / mark * 100.0


# ───────────────────────────── 메시지 ─────────────────────────────
def balance_text() -> str:
    acc = account()
    total, upnl = f(acc.get("total")), f(acc.get("unrealised_pnl"))
    return (
        "💰 <b>계좌 잔고</b>\n"
        f"평가금  <code>{total + upnl:,.2f} USDT</code>\n"
        f"지갑잔고 <code>{total:,.2f}</code>\n"
        f"미실현손익 <code>{upnl:+,.2f}</code>\n"
        f"사용 가능 <code>{f(acc.get('available')):,.2f}</code>\n"
        f"포지션 증거금 <code>{f(acc.get('position_margin')):,.2f}</code> · "
        f"주문 증거금 <code>{f(acc.get('order_margin')):,.2f}</code>"
    )


def positions_text() -> str:
    pos = positions()
    if not pos:
        return "📊 열린 포지션이 없습니다."
    lines = [f"📊 <b>포지션 {len(pos)}개</b>"]
    total_upnl = 0.0
    for sym, p in pos.items():
        size = f(p.get("size"))
        entry, mark = f(p.get("entry_price")), f(p.get("mark_price"))
        upnl = f(p.get("unrealised_pnl"))
        margin = f(p.get("margin")) or 1e-9
        total_upnl += upnl
        dist = liq_distance_pct(p)
        lines.append(
            f"\n<b>{sym}</b> {side(size)} {int(abs(size))}계약 · {p.get('leverage') or '교차'}배\n"
            f"진입 <code>{gt.bot.fmt_price(entry)}</code> → 현재 <code>{gt.bot.fmt_price(mark)}</code>\n"
            f"손익 <code>{upnl:+,.2f} USDT ({upnl / margin * 100:+.1f}%)</code>\n"
            f"청산가 <code>{gt.bot.fmt_price(f(p.get('liq_price')))}</code>"
            + (f" (남은 거리 {dist:.1f}%)" if dist is not None else "")
        )
    lines.append(f"\n합계 미실현손익 <code>{total_upnl:+,.2f} USDT</code>")
    return "\n".join(lines)


def today_text() -> str:
    now_kst = datetime.now(KST)
    start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    s = pnl_since(start)
    net = s["pnl"] + s["fee"] + s["fund"]
    return (
        f"📅 <b>오늘 손익</b> ({start:%m/%d} 00시 KST부터)\n"
        f"실현손익 <code>{s['pnl']:+,.2f}</code>\n"
        f"수수료 <code>{s['fee']:+,.2f}</code> · 펀딩비 <code>{s['fund']:+,.2f}</code>\n"
        f"순손익 <code>{net:+,.2f} USDT</code>"
    )


def handle(text: str) -> None:
    text = (text or "").strip()
    if text.startswith("/"):
        text = text.split()[0].split("@")[0]  # 그룹에서 오는 /balance@봇이름 형태 처리
    if text in ("/start", "/help"):
        send("🤖 계좌 확인 봇입니다.\n아래 버튼을 누르거나 /balance /positions /today 를 입력하세요.\n"
             "포지션 변화·청산 위험·큰 손익 변동은 자동으로 알려 드립니다.")
    elif text in ("💰 잔고", "/balance"):
        send(balance_text())
    elif text in ("📊 포지션", "/positions"):
        send(positions_text())
    elif text in ("📅 오늘 손익", "/today"):
        send(today_text())


# ───────────────────────────── 변화 감시 ─────────────────────────────
class Watcher:
    def __init__(self) -> None:
        self.prev = positions()
        self.equity_base = equity_of(account())
        self.liq_warned: Dict[str, bool] = {}
        self.last_check = datetime.now(timezone.utc)
        self.summary_date = None

    def check(self) -> None:
        now = datetime.now(timezone.utc)
        cur = positions()
        acc = account()

        for sym, p in cur.items():
            size = f(p.get("size"))
            old = self.prev.get(sym)
            if old is None:
                send(f"🆕 <b>포지션 진입</b> {sym} {side(size)}\n"
                     f"{int(abs(size))}계약 · 진입가 <code>{gt.bot.fmt_price(f(p.get('entry_price')))}</code>"
                     f" · {p.get('leverage') or '교차'}배")
            else:
                old_size = f(old.get("size"))
                if abs(size) > abs(old_size):
                    send(f"➕ <b>추가 진입</b> {sym} {int(abs(old_size))} → {int(abs(size))}계약")
                elif abs(size) < abs(old_size):
                    send(f"➖ <b>부분 청산</b> {sym} {int(abs(old_size))} → {int(abs(size))}계약\n"
                         f"현재 손익 <code>{f(p.get('unrealised_pnl')):+,.2f} USDT</code>")
            dist = liq_distance_pct(p)
            if dist is not None and dist <= LIQ_WARN_PCT and not self.liq_warned.get(sym):
                send(f"🚨 <b>청산 위험</b> {sym}\n청산가까지 {dist:.1f}% 남았습니다. "
                     f"(현재 <code>{gt.bot.fmt_price(f(p.get('mark_price')))}</code> / "
                     f"청산가 <code>{gt.bot.fmt_price(f(p.get('liq_price')))}</code>)")
                self.liq_warned[sym] = True
            elif dist is not None and dist > LIQ_WARN_PCT * 2:
                self.liq_warned[sym] = False

        closed = [sym for sym in self.prev if sym not in cur]
        if closed:
            s = pnl_since(self.last_check - timedelta(seconds=WATCH_SECONDS))
            for sym in closed:
                old = self.prev[sym]
                send(f"✅ <b>포지션 종료</b> {sym} {side(f(old.get('size')))}\n"
                     f"실현손익(최근 확인 이후 합계) <code>{s['pnl'] + s['fee']:+,.2f} USDT</code>\n"
                     f"지갑잔고 <code>{f(acc.get('total')):,.2f} USDT</code>")
                self.liq_warned.pop(sym, None)

        equity = equity_of(acc)
        if self.equity_base > 0:
            move = (equity - self.equity_base) / self.equity_base * 100.0
            if abs(move) >= EQUITY_ALERT_PCT:
                icon = "📈" if move > 0 else "📉"
                send(f"{icon} <b>평가금 변동 {move:+.1f}%</b>\n"
                     f"<code>{self.equity_base:,.2f} → {equity:,.2f} USDT</code>")
                self.equity_base = equity

        now_kst = now.astimezone(KST)
        if now_kst.hour == DAILY_SUMMARY_HOUR_KST and self.summary_date != now_kst.date():
            self.summary_date = now_kst.date()
            yesterday = (now_kst - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            s = pnl_since(yesterday)
            send(f"🌅 <b>아침 요약</b>\n{balance_text()}\n\n"
                 f"어제 0시부터 순손익 <code>{s['pnl'] + s['fee'] + s['fund']:+,.2f} USDT</code>\n\n"
                 f"{positions_text()}")

        self.prev = cur
        self.last_check = now


def main() -> None:
    if not TOKEN or not CHAT_ID:
        raise SystemExit("ACCOUNT_TELEGRAM_TOKEN / ACCOUNT_TELEGRAM_CHAT_ID 환경변수를 설정하세요.")
    if not gt.API_KEY or not gt.API_SECRET:
        raise SystemExit("GATE_API_KEY / GATE_API_SECRET 환경변수를 설정하세요.")
    watcher = Watcher()
    send(f"🤖 계좌 확인 봇 시작 ({'실계좌' if gt.LIVE else '테스트넷'})\n\n{balance_text()}")
    offset, last_watch = 0, 0.0
    while True:
        for upd in get_updates(offset):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("channel_post") or {}  # 개인·그룹·채널 모두 지원
            if str(msg.get("chat", {}).get("id")) != CHAT_ID:
                continue  # 내 채팅이 아니면 무시
            try:
                handle(msg.get("text", ""))
            except Exception:
                logging.exception("command failed")
                send("⚠️ 조회 중 오류가 났습니다. 잠시 후 다시 시도하세요.")
        if time.time() - last_watch >= WATCH_SECONDS:
            last_watch = time.time()
            try:
                watcher.check()
            except Exception:
                logging.exception("watch failed")


if __name__ == "__main__":
    main()
