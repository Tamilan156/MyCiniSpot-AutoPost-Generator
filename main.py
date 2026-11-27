
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

logging.basicConfig(level=logging.INFO)
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
POST_FOOTER = os.getenv("POST_FOOTER", "")
STATE_FILE = "/tmp/mycinispot_state.json"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
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
            json.dump({
                "tasks": tasks,
                "targets": list(targets)
            }, f, ensure_ascii=False)
        os.replace(temp, STATE_FILE)


def load_data():
    global tasks, targets
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        tasks = data.get("tasks", [])
        targets = set(str(x) for x in data.get("targets", []))
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("State loading failed")


def admin(uid):
    return uid in ADMIN_IDS


# ---------------- POST PARSER ----------------

def parse_post(text):
    if not text:
        return None

    title = re.search(r"(?im)^\s*🎬?\s*Title\s*:\s*(.+)$", text)
    year = re.search(r"(?im)^\s*📅?\s*Year\s*:\s*(\d{4})", text)
    audio = re.search(r"(?im)^\s*🔊?\s*Audio\s*:\s*(.+)$", text)
    quality = re.search(r"(?im)^\s*💿?\s*Quality\s*:\s*(.+)$", text)

    links = re.findall(
        r"(?im)^\s*([0-9]+(?:\.[0-9]+)?\s*(?:MB|GB)(?:\s*\([^)]*\))?)\s*[-:]\s*(https?://\S+)",
        text
    )

    if not title or not links:
        return None

    clean_links = []
    seen = set()

    for size, url in links:
        url = url.rstrip(").,]")
        if url not in seen:
            clean_links.append({
                "size": size.strip(),
                "url": url
            })
            seen.add(url)

    category = (
        "webseries"
        if re.search(r"(?i)#webseries|web\s*series", text)
        else "movies"
    )

    return {
        "title": title.group(1).strip(),
        "year": year.group(1) if year else "",
        "audio": audio.group(1).strip() if audio else "",
        "quality": quality.group(1).strip() if quality else "",
        "links": clean_links,
        "category": category,
        "status": "Pending",
        "poster": None,
        "rating": ""
    }


# ---------------- POSTER / RATING ----------------

def get_poster(title, year):
    if not TMDB_API_KEY:
        return None

    try:
        r = requests.get(
            "https://api.themoviedb.org/3/search/movie",
            params={
                "api_key": TMDB_API_KEY,
                "query": title,
                "year": year
            },
            timeout=12
        )
        r.raise_for_status()

        results = r.json().get("results", [])

        if results and results[0].get("poster_path"):
            return (
                "https://image.tmdb.org/t/p/w500"
                + results[0]["poster_path"]
            )

    except Exception:
        log.exception("TMDB error")

    return None


def get_rating(title, year):
    if not OMDB_API_KEY:
        return ""

    try:
        r = requests.get(
            "https://www.omdbapi.com/",
            params={
                "apikey": OMDB_API_KEY,
                "t": title,
                "y": year
            },
            timeout=12
        )
        r.raise_for_status()

        data = r.json()
        rating = data.get("imdbRating", "")

        if data.get("Response") == "True" and rating and rating != "N/A":
            return f"⭐ IMDb Rating: {rating}/10"

    except Exception:
        log.exception("OMDb error")

    return ""


# ---------------- CAPTION ----------------

def post_caption(task):
    category = (
        "📺 Web Series"
        if task.get("category") == "webseries"
        else "🎬 Movies"
    )

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
        suffix = (
            " [ #Single_File ]"
            if task.get("category") == "webseries"
            else ""
        )

        lines.append(
            f"📦 {escape(str(item['size']))} - "
            f"{escape(str(item['url']))}{suffix}"
        )

    if POST_FOOTER:
        lines.extend(["", escape(POST_FOOTER)])

    return "\n".join(lines)


# ---------------- TASK MANAGEMENT ----------------

