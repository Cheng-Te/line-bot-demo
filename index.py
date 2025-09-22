from flask import Flask, request
import os, json, re, requests

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

line_bot_api = LineBotApi(LINE_CHANNEL_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)

# ---------------- 狀態 ----------------
# 每個群的投票狀態（記憶體）
# state[group_id] = {
#   "topic": str,
#   "options": [str, ...],
#   "voted": { user_id: set([optIndex, ...]) }   # 0-based index
# }
state = {}

# 已知成員：凡在群內發過言或輸入 /join 的人
# known[group_id] = set(user_id)
known = {}

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

    # 記錄已知成員（發過言的人）
    known.setdefault(group_id, set()).add(user_id)

    # /help
    if text in ("/help", "help", "指令"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            "👉 指令：\n"
            "/poll 主題 | 選項1, 選項2, ...  開新投票（支援半形|或全形｜）\n"
            "/vote <序號(可多個)>            投票（例：/vote 1 或 /vote 1 3）\n"
            "/unvote <序號(可多個)>          取消已選（例：/unvote 2）\n"
            "/status                         目前進度（已投人數）\n"
            "/stats                          詳細統計（含誰投了哪些選項）\n"
            "/remind                         提醒並 @『已知但未投』的人\n"
            "/close                          結算並 @『已知但未投』的人，清空本輪\n"
            "/join                           登記自己（安靜成員輸入一次即可）\n"
        ))
        return

    # /join：讓安靜成員自助註冊
    if text == "/join":
        known.setdefault(group_id, set()).add(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            "已登記你在這輪投票中的身分。之後可被提醒（@）到～"
        ))
        return

    # /poll 主題 | 選項1, 選項2, ...
    if text.startswith("/poll"):
        topic, options = parse_poll_command(text)
        if not options:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                "格式範例：\n/poll 測試投票 | 9/1, 9/2, 9/3\n"
                "（注意：請用半形逗號 , 分隔；分隔符可用半形 | 或全形 ｜）"
            ))
            return
        state[group_id] = {"topic": topic or "未命名主題", "options": options, "voted": {}}

        # Quick Reply（單次點選）
        buttons = []
        for i, opt in enumerate(options, start=1):
            buttons.append(QuickReplyButton(action=MessageAction(label=f"{i}. {opt}", text=f"/vote {i}")))
        qr = QuickReply(items=buttons[:13])  # 上限 13

        tip = "（多選請用指令：例如 `/vote 1 3`；沒發言的同學請輸入 `/join` 後才提醒得到你）"
        line_bot_api.reply_message(event.reply_token, [
            TextSendMessage(
                text=f"🗳️ 開新投票：{state[group_id]['topic']}\n選項：\n" +
                     "\n".join([f"{i+1}. {o}" for i, o in enumerate(options)]) +
                     f"\n\n請點快速按鈕或輸入 `/vote <序號>` 投票\n{tip}",
                quick_reply=qr
            )
        ])
        return

    # /vote 1 或 /vote 1 3
    if text.startswith("/vote"):
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有進行中的投票。"))
            return
        idxs = parse_indices(text)
        if not idxs:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請提供選項序號（例：/vote 1 或 /vote 1 3）"))
            return

        poll = state[group_id]
        max_idx = len(poll["options"])
        picked = {i for i in idxs if 1 <= i <= max_idx}
        if not picked:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("選項序號超出範圍。"))
            return

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
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                f"🗑️ 已取消：{', '.join(map(str, sorted(removed)))}"
            ))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("沒有可取消的選項。"))
        return

    # /status
    if text == "/status":
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有進行中的投票。"))
            return
        poll = state[group_id]
        voters_count = len([uid for uid, picks in poll["voted"].items() if picks])
        msg = (
            f"🗳️ {poll['topic']}\n"
            f"已投：{voters_count} 人（已知成員：{len(known.get(group_id, set()))}）"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
        return

    # /stats（含誰投了哪些選項；用遮罩的 userId 顯示）
    if text == "/stats":
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有進行中的投票。"))
            return
        poll = state[group_id]
        counts = tally_counts(poll["options"], poll["voted"])

        # 反查各選項有哪些人投
        by_option = [[] for _ in poll["options"]]
        for uid, picks in poll["voted"].items():
            for idx in picks:
                if 0 <= idx < len(by_option):
                    by_option[idx].append(mask_uid(uid))

        lines = [f"📊 {poll['topic']} 統計："]
        for i, (opt, c) in enumerate(zip(poll["options"], counts), start=1):
            detail = "、".join(by_option[i-1]) if by_option[i-1] else "（無）"
            lines.append(f"{i}. {opt} － {c} 票 〔{detail}〕")
        line_bot_api.reply_message(event.reply_token, TextSendMessage("\n".join(lines)))
        return

    # /remind：只 @『已知但未投』的人（分批送）
    if text == "/remind":
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有進行中的投票。"))
            return
        poll = state[group_id]
        known_ids = known.get(group_id, set())
        voters   = {uid for uid, picks in poll["voted"].items() if picks}
        non_voters = sorted([uid for uid in known_ids if uid not in voters])

        if not non_voters:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                "目前沒有可提醒的未投人（或尚無已知成員）。"
            ))
            return

        push_with_mentions_batched(
            group_id,
            f"不投票是想被我鞭嗎 —— {poll['topic']} 提醒：",
            non_voters,
            batch_size=20
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            f"已提醒 {len(non_voters)} 位已知未投的人。"
        ))
        return

    # /close：結算 + @ 已知未投；清空本輪
    if text == "/close":
        if group_id not in state:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有進行中的投票。"))
            return
        poll = state[group_id]
        counts = tally_counts(poll["options"], poll["voted"])

        # 統計訊息
        by_option = [[] for _ in poll["options"]]
        for uid, picks in poll["voted"].items():
            for idx in picks:
                if 0 <= idx < len(by_option):
                    by_option[idx].append(mask_uid(uid))

        result_lines = [f"📦 {poll['topic']} 結算："]
        for i, (opt, c) in enumerate(zip(poll["options"], counts), start=1):
            detail = "、".join(by_option[i-1]) if by_option[i-1] else "（無）"
            result_lines.append(f"{i}. {opt} － {c} 票 〔{detail}〕")
        line_bot_api.reply_message(event.reply_token, TextSendMessage("\n".join(result_lines)))

        # @ 已知未投
        known_ids = known.get(group_id, set())
        voters    = {uid for uid, picks in poll["voted"].items() if picks}
        non_voters = sorted([uid for uid in known_ids if uid not in voters])
        if non_voters:
            push_with_mentions_batched(
                group_id,
                f"不投票是想被我鞭嗎 —— {poll['topic']} 截止提醒：",
                non_voters,
                batch_size=20
            )
        else:
            push_text(group_id, "🎉 已知成員皆完成投票，太讚了！")

        # 清掉本輪（可視需求是否保留 known）
        del state[group_id]
        return

    # 其他訊息不處理
    return

