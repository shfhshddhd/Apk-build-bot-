# Telegram APK Build Bot

Telegram bot that accepts an application-source ZIP, detects supported Android project types, builds a **debug APK**, and sends it back. It reports build failures with a category and relevant log tail rather than automatically blaming every failure on the uploaded ZIP.

## Supported project types

- **Native Android (Java/Kotlin + Gradle):** builds with `assembleDebug`.
- **Flutter:** detects `pubspec.yaml` plus `lib/main.dart`, runs `flutter pub get` and `flutter build apk --debug`.
- **React Native:** installs npm dependencies, bundles the JS entry point, and runs Android Gradle `assembleDebug`.
- **Expo (React Native):** installs npm dependencies, generates the native Android project with `expo prebuild`, and runs Gradle `assembleDebug`. Projects that require EAS cloud-only services, private credentials, or unsupported native configuration may still need project-specific setup.
- **Ionic/Capacitor:** installs npm dependencies, runs the project's build script when present, syncs Android, and runs Gradle `assembleDebug`.
- **Apache Cordova:** installs npm dependencies, adds the Android platform, and runs a debug Android build.
- **Multi-module Gradle projects:** detects Android Gradle projects with custom module names. A missing wrapper is supported only when the build container has a compatible system Gradle; otherwise the bot reports the missing toolchain.
- **Official Telegram Android source:** recognizes `https://github.com/DrKLO/Telegram/archive/refs/heads/<branch>.zip`, clones the matching official branch with recursive shallow submodules, installs Android SDK 36 / NDK 27.2.12479018 / CMake 3.22.1, and runs `assembleAfatDebug`. This special path avoids the GitHub archive's high file count and restores submodules that ZIP archives omit.

This is not a universal compiler for every framework or arbitrary ZIP. Unity, Godot, .NET MAUI, NativeScript, Kotlin Multiplatform edge cases, and projects requiring proprietary export tooling are not guaranteed to build through these generic paths. A valid source project still needs compatible dependencies, build files, SDK versions, and any private credentials or environment values required by that project. It produces debug APKs; release signing is not configured.

## Required repository secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

- `BOT_TOKEN`: Telegram bot token from BotFather.
- `ALLOWED_USER_IDS`: numeric Telegram user ID(s), comma-separated if needed.
- `TELEGRAM_API_ID`: Telegram API application ID from my.telegram.org (needed by the local Bot API workflow).
- `TELEGRAM_API_HASH`: Telegram API application hash from my.telegram.org (needed by the local Bot API workflow).

The workflow logs the bot out from Telegram's hosted API before starting the local Bot API server. Keep the local API configuration consistent; do not run the same bot token against hosted and local APIs simultaneously.

Never commit bot tokens or put them directly in source code.

## Start the bot

1. Add the two repository secrets.
2. Open **Actions → APK Build Bot → Run workflow**.
3. After the job starts, send `/start` to the Telegram bot.
4. Send a ZIP containing the full project source.

GitHub Actions is not permanent hosting. The workflow runner ends, and the bot stops when the job/runner ends.

## Current limits and important upload restriction

- Source ZIP: **200 MB by default**, configurable through `MAX_ZIP_BYTES`.
- Telegram attachment download uses the configured local Telegram Bot API server; actual limits still depend on the server, runner storage, and network.
- Alternate ZIP source: **public HTTPS direct-download URL up to 200 MB by default** (downloaded in chunks).
- Extracted source ZIP: **2 GB**.
- ZIP entries: **50,000**.
- Build timeout: **3 hours** by default for all supported build paths, including the official Telegram source build (checkout of Telegram source/submodules has a separate 40-minute timeout). Configure with `BUILD_TIMEOUT_SECONDS` if needed.
- APK delivery limit: **500 MB by default**, configurable through `MAX_APK_BYTES`; Telegram's local API, runner disk, network, and upload duration still constrain practical output size.
- Extracted-source limit is configurable through `MAX_UNZIPPED_BYTES` (default 2 GB); entry-count limit through `MAX_ZIP_FILES` (default 50,000); build timeout through `BUILD_TIMEOUT_SECONDS` (default 3 hours / 10,800 seconds).
- Build container resources are capped by Docker settings and the GitHub-hosted runner's available resources.

