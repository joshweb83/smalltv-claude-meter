# -*- coding: utf-8 -*-
"""
smalltv-claude-meter — Claude 사용량 모니터 (GeekMagic SmallTV Ultra)
---------------------------------------------------------------------
PC가 Claude 플랜 사용량(5시간 세션 / 주간 한도)을 읽어 240x240 화면을 그린 뒤,
같은 네트워크의 SmallTV Ultra에 1분마다 올립니다. 장비 펌웨어는 건드리지 않습니다.

비공식 프로젝트입니다. Anthropic과 무관하며, 사용하는 사용량 조회 경로는
공개 API가 아니어서 예고 없이 바뀔 수 있습니다.

실행:
  python claude_meter.py --install    # 설치(자동 시작 등록 + 실행)
  python claude_meter.py --uninstall  # 끄기 + 자동 시작 해제 + 장비 원래 화면
  python claude_meter.py --check      # 진단
  python claude_meter.py --preview    # 장비 전송 없이 preview.jpg 만 저장
  python claude_meter.py --demo       # 가짜 데이터로 화면 확인
"""
import argparse
import io
import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
SCRIPT_MTIME = Path(__file__).stat().st_mtime
CONFIG_PATH = HERE / "config.json"
DEFAULT_CONFIG = {
    "device_ip": "",             # 장비 IP (장비 부팅 화면 또는 공유기에서 확인)
    "interval_seconds": 60,      # 장비 업로드 간격(초)
    "usage_refresh_seconds": 900,  # 사용량 API 재조회 간격(초, 최소 600) — 잦으면 429 차단
    "layout": "mascot",          # mascot: 마스코트 포함 / ring: 원형 게이지 / countdown: 남은 시간 크게
    "image_name": "claude",      # 장비에 올라가는 파일 이름(claude_a.jpg / claude_b.jpg)
    "brightness": None,
    "rotate": False,             # True: 시계·날씨와 Claude 화면을 번갈아 표시
    "rotate_themes": [1, 3],     # 1=시계·날씨, 2=일기예보, 3=Claude(사진앨범), 4~7=시계 스타일
    "rotate_seconds": 15,        # 화면당 표시 시간(초)
    "session_key": "",           # claude.ai 쿠키 sessionKey (set_session.bat 으로 입력)
    "web_refresh_seconds": 300,  # claude.ai 사용량 조회 간격(초)
    "org_id": "",
}

W = H = 240
S = 2                            # 2배로 그린 뒤 축소
# Claude 아이덴티티 팔레트: 따뜻한 다크 배경 + 아이보리 글자 + 클레이 오렌지 포인트
BG = (31, 30, 29)                # #1F1E1D 다크
FG = (250, 249, 245)             # #FAF9F5 아이보리
DIM = (156, 153, 143)            # 웜 그레이
TRACK = (55, 53, 49)             # 게이지 바탕
ACCENT = (217, 119, 87)          # #D97757 Claude 클레이 오렌지
GREEN = ACCENT                   # 여유 → Claude 오렌지
AMBER = ACCENT                   # 주의 → Claude 오렌지(배지 문구로 구분)
RED = (224, 72, 60)              # 한도 임박(85%↑)만 경고 레드
FIVE_H = 5 * 3600


# ---------------------------------------------------------------- config
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"[안내] 기본 설정 파일을 만들었습니다: {CONFIG_PATH}")
    # 예전 설정(claude.jpg) 호환
    cfg["image_name"] = Path(cfg.get("image_name") or "claude").stem
    return cfg


def save_config(cfg):
    keep = {k: cfg.get(k, v) for k, v in DEFAULT_CONFIG.items()}
    keep["image_name"] = cfg.get("image_name", "claude")
    try:
        CONFIG_PATH.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[경고] config.json 저장 실패: {e}")


# ---------------------------------------------------------------- usage
# 사용량 출처(우선순위)
#  1) Claude Code 상태표시줄(statusLine) — Claude Code가 응답할 때마다 사용량을
#     넘겨줌. 네트워크 호출이 없어 차단(429) 걱정이 없음. --install-statusline 으로 설정
#  2) OAuth 사용량 API — 비공개 API라 자주 부르면 429로 차단됨. 10분 이상 간격 + 실패 시 대기
#  3) claude.ai sessionKey (config.json에 넣은 경우)
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
STATUS_FILE = CLAUDE_DIR / "claude-meter-statusline.json"
API_CACHE = CLAUDE_DIR / "claude-meter-api-cache.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def _read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(p, obj):
    try:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(p) + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        print(f"[경고] 캐시 저장 실패: {e}")


def _claude_code_token():
    data = _read_json(CLAUDE_DIR / ".credentials.json") or {}
    return data.get("claudeAiOauth", {}).get("accessToken")


def fetch_via_claude_code(token):
    r = requests.get(
        "https://api.anthropic.com/api/oauth/usage",
        headers={"Authorization": f"Bearer {token}",
                 "anthropic-beta": "oauth-2025-04-20",
                 "User-Agent": "claude-meter/1.2"},
        timeout=20)
    if r.status_code == 429:
        ra = r.headers.get("retry-after")
        err = RuntimeError("429 사용량 API 호출 제한")
        err.retry_after = int(ra) if ra and ra.isdigit() and int(ra) > 0 else None
        raise err
    r.raise_for_status()
    return r.json()