def add_task(new_task):
    key = (
        new_task["title"].lower(),
        new_task.get("year", "")
    )

    with lock:
        existing = next(
            (
                t for t in reversed(tasks)
                if (
                    t.get("title", "").lower(),
                    t.get("year", "")
                ) == key and t.get("status") == "Pending"
            ),
            None
        )

        if existing:
            known = {
                item.get("url")
                for item in existing.get("links", [])
            }

            for item in new_task.get("links", []):
                if item.get("url") not in known:
                    existing.setdefault("links", []).append(item)
                    known.add(item.get("url"))

            if not existing.get("poster") and new_task.get("poster"):
                existing["poster"] = new_task["poster"]

        else:
            tasks.append(new_task)

        save_data()


def visible_tasks(uid):
    category = category_filter.get(uid, "all")

    if category == "all":
        return tasks

    return [
        t for t in tasks
        if t.get("category") == category
    ]


def current_task(uid):
    items = visible_tasks(uid)

    if not items:
        return None, -1

    index = max(
        0,
        min(current_index.get(uid, len(items) - 1), len(items) - 1)
    )

    current_index[uid] = index

    return items[index], index


# ---------------- ADMIN PANEL ----------------

def keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.row(
        types.InlineKeyboardButton(
            "📤 Post",
            callback_data="post"
        )
    )

    kb.row(
        types.InlineKeyboardButton(
            "➕ Manual Add Post",
            callback_data="manual_add"
        )
    )

    kb.row(
        types.InlineKeyboardButton(
            "🎬 Movies",
            callback_data="movies"
        ),
        types.InlineKeyboardButton(
            "📺 Web Series",
            callback_data="webseries"
        )
    )

    kb.row(
        types.InlineKeyboardButton(
            "⬅️ Previous",
            callback_data="prev"
        ),
        types.InlineKeyboardButton(
            "Next ➡️",
            callback_data="next"
        )
    )

    kb.row(
        types.InlineKeyboardButton(
            "👁️ Preview",
            callback_data="preview"
        ),
        types.InlineKeyboardButton(
            "✏️ Edit",
            callback_data="edit"
        )
    )

    kb.row(
        types.InlineKeyboardButton(
            "📢 Select Channel",
            callback_data="channels"
        )
    )

    kb.row(
        types.InlineKeyboardButton(
            "✅ Approve",
            callback_data="approve"
        ),
        types.InlineKeyboardButton(
            "❌ Reject",
            callback_data="reject"
        )
    )

    kb.row(
        types.InlineKeyboardButton(
            "🔄 Refresh",
            callback_data="refresh"
        )
    )

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
    markup = keyboard()

    try:
        mid = panel_ids.get(uid)

        if mid:
            bot.edit_message_text(
                text,
                chat_id,
                mid,
                reply_markup=markup,
                disable_web_page_preview=True
            )
        else:
            msg = bot.send_message(
                chat_id,
                text,
                reply_markup=markup,
                disable_web_page_preview=True
            )
            panel_ids[uid] = msg.message_id

    except Exception:
        log.exception("Panel update failed")

        try:
            msg = bot.send_message(
                chat_id,
                text,
                reply_markup=markup,
                disable_web_page_preview=True
            )
            panel_ids[uid] = msg.message_id
        except Exception:
            log.exception("Panel send failed")


# ---------------- CHANNEL SELECTION ----------------

