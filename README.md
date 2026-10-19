# Telegram APK Build Bot

Telegram bot that accepts an application-source ZIP, detects supported Android project types, builds a **debug APK**, and sends it back. It reports build failures with a category and relevant log tail rather than automatically blaming every failure on the uploaded ZIP.

## Supported project types

- **Native Android (Java/Kotlin + Gradle):** builds with `assembleDebug`.
- **Flutter:** detects `pubspec.yaml` plus `lib/main.dart`, runs `flutter pub get` and `flutter build apk --debug`.
- **React Native and Ionic/Capacitor:** currently detected but not built automatically. The bot reports that the framework-specific Node/Metro or Capacitor setup is missing. Do not treat this message as proof that the ZIP is broken.

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

- ZIP download: **20 MB** using Telegram's standard hosted Bot API.
- Extracted source: **1 GB**.
- ZIP entries: **5,000**.
- Build timeout: **30 minutes**.
- APK delivery limit: **45 MB**.
- Build container resources are capped by Docker settings and the GitHub-hosted runner's available resources.

The hosted Telegram Bot API limits file downloads to 20 MB. Setting a larger ZIP constant does not bypass this Telegram API limit. Supporting 100–200 MB ZIP uploads requires configuring a local Telegram Bot API server or another upload/download path, plus reviewing GitHub Actions disk and runtime constraints.

## Error reporting

The bot categorizes common failures such as invalid ZIPs, unsupported project structure, SDK/toolchain failures, permissions, dependency/network errors, source compilation errors, resource/manifest errors, timeout/resource exhaustion, and missing APK output. When logs do not prove a single cause, the bot says that the root cause needs log review and includes the log tail instead of making a false claim.

A classifier is a diagnostic aid, not an infallible root-cause oracle. Some build errors have overlapping causes, so check the included log when the category is uncertain.

## Security

Only user IDs in `ALLOWED_USER_IDS` can use the bot. Building a project executes its Gradle/Flutter build scripts. Only build source ZIPs you trust. Docker isolation is helpful but does not make arbitrary untrusted build scripts risk-free.
