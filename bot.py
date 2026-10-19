import asyncio
import logging
import os
import ipaddress
import re
import socket
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_IDS = {int(x.strip()) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
DOCKER_IMAGE = os.environ.get("DOCKER_IMAGE", "ghcr.io/cirruslabs/android-sdk:35")
MAX_ZIP_BYTES = 200 * 1024 * 1024
TELEGRAM_HOSTED_DOWNLOAD_LIMIT = 20 * 1024 * 1024
MAX_UNZIPPED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ZIP_FILES = 50000
MAX_APK_BYTES = 45 * 1024 * 1024
BUILD_TIMEOUT_SECONDS = 1800
FLUTTER_IMAGE = os.environ.get("FLUTTER_IMAGE", "ghcr.io/cirruslabs/flutter:stable")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)
JOBS = {}

def keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("ℹ️ Help", callback_data="help"), InlineKeyboardButton("🔄 Status", callback_data="status")], [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])

def allowed(update):
    return bool(update.effective_user and update.effective_user.id in ALLOWED_USER_IDS)

async def safe_send(bot, chat_id, text, **kwargs):
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except TelegramError:
        log.exception("Telegram message delivery failed")

def safe_extract(zip_path: Path, dest: Path):
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        entries = zf.infolist()
        if len(entries) > MAX_ZIP_FILES:
            raise ValueError("ZIP mein bahut zyada files hain.")
        for entry in entries:
            name = entry.filename
            pp, wp = PurePosixPath(name), PureWindowsPath(name)
            if not name or "\\" in name or pp.is_absolute() or wp.is_absolute() or wp.drive or ".." in pp.parts:
                raise ValueError("ZIP mein unsafe file path mila.")
            mode = (entry.external_attr >> 16) & 0xffff
            if stat.S_ISLNK(mode):
                raise ValueError("ZIP mein symlink allowed nahi hai.")
            total += entry.file_size
            if total > MAX_UNZIPPED_BYTES:
                raise ValueError("Category: SOURCE ZIP SIZE. Extracted project 2 GB limit se bada hai.")
            target = dest.joinpath(*pp.parts)
            if entry.flag_bits & 0x1:
                raise ValueError("ZIP password-protected hai. Password protection hata kar dobara ZIP karein.")
            if entry.file_size and entry.compress_size == 0:
                raise ValueError("ZIP entry corrupt ya invalid lag rahi hai.")
            if entry.compress_size and entry.file_size / entry.compress_size > 2000:
                raise ValueError("ZIP mein suspicious compression ratio mila; ZIP bomb safety check ne extraction roki.")
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise ValueError("ZIP mein duplicate/conflicting file path mila: " + name)
                with zf.open(entry) as src, target.open("xb") as out:
                    shutil.copyfileobj(src, out)


class PublicHTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow HTTPS redirects only after rejecting non-public destinations."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_public_https_url(url: str):
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("Sirf public HTTPS direct-download links allowed hain.")
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("Download URL mein credentials ya non-standard port allowed nahi hai.")
        host = parsed.hostname.rstrip(".")
        if host.lower() in {"localhost", "localhost.localdomain"}:
            raise ValueError("Private/local network URLs allowed nahi hain.")
        try:
            literal = ipaddress.ip_address(host)
            addresses = [literal]
        except ValueError:
            records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(item[4][0]) for item in records]
        if not addresses or any(not addr.is_global for addr in addresses):
            raise ValueError("Download URL ka host public internet IP par resolve nahi hota.")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Download URL ka hostname resolve nahi hua; public HTTPS direct link check karein.") from exc


