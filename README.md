# My Cini Spot Auto Post Generator

## Features
- Telegram inline-button control panel: Previous / Next / Preview / Edit / Approve / Reject.
- Manual mode: send a movie post to the bot privately.
- Group mode: detects new matching messages across multiple groups (all joined groups by default, or a comma-separated allowlist).
- Strict format detection: requires Title, Year, Audio, Quality fields.
- Combines download links from the same source post into one premium post.
- Optional TMDB poster + TMDB rating; optional OMDb IMDb rating (no IMDb logo).
- SQLite duplicate protection on Movie Title + Year.
- Preview and admin approval required before channel publishing.

## Render setup
Build command: `pip install -r requirements.txt`
Start command: `gunicorn main:app --workers 1 --threads 4 --timeout 120`

Set Environment Variables:
- `BOT_TOKEN`: token from BotFather. Keep secret; never share it in chat.
- `ADMIN_IDS`: admin Telegram numeric IDs, comma-separated. Default in render.yaml is the user's known admin ID.
- `TARGET_CHANNEL`: e.g. `@My_Cine_Spots_1`. Add the bot as channel admin with posting permission.
- `SOURCE_GROUP_IDS`: optional comma-separated group IDs (example: `-1001234567890,-1009876543210`). Leave blank to accept matching posts from EVERY group/supergroup where the bot is a member. The old `SOURCE_GROUP_ID` variable is also supported for backward compatibility.
- `TMDB_API_KEY`: optional TMDB v3 API key for poster and TMDB rating.
- `OMDB_API_KEY`: optional OMDb key for IMDb rating.

## Telegram setup
1. Create a NEW bot with @BotFather and place its token in Render Environment as `BOT_TOKEN`.
2. Add the bot to EACH group you want it to monitor. It supports multiple groups. For reading ordinary group messages, disable Group Privacy in @BotFather (`/setprivacy` -> select bot -> Disable), then re-add it to each group. Alternatively make it an admin. If `SOURCE_GROUP_IDS` is blank, it accepts matching posts from all groups it can read; if you fill it, only those listed group IDs are accepted.
3. Add bot to target channel as admin with permission to post.
4. Deploy, then open the bot and send `/start` or `/panel`.

## Accepted message format
The bot only accepts text containing all four exact fields:

🎬 Title : Hanuman Ansh (2026)
📅 Year : 2026
🔊 Audio : Hindi
💿 Quality : ts, HQ, HDRip, 1080p, 720p

Download links can be on following lines, e.g. `1.61GB (720p) - https://example.com/link`.

## Important limitations
- Telegram Bot API does not provide old group chat history. Group mode sees new messages after the bot is added and configured.
- If the same movie/year appears again after it has already been rejected or published, duplicate protection prevents adding it again. Delete the row from SQLite only if you intentionally need to re-add it.
- Render Free instances may sleep/restart; local SQLite data may not persist across instance replacement. Use a persistent disk or external database for reliable long-term deduplication.
- This starter parses links from the message text. It does not bypass short-link pages or validate that a download link is alive.
- Movie/Web Series buttons are visual categories in this starter; the current strict parser handles the supplied movie format. Extend the parser if you want a separate web-series format.
