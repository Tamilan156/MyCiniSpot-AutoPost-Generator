import os
import re
import json
import time
import logging
import threading
from html import escape
from urllib.parse import urlparse

import requests
import telebot
from telebot import types
from flask import Flask, jsonify

# ============================================================
# My Cini Spot Auto Post Bot
# Render start command: python main.py
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("MyCiniSpot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
SOURCE_GROUP_IDS = {
    int(x.strip()) for x in os.getenv("SOURCE_GROUP_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
OMDB_API_KEY = os.getenv("OMDB_API_KEY", "").strip()
POST_FOOTER = os.getenv("POST_FOOTER", "").strip()
STATE_FILE = os.getenv("STATE_FILE", "/tmp/mycinispot_state.json")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it in Render Environment Variables.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)
app = Flask(__name__)

tasks = []
targets = set()
panel_ids = {}
current_index = {}
category_filter = {}
manual_states = {}
edit_states = {}
lock = threading.RLock()


# ---------------- STORAGE ----------------

def save_data():
    with lock:
        temp = STATE_FILE + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(
                {"tasks": tasks, "targets": sorted(targets)},
                f, ensure_ascii=False, indent=2
            )
        os.replace(temp, STATE_FILE)


def load_data():
    global tasks, targets
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        tasks = data.get("tasks", [])
        targets = set(str(x) for x in data.get("targets", []))
        log.info("Loaded %s tasks and %s channels", len(tasks), len(targets))
    except FileNotFoundError:
        log.info("No saved state found; starting fresh")
    except Exception:
        log.exception("State loading failed")


def is_admin(uid):
    return uid in ADMIN_IDS


def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------- POST PARSER ----------------

SIZE_LINK_RE = re.compile(
    r"(?im)^\s*([0-9]+(?:\.[0-9]+)?\s*(?:MB|GB)(?:\s*\([^)]*\))?)"
    r"\s*[-:]\s*(https?://\S+)\s*$"
)
URL_SIZE_RE = re.compile(
    r"(?im)^\s*(https?://\S+)\s*[-:]\s*"
    r"([0-9]+(?:\.[0-9]+)?\s*(?:MB|GB)(?:\s*\([^)]*\))?)\s*$"
)


def clean_url(url):
    return url.strip().rstrip(").,]>}")


def parse_post(text):
    """Accepts Title/Year/Audio/Quality labels and size-link lines."""
    if not text:
        return None

    title_match = re.search(r"(?im)^\s*(?:🎬\s*)?Title\s*:\s*(.+?)\s*$", text)
    year_match = re.search(r"(?im)^\s*(?:📅\s*)?Year\s*:\s*(\d{4})\b", text)
    audio_match = re.search(r"(?im)^\s*(?:🔊\s*)?Audio\s*:\s*(.+?)\s*$", text)
    quality_match = re.search(r"(?im)^\s*(?:💿\s*)?Quality\s*:\s*(.+?)\s*$", text)

    links = []
    for size, url in SIZE_LINK_RE.findall(text):
        links.append({"size": size.strip(), "url": clean_url(url)})
    for url, size in URL_SIZE_RE.findall(text):
        links.append({"size": size.strip(), "url": clean_url(url)})

    # Auto-detect a title from the first useful non-metadata line
    if title_match:
        title = title_match.group(1).strip()
    else:
        title = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if re.match(r"(?i)^(year|audio|quality|size|links?)\s*:", line):
                continue
            if re.match(r"(?i)^https?://", line):
                continue
            if SIZE_LINK_RE.match(line) or URL_SIZE_RE.match(line):
                continue
            title = re.sub(r"^[🎬📺🎥\s]+", "", line).strip()
            break

    if not title or not links:
        return None

    unique_links = []
    seen = set()
    for item in links:
        url = item["url"]
        if not url or url in seen or not valid_url(url):
            continue
        seen.add(url)
        unique_links.append(item)

    if not unique_links:
        return None

    category = (
        "webseries"
        if re.search(r"(?i)#webseries|web\s*series|series\s*:", text)
        else "movies"
    )

    return {
        "title": title[:200],
        "year": year_match.group(1) if year_match else "",
        "audio": audio_match.group(1).strip() if audio_match else "",
        "quality": quality_match.group(1).strip() if quality_match else "",
        "links": unique_links,
        "category": category,
        "status": "Pending",
        "poster": None,
        "rating": "",
        "created_at": int(time.time())
    }


def valid_url(value):
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


# ---------------- POSTER / RATING ----------------

def get_poster(title, year=""):
    if not TMDB_API_KEY:
        return None
    try:
        params = {"api_key": TMDB_API_KEY, "query": title}
        if year:
            params["year"] = year
        response = requests.get(
            "https://api.themoviedb.org/3/search/movie",
            params=params, timeout=12
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        if results and results[0].get("poster_path"):
            return "https://image.tmdb.org/t/p/w500" + results[0]["poster_path"]
    except Exception:
        log.exception("TMDB poster lookup failed")
    return None


def get_rating(title, year=""):
    if not OMDB_API_KEY:
        return ""
    try:
        response = requests.get(
            "https://www.omdbapi.com/",
            params={"apikey": OMDB_API_KEY, "t": title, "y": year},
            timeout=12
        )
        response.raise_for_status()
        data = response.json()
        rating = data.get("imdbRating", "")
        if data.get("Response") == "True" and rating and rating != "N/A":
            return f"⭐ IMDb Rating: {rating}/10"
    except Exception:
        log.exception("OMDb rating lookup failed")
    return ""


def enrich_task(task):
    if not task.get("poster"):
        task["poster"] = get_poster(task.get("title", ""), task.get("year", ""))
    if not task.get("rating"):
        task["rating"] = get_rating(task.get("title", ""), task.get("year", ""))


# ---------------- CAPTION ----------------

def post_caption(task):
    category = "📺 Web Series" if task.get("category") == "webseries" else "🎬 Movies"
    lines = [
        f"<b>{category}</b>",
        f"🎬 <b>{escape(str(task.get('title', '')))}</b>"
    ]

    if task.get("year"):
        lines.append(f"📅 Year: {escape(str(task['year']))}")
    if task.get("audio"):
        lines.append(f"🔊 Audio: {escape(str(task['audio']))}")
    if task.get("quality"):
        lines.append(f"💿 Quality: {escape(str(task['quality']))}")
    if task.get("rating"):
        lines.append(escape(str(task["rating"])))

    lines.extend(["", "#FastDownload ⚡"])

    for item in task.get("links", []):
        suffix = " [ #Single_File ]" if task.get("category") == "webseries" else ""
        lines.append(
            f"📦 {escape(str(item.get('size', '')))} - "
            f'<a href="{escape(str(item.get("url", "")), quote=True)}">Download Link</a>'
            f"{suffix}"
        )

    if POST_FOOTER:
        lines.extend(["", escape(POST_FOOTER)])
    return "\n".join(lines)


# ---------------- TASK MANAGEMENT ----------------

def add_task(new_task):
    key = (new_task["title"].strip().lower(), new_task.get("year", ""))
    with lock:
        existing = next(
            (
                task for task in reversed(tasks)
                if (task.get("title", "").strip().lower(), task.get("year", "")) == key
                and task.get("status") == "Pending"
            ),
            None
        )

        if existing:
            known = {item.get("url") for item in existing.get("links", [])}
            for item in new_task.get("links", []):
                if item.get("url") not in known:
                    existing.setdefault("links", []).append(item)
                    known.add(item.get("url"))
            if not existing.get("poster") and new_task.get("poster"):
                existing["poster"] = new_task["poster"]
            if not existing.get("rating") and new_task.get("rating"):
                existing["rating"] = new_task["rating"]
            result = existing
        else:
            tasks.append(new_task)
            result = new_task

        save_data()
        return result


def visible_tasks(uid):
    category = category_filter.get(uid, "all")
    with lock:
        items = list(tasks)
    if category == "all":
        return items
    return [task for task in items if task.get("category") == category]


def current_task(uid):
    items = visible_tasks(uid)
    if not items:
        return None, -1
    index = max(0, min(current_index.get(uid, 0), len(items) - 1))
    current_index[uid] = index
    return items[index], index


def find_task_index(task):
    with lock:
        for index, item in enumerate(tasks):
            if item is task:
                return index
        # Fallback for copied dicts
        for index, item in enumerate(tasks):
            if item.get("created_at") == task.get("created_at") and item.get("title") == task.get("title"):
                return index
    return -1


def panel_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(types.InlineKeyboardButton("📤 Post", callback_data="post"))
    kb.row(types.InlineKeyboardButton("➕ Manual Add Post", callback_data="manual_add"))
    kb.row(
        types.InlineKeyboardButton("🎬 Movies", callback_data="movies"),
        types.InlineKeyboardButton("📺 Web Series", callback_data="webseries")
    )
    kb.row(
        types.InlineKeyboardButton("⬅️ Previous", callback_data="prev"),
        types.InlineKeyboardButton("Next ➡️", callback_data="next")
    )
    kb.row(
        types.InlineKeyboardButton("👁️ Preview", callback_data="preview"),
        types.InlineKeyboardButton("✏️ Edit", callback_data="edit")
    )
    kb.row(types.InlineKeyboardButton("📢 Select Channel", callback_data="channels"))
    kb.row(
        types.InlineKeyboardButton("✅ Approve", callback_data="approve"),
        types.InlineKeyboardButton("❌ Reject", callback_data="reject")
    )
    kb.row(types.InlineKeyboardButton("🔄 Refresh", callback_data="refresh"))
    return kb


def panel_text(uid):
    task, index = current_task(uid)
    items = visible_tasks(uid)
    if not task:
        return (
            "🗂 <b>MY CINI SPOT TASK PANEL</b>\n\n"
            "📭 <b>No posts available.</b>\n"
            "➕ Manual Add Post மூலம் புதிய post சேர்க்கலாம்."
        )
    return (
        "🗂 <b>MY CINI SPOT TASK PANEL</b>\n"
        f"📌 Task: {index + 1}/{len(items)}\n"
        f"📍 Status: <b>{escape(str(task.get('status', 'Pending')))}</b>\n\n"
        + post_caption(task)
    )


def show_panel(uid, chat_id=None):
    chat_id = chat_id or uid
    text = panel_text(uid)
    markup = panel_keyboard()
    try:
        mid = panel_ids.get(uid)
        if mid:
            bot.edit_message_text(
                text, chat_id, mid, reply_markup=markup,
                disable_web_page_preview=True
            )
        else:
            msg = bot.send_message(
                chat_id, text, reply_markup=markup,
                disable_web_page_preview=True
            )
            panel_ids[uid] = msg.message_id
    except Exception:
        log.exception("Panel update failed; sending a new panel")
        try:
            msg = bot.send_message(
                chat_id, text, reply_markup=markup,
                disable_web_page_preview=True
            )
            panel_ids[uid] = msg.message_id
        except Exception:
            log.exception("Panel send failed")


def show_channels(uid, message_id=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for channel_id in sorted(targets):
        kb.add(types.InlineKeyboardButton(
            f"❌ Remove {channel_id}",
            callback_data=f"channel_remove:{channel_id}"
        ))
    kb.add(types.InlineKeyboardButton("➕ Add Channel Help", callback_data="channel_help"))
    kb.add(types.InlineKeyboardButton("⬅️ Back to Panel", callback_data="refresh"))
    text = (
        "📢 <b>SELECT CHANNELS</b>\n\n"
        "இந்த பட்டியலில் உள்ள Channel-களுக்கு Post அனுப்பப்படும்.\n"
        "Channel நீக்க அதன் button-ஐ அழுத்தவும்.\n\n"
        "புதிய Channel சேர்க்க:\n"
        "<code>/addchannel -1001234567890</code>\n\n"
        f"Total: <b>{len(targets)}</b>"
    )
    try:
        if message_id:
            bot.edit_message_text(text, uid, message_id, reply_markup=kb)
        else:
            bot.send_message(uid, text, reply_markup=kb)
    except Exception:
        log.exception("Channel panel failed")


# ---------------- MANUAL ADD ----------------

def manual_start(uid, chat_id):
    manual_states[uid] = {
        "step": "category",
        "chat_id": chat_id,
        "task": {
            "title": "", "year": "", "audio": "", "quality": "",
            "links": [], "category": "movies", "status": "Pending",
            "poster": None, "rating": "", "created_at": int(time.time())
        }
    }
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎬 Movies", callback_data="manual_movie"),
        types.InlineKeyboardButton("📺 Web Series", callback_data="manual_series")
    )
    kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="manual_cancel"))
    bot.send_message(chat_id, "➕ <b>MANUAL POST ADD</b>\n\nSelect Post Category:", reply_markup=kb)


def manual_prompt(uid, message):
    bot.send_message(
        manual_states[uid]["chat_id"], message,
        reply_markup=types.ReplyKeyboardRemove()
    )


def manual_preview(uid):
    state = manual_states[uid]
    task = state["task"]
    text = "👁️ <b>MANUAL POST PREVIEW</b>\n\n" + post_caption(task) + "\n\n<b>Save this post?</b>"
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("💾 Save Post", callback_data="manual_save"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="manual_cancel")
    )
    try:
        if task.get("poster"):
            bot.send_photo(uid, task["poster"], caption=text, reply_markup=kb)
        else:
            bot.send_message(uid, text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        log.exception("Manual preview failed")
        bot.send_message(uid, text, reply_markup=kb, disable_web_page_preview=True)


def manual_ask_poster(uid):
    manual_states[uid]["step"] = "poster"
    manual_prompt(uid, "🖼️ Poster Image URL அனுப்பவும்.\n\nPoster வேண்டாம் என்றால் Skip என்று அனுப்பவும்.")


def manual_receive(message):
    uid = message.from_user.id
    if uid not in manual_states or message.chat.type != "private":
        return False

    value = (message.text or "").strip()
    if not value:
        manual_prompt(uid, "❌ Text மட்டும் அனுப்புங்கள்.")
        return True

    if value.lower() == "/cancel":
        manual_states.pop(uid, None)
        bot.send_message(uid, "❌ Manual Add Cancelled.")
        show_panel(uid)
        return True

    state = manual_states[uid]
    task = state["task"]
    step = state["step"]

    if step == "title":
        if len(value) > 200:
            manual_prompt(uid, "Title மிகவும் நீளமாக உள்ளது.")
            return True
        task["title"] = value
        state["step"] = "year"
        manual_prompt(uid, "📅 Release Year உள்ளிடவும். தெரியாவிட்டால் Skip.")
    elif step == "year":
        if value.lower() != "skip":
            if not re.fullmatch(r"\d{4}", value):
                manual_prompt(uid, "❌ சரியான 4 இலக்க Year கொடுக்கவும் அல்லது Skip.")
                return True
            task["year"] = value
        state["step"] = "audio"
        manual_prompt(uid, "🔊 Audio Language உள்ளிடவும். உதா: Tamil + English (அல்லது Skip)")
    elif step == "audio":
        task["audio"] = "" if value.lower() == "skip" else value
        state["step"] = "quality"
        manual_prompt(uid, "💿 Quality உள்ளிடவும். உதா: 1080p WEB-DL (அல்லது Skip)")
    elif step == "quality":
        task["quality"] = "" if value.lower() == "skip" else value
        state["step"] = "size"
        manual_prompt(uid, "📦 File Size உள்ளிடவும். உதா: 700MB")
    elif step == "size":
        if not re.fullmatch(r"\d+(?:\.\d+)?\s*(?:MB|GB)", value, re.I):
            manual_prompt(uid, "❌ சரியான Size கொடுக்கவும். உதா: 700MB அல்லது 1.5GB")
            return True
        state["pending_size"] = value
        state["step"] = "link"
        manual_prompt(uid, "🔗 Download Link அனுப்பவும் (https://...)")
    elif step == "link":
        if not valid_url(value):
            manual_prompt(uid, "❌ சரியான HTTP/HTTPS Link அனுப்பவும்.")
            return True
        size = state.pop("pending_size", "")
        if not any(item["url"] == value for item in task["links"]):
            task["links"].append({"size": size, "url": value})
        state["step"] = "more_links"
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("➕ Add Another Link", callback_data="manual_more"),
            types.InlineKeyboardButton("➡️ Continue", callback_data="manual_done")
        )
        bot.send_message(uid, "✅ Download Link Added!\n\nவேறு Link சேர்க்க வேண்டுமா?", reply_markup=kb)
    elif step == "poster":
        if value.lower() == "skip":
            task["poster"] = None
        elif valid_url(value):
            task["poster"] = value
        else:
            manual_prompt(uid, "❌ சரியான Poster URL கொடுக்கவும் அல்லது Skip.")
            return True
        state["step"] = "preview"
        manual_preview(uid)
    return True


