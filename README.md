# Telegram APK Build Bot

Telegram bot that accepts an application-source ZIP, detects supported Android project types, builds a **debug APK**, and sends it back. It reports build failures with a category and relevant log tail rather than automatically blaming every failure on the uploaded ZIP.

## Supported project types

- **Native Android (Java/Kotlin + Gradle):** builds with `assembleDebug`.
- **Flutter:** detects `pubspec.yaml` plus `lib/main.dart`, runs `flutter pub get` and `flutter build apk --debug`.
- **React Native:** installs npm dependencies, bundles the JS entry point, and runs Android Gradle `assembleDebug`.
- **Ionic/Capacitor:** installs npm dependencies, runs the project's build script when present, syncs Android, and runs Gradle `assembleDebug`.

This is not a universal compiler for every framework or arbitrary ZIP. A valid source project still needs compatible dependencies, build files, SDK versions, and any private credentials or environment values required by that project. It produces debug APKs; release signing is not configured.

## Required repository secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

- `BOT_TOKEN`: Telegram bot token from BotFather.
- `ALLOWED_USER_IDS`: numeric Telegram user ID(s), comma-separated if needed.

Never commit bot tokens or put them directly in source code.

## Start the bot

1. Add the two repository secrets.
2. Open **Actions → APK Build Bot → Run workflow**.
3. After the job starts, send `/start` to the Telegram bot.
4. Send a ZIP containing the full project source.

GitHub Actions is not permanent hosting. The workflow runner ends, and the bot stops when the job/runner ends.

## Current limits and important upload restriction

- Telegram attachment download: **20 MB** through the standard hosted Bot API.
- Alternate ZIP source: **public HTTPS direct-download URL up to 200 MB** (downloaded in chunks).
- Extracted source: **1 GB**.
- ZIP entries: **5,000**.
- Build timeout: **30 minutes**.
- APK delivery limit: **45 MB**.
- Build container resources are capped by Docker settings and the GitHub-hosted runner's available resources.

### Sending a 100–200 MB source ZIP

Telegram's standard hosted Bot API cannot download attachments larger than 20 MB. For a larger project, upload the ZIP to a file host and paste its **direct HTTPS download link** into the bot chat. The bot checks that the URL uses HTTPS and resolves to public internet IPs, checks redirects, streams no more than 200 MB, and verifies that the response is a ZIP before extraction. Links that require sign-in, show a download-confirmation HTML page, or do not directly return a ZIP are not supported. Do not share private project links or credentials in public chats.

The direct-download route avoids Telegram's attachment-download ceiling; it does not raise Telegram's own 20 MB hosted API limit. For actual Telegram attachments above 20 MB, a local Telegram Bot API server is required.

## Error reporting

The bot categorizes common failures such as invalid ZIPs, unsupported project structure, SDK/toolchain failures, permissions, dependency/network errors, source compilation errors, resource/manifest errors, timeout/resource exhaustion, and missing APK output. When logs do not prove a single cause, the bot says that the root cause needs log review and includes the log tail instead of making a false claim.

A classifier is a diagnostic aid, not an infallible root-cause oracle. Some build errors have overlapping causes, so check the included log when the category is uncertain.

## Security

Only user IDs in `ALLOWED_USER_IDS` can use the bot. Building a project executes its Gradle/Flutter build scripts. Only build source ZIPs you trust. Docker isolation is helpful but does not make arbitrary untrusted build scripts risk-free.
