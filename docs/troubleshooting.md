# Troubleshooting

## The bot replies "You are not authorized to use this bot"

Cause: your Telegram user id is not in `TELEGRAM_ALLOWED_USERS`. When the list is empty the bot denies everyone, and the log says `TELEGRAM_ALLOWED_USERS is empty — bot will deny all commands until configured` at startup.

Fix: the reply includes your id. Add it to `TELEGRAM_ALLOWED_USERS` (comma-separated for several people) and recreate the container with `docker compose up -d`.

## The container stops right after it starts

Cause: a required variable is missing. The log names it: `Required environment variable 'SLSKD_API_KEY' is not set.` The required ones are `TELEGRAM_BOT_TOKEN`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SLSKD_HOST` and `SLSKD_API_KEY`.

Fix: set it in `.env` (or the compose `environment:` block) and start again.

## Permission denied on /downloads, /music or /data

Cause: the container runs as uid 1000 and cannot write a mounted folder. A folder Docker creates for a missing bind mount belongs to root.

Fix: give the folders to uid 1000 (`sudo chown -R 1000:1000 data`, and the same for your download and library folders), or mount folders that uid 1000 can already write.

## Downloads finish in slskd but nothing reaches the library

Cause: `/downloads` in the container is not the folder slskd writes its completed downloads to, so the bot never sees the file.

Fix: set `SLSKD_DOWNLOAD_PATH` to slskd's own download folder on the host, the same one slskd mounts.

## Reporting a bug

Open an [issue](https://github.com/GeiserX/telegram-slskd-local-bot/issues) with:

- the image tag or version you run;
- the log around the problem, with `LOG_LEVEL=DEBUG` if you can reproduce it (`docker logs slskd_importer`);
- the song name or Spotify link you sent, and what the bot answered.

Leave out tokens, API keys and your Telegram user id.
