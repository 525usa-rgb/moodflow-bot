from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, LocationMessage
)
import os
import datetime as dt
import random
import requests
import json
import time
import tempfile
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

# ====== 基本設定 ======
app = Flask(__name__)
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
OWM_API_KEY = os.getenv("OWM_API_KEY", "")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# HTTPセッション（再利用）
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "MoodFlowBot/1.0 (+https://example.com)"})
HTTP_TIMEOUT = 6
RETRY = 2

# キャッシュ（TTL）
WEATHER_TTL = 10 * 60     # 10分
GEOCODE_TTL = 24 * 60 * 60  # 24時間
_weather_cache: Dict[Tuple[float, float], Tuple[float, Dict[str, Any]]] = {}
_geocode_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

# ユーザー保存（簡易JSON）
STORE_PATH = Path("user_store.json")
_store_lock = threading.Lock()

# ====== ユーティリティ ======
def jst_now() -> dt.datetime:
    return dt.datetime.utcnow() + dt.timedelta(hours=9)

def time_block(hour: int) -> str:
    if 5 <= hour < 12: return "morning"
    if 12 <= hour < 18: return "day"
    if 18 <= hour < 23: return "evening"
    return "night"

def season(month: int) -> str:
    if month in (12, 1, 2): return "winter"
    if month in (3, 4, 5):  return "spring"
    if month in (6, 7, 8):  return "summer"
    return "autumn"

def is_weekend(weekday: int) -> bool:  # Mon=0 ... Sun=6
    return weekday in (5, 6)

def shorten(s: str, n: int = 40) -> str:
    return s if len(s) <= n else (s[: n - 1] + "…")

def _atomic_write_text(path: Path, text: str):
    tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
    try:
        tmp.write(text); tmp.flush(); os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.replace(tmp.name, path)

