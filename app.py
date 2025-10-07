from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import os
import datetime

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text

    # 現在時刻を日本時間（UTC+9）に変換
    jst_now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    hour = jst_now.hour

    # 朝（5時～11時）
    if 5 <= hour < 12:
        reply = f"☀️ MoodFlow：『{text}』。いいスタートが切れそうですね。今日も穏やかに。"

    # 昼（12時～17時）
    elif 12 <= hour < 18:
        reply = f"🌆 MoodFlow：『{text}』。集中できる時間、音楽と一緒に過ごしましょう。"

    # 夜（18時～22時）
    elif 18 <= hour < 23:
        reply = f"🌙 MoodFlow：『{text}』。今日もお疲れさまです。少し音に癒されましょう。"

    # 深夜（23時～4時）
    else:
        reply = f"💤 MoodFlow：『{text}』。夜風が静かですね。少し休んで、また明日。"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
