import os, re, json, time, sqlite3, threading, logging
from urllib.parse import urlparse
import requests
import telebot
from telebot import types
from flask import Flask

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('mycinispot')

BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}
# Optional comma-separated allowlist. Empty means accept matching posts from EVERY
# group/supergroup where this bot is a member and can read messages.
_SOURCE_GROUPS = os.getenv('SOURCE_GROUP_IDS', '').strip() or os.getenv('SOURCE_GROUP_ID', '').strip()
SOURCE_GROUP_IDS = {x.strip() for x in _SOURCE_GROUPS.split(',') if x.strip()}
TARGET_CHANNEL = os.getenv('TARGET_CHANNEL', '').strip()
TMDB_API_KEY = os.getenv('TMDB_API_KEY', '').strip()
OMDB_API_KEY = os.getenv('OMDB_API_KEY', '').strip()
TELEGRAM_JOIN = os.getenv('TELEGRAM_JOIN', 'https://t.me/My_Cine_Spots_1')
WHATSAPP_JOIN = os.getenv('WHATSAPP_JOIN', 'https://whatsapp.com/channel/0029VbCN6zUFsn0b2goG6r2C')
HOW_TO_LINK = os.getenv('HOW_TO_LINK', 'https://t.me/how_to_fast_download')
DB_PATH = os.getenv('DB_PATH', 'posts.db')

if not BOT_TOKEN:
    raise RuntimeError('BOT_TOKEN environment variable is required')
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML', threaded=True)
app = Flask(__name__)

SCHEMA = '''CREATE TABLE IF NOT EXISTS posts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, year TEXT, audio TEXT, quality TEXT,
 links TEXT, poster TEXT, tmdb_rating TEXT, imdb_rating TEXT, source TEXT,
 status TEXT DEFAULT 'pending', published_message_id INTEGER, created_at INTEGER,
 UNIQUE(title, year)
)'''

def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute(SCHEMA); c.commit()
    return c

def is_admin(uid): return uid in ADMIN_IDS

def parse_movie(text):
    if not text: return None
    # Strictly accept the user's agreed format: Title, Year, Audio, Quality labels.
    title_m = re.search(r'(?im)^\s*🎬\s*Title\s*:\s*(.+?)\s*$', text)
    year_m = re.search(r'(?im)^\s*📅\s*Year\s*:\s*(\d{4})\s*$', text)
    audio_m = re.search(r'(?im)^\s*🔊\s*Audio\s*:\s*(.+?)\s*$', text)
    quality_m = re.search(r'(?im)^\s*💿\s*Quality\s*:\s*(.+?)\s*$', text)
    if not all((title_m, year_m, audio_m, quality_m)): return None
    title_line = title_m.group(1).strip()
    # Prefer year in title, but keep explicit Year field as canonical year.
    title = re.sub(r'\s*\(\d{4}\)\s*$', '', title_line).strip()
    year = year_m.group(1)
    links = []
    for line in text.splitlines():
        m = re.search(r'(?i)(\d+(?:\.\d+)?\s*(?:MB|GB))?\s*(?:\(([^)]*)\))?\s*[-:]?\s*(https?://\S+)', line)
        if m:
            size = (m.group(1) or '').strip()
            q = (m.group(2) or '').strip()
            url = m.group(3).rstrip(').,')
            links.append({'size': size, 'quality': q, 'url': url})
    return {'title': title, 'year': year, 'audio': audio_m.group(1).strip(),
            'quality': quality_m.group(1).strip(), 'links': links, 'source': 'group/manual'}

