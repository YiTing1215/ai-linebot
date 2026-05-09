# -*- coding: utf-8 -*-
import os
import json
import base64
import datetime
import random
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    ImageMessage
)
from openai import OpenAI
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

# ── 載入環境變數 ──────────────────────────────────────────
load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET       = os.environ["LINE_CHANNEL_SECRET"]
OPENAI_API_KEY            = os.environ["OPENAI_API_KEY"]

# ── AI 人格設定 ───────────────────────────────────────────
AI_NAME = "AI同學"
SYSTEM_PROMPT = (
    f"你叫做「{AI_NAME}」，是一位親切、聰明的繁體中文 AI 助理。\n"
    "你的個性：\n"
    "- 說話自然、友善，帶點溫暖\n"
    "- 回答簡潔有重點，不廢話\n"
    "- 遇到不懂的問題會誠實說不知道\n"
    "- 不回答有害或不道德的內容\n\n"
    "請永遠用繁體中文回答。"
)

# ── 關鍵字回覆設定 ────────────────────────────────────────
KEYWORD_REPLIES = {
    "你好":     f"你好！我是 {AI_NAME}，有什麼我可以幫你的嗎？😊",
    "hi":       f"Hi！我是 {AI_NAME}，請問有什麼需要幫忙的？",
    "hello":    f"Hello！我是 {AI_NAME}，有什麼我可以幫你的嗎？",
    "功能":     "我可以幫你：\n📝 回答各種問題\n🖼️ 分析圖片內容\n🔑 觸發關鍵字功能\n📅 每日推播訂閱\n\n輸入「訂閱」可加入每日推播！",
    "訂閱":     "__SUBSCRIBE__",
    "取消訂閱": "__UNSUBSCRIBE__",
    "幫助":     f"輸入任何問題讓 {AI_NAME} 回答你！\n\n特殊指令：\n• 重置 / 清除對話\n• 訂閱 / 取消訂閱\n• 功能（查看所有功能）",
}

# ── 每日推播設定 ──────────────────────────────────────────
BROADCAST_HOUR   = 8
BROADCAST_MINUTE = 0
BROADCAST_MESSAGES = [
    f"☀️ 早安！我是 {AI_NAME}，今天也要加油喔！",
    f"📚 {AI_NAME} 小提醒：今天學一件新事物吧！",
    f"🌟 {AI_NAME} 說：保持好奇心，每天都有新發現！",
    f"💪 今天也是充滿可能的一天！有任何問題都可以問 {AI_NAME}。",
]

SUBSCRIBERS_FILE = "subscribers.json"

# ── 初始化 ────────────────────────────────────────────────
app           = Flask(__name__)
line_bot_api  = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler       = WebhookHandler(LINE_CHANNEL_SECRET)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

conversation_history = {}
MAX_HISTORY = 20


# ── 對話記憶 ──────────────────────────────────────────────
def get_history(user_id):
    if user_id not in conversation_history:
        conversation_history[user_id] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
    return conversation_history[user_id]


def trim_history(user_id):
    history = conversation_history[user_id]
    if len(history) > MAX_HISTORY + 1:
        conversation_history[user_id] = [history[0]] + history[-MAX_HISTORY:]


# ── 訂閱管理 ──────────────────────────────────────────────
def load_subscribers():
    if os.path.exists(SUBSCRIBERS_FILE):
        with open(SUBSCRIBERS_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_subscribers(subs):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(list(subs), f)


subscribers = load_subscribers()


def subscribe_user(user_id):
    subscribers.add(user_id)
    save_subscribers(subscribers)
    return f"✅ 訂閱成功！{AI_NAME} 每天早上 {BROADCAST_HOUR:02d}:{BROADCAST_MINUTE:02d} 會傳訊息給你 🌅"


def unsubscribe_user(user_id):
    subscribers.discard(user_id)
    save_subscribers(subscribers)
    return f"{AI_NAME} 已取消訂閱，輸入「訂閱」可重新加入 💬"


# ── 每日推播 ──────────────────────────────────────────────
def daily_broadcast():
    if not subscribers:
        return
    msg = random.choice(BROADCAST_MESSAGES)
    today = datetime.date.today().strftime("%Y/%m/%d")
    full_msg = f"📅 {today}\n\n{msg}"
    print(f"[推播] 傳送給 {len(subscribers)} 位訂閱者")
    for uid in list(subscribers):
        try:
            line_bot_api.push_message(uid, TextSendMessage(text=full_msg))
        except Exception as e:
            print(f"[推播失敗] uid={uid}, error={e}")


# ── APScheduler ───────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(
    daily_broadcast,
    trigger="cron",
    hour=BROADCAST_HOUR,
    minute=BROADCAST_MINUTE,
    id="daily_broadcast"
)
scheduler.start()