def load_store() -> Dict[str, Any]:
    with _store_lock:
        if STORE_PATH.exists():
            try:
                return json.loads(STORE_PATH.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

def save_store(data: Dict[str, Any]) -> None:
    with _store_lock:
        _atomic_write_text(STORE_PATH, json.dumps(data, ensure_ascii=False))

# ====== 天気 / 位置API ======
def http_get_json(url: str) -> Optional[Dict[str, Any]]:
    for _ in range(RETRY + 1):
        try:
            r = SESSION.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(0.6)
    return None

def get_weather_by_latlon(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    if not OWM_API_KEY:
        return None
    key = (round(lat, 4), round(lon, 4))
    now = time.time()
    if key in _weather_cache and now - _weather_cache[key][0] < WEATHER_TTL:
        return _weather_cache[key][1]
    url = (
        "https://api.openweathermap.org/data/2.5/weather"
        f"?lat={lat}&lon={lon}&units=metric&lang=ja&appid={OWM_API_KEY}"
    )
    data = http_get_json(url)
    if not data:  # 失敗時はキャッシュせず
        return None
    res = {
        "tag": data["weather"][0]["main"].lower(),  # rain/clear/clouds/…
        "desc": data["weather"][0]["description"],
        "temp": round(float(data["main"]["temp"])),
        "city": data.get("name") or ""
    }
    _weather_cache[key] = (now, res)
    return res

def geocode_city(q: str) -> Optional[Dict[str, Any]]:
    if not OWM_API_KEY:
        return None
    k = q.strip().lower()
    now = time.time()
    if k in _geocode_cache and now - _geocode_cache[k][0] < GEOCODE_TTL:
        return _geocode_cache[k][1]
    url = f"https://api.openweathermap.org/geo/1.0/direct?q={q}&limit=1&appid={OWM_API_KEY}"
    arr = http_get_json(url)
    if not arr:
        return None
    if not isinstance(arr, list) or not arr:
        return None
    top = arr[0]
    res = {"lat": float(top["lat"]), "lon": float(top["lon"]), "city": top.get("name", q)}
    _geocode_cache[k] = (now, res)
    return res

# ====== 文言（時間/季節/週末/天気） ======
GREET_BY_BLOCK = {
    "morning": ["☀️ おはようございます。", "☀️ 今日のはじまりですね。"],
    "day":     ["🌆 いい時間帯ですね。", "🌤 少し集中していきましょう。"],
    "evening": ["🌙 今日もおつかれさま。", "🌃 一日、よくがんばりました。"],
    "night":   ["💤 もう夜更けですね。", "🌌 静かな時間が流れています。"],
}
MOOD_BY_SEASON = {
    "spring": ["春の空気みたいに、やわらかい音を。", "芽吹くように、少しずつ整えていきましょう。"],
    "summer": ["夏の風が少し涼しい音を運んできます。", "熱をやわらげるクールダウンのリズムを。"],
    "autumn": ["秋の色が深まるような落ち着きで。", "少しノスタルジックな響きをどうぞ。"],
    "winter": ["冬の灯りみたいに、やさしく温かい音を。", "息の白さがほどけるようなスローなビートを。"],
}
TAIL_BY_WEEK = {
    False: ["では、良い一日を。", "静かに調子を上げていきましょう。"],
    True:  ["週末らしく、肩の力を抜いて。", "よい週末を。好きなテンポでいきましょう。"],
}
WEATHER_TONE = {
    "rain":         ["☔ 雨ですね。窓のリズムに合わせて、ゆるく。"],
    "drizzle":      ["🌧 霧雨。輪郭の柔らかい音が似合いそう。"],
    "thunderstorm": ["⚡ 雷の気配。低めのビートで落ち着きを。"],
    "snow":         ["❄️ 雪模様。温かい音で手を温めましょう。"],
    "clear":        ["☀️ 晴れ。軽やかなグルーヴで。"],
    "clouds":       ["☁️ くもり。輪郭の優しいトーンで。"],
    "mist":         ["🌫 霞がかかっています。アンビエント寄りで静かに。"],
}

def build_reply(user_text: str, weather: Optional[Dict[str, Any]], now: dt.datetime) -> str:
    blk = time_block(now.hour)
    sea = season(now.month)
    wk  = is_weekend(now.weekday())

    p1 = random.choice(GREET_BY_BLOCK[blk])
    p2 = random.choice(MOOD_BY_SEASON[sea])
    p3 = random.choice(TAIL_BY_WEEK[wk])

    shown = shorten(user_text, 40)

    wline = ""
    if weather:
        tag = weather.get("tag", "")
        base = WEATHER_TONE.get(tag)
        tone = random.choice(base) if base else ""
        city = weather.get("city") or "現在地"
        try:
            wline = f"{city}は{weather['desc']}（{weather['temp']}℃）。{tone}"
        except Exception:
            wline = tone

    msg = f"{p1}『{shown}』ですね。\n{p2}"
    if wline:
        msg += f"\n{wline}"
    msg += f"\n{p3}"
    return msg

# ====== ルーティング ======
@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# 位置情報：保存
@handler.add(MessageEvent, message=LocationMessage)
def handle_location(event):
    uid = event.source.user_id
    store = load_store()
    store[uid] = {
        "lat": event.message.latitude,
        "lon": event.message.longitude,
        "city": event.message.address or ""
    }
    save_store(store)
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="📍 位置情報を保存しました。以後、その地域の天気に合わせて返答します。")
    )

# テキスト：help / status / loc / 通常
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text = event.message.text.strip()
    uid = event.source.user_id
    store = load_store()

    low = text.lower()
    if low in ("help", "？", "ヘルプ"):
        msg = (
            "📝 使い方\n"
            "・現在地の天気を使う → 位置情報を送る\n"
            "・都市を指定する → 例: `loc 東京` / `loc:Osaka`\n"
            "・現在設定の確認 → `status`"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    if low == "status":
        pos = store.get(uid)
        if not pos:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="現在、場所は未設定です。位置情報を送るか `loc 東京` と送ってください。"))
            return
        w = get_weather_by_latlon(pos["lat"], pos["lon"])
        if w:
            msg = f"📍 設定: {pos.get('city','')} / 天気: {w['desc']}（{w['temp']}℃）"
        else:
            msg = f"📍 設定: {pos.get('city','')} / 天気: 取得できませんでした。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    if low.startswith(("loc ", "loc:")):
        q = text.split(" ",1)[-1].split(":",1)[-1].strip()
        geo = geocode_city(q)
        if geo:
            store[uid] = {"lat": geo["lat"], "lon": geo["lon"], "city": geo["city"]}
            save_store(store)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📍 場所を「{geo['city']}」に設定しました。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="位置を見つけられませんでした。例：`loc 東京` と送ってください。"))
        return

    # 通常応答
    now = jst_now()
    weather = None
    if uid in store:
        pos = store[uid]
        weather = get_weather_by_latlon(pos["lat"], pos["lon"])
    reply = build_reply(text, weather, now)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