def show_channels(uid, message_id=None):
    kb = types.InlineKeyboardMarkup(row_width=1)

    for channel_id in sorted(targets):
        kb.add(
            types.InlineKeyboardButton(
                f"❌ Remove {channel_id}",
                callback_data="channel_remove:" + channel_id
            )
        )

    kb.add(
        types.InlineKeyboardButton(
            "➕ Add Channel Help",
            callback_data="channel_help"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "⬅️ Back to Panel",
            callback_data="refresh"
        )
    )

    text = (
        "📢 <b>SELECT CHANNELS</b>\n\n"
        "இந்த பட்டியலில் உள்ள Channel-களுக்கு Post அனுப்பப்படும்.\n"
        "Channel நீக்க அதன் button-ஐ அழுத்தவும்.\n\n"
        "புதிய Channel சேர்க்க:\n"
        "<code>/addchannel -1001234567890</code>\n\n"
        f"Total: <b>{len(targets)}</b>"
    )

    if message_id:
        bot.edit_message_text(
            text,
            uid,
            message_id,
            reply_markup=kb
        )
    else:
        bot.send_message(uid, text, reply_markup=kb)


# ---------------- MANUAL ADD ----------------

def manual_start(uid, chat_id):
    manual_states[uid] = {
        "step": "category",
        "chat_id": chat_id,
        "task": {
            "title": "",
            "year": "",
            "audio": "",
            "quality": "",
            "links": [],
            "category": "movies",
            "status": "Pending",
            "poster": None,
            "rating": ""
        }
    }

    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.add(
        types.InlineKeyboardButton(
            "🎬 Movies",
            callback_data="manual_movie"
        ),
        types.InlineKeyboardButton(
            "📺 Web Series",
            callback_data="manual_series"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "❌ Cancel",
            callback_data="manual_cancel"
        )
    )

    bot.send_message(
        chat_id,
        "➕ <b>MANUAL POST ADD</b>\n\nSelect Post Category:",
        reply_markup=kb
    )


def manual_prompt(uid, message):
    bot.send_message(
        manual_states[uid]["chat_id"],
        message,
        reply_markup=types.ReplyKeyboardRemove()
    )


def valid_url(value):
    try:
        parsed = urlparse(value.strip())

        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.netloc)
        )

    except Exception:
        return False


def manual_preview(uid):
    state = manual_states[uid]
    task = state["task"]

    text = (
        "👁️ <b>MANUAL POST PREVIEW</b>\n\n"
        + post_caption(task)
        + "\n\n<b>Save this post?</b>"
    )

    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.row(
        types.InlineKeyboardButton(
            "💾 Save Post",
            callback_data="manual_save"
        ),
        types.InlineKeyboardButton(
            "❌ Cancel",
            callback_data="manual_cancel"
        )
    )

    if task.get("poster"):
        try:
            bot.send_photo(
                uid,
                task["poster"],
                caption=text,
                reply_markup=kb
            )
            return
        except Exception:
            log.exception("Poster preview failed")

    bot.send_message(
        uid,
        text,
        reply_markup=kb,
        disable_web_page_preview=True
    )


def manual_ask_poster(uid):
    manual_states[uid]["step"] = "poster"

    manual_prompt(
        uid,
        "🖼️ Poster Image URL அனுப்பவும்.\n\n"
        "Poster வேண்டாம் என்றால் Skip என்று அனுப்பவும்."
    )