def download_zip_url(url: str, destination: Path):
    """Stream a public HTTPS ZIP to disk with redirect and 200 MB protections."""
    validate_public_https_url(url)
    opener = urllib.request.build_opener(PublicHTTPSRedirectHandler())
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Telegram-APK-Build-Bot/1.0", "Accept": "application/zip, application/octet-stream, */*"},
    )
    try:
        with opener.open(request, timeout=45) as response:
            final_url = response.geturl()
            validate_public_https_url(final_url)
            length = response.headers.get("Content-Length")
            if length:
                try:
                    if int(length) > MAX_ZIP_BYTES:
                        raise ValueError("Category: SOURCE ZIP SIZE. Direct-download ZIP 200 MB limit se badi hai.")
                except ValueError as exc:
                    if "Category:" in str(exc):
                        raise
            written = 0
            with destination.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_ZIP_BYTES:
                        raise ValueError("Category: SOURCE ZIP SIZE. Direct-download ZIP 200 MB limit se badi hai.")
                    output.write(chunk)
            if written == 0:
                raise ValueError("Category: DIRECT DOWNLOAD. Link se empty file mili.")
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Category: DIRECT DOWNLOAD / HTTP {exc.code}. Link public aur directly downloadable hona chahiye.") from exc
    except urllib.error.URLError as exc:
        raise ValueError("Category: DIRECT DOWNLOAD / NETWORK. Link download nahi hua: " + str(exc.reason)[:300]) from exc
    if not zipfile.is_zipfile(destination):
        destination.unlink(missing_ok=True)
        raise ValueError("Category: DIRECT DOWNLOAD CONTENT. Link se ZIP file nahi mili. Shareable direct-download URL use karein; login/interstitial web pages supported nahi hain.")


def telegram_archive_branch(url: str):
    """Recognize the official Telegram Android GitHub branch archive URL."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != "github.com":
        return None
    match = re.fullmatch(r"/DrKLO/Telegram/archive/refs/heads/([A-Za-z0-9._-]+)\.zip", parsed.path, re.IGNORECASE)
    return match.group(1) if match else None


def clone_telegram_source(destination: Path, branch: str):
    """Clone official Telegram source including required git submodules."""
    command = [
        "git", "clone", "--depth", "1", "--recurse-submodules", "--shallow-submodules",
        "--branch", branch, "https://github.com/DrKLO/Telegram.git", str(destination),
    ]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", timeout=2400, check=False)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        raise ValueError("Category: TELEGRAM SOURCE CHECKOUT TIMEOUT. Telegram repository/submodules ko 40 minute mein checkout nahi kiya ja saka.\n" + output[-1800:]) from exc
    except FileNotFoundError as exc:
        raise ValueError("Category: BOT RUNNER. Git executable nahi mila; GitHub Actions runner setup check karein.") from exc
    if result.returncode != 0:
        raise ValueError("Category: TELEGRAM SOURCE CHECKOUT. Official Telegram repository ya uske submodules download nahi hue.\n" + result.stdout[-2200:])
    if not (destination / "TMessagesProj").is_dir() or not (destination / ".gitmodules").is_file():
        raise ValueError("Category: TELEGRAM SOURCE STRUCTURE. Clone mein expected Telegram Android source files nahi mile.")


def detect_project(folder: Path):
    """Return (project_root, engine, human-readable name)."""
    if ((folder / "TMessagesProj").is_dir() and (folder / "TMessagesProj_App").is_dir()
            and (folder / "settings.gradle").is_file() and (folder / ".gitmodules").is_file()):
        return folder, "telegram", "Telegram Android (with submodules)"
    for pubspec in folder.rglob("pubspec.yaml"):
        root = pubspec.parent
        if (root / "lib" / "main.dart").is_file():
            return root, "flutter", "Flutter"
    candidates = [folder] + [p.parent for p in folder.rglob("gradlew") if p.is_file()]
    seen_roots = set()
    for root in candidates:
        if root in seen_roots:
            continue
        seen_roots.add(root)
        has_settings = (root / "settings.gradle").is_file() or (root / "settings.gradle.kts").is_file()
        has_build_script = any(p.is_file() for pattern in ("build.gradle", "build.gradle.kts") for p in root.rglob(pattern))
        if (root / "gradlew").is_file() and has_settings and has_build_script:
            return root, "gradle", "Native Android / Multi-module Gradle"
    for package_file in folder.rglob("package.json"):
        root = package_file.parent
        try:
            import json
            package = json.loads(package_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        # Expo includes React Native, so detect it first.
        if "expo" in deps:
            return root, "expo", "Expo (React Native)"
        if "cordova" in deps or (root / "config.xml").is_file():
            return root, "cordova", "Apache Cordova"
        if "react-native" in deps:
            if not (root / "android" / "gradlew").is_file():
                raise ValueError("Category: PROJECT STRUCTURE. React Native project mila, lekin android/gradlew missing hai. Android platform files ke saath project export karein.")
            return root, "react-native", "React Native"
        if any(x in deps for x in ("@capacitor/android", "@ionic/angular", "@ionic/react", "@ionic/vue")):
            if not (root / "android" / "gradlew").is_file():
                raise ValueError("Category: PROJECT STRUCTURE. Ionic/Capacitor project mila, lekin android/gradlew missing hai. Android platform add/sync karke project ZIP karein.")
            return root, "capacitor", "Ionic/Capacitor"
    raise ValueError("Category: PROJECT STRUCTURE. Supported Android project structure detect nahi hua. Native Android ke liye gradlew + settings.gradle(.kts) + app/build.gradle(.kts), ya Flutter ke liye pubspec.yaml + lib/main.dart chahiye. Agar files nested folder mein hain, poora project folder ZIP karein.")


def diagnose_build_failure(log_text: str, returncode: int) -> str:
    low = log_text.lower()
    tail = log_text[-4500:].strip() or "(Build log khaali hai.)"
    rules = [
        (("aapt2", "daemon startup failed", "failed to start daemon"), "BUILD ENVIRONMENT / ANDROID TOOLCHAIN", "Android build tool ya AAPT2 daemon start nahi hua. SDK/toolchain, permissions ya runner resources issue ho sakta hai; source ZIP ko bina proof dosh nahi diya ja sakta."),
        (("permission denied", "operation not permitted", "read-only file system", "accessdeniedexception"), "BUILD ENVIRONMENT / FILE PERMISSIONS", "Build process ko file/folder access nahi mila. Permissions ya container filesystem check karein."),
        (("no space left on device", "outofmemoryerror", "cannot allocate memory"), "RUNNER RESOURCES", "Disk space ya memory khatam hui. Ye apne aap mein source-code error ka proof nahi."),
        (("unknownhostexception", "could not resolve host", "connection timed out", "network is unreachable", "failed to resolve"), "NETWORK / DEPENDENCY DOWNLOAD", "Dependency download/resolve nahi hui. Network, repository ya dependency server check karein."),
        (("sdk location not found", "licenses not accepted", "failed to find target", "failed to install the following android sdk"), "ANDROID SDK CONFIGURATION", "Required Android SDK package, SDK path ya license configuration missing hai."),
        (("could not resolve all files", "could not find", "could not resolve", "failed to transform", "could not get resource"), "DEPENDENCY / PROJECT CONFIGURATION", "Gradle dependency/plugin resolve nahi hua. Versions, repositories, network aur dependency declarations check karein."),
        (("compilation error", "e: ", "error: ", "cannot find symbol", "unresolved reference", "failed to compile"), "SOURCE CODE / COMPILATION", "Compiler ne source code mein error report kiya. Neeche actual compiler log dekhein."),
        (("manifest merger failed", "resource linking failed", "error: resource"), "ANDROID RESOURCES / MANIFEST", "Manifest, resource reference ya dependency compatibility issue mila."),
        (("gradle-wrapper", "unsupported class file major version", "minimum supported gradle version"), "GRADLE / JAVA TOOLCHAIN", "Gradle wrapper, Java version ya Android Gradle Plugin compatibility issue ho sakta hai.")
    ]
    for needles, category, reason in rules:
        if any(n in low for n in needles):
            return f"Category: {category}\nAssessment: {reason}\n\nBuild log ka aakhri hissa:\n{tail}"
    return f"Category: BUILD FAILED — ROOT CAUSE NEEDS LOG REVIEW\nAssessment: Build command exit code {returncode} ke saath fail hui. Available log se source ZIP aur builder ki galti ko certainty se alag nahi kiya ja saka. Neeche raw log hai.\n\nBuild log ka aakhri hissa:\n{tail}"


def build_apk(root: Path, engine: str, cancel: threading.Event):
    if engine == "flutter":
        image = FLUTTER_IMAGE
        shell_command = "set -e; flutter --version; flutter pub get; flutter build apk --debug"
    elif engine == "telegram":
        image = DOCKER_IMAGE
        shell_command = (
            "set -e; trap 'chmod -R a+rwX /work 2>/dev/null || true' EXIT; "
            "echo '=== Runner storage and memory before Telegram build ==='; df -h / /work /tmp; free -h || true; "
            "yes | sdkmanager --licenses >/dev/null 2>&1 || true; "
            "sdkmanager 'platforms;android-36' 'build-tools;36.0.0' 'ndk;27.2.12479018' 'cmake;3.22.1'; "
            "echo '=== Telegram Gradle conservative-memory mode ==='; "
            "export GRADLE_OPTS='-Xmx2g -XX:MaxMetaspaceSize=512m -Dfile.encoding=UTF-8'; "
            "bash ./gradlew --no-daemon --max-workers=1 --no-parallel "
            "-Dorg.gradle.jvmargs='-Xmx2g -XX:MaxMetaspaceSize=512m -Dfile.encoding=UTF-8' "
            "-Dkotlin.compiler.execution.strategy=in-process assembleAfatDebug"
        )
    else:
        image = DOCKER_IMAGE
        if engine in ("react-native", "capacitor", "expo", "cordova"):
            if engine == "react-native":
                setup = ("if [ ! -d node_modules ]; then if [ -f package-lock.json ]; then npm ci; else npm install; fi; fi; "
                         "mkdir -p android/app/src/main/assets; ENTRY=index.js; [ -f index.js ] || ENTRY=index.android.js; "
                         "npx react-native bundle --platform android --dev false --entry-file \"$ENTRY\" "
                         "--bundle-output android/app/src/main/assets/index.android.bundle --assets-dest android/app/src/main/res")
                shell_command = ("set -e; trap 'chmod -R a+rwX /work 2>/dev/null || true' EXIT; "
                                 "if ! command -v npm >/dev/null 2>&1; then apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs; fi; "
                                 + setup + "; cd android; bash ./gradlew --no-daemon assembleDebug")
            elif engine == "capacitor":
                setup = ("if [ ! -d node_modules ]; then if [ -f package-lock.json ]; then npm ci; else npm install; fi; fi; "
                         "if node -e \"process.exit(require('./package.json').scripts?.build ? 0 : 1)\"; then npm run build; fi; npx cap sync android")
                shell_command = ("set -e; trap 'chmod -R a+rwX /work 2>/dev/null || true' EXIT; "
                                 "if ! command -v npm >/dev/null 2>&1; then apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs npm; fi; "
                                 + setup + "; cd android; bash ./gradlew --no-daemon assembleDebug")
            elif engine == "expo":
                setup = ("if [ -f package-lock.json ]; then npm ci; else npm install; fi; "
                         "npx expo prebuild --platform android --no-install; cd android; bash ./gradlew --no-daemon assembleDebug")
                shell_command = ("set -e; trap 'chmod -R a+rwX /work 2>/dev/null || true' EXIT; "
                                 "if ! command -v npm >/dev/null 2>&1; then apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs npm; fi; "
                                 + setup)
            else:
                setup = ("if [ -f package-lock.json ]; then npm ci; else npm install; fi; "
                         "npx --yes cordova platform add android; npx --yes cordova build android")
                shell_command = ("set -e; trap 'chmod -R a+rwX /work 2>/dev/null || true' EXIT; "
                                 "if ! command -v npm >/dev/null 2>&1; then apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs npm; fi; "
                                 + setup)
        else:
            shell_command = (
                "trap 'chmod -R a+rwX /work 2>/dev/null || true' EXIT; set -e; "
                "if [ ! -s gradle/wrapper/gradle-wrapper.jar ]; then "
                "test -s gradle/wrapper/gradle-wrapper.properties || "
                "{ echo 'Gradle wrapper properties missing: gradle/wrapper/gradle-wrapper.properties'; exit 1; }; "
                "mkdir -p gradle/wrapper; "
                "python3 -c \"import urllib.request; urllib.request.urlretrieve('https://raw.githubusercontent.com/gradle/gradle/v8.14.3/gradle/wrapper/gradle-wrapper.jar', 'gradle/wrapper/gradle-wrapper.jar')\"; "
                "fi; bash ./gradlew --no-daemon assembleDebug"
            )
    cmd = [
        "docker", "run", "--rm", "--network=bridge",
        "--memory=6g", "--memory-swap=6g", "--cpus=2",
        "--pids-limit=512", "--security-opt=no-new-privileges",
        "--tmpfs=/tmp:rw,exec,nosuid,size=1g",
        "-e", "HOME=/tmp", "-e", "GRADLE_USER_HOME=/work/.gradle-cache",
        "-v", f"{root.resolve()}:/work", "-w", "/work",
        image, "bash", "-lc", shell_command,
    ]
    log_path = root.parent / "build.log"
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as f:
            proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
            started = time.monotonic()
            timeout_seconds = 5400 if engine == "telegram" else BUILD_TIMEOUT_SECONDS
            while proc.poll() is None:
                if cancel.is_set() or time.monotonic() - started > timeout_seconds:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    if cancel.is_set():
                        raise RuntimeError("Build cancel kar di gayi.")
                    raise RuntimeError(f"Category: BUILD TIMEOUT / RUNNER RESOURCES. Build {timeout_seconds // 60} minute se zyada chali; ye source ZIP ke kharab hone ka proof nahi.")
                time.sleep(1)
            if proc.returncode:
                raise RuntimeError(diagnose_build_failure(log_path.read_text(errors="replace"), proc.returncode))
    except FileNotFoundError as e:
        raise RuntimeError("Category: BOT / RUNNER CONFIGURATION. Docker executable runner par nahi mila. GitHub Actions workflow aur Docker setup check karein.") from e
    if engine == "flutter":
        apks = list((root / "build" / "app" / "outputs" / "flutter-apk").glob("*.apk"))
    elif engine in ("react-native", "capacitor", "expo", "cordova"):
        apks = [p for p in root.rglob("*.apk") if "outputs" in p.parts and "apk" in p.parts and "debug" in p.parts]
    else:
        apks = [p for p in root.rglob("*.apk") if "outputs" in p.parts and "apk" in p.parts and "debug" in p.parts]
    if not apks:
        raise RuntimeError("Category: BUILD OUTPUT / BOT CONFIGURATION. Build command successful dikhi, lekin expected debug APK nahi mili. Output path aur build logs check karein.")
    apk = max(apks, key=lambda p: p.stat().st_mtime)
    if apk.stat().st_size > MAX_APK_BYTES:
        raise RuntimeError(f"Category: APK DELIVERY LIMIT. APK {apk.stat().st_size // (1024 * 1024)} MB hai; current bot limit 45 MB hai. Ye build failure nahi, delivery-size limit hai.")
    return apk

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Aapko is bot ko use karne ki permission nahi hai.")
        return
    await update.message.reply_text("Android project ZIP bhejein (Telegram attachment 20 MB tak) ya public HTTPS direct-download link paste karein (ZIP max 200 MB). Native Android, Flutter, React Native aur Ionic/Capacitor supported build paths available hain.", reply_markup=keyboard())

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        await q.answer("Permission nahi hai.", show_alert=True); return
    await q.answer()
    uid, action = update.effective_user.id, q.data
    if action == "help":
        await q.message.reply_text("Android project ZIP bhejein (Telegram attachment 20 MB tak) ya public HTTPS direct-download link paste karein (ZIP max 200 MB). Login-required links supported nahi hain.", reply_markup=keyboard())
    elif action == "status":
        job = JOBS.get(uid)
        await q.message.reply_text("Build status: " + job["status"] if job else "Abhi koi build nahi chal rahi.", reply_markup=keyboard())
    elif action == "cancel":
        job = JOBS.get(uid)
        if job:
            job["cancel"].set(); job["status"] = "Cancel ho rahi hai…"
            await q.message.reply_text("Cancel request bhej di.")
        else: await q.message.reply_text("Koi active build nahi hai.")

async def receive_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Aapko is bot ko use karne ki permission nahi hai."); return
    uid, doc = update.effective_user.id, update.message.document
    if uid in JOBS:
        await update.message.reply_text("Aapki ek build pehle se chal rahi hai."); return
    if not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        await update.message.reply_text("Kripya .zip file bhejein."); return
    if doc.file_size and doc.file_size > TELEGRAM_HOSTED_DOWNLOAD_LIMIT:
        await update.message.reply_text("Category: TELEGRAM HOSTED API LIMIT. Telegram attachment download yahan 20 MB tak supported hai. 20–200 MB ZIP ke liye isi ZIP ka public HTTPS direct-download link paste karein."); return
    job = {"status": "ZIP download ho rahi hai…", "cancel": threading.Event()}
    JOBS[uid] = job
    await update.message.reply_text("ZIP mil gayi. Build shuru kar raha hoon…", reply_markup=keyboard())
    job["task"] = asyncio.create_task(run_build(update, context, doc, job))


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Aapko is bot ko use karne ki permission nahi hai.")
        return
    url = (update.message.text or "").strip()
    if not url.lower().startswith("https://"):
        return
    uid = update.effective_user.id
    if uid in JOBS:
        await update.message.reply_text("Aapki ek build pehle se chal rahi hai.")
        return
    if len(url) > 4096:
        await update.message.reply_text("Download URL bahut lamba hai.")
        return
    job = {"status": "HTTPS link validate/download ho raha hai…", "cancel": threading.Event()}
    JOBS[uid] = job
    await update.message.reply_text("HTTPS link receive hua. Public-host safety checks ke baad ZIP (max 200 MB) download karke build karunga.", reply_markup=keyboard())
    job["task"] = asyncio.create_task(run_build(update, context, None, job, direct_url=url))


async def run_build(update, context, doc, job, direct_url=None):
    uid, chat_id = update.effective_user.id, update.effective_chat.id
    try:
        with tempfile.TemporaryDirectory(prefix="apk_bot_") as td:
            base = Path(td); zip_path = base / "project.zip"; src = base / "source"; src.mkdir()
            job["status"] = "ZIP download ho rahi hai…"
            telegram_branch = telegram_archive_branch(direct_url) if direct_url else None
            if telegram_branch:
                validate_public_https_url(direct_url)
                job["status"] = "Telegram source aur submodules clone ho rahe hain…"
                await safe_send(context.bot, chat_id, "Official Telegram Android source archive pehchana gaya. ZIP entry limit bypass karne ke bajay repository ko required submodules ke saath clone kar raha hoon; build lamba chal sakta hai.")
                await asyncio.to_thread(clone_telegram_source, src, telegram_branch)
            elif direct_url:
                await asyncio.to_thread(download_zip_url, direct_url, zip_path)
            else:
                tgfile = await context.bot.get_file(doc.file_id)
                await tgfile.download_to_drive(custom_path=str(zip_path))
            if job["cancel"].is_set(): raise RuntimeError("Build cancel kar di gayi.")
            if not telegram_branch:
                job["status"] = "ZIP check/extract ho rahi hai…"
                await asyncio.to_thread(safe_extract, zip_path, src)
            else:
                job["status"] = "Telegram source structure check ho rahi hai…"
            root, engine, project_name = detect_project(src)
            if job["cancel"].is_set(): raise RuntimeError("Build cancel kar di gayi.")
            job["status"] = f"{project_name} project build ho raha hai…"
            await safe_send(context.bot, chat_id, f"Project detect hua: {project_name}. Required build tools se build kar raha hoon.")
            apk = await asyncio.to_thread(build_apk, root, engine, job["cancel"])
            job["status"] = "APK bheji ja rahi hai…"
            with apk.open("rb") as f:
                await context.bot.send_document(chat_id=chat_id, document=f, filename=apk.name, caption="Debug APK ready hai.")
    except zipfile.BadZipFile:
        await safe_send(context.bot, chat_id, "Category: UPLOADED ZIP FILE. File valid ZIP archive nahi hai ya corrupt hai. Project ko dobara ZIP karke bhejein.", reply_markup=keyboard())
    except (ValueError, RuntimeError) as e:
        await safe_send(context.bot, chat_id, "APK build nahi hui:\n" + str(e)[-3000:], reply_markup=keyboard())
    except TelegramError as e:
        log.exception("Telegram file transfer failed")
        await safe_send(context.bot, chat_id, "Category: TELEGRAM / FILE TRANSFER. File transfer fail hua. Size limit, network ya Telegram API response check karein.\nTechnical error: " + str(e)[:1200], reply_markup=keyboard())
    except OSError as e:
        log.exception("Filesystem or runner error")
        await safe_send(context.bot, chat_id, "Category: BOT / RUNNER FILESYSTEM. File operation fail hui; ye ZIP ke source-code issue ka proof nahi.\nTechnical error: " + type(e).__name__ + ": " + str(e)[:1200], reply_markup=keyboard())
    except Exception as e:
        log.exception("Unexpected build/bot error")
        await safe_send(context.bot, chat_id, "Category: BOT INTERNAL ERROR. Bot ke internal operation mein unexpected exception aayi; isse ZIP ki galti sabit nahi hoti.\nTechnical error: " + type(e).__name__ + ": " + str(e)[:1200] + "\nFull traceback GitHub Actions log mein hai.", reply_markup=keyboard())
    finally:
        if JOBS.get(uid) is job: JOBS.pop(uid, None)

async def errors(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled Telegram error", exc_info=(type(context.error), context.error, context.error.__traceback__) if context.error else None)

def main():
    if not ALLOWED_USER_IDS:
        raise RuntimeError("ALLOWED_USER_IDS secret set karein.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.Document.ALL, receive_zip))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
    app.add_error_handler(errors)
    app.run_polling()

if __name__ == "__main__":
    main()