# ---------------- 工具方法 ----------------
def parse_poll_command(text: str):
    """支援半形|與全形｜，逗號請用半形 ,"""
    m = re.match(r"^/poll\s*(.*)$", text, flags=re.I)
    if not m:
        return None, []
    payload = m.group(1).strip()
    # 全形｜轉半形|
    payload = payload.replace("｜", "|")
    topic, options_part = None, ""
    if "|" in payload:
        topic, options_part = [p.strip() for p in payload.split("|", 1)]
    else:
        # 沒給 |，嘗試用第一個逗號前當主題
        parts = [p.strip() for p in payload.split(",")]
        if len(parts) >= 2:
            topic = parts[0]
            options_part = ", ".join(parts[1:])
        else:
            return payload, []
    options = [o.strip() for o in options_part.split(",") if o.strip()]
    # 去重保序
    seen, uniq = set(), []
    for o in options:
        if o not in seen:
            uniq.append(o); seen.add(o)
    return topic, uniq

def parse_indices(text: str):
    nums = re.findall(r"\d+", text)
    return [int(n) for n in nums]

def tally_counts(options, voted_map):
    counts = [0] * len(options)
    for picks in voted_map.values():
        for idx in picks:
            if 0 <= idx < len(options):
                counts[idx] += 1
    return counts

def mask_uid(uid: str):
    # 遮罩顯示用：U123456…abcd
    if not uid or len(uid) < 8:
        return uid
    return f"{uid[:6]}…{uid[-4:]}"

def push_text(group_id: str, text: str):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}"}
    payload = {"to": group_id, "messages": [{"type": "text", "text": text}]}
    requests.post("https://api.line.me/v2/bot/message/push",
                  headers=headers,
                  data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))

def push_with_mentions(group_id: str, prefix: str, user_ids):
    # 建文字 + 計算每個 @ 的 index/length
    body_text = prefix + " "
    spans = []  # 收集 (index, length, userId)
    for i, uid in enumerate(user_ids, start=1):
        tag = f"@user{i}"
        index = len(body_text)
        length = len(tag)
        body_text += tag + ("、" if i != len(user_ids) else "")
        spans.append({"index": index, "length": length, "userId": uid})

    # ✅ 使用 Text v2 的 entities.mention（取代舊的 "mention": {...} 寫法）
    payload = {
        "to": group_id,
        "messages": [{
            "type": "text",
            "text": body_text,
            "entities": {
                "mention": spans
            }
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

def push_with_mentions_batched(group_id, prefix, user_ids, batch_size=20):
    for i in range(0, len(user_ids), batch_size):
        batch = user_ids[i:i+batch_size]
        push_with_mentions(group_id, prefix, batch)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