# ---------------- COMMANDS ----------------

@bot.message_handler(commands=["start", "panel"])
def start(message):
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "⛔ Admin only.")
        return
    show_panel(uid, message.chat.id)


@bot.message_handler(commands=["cancel"])
def cancel(message):
    uid = message.from_user.id
    if uid in manual_states:
        manual_states.pop(uid, None)
        bot.reply_to(message, "❌ Manual Add Cancelled.")
        show_panel(uid, message.chat.id)
    elif uid in edit_states:
        edit_states.pop(uid, None)
        bot.reply_to(message, "❌ Edit Cancelled.")
        show_panel(uid, message.chat.id)


@bot.message_handler(commands=["addchannel"])
def add_channel(message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        bot.reply_to(message, "Usage: /addchannel -1001234567890")
        return
    try:
        chat = bot.get_chat(parts[1].strip())
        me = bot.get_me()
        member = bot.get_chat_member(chat.id, me.id)
        if member.status != "administrator":
            bot.reply_to(message, "முதலில் Bot-ஐ Channel Admin ஆக்குங்கள்.")
            return
        targets.add(str(chat.id))
        save_data()
        bot.reply_to(message, f"✅ Channel added: <code>{chat.id}</code>")
    except Exception:
        log.exception("Channel add failed")
        bot.reply_to(message, "Channel add ஆகவில்லை. Bot Admin என்பதை சரிபார்க்கவும்.")


@bot.message_handler(commands=["targets"])
def show_targets(message):
    if is_admin(message.from_user.id):
        bot.reply_to(message, "\n".join(sorted(targets)) if targets else "No target channels added.")


@bot.message_handler(commands=["myid"])
def my_id(message):
    bot.reply_to(message, f"Your Telegram ID: <code>{message.from_user.id}</code>")


# ---------------- PUBLISH / PREVIEW HELPERS ----------------

def send_task_to_channel(task, channel_id):
    caption = post_caption(task)
    poster = task.get("poster")
    if poster:
        try:
            bot.send_photo(
                channel_id, poster, caption=caption,
                parse_mode="HTML"
            )
            return True
        except Exception:
            log.exception("Photo post failed for %s; trying text", channel_id)
    bot.send_message(
        channel_id, caption,
        parse_mode="HTML", disable_web_page_preview=True
    )
    return True


def publish_current(uid):
    task, _ = current_task(uid)
    if not task:
        bot.send_message(uid, "📭 No post selected.")
        return
    if not targets:
        bot.send_message(uid, "❌ Target Channel இல்லை. /addchannel மூலம் சேர்க்கவும்.")
        return
    success, failed = [], []
    for channel_id in sorted(targets):
        try:
            send_task_to_channel(task, channel_id)
            success.append(channel_id)
        except Exception:
            log.exception("Publish failed for channel %s", channel_id)
            failed.append(channel_id)

    if success:
        task["status"] = "Published"
        task["published_to"] = success
        save_data()
    bot.send_message(
        uid,
        "📤 <b>Publish Result</b>\n"
        f"✅ Success: {len(success)}\n"
        f"❌ Failed: {len(failed)}"
    )
    show_panel(uid)


def preview_current(uid):
    task, _ = current_task(uid)
    if not task:
        bot.send_message(uid, "📭 No post available.")
        return
    text = "👁️ <b>POST PREVIEW</b>\n\n" + post_caption(task)
    try:
        if task.get("poster"):
            bot.send_photo(uid, task["poster"], caption=text)
        else:
            bot.send_message(uid, text, disable_web_page_preview=True)
    except Exception:
        log.exception("Preview send failed")
        bot.send_message(uid, text, disable_web_page_preview=True)


# ---------------- CALLBACKS ----------------

@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    uid = call.from_user.id
    data = call.data or ""

    if not is_admin(uid):
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if data == "refresh":
        show_panel(uid)
    elif data == "post":
        publish_current(uid)
    elif data == "movies":
        category_filter[uid] = "movies"
        current_index[uid] = 0
        show_panel(uid)
    elif data == "webseries":
        category_filter[uid] = "webseries"
        current_index[uid] = 0
        show_panel(uid)
    elif data == "prev":
        items = visible_tasks(uid)
        current_index[uid] = max(0, current_index.get(uid, 0) - 1)
        show_panel(uid)
    elif data == "next":
        items = visible_tasks(uid)
        current_index[uid] = min(max(0, len(items) - 1), current_index.get(uid, 0) + 1)
        show_panel(uid)
    elif data == "preview":
        preview_current(uid)
    elif data == "edit":
        task, _ = current_task(uid)
        if not task:
            bot.send_message(uid, "📭 Edit செய்ய Post இல்லை.")
            return
        edit_states[uid] = True
        bot.send_message(
            uid,
            "✏️ திருத்தப்பட்ட முழு Post Text-ஐ அனுப்புங்கள்.\n\n"
            "Format:\n<code>Title: Movie Name\nYear: 2026\nAudio: Tamil\n"
            "Quality: 1080p\n700MB - https://example.com/file</code>\n\n"
            "/cancel மூலம் நிறுத்தலாம்."
        )
    elif data == "approve":
        task, _ = current_task(uid)
        if not task:
            bot.send_message(uid, "📭 Approve செய்ய Post இல்லை.")
            return
        task["status"] = "Approved"
        save_data()
        show_panel(uid)
    elif data == "reject":
        task, _ = current_task(uid)
        if not task:
            bot.send_message(uid, "📭 Reject செய்ய Post இல்லை.")
            return
        task["status"] = "Rejected"
        save_data()
        show_panel(uid)
    elif data == "channels":
        show_channels(uid, call.message.message_id if call.message else None)
    elif data.startswith("channel_remove:"):
        channel_id = data.split(":", 1)[1]
        targets.discard(channel_id)
        save_data()
        show_channels(uid, call.message.message_id if call.message else None)
    elif data == "channel_help":
        bot.send_message(
            uid,
            "1. Bot-ஐ உங்கள் Channel-ல் Admin ஆக்குங்கள்.\n"
            "2. பிறகு இதை அனுப்புங்கள்:\n<code>/addchannel -1001234567890</code>"
        )
    elif data == "manual_add":
        manual_start(uid, uid)
    elif data == "manual_movie":
        if uid not in manual_states:
            return
        manual_states[uid]["task"]["category"] = "movies"
        manual_states[uid]["step"] = "title"
        manual_prompt(uid, "🎬 Movie Title அனுப்பவும்.")
    elif data == "manual_series":
        if uid not in manual_states:
            return
        manual_states[uid]["task"]["category"] = "webseries"
        manual_states[uid]["step"] = "title"
        manual_prompt(uid, "📺 Web Series Title அனுப்பவும்.")
    elif data == "manual_more":
        if uid not in manual_states:
            return
        manual_states[uid]["step"] = "size"
        manual_prompt(uid, "📦 அடுத்த File Size அனுப்பவும். உதா: 1.2GB")
    elif data == "manual_done":
        if uid not in manual_states:
            return
        enrich_task(manual_states[uid]["task"])
        manual_ask_poster(uid)
    elif data == "manual_save":
        state = manual_states.pop(uid, None)
        if not state:
            return
        task = state["task"]
        enrich_task(task)
        add_task(task)
        bot.send_message(uid, "✅ Post Saved to Task Panel.")
        show_panel(uid)
    elif data == "manual_cancel":
        manual_states.pop(uid, None)
        bot.send_message(uid, "❌ Manual Add Cancelled.")
        show_panel(uid)


# ---------------- RECEIVE SOURCE POSTS / MANUAL / EDIT ----------------

@bot.message_handler(content_types=["text", "photo"])
def receive_post(message):
    uid = message.from_user.id if message.from_user else 0

    # Admin private chat: manual post flow
    if message.chat.type == "private" and uid in manual_states:
        manual_receive(message)
        return

    raw_text = message.caption or message.text or ""

    # Admin private chat: edit selected task using the same post format
    if message.chat.type == "private" and uid in edit_states:
        updated = parse_post(raw_text)
        if not updated:
            bot.reply_to(
                message,
                "❌ Format புரியவில்லை. Title மற்றும் Size - Link உள்ள Post அனுப்பவும்."
            )
            return
        task, _ = current_task(uid)
        if task:
            updated["poster"] = task.get("poster")
            updated["rating"] = task.get("rating")
            with lock:
                index = find_task_index(task)
                if index >= 0:
                    tasks[index] = updated
                    save_data()
        edit_states.pop(uid, None)
        bot.reply_to(message, "✅ Post Updated.")
        show_panel(uid)
        return

    # Only configured source groups are accepted
    if message.chat.type in ("group", "supergroup"):
        if SOURCE_GROUP_IDS and message.chat.id not in SOURCE_GROUP_IDS:
            return
        parsed = parse_post(raw_text)
        if not parsed:
            return
        enrich_task(parsed)
        add_task(parsed)
        log.info("New source post received: %s", parsed["title"])
        # Notify admins privately; bot must have a private chat started by admin
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(admin_id, "📥 <b>New post added to Task Panel</b>\n" + escape(parsed["title"]))
                show_panel(admin_id)
            except Exception:
                pass


# ---------------- HEALTH CHECK ----------------

@app.get("/")
def home():
    return jsonify({
        "status": "running",
        "bot": "My Cini Spot Auto Post Bot",
        "tasks": len(tasks),
        "channels": len(targets)
    })


@app.get("/health")
def health():
    return jsonify({"ok": True})


# ---------------- STARTUP ----------------

def run_bot():
    while True:
        try:
            log.info("Telegram polling started")
            bot.infinity_polling(
                skip_pending=True,
                timeout=30,
                long_polling_timeout=30
            )
        except Exception:
            log.exception("Telegram polling stopped; retrying in 5 seconds")
            time.sleep(5)


if __name__ == "__main__":
    load_data()
    polling_thread = threading.Thread(target=run_bot, daemon=True)
    polling_thread.start()
    app.run(host="0.0.0.0", port=PORT)
