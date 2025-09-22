from flask import Flask, request, abort
import os
import json
import requests

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 健康檢查：Render 看到 200 OK 代表服務活著
@app.get("/")
def hello():
    return "OK", 200

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_TOKEN = os.getenv("LINE_CHANNEL_TOKEN")

line_bot_api = LineBotApi(LINE_CHANNEL_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 以 groupId 為 key 的投票狀態
state = {}  # { group_id: {"topic": str, "voted": set(user_id)} }

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event: MessageEvent):
    src = event.source
    if src.type != "group":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("請把我加到群組裡一起玩喔～（群組指令：/poll /vote /status /close）")
        )
        return

    group_id = src.group_id
    user_id = src.user_id
    text = event.message.text.strip()

    if text.startswith("/poll"):
        topic = text[5:].strip() or "未命名主題"
        state[group_id] = {"topic": topic, "voted": set()}
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"🗳️ 開新投票：{topic}\n請大家輸入 /vote 投票"))
        return

    if text == "/vote":
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("目前沒有進行中的投票。先用 `/poll 主題` 開始一輪吧！"))
            return
        voted = state[group_id]["voted"]
        if user_id in voted:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("已經記錄過你的投票囉 ✅"))
            return
        voted.add(user_id)
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("已記錄你的投票 ✅"))
        return

    if text == "/status":
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("目前沒有進行中的投票。用 `/poll 主題` 開始一輪。"))
            return
        topic = state[group_id]["topic"]
        voted = state[group_id]["voted"]
        all_ids = get_all_group_member_ids(group_id)
        cnt_all = len(all_ids)
        cnt_voted = len(voted)
        cnt_unvoted = len([uid for uid in all_ids if uid not in voted])
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"🗳️ {topic}\n已投：{cnt_voted} / 全部：{cnt_all}（未投：{cnt_unvoted}）"))
        return

    if text == "/close":
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("目前沒有進行中的投票。用 `/poll 主題` 開始一輪。"))
            return

        topic = state[group_id]["topic"]
        voted = state[group_id]["voted"]
        all_ids = get_all_group_member_ids(group_id)
        non_voters = [uid for uid in all_ids if uid not in voted]

        if not non_voters:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage(f"🎉 {topic} 已全員投票完成！太棒了！"))
        else:
            push_with_mentions(group_id, f"⏰ {topic} 截止：以下同學尚未投票：", non_voters)

        del state[group_id]
        return

    if text in ("/help", "help", "指令"):
        help_text = (
            "👉 指令清單：\n"
            "/poll <主題>  開一輪投票\n"
            "/vote         登記投票\n"
            "/status       查看進度\n"
            "/close        結算並 @ 未投的人\n"
            "——「催票鞭刑人」"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(help_text))
        return
    return

def get_all_group_member_ids(group_id: str):
    ids, start = [], None
    while True:
        res = line_bot_api.get_group_member_ids(group_id, start)
        ids.extend(res.member_ids)
        if res.next is None:
            break
        start = res.next
    return ids

def push_with_mentions(group_id: str, text: str, user_ids):
    body_text = text + " "
    mentionees = []
    for i, uid in enumerate(user_ids, start=1):
        tag = f"@user{i}"
        index = len(body_text)
        length = len(tag)
        body_text += tag + ("、" if i != len(user_ids) else "")
        mentionees.append({"index": index, "length": length, "userId": uid})

    payload = {
        "to": group_id,
        "messages": [{
            "type": "text",
            "text": body_text,
            "mention": {"mentionees": mentionees}
        }]
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}"
    }
    resp = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers=headers,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    if resp.status_code >= 300:
        print("Push mention failed:", resp.status_code, resp.text)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