def enrich(item):
    if not TMDB_API_KEY: return item
    try:
        r = requests.get('https://api.themoviedb.org/3/search/movie', params={
            'api_key': TMDB_API_KEY, 'query': item['title'], 'year': item['year'], 'include_adult': 'false'
        }, timeout=12)
        r.raise_for_status(); results = r.json().get('results', [])
        if results:
            # Search includes title + year; take the closest year match.
            exact = [x for x in results if (x.get('release_date') or '')[:4] == item['year']]
            hit = exact[0] if exact else results[0]
            if hit.get('poster_path') and not item.get('poster'): item['poster'] = 'https://image.tmdb.org/t/p/w500' + hit['poster_path']
            if hit.get('vote_average') is not None: item['tmdb_rating'] = f"{float(hit['vote_average']):.1f}/10"
            item['overview'] = hit.get('overview', '')
            item['tmdb_id'] = hit.get('id')
    except Exception as e: log.warning('TMDB lookup failed: %s', e)
    if OMDB_API_KEY:
        try:
            r = requests.get('https://www.omdbapi.com/', params={'apikey': OMDB_API_KEY, 't': item['title'], 'y': item['year']}, timeout=12)
            data = r.json()
            if data.get('Response') == 'True' and data.get('imdbRating') not in ('N/A', None):
                item['imdb_rating'] = f"{data['imdbRating']}/10"
        except Exception as e: log.warning('OMDb lookup failed: %s', e)
    return item

def save_item(item):
    item = enrich(item)
    c = db()
    try:
        cur = c.execute('INSERT INTO posts(title,year,audio,quality,links,poster,tmdb_rating,imdb_rating,source,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (item['title'], item['year'], item['audio'], item['quality'], json.dumps(item.get('links', [])),
             item.get('poster'), item.get('tmdb_rating'), item.get('imdb_rating'), item.get('source','manual'), 'pending', int(time.time())))
        c.commit(); return cur.lastrowid, True
    except sqlite3.IntegrityError:
        row = c.execute('SELECT id, poster, links FROM posts WHERE lower(title)=lower(?) AND year=?', (item['title'], item['year'])).fetchone()
        if row:
            # A later copy may contain the original group poster or additional links.
            updates=[]; values=[]
            if item.get('poster') and not row['poster']:
                updates.append('poster=?'); values.append(item['poster'])
            if item.get('links'):
                old_links=json.loads(row['links'] or '[]')
                seen={x.get('url') for x in old_links}
                merged=old_links + [x for x in item['links'] if x.get('url') not in seen]
                if len(merged) != len(old_links): updates.append('links=?'); values.append(json.dumps(merged))
            if updates:
                values.append(row['id']); c.execute('UPDATE posts SET '+', '.join(updates)+' WHERE id=?', values); c.commit()
            return (row['id'] if row else None), False
        return None, False
    finally: c.close()

def get_post(pid):
    c=db(); row=c.execute('SELECT * FROM posts WHERE id=?',(pid,)).fetchone(); c.close()
    if not row: return None
    d=dict(row); d['links']=json.loads(d['links'] or '[]'); return d

def pending_ids():
    c=db(); rows=c.execute("SELECT id FROM posts WHERE status='pending' ORDER BY id").fetchall(); c.close()
    return [r['id'] for r in rows]

def render_post(p):
    title = f"{p['title']} ({p['year']})"
    lines = [f"🎬 <b>{esc(title)}</b>", f"🔊 <b>Audio:</b> {esc(p['audio'])}", f"💿 <b>Quality:</b> {esc(p['quality'])}"]
    if p.get('tmdb_rating'): lines.append(f"⭐ TMDB Rating: {esc(p['tmdb_rating'])}")
    if p.get('imdb_rating'): lines.append(f"⭐ IMDb Rating: {esc(p['imdb_rating'])}")
    if p.get('links'):
        lines += ['', '⚡ <b>FAST DOWNLOAD</b>']
        for i, link in enumerate(p['links'], 1):
            label = ' • '.join(x for x in [link.get('size'), link.get('quality') or (p['quality'] if not link.get('size') else '')] if x)
            lines.append(f"{i}. {esc(label or 'Download')} — <a href=\"{esc(link['url'])}\">Open Link</a>")
    lines += ['', '━━━━━━━━━━━━━━', '📢 <b>TELEGRAM JOIN</b>', f'🔥 <a href="{esc(TELEGRAM_JOIN)}">@My_Cine_Spots_1</a>', '', '📲 <b>WHATSAPP JOIN</b>', f'👉 <a href="{esc(WHATSAPP_JOIN)}">Join Our WhatsApp Channel</a>', '', f'🎬 <a href="{esc(HOW_TO_LINK)}">HOW TO DOWNLOAD</a>', '━━━━━━━━━━━━━━']
    return '\n'.join(lines)