### Sending a 100–200 MB source ZIP

Telegram's standard hosted Bot API cannot download attachments larger than 20 MB. For a larger project, upload the ZIP to a file host and paste its **direct HTTPS download link** into the bot chat. The bot checks that the URL uses HTTPS and resolves to public internet IPs, checks redirects, streams no more than 200 MB, and verifies that the response is a ZIP before extraction. Links that require sign-in, show a download-confirmation HTML page, or do not directly return a ZIP are not supported. Do not share private project links or credentials in public chats.

The direct-download route avoids Telegram's attachment-download ceiling; it does not raise Telegram's own 20 MB hosted API limit. For actual Telegram attachments above 20 MB, a local Telegram Bot API server is required. The special official Telegram archive URL is handled by a recursive shallow Git clone instead of downloading/extracting the archive ZIP, because the project requires Git submodules. The clone and native build may still exceed available runner disk, memory, or time; a successful build is not guaranteed on every runner.

## Modular build adapters and toolchain setup

Build execution is separated into `build_adapters.py`, so each supported framework can use its own command path without expanding the Telegram handler. Native Gradle and generated Android projects run an SDK preflight that reads common literal `compileSdk`, `targetSdk`, `buildToolsVersion`, and `ndkVersion` declarations from Gradle files, installs requested SDK packages when detected, and reports available Java/RAM/disk details. Gradle task discovery prefers `assembleDebug` and falls back to discovered flavor-specific `assemble*Debug` tasks. APK output discovery also recognizes case-insensitive flavor output directory names. If a Gradle wrapper JAR is missing, the bot reads the declared wrapper distribution version before attempting to retrieve the matching JAR instead of always downloading one hard-coded version. Node-based builds use Node.js 20 when the image has no suitable Node version and honor npm, Yarn, or pnpm lockfiles when installing JavaScript dependencies.

The current runner defaults to **one active build at a time** (`MAX_ACTIVE_BUILDS=1`) to reduce the risk of multiple 6 GB containers exhausting a standard GitHub runner. Requests arriving while busy are rejected with a retry message rather than silently competing for memory.

## Project inspection and artifact verification

The project inspector ignores generated/dependency directories such as `node_modules`, `.git`, `.gradle`, `build`, and `dist`, and caps the number of inspected files. Framework projects are detected before generated Android subprojects so Expo and React Native projects can receive their framework-specific build steps. After the build, the bot checks that the output is non-empty, a valid ZIP/APK archive, has no damaged archive entries, and contains `AndroidManifest.xml`. The delivery caption includes artifact size and SHA-256 checksum. These checks improve confidence but do not prove that every device can install or run the APK.

Run validation from GitHub Actions → **Validate APK Build Bot** → **Run workflow**, or locally with `pip install -r requirements.txt`, then `python -m py_compile bot.py project_inspector.py build_adapters.py` and `python -m unittest discover -s tests -v`. The workflow checks Python syntax, framework detection, generated shell syntax, and static safety/artifact guards; it does not perform a full real-world APK build for every framework.

## Telegram commands

- `/start` and `/help` — usage guide.
- `/status` — current build stage and estimated progress.
- `/cancel` — request cancellation of your active build.
- `/frameworks` — supported build paths and known exclusions.
- `/limits` — current configured file, extraction, timeout, and APK limits.
- `/about` — architecture summary and build limitations.

The bot currently keeps active job status in memory; completed-build history and persistent log retrieval are not implemented.

## Error reporting

The bot categorizes common failures such as invalid ZIPs, unsupported project structure, SDK/toolchain failures, permissions, dependency/network errors, source compilation errors, resource/manifest errors, timeout/resource exhaustion, and missing APK output. When logs do not prove a single cause, the bot says that the root cause needs log review and includes the log tail instead of making a false claim.

A classifier is a diagnostic aid, not an infallible root-cause oracle. Some build errors have overlapping causes, so check the included log when the category is uncertain.

## Security

Only user IDs in `ALLOWED_USER_IDS` can use the bot. Building a project executes its Gradle/Flutter build scripts. Only build source ZIPs you trust. Docker isolation is helpful but does not make arbitrary untrusted build scripts risk-free.