def manual_receive(message):
    uid = message.from_user.id

    if uid not in manual_states:
        return False

    if message.chat.type != "private":
        return True

    value = (message.text or "").strip()

    if not value:
        manual_prompt(
            uid,
            "❌ Text மட்டும் அனுப்புங்கள்."
        )
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

        manual_prompt(
            uid,
            "📅 Release Year உள்ளிடவும். தெரியாவிட்டால் Skip."
        )

    elif step == "year":
        if value.lower() != "skip":
            if not re.fullmatch(r"\d{4}", value):
                manual_prompt(
                    uid,
                    "❌ சரியான 4 இலக்க Year கொடுக்கவும் அல்லது Skip."
                )
                return True

            task["year"] = value

        state["step"] = "audio"

        manual_prompt(
            uid,
            "🔊 Audio Language உள்ளிடவும். உதா: Tamil + English (அல்லது Skip)"
        )

    elif step == "audio":
        task["audio"] = "" if value.lower() == "skip" else value
        state["step"] = "quality"

        manual_prompt(
            uid,
            "💿 Quality உள்ளிடவும். உதா: 1080p WEB-DL (அல்லது Skip)"
        )

    elif step == "quality":
        task["quality"] = "" if value.lower() == "skip" else value
        state["step"] = "size"

        manual_prompt(
            uid,
            "📦 File Size உள்ளிடவும். உதா: 700MB"
        )

    elif step == "size":
        if not re.fullmatch(
            r"\d+(?:\.\d+)?\s*(?:MB|GB)",
            value,
            re.I
        ):
            manual_prompt(
                uid,
                "❌ சரியான Size கொடுக்கவும். உதா: 700MB அல்லது 1.5GB"
            )
            return True

        state["pending_size"] = value
        state["step"] = "link"

        manual_prompt(
            uid,
            "🔗 Download Link அனுப்பவும் (https://...)"
        )

    elif step == "link":
        if not valid_url(value):
            manual_prompt(
                uid,
                "❌ சரியான HTTP/HTTPS Link அனுப்பவும்."
            )
            return True

        size = state.pop("pending_size", "")

        if not any(x["url"] == value for x in task["links"]):
            task["links"].append({
                "size": size,
                "url": value
            })

        state["step"] = "more_links"

        kb = types.InlineKeyboardMarkup(row_width=2)

        kb.add(
            types.InlineKeyboardButton(
                "➕ Add Another Link",
                callback_data="manual_more"
            ),
            types.InlineKeyboardButton(
                "➡️ Continue",
                callback_data="manual_done"
            )
        )

        bot.send_message(
            uid,
            "✅ Download Link Added!\n\nவேறு Link சேர்க்க வேண்டுமா?",
            reply_markup=kb
        )

    elif step == "poster":
        if value.lower() == "skip":
            task["poster"] = None

        elif valid_url(value):
            task["poster"] = value

        else:
            manual_prompt(
                uid,
                "❌ சரியான Poster URL கொடுக்கவும் அல்லது Skip."
            )
            return True

        state["step"] = "preview"
        manual_preview(uid)

    return True


# ---------------- COMMANDS ----------------

@bot.message_handler(commands=["start", "panel"])
def start(message):
    uid = message.from_user.id

    if not admin(uid):
        bot.reply_to(message, "⛔ Admin only.")
        return

    show_panel(uid, message.chat.id)


@bot.message_handler(commands=["cancel"])
def cancel_manual(message):
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
    if not admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)

    if len(parts) != 2:
        bot.reply_to(
            message,
            "Usage: /addchannel -1001234567890"
        )
        return

    try:
        chat = bot.get_chat(parts[1].strip())
        me = bot.get_me()
        member = bot.get_chat_member(chat.id, me.id)

        if member.status != "administrator":
            bot.reply_to(
                message,
                "முதலில் Bot-ஐ Channel Admin ஆக்குங்கள்."
            )
            return

        targets.add(str(chat.id))
        save_data()

        bot.reply_to(
            message,
            f"✅ Channel added: <code>{chat.id}</code>"
        )

    except Exception:
        log.exception("Channel add failed")

        bot.reply_to(
            message,
            "Channel add ஆகவில்லை. Bot Admin என்பதை சரிபார்க்கவும்."
        )


@bot.message_handler(commands=["targets"])
def show_targets(message):
    if admin(message.from_user.id):
        bot.reply_to(
            message,
            "\n".join(sorted(targets))
            if targets else "No target channels added."
        )


# ---------------- RECEIVE POSTS / EDIT ----------------

@bot.message_handler(content_types=["text", "photo"])
def receive_post(message):
    uid = message.from_user.id

    if uid in manual_states and message.chat.type == "private":
        manual_receive(message)
        return

    raw_text = message.caption or message.text or ""

    if uid in edit_states and message.chat.type == "private":
        updated = parse_post(raw_text)

        if not update
