# app.py
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, LocationMessage,
    FlexSendMessage
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
import unicodedata

# =============================
# 基本設定
# =============================
app = Flask(__name__)
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
OWM_API_KEY = os.getenv("OWM_API_KEY", "")  # OpenWeatherMap（任意）

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# HTTPセッション
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "MoodFlowBot/1.0"})
HTTP_TIMEOUT = 6
RETRY = 2

# キャッシュ（TTL）
WEATHER_TTL = 10 * 60      # 10分
GEOCODE_TTL = 24 * 60 * 60 # 24時間
_weather_cache: Dict[Tuple[float, float], Tuple[float, Dict[str, Any]]] = {}
_geocode_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

# 簡易ユーザーストア（位置情報）
STORE_PATH = Path("user_store.json")
_store_lock = threading.Lock()


# =============================
# ユーティリティ
# =============================
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


# =============================
# 外部API（天気・ジオコーディング）
# =============================
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
    now_ts = time.time()
    if key in _weather_cache and now_ts - _weather_cache[key][0] < WEATHER_TTL:
        return _weather_cache[key][1]
    url = (
        "https://api.openweathermap.org/data/2.5/weather"
        f"?lat={lat}&lon={lon}&units=metric&lang=ja&appid={OWM_API_KEY}"
    )
    data = http_get_json(url)
    if not data:
        return None
    res = {
        "tag": data["weather"][0]["main"].lower(),  # rain/clear/clouds/…
        "desc": data["weather"][0]["description"],
        "temp": round(float(data["main"]["temp"])),
        "city": data.get("name") or ""
    }
    _weather_cache[key] = (now_ts, res)
    return res

def geocode_city(q: str) -> Optional[Dict[str, Any]]:
    if not OWM_API_KEY:
        return None
    k = q.strip().lower()
    now_ts = time.time()
    if k in _geocode_cache and now_ts - _geocode_cache[k][0] < GEOCODE_TTL:
        return _geocode_cache[k][1]
    url = f"https://api.openweathermap.org/geo/1.0/direct?q={q}&limit=1&appid={OWM_API_KEY}"
    arr = http_get_json(url)
    if not arr or not isinstance(arr, list) or not arr:
        return None
    top = arr[0]
    res = {"lat": float(top["lat"]), "lon": float(top["lon"]), "city": top.get("name", q)}
    _geocode_cache[k] = (now_ts, res)
    return res


# =============================
# 文言テーブル（時間/季節/週末/天気/相づち）
# =============================
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
    "thunderstorm": ["⚡ 雷の気配。低めのビートで熱を下げよう。"],
    "snow":         ["❄️ 雪模様。温かい音で手を温めましょう。"],
    "clear":        ["☀️ 晴れ。軽やかなグルーヴで。"],
    "clouds":       ["☁️ くもり。輪郭の優しいトーンで。"],
    "mist":         ["🌫 霞がかかっています。アンビエント寄りで静かに。"],
}
ACKS = [
    "メッセージ、受け取りました。",
    "その気分、大切にしましょう。",
    "ゆっくりいきましょう。",
    "今の心地に寄り添います。",
    "落ち着いて、音に身をあずけて。"
]


# =============================
# 感情推定（軽量辞書＆ヒューリスティック）
# =============================
EMO_LEXICON = {
    "joy":      ["うれ", "嬉", "楽しい", "最高", "やった", "わくわく", "ﾜｸﾜｸ", "良かった", "😍", "🥳", "✨"],
    "grateful": ["ありがとう", "感謝", "助か", "サンキュー", "🙏"],
    "sad":      ["さみ", "寂", "つら", "辛", "悲しい", "泣", "落ち込", "しんど", "最悪", "😭", "😢", "😞"],
    "angry":    ["怒", "ムカ", "腹立", "イライラ", "許せ", "💢", "😡"],
    "anxious":  ["不安", "こわ", "怖", "緊張", "心配", "焦り", "ドキドキ", "😰", "😱"],
    "tired":    ["疲れ", "ねむ", "眠", "だる", "限界", "バテ", "ぐったり", "😴", "💤"],
    "calm":     ["落ち着", "静か", "まったり", "穏や", "ほっと", "安ら", "☺️"],
    "excited":  ["楽しみ", "テンション", "やるぞ", "燃える", "🔥", "！"],
    "lonely":   ["ひとり", "独り", "孤独", "さみ", "誰も", "🥺"],
}
EMO_LINES = {
    "joy":      ["その嬉しさ、音でさらに彩りを。", "いいね、その明るさでいきましょう。"],
    "grateful": ["こちらこそ、ありがとう。穏やかなループをどうぞ。"],
    "sad":      ["今日は無理しないで。呼吸を整えて、やさしい音に身を預けよう。"],
    "angry":    ["気持ちを言葉にできてえらい。低めのビートで熱を下げよう。"],
    "anxious":  ["深呼吸。テンポを落として、心拍に寄り添う音を。"],
    "tired":    ["おつかれさま。短いループでゆっくり回復を。"],
    "calm":     ["静かな気分。長く伸びる音が合いそう。"],
    "excited":  ["その勢い、いいですね。跳ねるビートでいきましょう。"],
    "lonely":   ["ひとりの時間も、音がそっと寄り添います。"],
}

def detect_emotion(text: str) -> Optional[str]:
    if not text:
        return None
    norm = unicodedata.normalize("NFKC", text.lower())
    score = {k: 0 for k in EMO_LEXICON.keys()}
    for tag, words in EMO_LEXICON.items():
        for w in words:
            if w and w in norm:
                score[tag] += 1
    # ! が多いほど興奮寄り、… は疲労寄りに補正
    ex = norm.count("!") + norm.count("！")
    if ex >= 2:
        score["excited"] += 1
    ell = norm.count("…") + norm.count("...") + norm.count("。。")
    if ell >= 1:
        score["tired"] += 1
    tag = max(score, key=score.get)
    return tag if score[tag] > 0 else None