def esc(s):
    from html import escape
    return escape(str(s or ''), quote=True)

def panel_markup(ids, index):
    p = get_post(ids[index]); kb=types.InlineKeyboardMarkup(row_width=2)
    kb.row(types.InlineKeyboardButton('🎬 Movies', callback_data='noop'), types.InlineKeyboardButton('📺 Web Series', callback_data='noop'))
    kb.row(types.InlineKeyboardButton('⬅️ Previous', callback_data=f'nav:{max(0,index-1)}'), types.InlineKeyboardButton('Next ➡️', callback_data=f'nav:{min(len(ids)-1,index+1)}'))
    kb.row(types.InlineKeyboardButton('👁️ Preview Post', callback_data=f'preview:{p["id"]}'))
    kb.row(types.InlineKeyboardButton('✏️ Edit Post', callback_data=f'edit:{p["id"]}'), types.InlineKeyboardButton('✅ Approve', callback_data=f'approve:{p["id"]}'))
    kb.row(types.InlineKeyboardButton('❌ Reject Post', callback_data=f'reject:{p["id"]}'))
    kb.row(types.InlineKeyboardButton('🔄 Refresh', callback_data='panel:0'))
    return kb, p

def send_panel(chat_id, index=0, message_id=None):
    ids=pending_ids()
    if not ids:
        txt='🎬 <b>My Cini Spot – Post Manager</b>\n\nPending Posts இல்லை. Group-ல் புதிய format post வரட்டும் அல்லது இங்கே Movie format அனுப்பு.'
        if message_id:
            try: bot.edit_message_text(txt, chat_id, message_id, reply_markup=None)
            except: bot.send_message(chat_id, txt)
        else: bot.send_message(chat_id, txt)
        return
    index=max(0,min(index,len(ids)-1)); kb,p=panel_markup(ids,index)
    txt=f"🎬 <b>MY CINI SPOT – POST MANAGER</b>\n\n<b>{esc(p['title'])} ({esc(p['year'])})</b>\n🔊 {esc(p['audio'])} | 💿 {esc(p['quality'])}\n\n📂 Pending: {len(ids)}\n🎞️ Selected: {index+1}/{len(ids)}\n\nButton அழுத்தி Preview / Edit / Approve செய்யவும்."
    if message_id:
        try: bot.edit_message_text(txt,chat_id,message_id,reply_markup=kb)
        except: bot.send_message(chat_id,txt,reply_markup=kb)
    else: bot.send_message(chat_id,txt,reply_markup=kb)

@bot.message_handler(commands=['start','help','panel'])
def start(m):
    if not is_admin(m.from_user.id): return
    bot.send_message(m.chat.id, '🎬 <b>My Cini Spot Auto Post Generator</b>\n\nMovie format-ஐ இங்கே அனுப்பலாம். Group-ல் வரும் அதே format-ஐயும் Bot கண்டறியும்.\n\n/panel – Button control panel\n\nFormat: 🎬 Title / 📅 Year / 🔊 Audio / 💿 Quality', parse_mode='HTML')
    send_panel(m.chat.id)

@bot.message_handler(commands=['panel'])
def panel_cmd(m):
    if is_admin(m.from_user.id): send_panel(m.chat.id)

