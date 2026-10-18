# Telegram APK Build Bot

Telegram bot that accepts an Android Gradle project ZIP, builds a **debug APK** in Docker, and sends it back.

## Required repository secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

- `BOT_TOKEN`: Telegram bot token from BotFather.
- `ALLOWED_USER_IDS`: your numeric Telegram user ID. Multiple IDs can be comma-separated.

Never commit bot tokens or put them directly in source code.

## Start the bot

1. Add the two repository secrets.
2. Open **Actions → APK Build Bot → Run workflow**.
3. After the job starts, send `/start` to your Telegram bot.
4. Send an Android Gradle project ZIP.

The GitHub Actions runner is configured for up to 350 minutes. GitHub Actions is not permanent hosting; the bot stops when the job/runner ends and must be restarted.

## Project ZIP requirements

The ZIP must contain `gradlew`, `settings.gradle` or `settings.gradle.kts`, and `app/build.gradle` or `app/build.gradle.kts`. It builds `assembleDebug`; it does not sign a release APK.

Limits: ZIP 20 MB, extracted source 250 MB, up to 5,000 ZIP entries, build timeout 10 minutes, APK 45 MB.

## Security

Only user IDs in `ALLOWED_USER_IDS` can use the bot. Gradle projects execute build scripts; only build ZIP files from sources you trust.