def fetch_via_session_key(cfg):
    """Claude 앱·웹이 쓰는 claude.ai 사용량 경로(설정 > 사용량 화면과 같은 값)."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json",
                      "Referer": "https://claude.ai/settings/usage",
                      "Origin": "https://claude.ai",
                      "anthropic-client-platform": "web_claude_ai"})
    s.cookies.set("sessionKey", cfg["session_key"].strip(), domain="claude.ai")

    def get(url):
        r = s.get(url, timeout=20)
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            err = RuntimeError("429 claude.ai 호출 제한")
            err.retry_after = int(ra) if ra and ra.isdigit() and int(ra) > 0 else None
            raise err
        if r.status_code in (401, 403):
            cf = "cloudflare" in r.text.lower() or "just a moment" in r.text.lower()
            raise RuntimeError("Cloudflare 차단(403)" if cf else
                               f"HTTP {r.status_code} 세션키 확인 필요")
        r.raise_for_status()
        return r.json()

    org = cfg.get("org_id")
    if not org:
        orgs = get("https://claude.ai/api/organizations")
        pick = next((o for o in orgs if "chat" in (o.get("capabilities") or [])), orgs[0])
        org = cfg["org_id"] = pick["uuid"]
        save_config(cfg)
    data = get(f"https://claude.ai/api/organizations/{org}/usage")
    new_key = s.cookies.get("sessionKey", domain="claude.ai") or s.cookies.get("sessionKey")
    if new_key and new_key != cfg["session_key"].strip():     # 세션키 자동 갱신 반영
        cfg["session_key"] = new_key
        save_config(cfg)
        print("[갱신] claude.ai 세션키가 바뀌어 config.json에 저장했습니다.")
    return data


def _from_statusline():
    """상태표시줄 훅이 남긴 파일 → 내부 형식."""
    st = _read_json(STATUS_FILE)
    if not st or not st.get("rate_limits"):
        return None
    out = {}
    for k, blk in st["rate_limits"].items():
        if not isinstance(blk, dict) or blk.get("used_percentage") is None:
            continue
        ra = blk.get("resets_at")
        out[k] = {"utilization": blk["used_percentage"],
                  "resets_at": (datetime.fromtimestamp(ra, timezone.utc).isoformat()
                                if isinstance(ra, (int, float)) else ra)}
    return (st.get("t", 0), out, "Claude Code") if out else None


class UsageUnavailable(RuntimeError):
    pass


def get_usage(cfg, demo=False):
    """(usage, source, age_seconds) 반환. 실패해도 마지막 값을 최대한 유지."""
    if demo:
        u, src = demo_usage()
        return u, src, 0
    now = time.time()
    sl = _from_statusline()
    cache = _read_json(API_CACHE) or {}
    has_key = bool(cfg.get("session_key", "").strip())
    # claude.ai(앱·웹 경로)는 5분, OAuth API(429가 잦음)는 최소 10분 간격
    refresh = (max(180, int(cfg.get("web_refresh_seconds", 300))) if has_key else
               max(600, int(cfg.get("usage_refresh_seconds", 900))))
    sl_fresh = sl and now - sl[0] < 600

    api_due = (now - cache.get("t", 0) >= refresh and now >= cache.get("backoff_until", 0))
    if not sl_fresh and api_due:
        token = _claude_code_token()
        try:
            if has_key:
                data, src = fetch_via_session_key(cfg), "claude.ai"
            elif token:
                data, src = fetch_via_claude_code(token), "Claude Code"
            else:
                data = None
            if data is not None:
                cache = {"t": now, "data": data, "source": src, "fails": 0}
                _write_json(API_CACHE, cache)
                print(f"[{datetime.now():%H:%M:%S}] 사용량 API 조회 성공 ({src})")
        except Exception as e:
            fails = cache.get("fails", 0) + 1
            wait = getattr(e, "retry_after", None) or min(3600, 600 * 2 ** (fails - 1))
            code = getattr(getattr(e, "response", None), "status_code", None)
            short = (f"HTTP {code}" + (" 로그인 만료" if code == 401 else "")) if code else str(e)[:34]
            cache.update(fails=fails, backoff_until=now + wait, last_error=short)
            _write_json(API_CACHE, cache)
            print(f"[{datetime.now():%H:%M:%S}] 사용량 API 실패: {e} → {wait // 60}분 뒤 재시도")

    cands = []
    if sl:
        cands.append(sl)
    if cache.get("data"):
        cands.append((cache["t"], cache["data"], cache.get("source", "API")))
    if cands:
        t, data, src = max(cands, key=lambda c: c[0])
        return data, src, int(now - t)

    if not _claude_code_token() and not cfg.get("session_key"):
        raise UsageUnavailable("set_session.bat 으로 sessionKey 설정 필요")
    left = int(cache.get("backoff_until", now) - now)
    msg = cache.get("last_error", "조회 대기 중")
    if left > 0:
        msg += f" · {left // 60 + 1}분 뒤 재시도"
    raise UsageUnavailable(msg)


def demo_usage():
    now = time.time()
    iso = lambda s: datetime.fromtimestamp(now + s, timezone.utc).isoformat()
    return {
        "five_hour": {"utilization": 63.0, "resets_at": iso(2 * 3600 + 14 * 60 + 37)},
        "seven_day": {"utilization": 41.0, "resets_at": iso(3 * 86400 + 5 * 3600)},
        "seven_day_opus": {"utilization": 88.0, "resets_at": iso(3 * 86400 + 5 * 3600)},
    }, "DEMO"


# ---------------------------------------------------------------- helpers
_FONT_CACHE = {}


def _font(size, bold=False):
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    names = (["malgunbd.ttf", "NotoSansCJK-Bold.ttc", "NotoSansKR-Bold.ttf"]
             if bold else
             ["malgun.ttf", "NotoSansCJK-Regular.ttc", "NotoSansKR-Regular.ttf"])
    dirs = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
            Path("/usr/share/fonts/opentype/noto"), Path("/System/Library/Fonts"), HERE]
    font = None
    for d in dirs:
        for n in names:
            p = d / n
            if p.exists():
                try:
                    font = ImageFont.truetype(str(p), size, index=1 if p.suffix == ".ttc" else 0)
                except Exception:
                    font = ImageFont.truetype(str(p), size)
                break
        if font:
            break
    font = font or ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def F(size, bold=False):
    return _font(int(round(size * 1.2 * S)), bold)      # 서체 20% 확대 유지


def _color(pct):
    return GREEN if pct < 60 else AMBER if pct < 85 else RED


def _parse(ts):
    """ISO 문자열(소수점 자릿수 무관)·epoch 숫자 모두 처리 — 구버전 Python 호환."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.replace(".", "", 1).isdigit()):
        v = float(ts)
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc)
    import re
    s = str(ts).strip().replace("Z", "+00:00")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?([+-]\d{2}:?\d{2})?$", s)
    if not m:
        return None
    date, hms, frac, tz = m.groups()
    frac = (frac or "0")[:6].ljust(6, "0")
    tz = tz or "+00:00"
    if ":" not in tz:
        tz = tz[:3] + ":" + tz[3:]
    try:
        return datetime.fromisoformat(f"{date}T{hms}.{frac}{tz}")
    except ValueError:
        return None


def _left_text(resets_at, now):
    t = _parse(resets_at)
    if not t:
        return ""
    sec = max(0, int((t - now).total_seconds()))
    d, rem = divmod(sec, 86400)
    h = rem // 3600
    return f"{d}일 {h}시간" if d else f"{h}시간 {(rem % 3600) // 60}분"


def _pct(block):
    if not block:
        return None
    v = block.get("utilization")
    return None if v is None else max(0.0, min(100.0, float(v)))


def _text_c(d, cx, y, text, font, fill):
    d.text((cx - d.textlength(text, font=font) / 2, y), text, font=font, fill=fill)


def _mono(d, cx, y, text, font, fill, colon_fill=None):
    """숫자 폭을 고정해 초가 바뀌어도 글자가 흔들리지 않게 그림."""
    dw = max(d.textlength(c, font=font) for c in "0123456789")
    cw = d.textlength(":", font=font) * 1.15
    widths = [cw if c == ":" else dw for c in text]
    x = cx - sum(widths) / 2
    for c, w in zip(text, widths):
        fl = colon_fill if (c == ":" and colon_fill) else fill
        d.text((x + (w - d.textlength(c, font=font)) / 2, y), c, font=font, fill=fl)
        x += w


