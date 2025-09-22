from flask import Flask, request
import os, json, requests, re

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    QuickReply, QuickReplyButton, MessageAction
)

app = Flask(__name__)

# 健康檢查
@app.route("/", methods=["GET", "POST"])
def hello():
    return "OK", 200

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_TOKEN  = os.getenv("LINE_CHANNEL_TOKEN")
REMIND_SECRET       = os.getenv("REMIND_SECRET")  # 可留空；若要外部排程打 /remind-all 再設定

line_bot_api = LineBotApi(LINE_CHANNEL_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)

# ---------------- 投票狀態（記憶體） ----------------
# state[group_id] = {
#   "topic": str,
#   "options": [str, ...],
#   "voted": { user_id: set([index, ...]) }
# }
state = {}

# ---------------- Webhook ----------------
@app.route("/webhook", methods=["GET", "POST"])
@app.route("/webhook/", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "OK", 200
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        return "OK", 200
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "OK", 200
    return "OK", 200

# ---------------- 指令處理 ----------------
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event: MessageEvent):
    src = event.source
    if src.type != "group":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("請把我加到群組使用喔～（/help 看指令）")
        )
        return

    group_id = src.group_id
    user_id  = src.user_id
    text     = event.message.text.strip()

    # /help
    if text in ("/help", "help", "指令"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            "👉 指令：\n"
            "/poll 主題 | 選項1, 選項2, 選項3  開新投票（自訂主題與選項）\n"
            "/vote <選項序號(可多個)>      投票（例：/vote 1 或 /vote 1 3）\n"
            "/unvote <序號>                取消某個選項（例：/unvote 2）\n"
            "/status                       目前統計＋未投人數\n"
            "/stats                        詳細統計（各選項人數）\n"
            "/remind                       進行中提醒並 @ 未投的人\n"
            "/close                        結算並 @ 未投的人，並清空本輪\n"
            "小技巧：建立投票會附上快速按鈕，點一下就能投票。\n"
        ))
        return

    # /poll 主題 | 選項1, 選項2, 選項3
    if text.startswith("/poll"):
        topic, options = parse_poll_command(text)
        if not options:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                "格式範例：\n/poll 午餐要吃什麼？ | 便當, 麵, 火鍋"
            ))
            return
        state[group_id] = {"topic": topic or "未命名主題", "options": options, "voted": {}}

        # 建立快速按鈕（每個選項一顆，點擊會送出 /vote <index>）
        buttons = []
        for i, opt in enumerate(options, start=1):
            buttons.append(QuickReplyButton(action=MessageAction(label=f"{i}. {opt}", text=f"/vote {i}")))
        qr = QuickReply(items=buttons[:13])  # Quick Reply 上限 13

        line_bot_api.reply_message(event.reply_token, [
            TextSendMessage(
                text=f"🗳️ 開新投票：{state[group_id]['topic']}\n選項：\n" + "\n".join(
                    [f"{i+1}. {o}" for i, o in enumerate(options)]
                ) + "\n\n請點快速按鈕或輸入 `/vote <序號>` 投票",
                quick_reply=qr
            )
        ])
        return

    # /vote 1 或 /vote 1 3（多選）
    if text.startswith("/vote"):
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有進行中的投票。先用 `/poll` 開始一輪吧！"))
            return
        idxs = parse_indices(text)
        if not idxs:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請提供選項序號（例：/vote 1 或 /vote 1 3）"))
            return

        poll = state[group_id]
        max_idx = len(poll["options"])
        # 過濾合法序號
        picked = {i for i in idxs if 1 <= i <= max_idx}
        if not picked:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("選項序號超出範圍。"))
            return

        # 多選：累積到使用者的 set
        voted_map = poll["voted"]
        prev = voted_map.get(user_id, set())
        newset = set(prev) | {i-1 for i in picked}  # 轉 0-based
        voted_map[user_id] = newset

        pretty = ", ".join([f"{i}. {poll['options'][i-1]}" for i in sorted(picked)])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已登記你的投票：{pretty}"))
        return

    # /unvote 2
    if text.startswith("/unvote"):
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有進行中的投票。"))
            return
        idxs = parse_indices(text)
        if not idxs:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請提供要取消的序號（例：/unvote 2）"))
            return
        poll = state[group_id]
        chosen = poll["voted"].get(user_id, set())
        removed = set()
        for i in idxs:
            if 1 <= i <= len(poll["options"]) and (i-1) in chosen:
                chosen.remove(i-1)
                removed.add(i)
        poll["voted"][user_id] = chosen
        if removed:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(f"🗑️ 已取消：{', '.join(map(str, sorted(removed)))}"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("沒有可取消的選項。"))
        return

    # /status
    if text == "/status":
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有進行中的投票。"))
            return
        poll = state[group_id]
        all_ids = get_all_group_member_ids(group_id)
        non_voters = calc_non_voters(all_ids, poll["voted"])
        msg = (
            f"🗳️ {poll['topic']}\n"
            f"已投：{len(poll['voted'])} / 全部：{len(all_ids)}（未投：{len(non_voters)}）"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
        return

    # /stats 詳細統計
    if text == "/stats":
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有進行中的投票。"))
            return
        poll = state[group_id]
        counts = tally_counts(poll["options"], poll["voted"])
        lines = [f"📊 {poll['topic']} 統計："]
        for i, (opt, c) in enumerate(zip(poll["options"], counts), start=1):
            lines.append(f"{i}. {opt} － {c} 票")
        line_bot_api.reply_message(event.reply_token, TextSendMessage("\n".join(lines)))
        return

    # /remind（不中止投票，@ 未投）
    if text == "/remind":
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有進行中的投票。"))
            return
        poll = state[group_id]
        all_ids = get_all_group_member_ids(group_id)
        non_voters = calc_non_voters(all_ids, poll["voted"])
        if not non_voters:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有未投的人 👍"))
        else:
            push_with_mentions(group_id, f"不投票是想被我鞭嗎 —— {poll['topic']} 提醒：", non_voters)
        return

    # /close（結算並 @ 未投，清空）
    if text == "/close":
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有進行中的投票。"))
            return
        poll = state[group_id]
        all_ids = get_all_group_member_ids(group_id)
        non_voters = calc_non_voters(all_ids, poll["voted"])

        # 統計訊息
        counts = tally_counts(poll["options"], poll["voted"])
        result_lines = [f"📦 {poll['topic']} 結算："]
        for i, (opt, c) in enumerate(zip(poll["options"], counts), start=1):
            result_lines.append(f"{i}. {opt} － {c} 票")
        line_bot_api.reply_message(event.reply_token, TextSendMessage("\n".join(result_lines)))

        # @ 未投
        if non_voters:
            push_with_mentions(group_id, f"不投票是想被我鞭嗎 —— {poll['topic']} 截止提醒：", non_voters)
        else:
            push_text(group_id, "🎉 全員完成投票，太讚了！")

        del state[group_id]
        return

    # 其它訊息不回（避免干擾）
    return

# ---------------- 外部排程（可選）：提醒所有群 ----------------
@app.get("/remind-all")
def remind_all():
    # 若要開放外部排程（cron-job.org）打這個端點，請在 Render 設 REMIND_SECRET
    if REMIND_SECRET and request.args.get("key") != REMIND_SECRET:
        return "forbidden", 403

    # 對所有有進行中投票的群，@ 未投
    seen = 0
    for gid, poll in list(state.items()):
        all_ids = get_all_group_member_ids(gid)
        non_voters = calc_non_voters(all_ids, poll["voted"])
        if non_voters:
            push_with_mentions(gid, f"不投票是想被我鞭嗎 —— {poll['topic']} 例行提醒：", non_voters)
            seen += 1
    return f"ok groups={seen}", 200

# ---------------- 工具方法 ----------------
def parse_poll_command(text: str):
    """
    /poll 主題 | 選項1, 選項2, 選項3
    """
    m = re.match(r"^/poll\s*(.*)$", text, flags=re.I)
    if not m:
        return None, []
    payload = m.group(1).strip()
    topic, options_part = None, ""
    if "|" in payload:
        topic, options_part = [p.strip() for p in payload.split("|", 1)]
    else:
        # 沒給 | 選項，嘗試只用逗號當分隔
        parts = [p.strip() for p in payload.split(",")]
        if len(parts) >= 2:
            topic = parts[0]
            options_part = ", ".join(parts[1:])
        else:
            return payload, []
    options = [o.strip() for o in options_part.split(",") if o.strip()]
    # 去重、保留順序
    seen = set()
    uniq = []
    for o in options:
        if o not in seen:
            uniq.append(o); seen.add(o)
    return topic, uniq

def parse_indices(text: str):
    # 從 "/vote 1 3" 或 "/unvote 2" 抽出數字序號
    nums = re.findall(r"\d+", text)
    return [int(n) for n in nums]

def get_all_group_member_ids(group_id: str):
    ids, start = [], None
    while True:
        res = line_bot_api.get_group_member_ids(group_id, start)
        ids.extend(res.member_ids)
        if res.next is None:
            break
        start = res.next
    return ids

def calc_non_voters(all_ids, voted_map):
    # 未投：沒出現在 voted_map 的人，或 set 為空
    voters = {uid for uid, picks in voted_map.items() if picks}
    return [uid for uid in all_ids if uid not in voters]

def tally_counts(options, voted_map):
    counts = [0] * len(options)
    for picks in voted_map.values():
        for idx in picks:
            if 0 <= idx < len(options):
                counts[idx] += 1
    return counts

def push_text(group_id: str, text: str):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}"}
    payload = {"to": group_id, "messages": [{"type": "text", "text": text}]}
    requests.post("https://api.line.me/v2/bot/message/push",
                  headers=headers,
                  data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))

def push_with_mentions(group_id: str, prefix: str, user_ids):
    body_text = prefix + " "
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
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}"}
    resp = requests.post("https://api.line.me/v2/bot/message/push",
                         headers=headers,
                         data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if resp.status_code >= 300:
        print("Push mention failed:", resp.status_code, resp.text)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