# ── OpenAI 文字對話 ───────────────────────────────────────
def chat_with_openai(user_id, user_message):
    history = get_history(user_id)
    history.append({"role": "user", "content": user_message})
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=history,
            temperature=0.7,
            max_tokens=1000,
        )
        reply = response.choices[0].message.content.strip()
        history.append({"role": "assistant", "content": reply})
        trim_history(user_id)
        return reply
    except Exception as e:
        history.pop()
        print(f"[OpenAI Error] {e}")
        return f"抱歉，{AI_NAME} 現在有點問題，請稍後再試 🙏"


# ── OpenAI 圖片辨識 ───────────────────────────────────────
def analyze_image_with_openai(image_bytes):
    try:
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"請用繁體中文詳細描述這張圖片的內容，"
                                "包括主要物件、場景、顏色、文字等。"
                                "如果圖片包含文字，請一併辨識出來。"
                                f"回答時以「{AI_NAME} 幫你看圖片：」開頭。"
                            )
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_image}",
                                "detail": "high"
                            }
                        }
                    ]
                }
            ],
            max_tokens=1000,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Vision Error] {e}")
        return f"抱歉，{AI_NAME} 無法辨識這張圖片，請再試一次 🙏"


# ── LINE Webhook ──────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200


# ── 處理文字訊息 ──────────────────────────────────────────
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id      = event.source.user_id
    user_message = event.message.text.strip()
    lower_msg    = user_message.lower()

    # 1. 重置對話
    if user_message in ["重置", "清除對話", "/reset", "/clear"]:
        conversation_history.pop(user_id, None)
        reply = f"好的！{AI_NAME} 已清除對話記憶，我們重新開始吧 😊"

    # 2. 關鍵字比對
    elif any(kw in user_message or kw in lower_msg for kw in KEYWORD_REPLIES):
        matched_key = next(
            (kw for kw in KEYWORD_REPLIES if kw in user_message or kw in lower_msg),
            None
        )
        action = KEYWORD_REPLIES[matched_key]
        if action == "__SUBSCRIBE__":
            reply = subscribe_user(user_id)
        elif action == "__UNSUBSCRIBE__":
            reply = unsubscribe_user(user_id)
        else:
            reply = action

    # 3. 一般 AI 對話
    else:
        reply = chat_with_openai(user_id, user_message)

    print(f"[User {user_id[:6]}...] {user_message}")
    print(f"[{AI_NAME}] {reply[:50]}...")

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )


# ── 處理圖片訊息 ──────────────────────────────────────────
@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id    = event.source.user_id
    message_id = event.message.id
    try:
        message_content = line_bot_api.get_message_content(message_id)
        image_bytes = b"".join(chunk for chunk in message_content.iter_content())
        reply = analyze_image_with_openai(image_bytes)
    except Exception as e:
        print(f"[Image Error] {e}")
        reply = f"抱歉，{AI_NAME} 無法取得這張圖片，請再傳一次 🙏"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )


# ── 健康檢查 ──────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health_check():
    return f"{AI_NAME} LINE Bot is running! 🚀 訂閱人數：{len(subscribers)}", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 {AI_NAME} LINE Bot 啟動中，port={port}")
    print(f"📅 每日推播時間：{BROADCAST_HOUR:02d}:{BROADCAST_MINUTE:02d}")
    app.run(host="0.0.0.0", port=port, debug=False)