def _bar(d, x0, y, x1, h, pct, color):
    d.rounded_rectangle((x0, y, x1, y + h), radius=h // 2, fill=TRACK)
    if pct:
        w = max(h, (x1 - x0) * pct / 100)
        d.rounded_rectangle((x0, y, x0 + w, y + h), radius=h // 2, fill=color)


# ---------------------------------------------------------------- drawing
def render(usage, source, now=None, error=None, seconds=True):
    now = now or datetime.now(timezone.utc)
    img = Image.new("RGB", (W * S, H * S), BG)
    d = ImageDraw.Draw(img)
    s = lambda v: v * S

    # 헤더
    d.ellipse((s(12), s(12), s(24), s(24)), fill=ACCENT)
    d.text((s(31), s(5)), "Claude", font=F(15, True), fill=FG)
    clock = now.astimezone().strftime("%H:%M")
    d.text((s(W - 12) - d.textlength(clock, font=F(12)), s(8)), clock, font=F(12), fill=DIM)

    if error:
        limited = "429" in str(error) or "대기" in str(error)
        _text_c(d, s(W / 2), s(80), "조회 대기 중" if limited else "연결 오류",
                F(18, True), AMBER if limited else RED)
        msg = str(error)[:54]
        for i in range(0, len(msg), 18):
            _text_c(d, s(W / 2), s(118 + i // 18 * 19), msg[i:i + 18], F(11), DIM)
        return img.resize((W, H), Image.LANCZOS)

    five = usage.get("five_hour") or {}
    week = usage.get("seven_day") or {}
    p5 = _pct(five)
    reset = _parse(five.get("resets_at"))
    remain = max(0, int((reset - now).total_seconds())) if reset else None
    active = remain is not None and remain > 0
    if reset and not active:
        p5 = 0.0                 # 리셋 시각이 지났으면 세션 사용량은 0
    c5 = _color(p5) if (p5 is not None and active) else DIM

    # 라벨
    label = "5시간 세션 리셋까지" if active else "5시간 세션"
    d.text((s(14), s(36)), label, font=F(11, True), fill=DIM)

    # 큰 카운트다운 (콜론은 짝수 초에 흐리게 → 살아 있는 느낌)
    if active:
        h, rem = divmod(remain, 3600)
        m, sec = divmod(rem, 60)
        if seconds:
            txt = f"{h}:{m:02d}:{sec:02d}"
            colon = FG if sec % 2 else (110, 110, 120)
            _mono(d, s(W / 2), s(52), txt, F(38, True), FG, colon)
        else:                                   # 정지 이미지: 분 단위로 크게
            m = (rem + 59) // 60                # 올림(0:00 표시 방지)
            if m == 60:
                h, m = h + 1, 0
            _mono(d, s(W / 2 - 14), s(46), f"{h}:{m:02d}", F(46, True), FG)
            d.text((s(W / 2 + 62), s(80)), "남음", font=F(12, True), fill=DIM)
    else:
        _text_c(d, s(W / 2), s(50), "대기 중", F(26, True), DIM)
        why = ("새 세션 정보 기다리는 중" if reset else "최근 5시간 사용 없음")
        _text_c(d, s(W / 2), s(88), why, F(10), DIM)

    # 남은 시간 진행선(5시간 중 경과 비율)
    if active:
        elapsed = 100 * (1 - remain / FIVE_H)
        _bar(d, s(14), s(112), s(W - 14), s(4), max(0, min(100, elapsed)), (70, 70, 80))

    # 세션 사용량
    y = 126
    d.text((s(14), s(y)), "세션 사용", font=F(13, True), fill=FG)
    status = ("" if p5 is None else
              "여유" if p5 < 60 else "주의" if p5 < 85 else "한도 임박")
    val = "--" if p5 is None else f"{p5:.0f}%"
    vw = d.textlength(val, font=F(13, True))
    d.text((s(W - 14) - vw, s(y)), val, font=F(13, True), fill=c5 if p5 is not None else DIM)
    if status:
        sw = d.textlength(status, font=F(10, True))
        bx1 = s(W - 14) - vw - s(6)
        d.rounded_rectangle((bx1 - sw - s(12), s(y + 2), bx1, s(y + 19)), radius=s(8), fill=c5)
        d.text((bx1 - sw - s(6), s(y + 2)), status, font=F(10, True), fill=BG)
    _bar(d, s(14), s(y + 24), s(W - 14), s(8), p5, c5)

    # 구분선
    d.line((s(14), s(168), s(W - 14), s(168)), fill=TRACK, width=S)

    # 주간 (전체 + 모델별 중 1개)
    rows = [("주간", week)]
    for key, lb in (("seven_day_opus", "Opus"), ("seven_day_sonnet", "Sonnet")):
        if _pct(usage.get(key)) is not None:
            rows.append((lb, usage[key]))
            break
    lw = d.textlength("Sonnet", font=F(12, True)) + s(8)
    vx = d.textlength("100%", font=F(12, True)) + s(8)
    y = 175
    for lb, blk in rows:
        p = _pct(blk)
        c = DIM if p is None else _color(p)
        d.text((s(14), s(y - 3)), lb, font=F(12, True), fill=FG)
        v = "--" if p is None else f"{p:.0f}%"
        d.text((s(W - 14) - d.textlength(v, font=F(12, True)), s(y - 3)), v,
               font=F(12, True), fill=c)
        _bar(d, s(14) + lw, s(y + 5), s(W - 14) - vx, s(7), p, c)
        y += 21

    # 하단 (프레임에 가리지 않도록 y≤218)
    wl = _left_text(week.get("resets_at"), now)
    foot = "  ·  ".join(t for t in ((f"주간 리셋 {wl}" if wl else ""), source) if t)
    _text_c(d, s(W / 2), s(int(y) + 1), foot, F(10), DIM)
    return img.resize((W, H), Image.LANCZOS)


def _ring(d, cx, cy, r, width, pct, color):
    box = (cx - r, cy - r, cx + r, cy + r)
    d.ellipse(box, outline=TRACK, width=width)
    if not pct:
        return
    start, end = -90, -90 + 360 * pct / 100
    d.arc(box, start, end, fill=color, width=width)
    rc = r - width / 2                       # 둥근 끝 처리
    for ang in (start, end):
        a = math.radians(ang)
        x, y = cx + rc * math.cos(a), cy + rc * math.sin(a)
        d.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=color)


PIXEL_ICON = [            # 원본 픽셀 아이콘(막대 계기판) — 7x7
    ".....##",
    ".....##",
    "...####",
    "...####",
    ".######",
    "#######",
    "#######",
]


def _px(size, bold=True):
    return _font(int(round(size * S)), bold)


def _pixel_icon(d, x, y, cell, color):
    for r, row in enumerate(PIXEL_ICON):
        for c, ch in enumerate(row):
            if ch == "#":
                d.rectangle((x + c * cell, y + r * cell, x + (c + 1) * cell - 1,
                             y + (r + 1) * cell - 1), fill=color)


def render_ring(usage, source, now=None):
    """원형 게이지 디자인(큰 글자 버전): 5시간 세션 + 남은 시간 + 주간 막대."""
    now = now or datetime.now(timezone.utc)
    img = Image.new("RGB", (W * S, H * S), BG)
    d = ImageDraw.Draw(img)
    s = lambda v: v * S

    # 헤더: 픽셀 아이콘(갱신마다 밝기 교차 = 하트비트) + Claude + 시각
    global _BEAT
    _BEAT = not globals().get("_BEAT", False)
    icon_c = ACCENT if _BEAT else (170, 92, 66)
    _pixel_icon(d, s(12), s(13), s(3), icon_c)
    d.text((s(38), s(6)), "Claude", font=_px(22), fill=FG)
    clock = now.astimezone().strftime("%H:%M")
    d.text((s(W - 12) - d.textlength(clock, font=_px(16, False)), s(10)), clock,
           font=_px(16, False), fill=DIM)

    five = usage.get("five_hour") or {}
    week = usage.get("seven_day") or {}
    p5 = _pct(five)
    reset = _parse(five.get("resets_at"))
    if reset and reset <= now:
        p5 = 0.0                             # 리셋 지났으면 0%
    c5 = DIM if p5 is None else _color(p5)

    # 5시간 세션 원형 게이지
    cx, cy, r, rw = 60, 98, 48, 11
    _ring(d, s(cx), s(cy), s(r), s(rw), p5, c5)
    # 시간 진행 흰 점
    if reset and reset > now:
        frac = max(0.0, min(1.0, 1 - (reset - now).total_seconds() / FIVE_H))
        a = math.radians(-90 + 360 * frac)
        rc, dr = s(r) - s(rw) / 2, s(6.5)
        px, py = s(cx) + rc * math.cos(a), s(cy) + rc * math.sin(a)
        d.ellipse((px - dr, py - dr, px + dr, py + dr), fill=FG, outline=BG, width=S * 2)
    num = "--" if p5 is None else f"{p5:.0f}"
    fb, fu = _px(36), _px(15)
    nw, uw = d.textlength(num, font=fb), d.textlength("%", font=fu)
    x = s(cx) - (nw + uw + s(1)) / 2
    d.text((x, s(cy - 25)), num, font=fb, fill=FG)
    d.text((x + nw + s(1), s(cy - 1)), "%", font=fu, fill=FG)

    # 오른쪽: 5시간 세션 / 남은 시간 / 상태
    x0 = 120
    d.text((s(x0), s(50)), "5시간 세션", font=_px(17), fill=FG)
    if reset and reset > now:
        rem = int((reset - now).total_seconds())
        h, m = divmod((rem + 59) // 60, 60)
        cd, cc = f"{h}:{m:02d}", FG
    else:
        cd, cc = "-:--", DIM
    fcd = _px(36)
    d.text((s(x0 - 1), s(68)), cd, font=fcd, fill=cc)
    d.text((s(x0) + d.textlength(cd, font=fcd) + s(6), s(88)), "남음",
           font=_px(14, False), fill=DIM)
    status = "여유" if p5 is None or p5 < 60 else "주의" if p5 < 85 else "한도 임박"
    fs = _px(15)
    tw = d.textlength(status, font=fs)
    d.rounded_rectangle((s(x0), s(120), s(x0) + tw + s(26), s(144)), radius=s(12), fill=c5)
    d.text((s(x0 + 13), s(121)), status, font=fs, fill=BG)

    d.line((s(14), s(156), s(W - 14), s(156)), fill=TRACK, width=S)

    # 주간(+모델별) 막대
    rows = [("주간", week)]
    for key, label in (("seven_day_opus", "Opus"), ("seven_day_sonnet", "Sonnet")):
        if _pct(usage.get(key)) is not None:
            rows.append((label, usage[key]))
    rows = rows[:2]
    one = len(rows) == 1
    fl, fv = (_px(22), _px(20)) if one else (_px(14), _px(14))
    y, step, bh = (164, 0, 9) if one else (159, 18, 7)
    for label, blk in rows:
        p = _pct(blk)
        c = DIM if p is None else _color(p)
        val = "--" if p is None else f"{p:.0f}%"
        d.text((s(14), s(y)), label, font=fl, fill=FG)
        vx = s(W - 14) - d.textlength(val, font=fv)
        d.text((vx, s(y + (1 if one else 0))), val, font=fv, fill=c)
        bx0 = s(14) + d.textlength("Sonnet" if not one else "주간", font=fl) + s(14)
        bx1 = s(W - 14) - d.textlength("100%", font=fv) - s(10)
        by = s(y + (10 if one else 7))
        _bar(d, bx0, by, bx1, s(bh), p, c)
        wr = _parse((blk or {}).get("resets_at")) or _parse(week.get("resets_at"))
        if wr and wr > now:
            frac = max(0.0, min(1.0, 1 - (wr - now).total_seconds() / (7 * 86400)))
            px, py, dr = bx0 + (bx1 - bx0) * frac, by + s(bh) / 2, s(6)
            d.ellipse((px - dr, py - dr, px + dr, py + dr), fill=FG, outline=BG, width=S * 2)
        y += step

    wl = _left_text(week.get("resets_at"), now)
    if wl:
        fy, ff = (198, _px(14, False)) if one else (200, _px(12, False))
        _text_c(d, s(W / 2), s(fy), f"주간 리셋 {wl} 남음", ff, DIM)
    return img.resize((W, H), Image.LANCZOS)


SLIME = [                # 원본 픽셀 슬라임(14x10)
    "....######....",
    "..##########..",
    ".############.",
    ".############.",
    "##############",
    "##############",
    "##############",
    "##############",
    ".############.",
    "..##..##..##..",
]
SWEAT = (140, 190, 230)


def _slime(d, x, y, c, mood, beat):
    """mood: 0 여유(웃음) / 1 주의(무표정+땀) / 2 한도 임박(찡그림+땀)."""
    y += 0 if beat else c // 2                     # 갱신마다 살짝 통통 튀는 느낌
    body = ACCENT if mood < 2 else RED
    for r, row in enumerate(SLIME):
        for k, ch in enumerate(row):
            if ch == "#":
                d.rectangle((x + k * c, y + r * c, x + (k + 1) * c - 1, y + (r + 1) * c - 1),
                            fill=body)
    cell = lambda cc, rr, col: d.rectangle((x + cc * c, y + rr * c, x + (cc + 1) * c - 1,
                                            y + (rr + 1) * c - 1), fill=col)
    cell(3, 1, FG)                                  # 반짝임
    if mood == 2:                                   # 질끈 감은 눈
        for cc in (3, 4, 9, 10):
            cell(cc, 4, BG)
        for cc in (6, 7):                           # 벌린 입
            cell(cc, 6, BG); cell(cc, 7, BG)
    else:
        for cc in (4, 9):                           # 눈
            cell(cc, 3, BG); cell(cc, 4, BG)
        if mood == 0:                               # 웃는 입
            cell(5, 6, BG); cell(8, 6, BG); cell(6, 7, BG); cell(7, 7, BG)
        else:                                       # 일자 입
            for cc in (5, 6, 7, 8):
                cell(cc, 7, BG)
    if mood >= 1:                                   # 땀방울
        cell(13, 1, SWEAT); cell(13, 2, SWEAT); cell(14, 2, SWEAT)
    # 그림자
    d.ellipse((x + 2 * c, y + 10 * c + c // 2 + (c // 2 if beat else 0),
               x + 12 * c, y + 11 * c + c), fill=(24, 23, 22))


def render_mascot(usage, source, now=None):
    now = now or datetime.now(timezone.utc)
    img = Image.new("RGB", (W * S, H * S), BG)
    d = ImageDraw.Draw(img)
    s = lambda v: v * S

    global _BEAT
    _BEAT = not globals().get("_BEAT", False)
    d.ellipse((s(12), s(12), s(28), s(28)), fill=ACCENT)
    d.ellipse((s(17), s(17), s(23), s(23)), fill=FG if _BEAT else BG)
    d.text((s(34), s(6)), "Claude", font=_px(21), fill=FG)
    clock = now.astimezone().strftime("%H:%M")
    d.text((s(W - 12) - d.textlength(clock, font=_px(15, False)), s(10)), clock,
           font=_px(15, False), fill=DIM)

    five = usage.get("five_hour") or {}
    week = usage.get("seven_day") or {}
    p5 = _pct(five)
    reset = _parse(five.get("resets_at"))
    if reset and reset <= now:
        p5 = 0.0
    c5 = DIM if p5 is None else _color(p5)

    cx, cy, r, rw = 58, 92, 46, 10
    _ring(d, s(cx), s(cy), s(r), s(rw), p5, c5)
    if reset and reset > now:
        frac = max(0.0, min(1.0, 1 - (reset - now).total_seconds() / FIVE_H))
        a = math.radians(-90 + 360 * frac)
        rc, dr = s(r) - s(rw) / 2, s(6.5)
        px, py = s(cx) + rc * math.cos(a), s(cy) + rc * math.sin(a)
        d.ellipse((px - dr, py - dr, px + dr, py + dr), fill=FG, outline=BG, width=S * 2)
    num = "--" if p5 is None else f"{p5:.0f}"
    fb, fu = _px(32), _px(14)
    nw, uw = d.textlength(num, font=fb), d.textlength("%", font=fu)
    x = s(cx) - (nw + uw + s(1)) / 2
    d.text((x, s(cy - 28)), num, font=fb, fill=FG)
    d.text((x + nw + s(1), s(cy - 7)), "%", font=fu, fill=FG)
    _text_c(d, s(cx), s(cy + 10), "사용량", _px(12, False), DIM)

    x0 = 118
    d.text((s(x0), s(40)), "5시간 세션", font=_px(15), fill=FG)
    if reset and reset > now:
        rem = int((reset - now).total_seconds())
        h, m = divmod((rem + 59) // 60, 60)
        cd, cc = f"{h}:{m:02d}", FG
    else:
        cd, cc = "-:--", DIM
    fcd = _px(32)
    d.text((s(x0 - 1), s(55)), cd, font=fcd, fill=cc)
    d.text((s(x0) + d.textlength(cd, font=fcd) + s(5), s(72)), "남음", font=_px(13, False), fill=DIM)
    status = "여유" if p5 is None or p5 < 60 else "주의" if p5 < 85 else "한도 임박"
    fs = _px(13)
    tw = d.textlength(status, font=fs)
    d.rounded_rectangle((s(x0), s(98), s(x0) + tw + s(22), s(119)), radius=s(10), fill=c5)
    d.text((s(x0 + 11), s(99)), status, font=fs, fill=BG)

    mood = 0 if (p5 is None or p5 < 60) else 1 if p5 < 85 else 2
    _slime(d, s(146), s(124), s(4), mood, _BEAT)

    d.line((s(14), s(174), s(W - 14), s(174)), fill=TRACK, width=S)

    pw = _pct(week)
    cw = DIM if pw is None else _color(pw)
    d.text((s(14), s(179)), "주간 사용량", font=_px(14), fill=FG)
    wl = _left_text(week.get("resets_at"), now)
    if wl:
        lx = s(14) + d.textlength("주간 사용량", font=_px(14)) + s(6)
        d.text((lx, s(182)), f"{wl} 남음", font=_px(11, False), fill=DIM)
    v = "--" if pw is None else f"{pw:.0f}%"
    d.text((s(W - 14) - d.textlength(v, font=_px(16)), s(177)), v, font=_px(16), fill=cw)
    bx0, bx1, by, bh = s(14), s(W - 14), s(200), s(8)
    _bar(d, bx0, by, bx1, bh, pw, cw)
    wr = _parse(week.get("resets_at"))
    if wr and wr > now:
        frac = max(0.0, min(1.0, 1 - (wr - now).total_seconds() / (7 * 86400)))
        px, py, dr = bx0 + (bx1 - bx0) * frac, by + bh / 2, s(6)
        d.ellipse((px - dr, py - dr, px + dr, py + dr), fill=FG, outline=BG, width=S * 2)
    return img.resize((W, H), Image.LANCZOS)


# ---------------------------------------------------------------- device
class SmallTV:
    def __init__(self, ip):
        self.base = f"http://{ip}"

    def _get(self, path, tries=3):
        # ESP8266은 GIF 재생 중에 응답이 몇 초씩 늦을 수 있어 넉넉히 기다리고 재시도
        for i in range(tries):
            try:
                return requests.get(self.base + path, timeout=25)
            except requests.exceptions.RequestException:
                if i == tries - 1:
                    raise
                time.sleep(2)

    rotate_cfg = None

    def ensure_album(self, cfg=None):
        self._get("/set?i_i=5&autoplay=0")       # 사진앨범 슬라이드쇼 끄기
        cfg = cfg or {}
        self.rotate_cfg = cfg if cfg.get("rotate") else None
        if cfg.get("rotate"):
            # 펌웨어 기본 기능인 '테마 자동 전환'으로 시계·날씨 ↔ Claude 화면 번갈아 표시
            themes = {int(t) for t in cfg.get("rotate_themes", [1, 3])} | {3}
            flags = ",".join("1" if i in themes else "0" for i in range(1, 8))
            sec = max(5, int(cfg.get("rotate_seconds", 15)))
            # 장비 설정 페이지(settings.html)가 보내는 형식 그대로: theme_interval
            self._get(f"/set?theme_list={flags}&sw_en=1&theme_interval={sec}")
            self._get(f"/set?theme={min(themes)}")
        else:
            self._get("/set?theme_list=0,0,1,0,0,0,0&sw_en=0&theme_interval=15")
            self._get("/set?theme=3")

    def _post(self, filename, data, mime):
        try:
            requests.post(self.base + "/doUpload?dir=/image/",
                          files={"file": (filename, io.BytesIO(data), mime)}, timeout=60)
        except requests.exceptions.ConnectionError as e:
            # 펌웨어 버그(중복 Content-Length)로 오류가 나도 업로드는 성공하는 경우가 많음
            if "Content-Length" not in str(e) and "Data after" not in str(e):
                raise

    def upload_swap(self, data, base, brightness=None):
        """빈 화면(No image) 없이 교체: A/B 두 파일을 번갈아 씀.
        새 파일 업로드 → 새 파일로 표시 전환 → 그다음에 이전 파일 삭제."""
        a, b = f"{base}_a.jpg", f"{base}_b.jpg"
        new = b if getattr(self, "_shown", None) == a else a
        old = a if new == b else b
        self._post(new, data, "image/jpeg")
        time.sleep(0.3)
        self._get(f"/set?img=/image//{new}")
        self._shown = new
        legacy = () if getattr(self, "_cleaned", False) else (f"{base}.jpg", f"{base}.gif")
        self._cleaned = True
        for f in (old, *legacy):                           # 화면 전환 뒤에 정리
            try:
                self._get(f"/delete?file=/image//{f}", tries=1)
            except Exception:
                pass
        if self.rotate_cfg:                                # 자동 전환 모드면 다시 켜기
            cfg = self.rotate_cfg
            themes = {int(t) for t in cfg.get("rotate_themes", [1, 3])} | {3}
            flags = ",".join("1" if i in themes else "0" for i in range(1, 8))
            sec = max(5, int(cfg.get("rotate_seconds", 15)))
            self._get(f"/set?theme_list={flags}&sw_en=1&theme_interval={sec}")
        if brightness is not None:
            self._get(f"/set?brt={int(brightness)}")

# ---------------------------------------------------------------- statusline hook
HOOK_NAME = "claude_meter_hook.py"
PREV_FILE_NAME = "claude-meter-prev-statusline.json"
HOOK_SRC = r"""# Claude Code 상태표시줄 훅 — claude-meter 가 설치함
# 1) Claude Code가 넘겨주는 사용량(rate_limits)을 파일로 남기고
# 2) 원래 쓰던 상태표시줄(예: Orca)이 있으면 같은 입력으로 그대로 실행해 결과를 보여줌
import json, os, shutil, subprocess, sys, time
from pathlib import Path
base = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
raw = sys.stdin.buffer.read()
try:
    d = json.loads(raw.decode("utf-8") or "{}")
except Exception:
    d = {}
rl = d.get("rate_limits") or {}
if rl:
    try:
        p = base / "claude-meter-statusline.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"t": time.time(), "rate_limits": rl}), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass

out = ""
try:
    prev = json.loads((base / "claude-meter-prev-statusline.json").read_text(encoding="utf-8"))
    cmd = prev.get("command") if isinstance(prev, dict) else None
except Exception:
    cmd = None
if cmd:
    bash = None
    if os.name == "nt":
        git = shutil.which("git")
        cands = ([str(Path(git).resolve().parents[1] / "bin" / "bash.exe")] if git else []) + [
            "C:/Program Files/Git/bin/bash.exe", "C:/Program Files (x86)/Git/bin/bash.exe"]
        bash = next((c for c in cands if Path(c).exists()), None)
    else:
        bash = shutil.which("bash") or "/bin/sh"
    try:
        args = [bash, "-c", cmd] if bash else ["powershell", "-NoProfile", "-Command", cmd]
        r = subprocess.run(args, input=raw, capture_output=True, timeout=8,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = r.stdout.decode("utf-8", "replace").rstrip()
    except Exception:
        out = ""
if out:
    print(out)
else:
    parts = [(d.get("model") or {}).get("display_name", "")]
    f = rl.get("five_hour") or {}
    if f.get("used_percentage") is not None:
        left = max(0, int((f.get("resets_at") or 0) - time.time()))
        parts.append(f"5h {f['used_percentage']:.0f}% ({left // 3600}:{left % 3600 // 60:02d} 남음)")
    w = rl.get("seven_day") or {}
    if w.get("used_percentage") is not None:
        parts.append(f"주간 {w['used_percentage']:.0f}%")
    print(" · ".join(x for x in parts if x))
"""


def _settings():
    settings_p = CLAUDE_DIR / "settings.json"
    settings = _read_json(settings_p) if settings_p.exists() else {}
    return settings_p, settings


def install_statusline(force=False):
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    hook = CLAUDE_DIR / HOOK_NAME
    hook.write_text(HOOK_SRC, encoding="utf-8")
    settings_p, settings = _settings()
    if settings is None:
        print(f"[오류] {settings_p} 를 읽을 수 없습니다(JSON 형식 오류). 수동으로 확인하세요.")
        return False
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe":
        py = py.with_name("python.exe")
    py_s = py.as_posix()
    if " " in py_s:
        py_s = "python"
    cmd = f"{py_s} ~/.claude/{HOOK_NAME}"
    cur = settings.get("statusLine")
    prev_p = CLAUDE_DIR / PREV_FILE_NAME
    if cur and HOOK_NAME not in json.dumps(cur):
        if force:
            prev_p.unlink(missing_ok=True)
            print("[안내] 기존 상태표시줄을 대체합니다(--force).")
        else:
            # 기존 상태표시줄(예: Orca)은 지우지 않고 우리 훅 뒤에서 그대로 실행
            _write_json(prev_p, cur)
            print("[연결] 기존 상태표시줄을 유지한 채 함께 실행되도록 연결했습니다.")
    if settings_p.exists():
        (CLAUDE_DIR / "settings.json.bak-claude-meter").write_text(
            settings_p.read_text(encoding="utf-8"), encoding="utf-8")
    new = dict(cur) if isinstance(cur, dict) and HOOK_NAME in json.dumps(cur) else {}
    new.update({"type": "command", "command": cmd})
    settings["statusLine"] = new
    settings_p.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[완료] 상태표시줄 훅 설치: {hook}")
    print(f"       settings.json statusLine = {cmd}")
    print("       Claude Code(Orca 포함)에서 대화하면 사용량이 장비로 넘어갑니다.")
    return True


def uninstall_statusline():
    settings_p, settings = _settings()
    if not settings:
        print("[안내] 설정 파일이 없습니다.")
        return
    prev_p = CLAUDE_DIR / PREV_FILE_NAME
    prev = _read_json(prev_p)
    if prev:
        settings["statusLine"] = prev
        prev_p.unlink(missing_ok=True)
        print("[복구] 원래 상태표시줄로 되돌렸습니다.")
    else:
        settings.pop("statusLine", None)
        print("[제거] 상태표시줄 설정을 지웠습니다.")
    settings_p.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def set_session_key():
    cfg = load_config()
    print("claude.ai 에 로그인된 크롬/엣지에서")
    print("  F12 → Application(애플리케이션) → Cookies → https://claude.ai → sessionKey 의 Value 복사")
    key = input("sessionKey 붙여넣기 (sk-ant-… ) : ").strip().strip('"')
    if not key.startswith("sk-ant-"):
        print("[오류] sk-ant- 로 시작하는 값이 아닙니다. 다시 확인하세요.")
        return
    cfg["session_key"], cfg["org_id"] = key, ""
    try:
        data = fetch_via_session_key(cfg)
    except Exception as e:
        print(f"[실패] 확인 중 오류: {e}")
        return
    save_config(cfg)
    _write_json(API_CACHE, {"t": time.time(), "data": data, "source": "claude.ai", "fails": 0})
    f, w = data.get("five_hour") or {}, data.get("seven_day") or {}
    print(f"[성공] 5시간 세션 {f.get('utilization')}% · 주간 {w.get('utilization')}%")
    print("       config.json 에 저장했습니다. autostart.bat 을 실행하세요.")


# ---------------------------------------------------------------- install / uninstall
def _startup_dir():
    return (Path(os.environ.get("APPDATA", Path.home())) /
            "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")


def _pythonw():
    py = Path(sys.executable)
    w = py.with_name("pythonw.exe")
    return w if w.exists() else py


def do_install():
    script = Path(__file__).resolve()
    cfg = load_config()
    if not cfg.get("device_ip"):
        print("SmallTV 장비 IP를 입력하세요. (장비를 켤 때 화면에 표시되거나, 공유기 관리 페이지에서 확인)")
        ip = input("장비 IP (예: 192.168.0.25): ").strip()
        if not ip:
            print("[중단] IP가 없어 설치를 멈춥니다. config.json 의 device_ip 에 넣고 다시 실행하세요.")
            return
        cfg["device_ip"] = ip
        save_config(cfg)
    try:
        info = SmallTV(cfg["device_ip"])._get("/v.json", tries=1).json()
        print(f"[확인] 장비 응답: {info.get('m')} {info.get('v')}")
    except Exception as e:
        print(f"[경고] 지금은 장비가 응답하지 않습니다({e}). 설치는 계속하고, 연결될 때까지 자동 재시도합니다.")
    print("[1/3] Claude Code 상태표시줄 연동")
    try:
        install_statusline()
    except Exception as e:
        print(f"      건너뜀: {e}")

    print("[2/3] PC 켤 때 자동 시작 등록")
    if os.name == "nt":
        sd = _startup_dir()
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "ClaudeMeter.bat").unlink(missing_ok=True)          # 예전 방식 정리
        vbs = (f'Set sh = CreateObject("WScript.Shell")\r\n'
               f'sh.CurrentDirectory = "{script.parent}"\r\n'
               f'sh.Run """{_pythonw()}"" ""{script}""", 0, False\r\n')
        (sd / "ClaudeMeter.vbs").write_text(vbs, encoding="utf-16")  # 한글 경로 대응
        print(f"      등록: {sd / 'ClaudeMeter.vbs'}")
    else:
        print("      Windows가 아니라 건너뜀")

    print("[3/3] 모니터 실행(기존 실행분은 자동 교체)")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(_pythonw()), str(script)], cwd=str(script.parent),
                     creationflags=flags, close_fds=True)
    print("\n완료! 이 창은 닫아도 됩니다. 1분 안에 장비 화면이 바뀝니다.")


def do_uninstall():
    if os.name == "nt":
        for n in ("ClaudeMeter.vbs", "ClaudeMeter.bat"):
            (_startup_dir() / n).unlink(missing_ok=True)
    try:
        uninstall_statusline()
    except Exception as e:
        print(f"[안내] 상태표시줄 복구 건너뜀: {e}")
    PID_FILE.write_text("stop")
    print("모니터를 끄고 자동 시작을 해제했습니다.")
    time.sleep(2)                                   # 모니터가 종료될 시간
    try:                                            # 장비를 원래 시계·날씨 화면으로
        cfg = load_config()
        tv = SmallTV(cfg["device_ip"])
        for ext in ("gif", "jpg"):
            tv._get(f"/delete?file=/image//{cfg['image_name']}.{ext}")
        tv._get("/set?theme_list=1,0,0,0,0,0,0&sw_en=0&theme_interval=15")
        tv._get("/set?theme=1")
        print("장비를 원래 시계·날씨 화면으로 되돌렸습니다.")
    except Exception as e:
        print(f"[안내] 장비 화면 복구 실패: {e}")


# ---------------------------------------------------------------- single instance
# 새로 실행되면 기존 모니터를 정리하고 자리를 넘겨받음(업데이트 후 재실행만 하면 됨)
import subprocess
import tempfile

PID_FILE = Path(tempfile.gettempdir()) / "claude-meter.pid"


def _find_other_meters():
    """명령줄에 claude_meter.py 가 들어간 다른 python 프로세스 PID 목록(Windows)."""
    if os.name != "nt":
        return []
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          "Where-Object { $_.CommandLine -like '*claude_meter.py*' -and "
          "$_.CommandLine -notlike '*statusline*' -and $_.CommandLine -notlike '*--set-session*' -and $_.CommandLine -notlike '*--setup*' -and "
          "$_.CommandLine -notlike '*--stop*' -and "
          "$_.CommandLine -notlike '*install*' -and "
          "$_.CommandLine -notlike '*--preview*' -and $_.CommandLine -notlike '*--once*' } | "
          "ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return [int(x) for x in out.split() if x.isdigit() and int(x) != os.getpid()]
    except Exception:
        return []


def take_over():
    # 1) PID 파일을 내 것으로 바꿔 기존 인스턴스가 스스로 종료하도록 신호
    PID_FILE.write_text(str(os.getpid()))
    others = _find_other_meters()
    if not others:
        return
    print(f"[정리] 기존 모니터 {len(others)}개 종료 중…")
    # 2) 최대 5초 기다렸다가 3) 남아 있으면 강제 종료(이전 버전 대응)
    for _ in range(10):
        time.sleep(0.5)
        others = _find_other_meters()
        if not others:
            break
    for pid in others:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print("[정리] 완료 — 새 버전으로 실행합니다.")


def still_owner():
    try:
        return PID_FILE.read_text().strip() == str(os.getpid())
    except Exception:
        return True


def wait_or_exit(seconds):
    """대기 중에도 1초마다 자리를 빼앗겼는지 확인 → 새 인스턴스가 뜨면 조용히 종료."""
    end = time.time() + seconds
    while time.time() < end:
        if not still_owner():
            print("[종료] 새로 실행된 모니터에 자리를 넘깁니다.")
            sys.exit(0)
        time.sleep(1)

# ---------------------------------------------------------------- main
def tick(cfg, tv, demo=False, preview=False):
    name = cfg["image_name"]
    try:
        usage, source, age = get_usage(cfg, demo)
        print(f"[{datetime.now():%H:%M:%S}] 5h={_pct(usage.get('five_hour'))} "
              f"7d={_pct(usage.get('seven_day'))} ({source}, {age // 60}분 전 데이터)")
        if age >= 120:
            source = f"{source} · {age // 60}분 전"
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] 사용량 없음: {e}")
        usage, source, err = {}, "", e
    else:
        err = None

    layout = cfg.get("layout", "mascot")
    if not err and layout == "mascot":
        img = render_mascot(usage, source)
    elif not err and layout == "ring":
        img = render_ring(usage, source)
    else:
        img = render(usage, source, error=err, seconds=False)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    img.save(HERE / "preview.jpg", quality=90)
    if not preview:
        tv.upload_swap(buf.getvalue(), name, cfg.get("brightness"))


def connect_device(cfg, tv):
    info = tv._get("/v.json").json()
    print(f"[{datetime.now():%H:%M:%S}] [연결] {info.get('m')} {info.get('v')} @ {cfg['device_ip']}")
    if True:
        # 예전 버전이 남긴 GIF가 있으면 장비가 버벅이므로 먼저 지움
        try:
            tv._get(f"/delete?file=/image//{cfg['image_name']}.gif", tries=2)
        except Exception:
            pass
    tv.ensure_album(cfg)


def run_check():
    """무엇을 받고 있는지 진단 출력."""
    cfg = load_config()
    now = time.time()
    print("=" * 56)
    print(" Claude 사용량 모니터 진단")
    print("=" * 56)
    print(f"Python {sys.version.split()[0]} · 장비 {cfg['device_ip']}")
    try:
        info = requests.get(f"http://{cfg['device_ip']}/v.json", timeout=5).json()
        print(f"[장비] 연결 OK — {info}")
    except Exception as e:
        print(f"[장비] 연결 실패 — {e}")
    st = _read_json(STATUS_FILE)
    if st:
        print(f"[상태표시줄] {int(now - st.get('t', 0)) // 60}분 전 데이터")
        print("   " + json.dumps(st.get("rate_limits"), ensure_ascii=False)[:300])
    else:
        print("[상태표시줄] 데이터 없음 (Claude Code에서 아직 대화 안 함)")
    c = _read_json(API_CACHE) or {}
    print(f"[API] 토큰 {'있음' if _claude_code_token() else '없음'} · "
          f"세션키 {'있음' if cfg.get('session_key') else '없음'} · 실패 {c.get('fails', 0)}회")
    if c.get("data"):
        print(f"   {int(now - c.get('t', 0)) // 60}분 전 데이터: "
              + json.dumps({k: c['data'].get(k) for k in ('five_hour', 'seven_day')},
                           ensure_ascii=False)[:300])
    if c.get("backoff_until", 0) > now:
        print(f"   대기 중: {int(c['backoff_until'] - now) // 60 + 1}분 뒤 재시도 ({c.get('last_error')})")
    print("-" * 56)
    try:
        u, src, age = get_usage(cfg)
        f = u.get("five_hour") or {}
        r = _parse(f.get("resets_at"))
        print(f"[결과] 출처 {src} · {age // 60}분 전")
        print(f"   5시간: {f.get('utilization')}% · resets_at={f.get('resets_at')!r}")
        if r:
            left = int((r - datetime.now(timezone.utc)).total_seconds())
            print(f"   → 리셋까지 {left // 3600}시간 {left % 3600 // 60}분"
                  + ("  (이미 지남 → '대기 중' 표시)" if left <= 0 else ""))
        else:
            print("   → 리셋 시각 없음 → '대기 중' 표시 (최근 5시간 사용 기록 없음일 수 있음)")
    except Exception as e:
        print(f"[결과] 사용량 없음 — {e}")
    log = HERE / "meter.log"
    if log.exists():
        print("-" * 56 + "\n[최근 로그]")
        print("".join(log.read_text(encoding="utf-8", errors="replace").splitlines(True)[-8:]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--install-statusline", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--uninstall-statusline", action="store_true")
    ap.add_argument("--set-session", action="store_true")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if sys.stdout is None or sys.stderr is None:      # pythonw(창 없음) → 로그 파일로
        log = HERE / "meter.log"
        try:
            if log.exists() and log.stat().st_size > 300_000:
                log.unlink()
            sys.stdout = sys.stderr = open(log, "a", encoding="utf-8", buffering=1)
        except Exception:
            pass

    if args.check:
        run_check()
        return
    if args.install_statusline:
        install_statusline(args.force)
        return
    if args.uninstall_statusline:
        uninstall_statusline()
        return
    if args.set_session:
        set_session_key()
        return
    if args.install:
        do_install()
        return
    if args.uninstall:
        do_uninstall()
        return
    if args.stop:
        PID_FILE.write_text("stop")       # 실행 중인 모니터가 1초 안에 스스로 종료
        print("[종료] 실행 중인 모니터에 종료 신호를 보냈습니다.")
        return
    if args.setup:
        try:
            install_statusline()
        except Exception as e:
            print(f"[안내] 상태표시줄 연동 건너뜀: {e}")
        return

    cfg = load_config()
    if not cfg.get("device_ip") and not args.preview:
        print("[설정 필요] config.json 의 device_ip 에 장비 IP를 넣거나 install.bat 을 실행하세요.")
        sys.exit(1)
    looping = not (args.once or args.preview)
    if looping:
        take_over()
        hook = CLAUDE_DIR / HOOK_NAME          # 설치된 훅도 최신 버전으로 자동 갱신
        try:
            if hook.exists() and hook.read_text(encoding="utf-8") != HOOK_SRC:
                hook.write_text(HOOK_SRC, encoding="utf-8")
                print("[갱신] 상태표시줄 훅을 새 버전으로 바꿨습니다.")
        except Exception:
            pass
    tv = SmallTV(cfg["device_ip"])
    if not args.preview:
        try:
            connect_device(cfg, tv)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] 장비 응답 없음({e}) — 꺼지지 않고 30초마다 다시 시도합니다.")
            if not looping:
                sys.exit(1)
            while True:                       # 장비가 바쁘거나 잠시 끊겨도 포기하지 않음
                wait_or_exit(30)
                try:
                    connect_device(cfg, tv)
                    break
                except Exception as e2:
                    print(f"[{datetime.now():%H:%M:%S}] 장비 응답 없음: {e2}")

    if args.once or args.preview:
        tick(cfg, tv, args.demo, args.preview)
        return
    while True:
        if not still_owner():
            sys.exit(0)
        if Path(__file__).stat().st_mtime != SCRIPT_MTIME:
            # 프로그램 파일이 새 버전으로 바뀌면 스스로 새 버전을 띄우고 자리를 넘김
            print(f"[{datetime.now():%H:%M:%S}] [업데이트] 새 버전 감지 — 다시 시작합니다.")
            flags = 0
            if os.name == "nt":
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([str(_pythonw()), str(Path(__file__).resolve())],
                             cwd=str(HERE), creationflags=flags, close_fds=True)
            sys.exit(0)
        try:
            new = load_config()                 # 설정 변경을 재실행 없이 반영
            if (new.get("rotate"), new.get("rotate_themes"), new.get("rotate_seconds")) != \
               (cfg.get("rotate"), cfg.get("rotate_themes"), cfg.get("rotate_seconds")):
                tv.ensure_album(new)
            if new.get("device_ip") != cfg.get("device_ip"):
                tv = SmallTV(new["device_ip"])
                connect_device(new, tv)
            cfg = new
            tick(cfg, tv, args.demo)
        except Exception as e:
            print(f"[경고] 장비 전송 실패: {e}")
        wait_or_exit(max(30, int(cfg["interval_seconds"])))


if __name__ == "__main__":
    main()