# =============================
# 返信テキスト（オウム返しなし + 感情 + 天気）
# =============================
def build_reply(user_text: str, weather: Optional[Dict[str, Any]], now: dt.datetime) -> str:
    blk = time_block(now.hour)
    sea = season(now.month)
    wk  = is_weekend(now.weekday())

    p1 = random.choice(GREET_BY_BLOCK[blk])
    p2 = random.choice(MOOD_BY_SEASON[sea])
    p3 = random.choice(TAIL_BY_WEEK[wk])
    a  = random.choice(ACKS)

    emo = detect_emotion(user_text)
    emo_line = random.choice(EMO_LINES[emo]) if emo and emo in EMO_LINES else ""

    wline = ""
    if weather:
        tag  = weather.get("tag", "")
        base = WEATHER_TONE.get(tag)
        tone = random.choice(base) if base else ""
        city = weather.get("city") or "現在地"
        try:
            wline = f"{city}は{weather['desc']}（{weather['temp']}℃）。{tone}"
        except Exception:
            wline = tone

    parts = [f"{p1}{a}", p2]
    if emo_line:
        parts.append(emo_line)
    if wline:
        parts.append(wline)
    parts.append(p3)
    return "\n".join(parts)


# =============================
# YouTube プレイリスト（紹介用）
# =============================
# すべてあなたの LoFi Beats を指しています。別テーマができたら URL を差し替えてください。
# ====== 固定プレイリスト運用（URLは常に同じ） ======
PLAYLIST_URL = "https://youtube.com/playlist?list=PLTKjLZap9yJyabiXUYzwxnoKs7CFvCT8A&si=iNmQRGT7Ii3JUg2_"

# タイトル・説明だけを状況で言い換える（URLは固定）
def contextual_playlist_item(block: str, weather_tag: str | None, emotion: str | None) -> dict:
    # デフォルト文言
    title = "LoFi Beats – Nomadic Flow"
    desc  = "穏やかな集中とリラックスに。"
    cover = "https://img.youtube.com/vi/jfKfPfyJRdk/hqdefault.jpg"  # 任意で差し替えOK

    # 時間帯
    if block == "morning":
        title = "Morning Flow – Chillhop for Focus"
        desc  = "静かな朝の立ち上がりに。"
    elif block == "evening":
        title = "Evening Chill – LoFi for Wind Down"
        desc  = "一日の終わりに、ゆっくりと。"
    elif block == "night":
        title = "Midnight LoFi – Slow & Cozy"
        desc  = "夜更けはゆるく、深呼吸。"

    # 天気
    if weather_tag in ("rain", "drizzle", "thunderstorm", "mist"):
        title = "Rainy Café – LoFi Jazz for Calm Days"
        desc  = "雨音といっしょに、やわらかく。"

    # 感情
    if emotion in ("tired", "sad", "lonely", "anxious"):
        title = "Calm & Warm – Gentle LoFi"
        desc  = "肩の力を抜いて、やさしいトーンで。"
    elif emotion in ("joy", "excited"):
        title = "Upbeat Chill – Light & Groovy"
        desc  = "気分に少し明るさを足していこう。"

    return {
        "title": title,
        "url": PLAYLIST_URL,
        "cover": cover,
        "desc": desc
    }

def make_playlist_flex(item: dict) -> dict:
    return {
        "type": "bubble",
        "hero": {
            "type": "image",
            "url": item.get("cover") or "https://i.imgur.com/2x5oH9K.jpg",
            "size": "full",
            "aspectMode": "cover",
            "aspectRatio": "20:13"
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": item["title"], "weight": "bold", "size": "md", "wrap": True},
                {"type": "text", "text": item.get("desc", ""), "size": "sm", "color": "#888888", "wrap": True, "margin": "sm"}
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "uri", "label": "プレイリストを聴く", "uri": item["url"]}}
            ],
            "flex": 0
        }
    }

# =============================
# ルーティング
# =============================
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

# 位置情報で保存
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
            "・位置情報を送る → 天気連動\n"
            "・都市設定 → 例: `loc 東京`\n"
            "・状態確認 → `status`\n"
            "（普通に話しかけてもOKです）"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    if low == "status":
        pos = store.get(uid)
        if not pos:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage(text="現在、場所は未設定です。位置情報を送るか `loc 東京` と送ってください。"))
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
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage(text=f"📍 場所を「{geo['city']}」に設定しました。"))
        else:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage(text="位置を見つけられませんでした。例：`loc 東京` と送ってください。"))
        return

    # === 通常応答 ===
    now = jst_now()
    pos = store.get(uid)
    weather = get_weather_by_latlon(pos["lat"], pos["lon"]) if pos else None

    # テキスト
    reply = build_reply(text, weather, now)

    # プレイリスト選択（時間・天気・感情ベース）
    emo = detect_emotion(text)
    wtag = (weather or {}).get("tag")
    key = recommend_playlist_key(wtag, now, emo)
    pl = PLAYLISTS.get(key) or PLAYLISTS["default"]

    # Flex + テキストで返す
    flex = FlexSendMessage(alt_text="おすすめプレイリスト", contents=make_playlist_flex(pl))
    line_bot_api.reply_message(event.reply_token, [
        TextSendMessage(text=reply + "\n\n🎧 今日のおすすめプレイリストをどうぞ。"),
        flex
    ])


# =============================
# エントリポイント
# =============================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