@bot.message_handler(func=lambda m: True, content_types=['text', 'photo'])
def incoming(m):
    if not m.from_user: return
    # Accept the exact agreed movie format in text messages or photo captions.
    body = m.caption or m.text or ''
    is_group = m.chat.type in ('group','supergroup')
    if is_group:
        if SOURCE_GROUP_IDS and str(m.chat.id) not in SOURCE_GROUP_IDS: return
        item=parse_movie(body)
        if item:
            item['source']='group'
            if m.photo:
                item['poster'] = m.photo[-1].file_id
            pid,new=save_item(item)
            if new:
                for aid in ADMIN_IDS:
                    try: bot.send_message(aid, f'🆕 New Movie detected: <b>{esc(item["title"])} ({item["year"]})</b>\nUse /panel to choose and approve.', parse_mode='HTML')
                    except Exception as e: log.warning('Admin notify failed: %s',e)
        return
    if not is_admin(m.from_user.id): return
    item=parse_movie(body)
    if not item:
        bot.reply_to(m, '❗ சரியான Movie Format கிடைக்கவில்லை. 🎬 Title, 📅 Year, 🔊 Audio, 💿 Quality வரிகள் இருக்க வேண்டும்.')
        return
    item['source']='manual'
    if m.photo:
        item['poster'] = m.photo[-1].file_id
    pid,new=save_item(item)
    if not new:
        bot.reply_to(m, '⚠️ இந்த Movie + Year ஏற்கனவே உள்ளது. புதிய Poster / Download Links கிடைத்திருந்தால் சேர்க்கப்பட்டுள்ளன.')
    else:
        bot.reply_to(m, '✅ Movie சேர்க்கப்பட்டது. Poster கிடைத்தால் Preview-ல் தெரியும். Publish செய்ய முன் Preview & Approve செய்யவும்.')
    send_panel(m.chat.id)

