# My Cini Spot Auto Post Bot

## Render setup

Create a **Web Service** from this folder/repository.

- Build Command: `pip install -r requirements.txt`
- Start Command: `python main.py`

## Environment Variables

Required:
- `BOT_TOKEN` = Telegram bot token from BotFather
- `ADMIN_IDS` = your Telegram numeric user ID (multiple IDs comma-separated)

Optional:
- `SOURCE_GROUP_IDS` = source group IDs, comma-separated (example: `-1001111111111,-1002222222222`)
- `TMDB_API_KEY` = TMDB API key for movie posters
- `OMDB_API_KEY` = OMDb API key for IMDb ratings
- `POST_FOOTER` = optional footer text added to published posts
- `STATE_FILE` = defaults to `/tmp/mycinispot_state.json`
- `PORT` = Render supplies this automatically

## Telegram setup

1. Add the bot to each source group and allow it to read messages. For reliable group reading, disable BotFather privacy mode (`/setprivacy` -> Disable), or make the bot an admin.
2. Add the bot as an administrator in each target channel with permission to post messages.
3. Open the bot privately and send `/start`.
4. Use `/addchannel -100xxxxxxxxxx` to register each target channel.
5. Send source posts in the accepted format, or use **Manual Add Post**.

## Accepted source post format

```text
Title: Movie Name
Year: 2026
Audio: Tamil + English
Quality: 1080p WEB-DL
700MB - https://example.com/file1
1.5GB - https://example.com/file2
```

For web series, include `#webseries` or `Web Series` in the source text.

## Important storage note

The default `/tmp` JSON file is temporary. Render's normal filesystem is ephemeral, so data can be lost after redeploy/restart. For durable storage, attach a persistent disk (paid on Render) and set `STATE_FILE` to a path on that disk, or replace this JSON storage with an external database.

## Current implementation notes

- New source posts are added as Pending.
- Same title + year Pending tasks are merged; duplicate URLs are skipped.
- The **Post** button publishes the selected task to all registered target channels.
- **Approve** changes status to Approved; it does not publish automatically.
- **Edit** expects a complete replacement post in the accepted format.
- TMDB/OMDb lookups are optional and require API keys.