@bot.callback_query_handler(func=lambda c: True)
def callbacks(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id,'Admin only'); return
    data=c.data or ''; chat=c.message.chat.id; mid=c.message.message_id
    if data=='noop': bot.answer_callback_query(c.id); return
    if data.startswith('panel:') or data.startswith('nav:'):
        idx=int(data.split(':')[1]); bot.answer_callback_query(c.id); send_panel(chat,idx,mid); return
    if data.startswith('preview:'):
        pid=int(data.split(':')[1]); p=get_post(pid)
        if not p: bot.answer_callback_query(c.id,'Post not found'); return
        text=render_post(p); kb=types.InlineKeyboardMarkup().row(types.InlineKeyboardButton('✏️ Edit',callback_data=f'edit:{pid}'),types.InlineKeyboardButton('✅ Approve & Publish',callback_data=f'approve:{pid}')).row(types.InlineKeyboardButton('⬅️ Back to Panel',callback_data='panel:0'))
        if p.get('poster'):
            try: bot.send_photo(chat,p['poster'],caption=text,reply_markup=kb,parse_mode='HTML')
            except: bot.send_message(chat,text,reply_markup=kb,parse_mode='HTML',disable_web_page_preview=False)
        else: bot.send_message(chat,text,reply_markup=kb,parse_mode='HTML',disable_web_page_preview=False)
        bot.answer_callback_query(c.id); return
    if data.startswith('edit:'):
        pid=int(data.split(':')[1]); p=get_post(pid)
        if not p: bot.answer_callback_query(c.id,'Post not found'); return
        kb=types.InlineKeyboardMarkup(row_width=2)
        for field,label in [('title','🎬 Title'),('year','📅 Year'),('audio','🔊 Audio'),('quality','💿 Quality'),('links','🔗 Links')]:
            kb.add(types.InlineKeyboardButton(label,callback_data=f'field:{pid}:{field}'))
        kb.add(types.InlineKeyboardButton('⬅️ Back',callback_data=f'preview:{pid}'))
        bot.send_message(chat,'✏️ மாற்ற வேண்டிய விவரத்தைத் தேர்வு செய். அடுத்ததாக புதிய value அனுப்பு.',reply_markup=kb)
        bot.answer_callback_query(c.id); return
    if data.startswith('field:'):
        _,pid,field=data.split(':'); bot.answer_callback_query(c.id)
        bot.send_message(chat, f'புதிய {field} value அனுப்பு. Cancel செய்ய /cancel அனுப்பு.')
        bot.register_next_step_handler_by_chat_id(chat, lambda msg: save_edit(msg,int(pid),field))
        return
    if data.startswith('approve:'):
        pid=int(data.split(':')[1]); p=get_post(pid)
        if not p: bot.answer_callback_query(c.id,'Post not found'); return
        if p['status']=='published': bot.answer_callback_query(c.id,'Already published'); return
        try:
            caption=render_post(p)
            if p.get('poster'):
                sent=bot.send_photo(TARGET_CHANNEL,p['poster'],caption=caption,parse_mode='HTML')
            else: sent=bot.send_message(TARGET_CHANNEL,caption,parse_mode='HTML',disable_web_page_preview=False)
            con=db(); con.execute("UPDATE posts SET status='published',published_message_id=? WHERE id=?",(sent.message_id,pid)); con.commit(); con.close()
            bot.answer_callback_query(c.id,'Published!'); bot.send_message(chat,'✅ Channel-ல் Publish ஆகிவிட்டது.')
            send_panel(chat)
        except Exception as e:
            log.exception('Publish failed'); bot.answer_callback_query(c.id,'Publish failed'); bot.send_message(chat,f'❌ Publish ஆகவில்லை. Bot channel admin-ஆக உள்ளதா, TARGET_CHANNEL சரியா என்று பார்க்கவும்.\n{esc(str(e))}')
        return
    if data.startswith('reject:'):
        pid=int(data.split(':')[1]); con=db(); con.execute("UPDATE posts SET status='rejected' WHERE id=? AND status='pending'",(pid,)); con.commit(); con.close()
        bot.answer_callback_query(c.id,'Rejected'); send_panel(chat); return
    bot.answer_callback_query(c.id)

def save_edit(m,pid,field):
    if not m.from_user or not is_admin(m.from_user.id): return
    value=(m.text or '').strip()
    if value=='/cancel': bot.reply_to(m,'Edit cancelled.'); return
    p=get_post(pid)
    if not p: bot.reply_to(m,'Post not found.'); return
    if field=='links':
        links=[]
        for line in value.splitlines():
            u=re.search(r'https?://\S+',line)
            if u:
                size=re.search(r'\d+(?:\.\d+)?\s*(?:MB|GB)',line,re.I)
                q=re.search(r'\(([^)]+)\)',line)
                links.append({'size':size.group(0) if size else '', 'quality':q.group(1) if q else '', 'url':u.group(0).rstrip('.,')})
        if not links: bot.reply_to(m,'ஒரு valid https:// link-ஆவது அனுப்பு.'); return
        value=json.dumps(links)
    allowed={'title','year','audio','quality','links'}
    if field not in allowed: return
    con=db(); con.execute(f'UPDATE posts SET {field}=? WHERE id=?',(value,pid)); con.commit(); con.close()
    bot.reply_to(m,'✅ Updated. Preview பார்க்க /panel அழுத்து.')
    send_panel(m.chat.id)

@app.get('/')
def home(): return 'My Cini Spot Auto Post Generator is running.'
@app.get('/health')
def health(): return {'ok': True}

def run_bot():
    while True:
        try:
            bot.remove_webhook(); bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)
        except Exception:
            log.exception('Polling stopped; retrying in 5 seconds'); time.sleep(5)

# Start Telegram polling when the WSGI app is imported by Gunicorn.
threading.Thread(target=run_bot, daemon=True, name='telegram-polling').start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
