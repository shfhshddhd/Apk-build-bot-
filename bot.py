import asyncio
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_IDS = {int(x.strip()) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
DOCKER_IMAGE = os.environ.get("DOCKER_IMAGE", "ghcr.io/cirruslabs/android-sdk:35")
MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_UNZIPPED_BYTES = 250 * 1024 * 1024
MAX_ZIP_FILES = 5000
MAX_APK_BYTES = 45 * 1024 * 1024
BUILD_TIMEOUT_SECONDS = 600
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
                raise ValueError("Extracted ZIP 250 MB se badi hai.")
            target = dest.joinpath(*pp.parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)

def find_root(folder: Path):
    candidates = [folder] + [p.parent for p in folder.rglob("gradlew") if p.is_file()]
    for root in candidates:
        if ((root / "gradlew").is_file()
            and ((root / "settings.gradle").is_file() or (root / "settings.gradle.kts").is_file())
            and ((root / "app/build.gradle").is_file() or (root / "app/build.gradle.kts").is_file())):
            return root
    raise ValueError("Android Gradle project nahi mila. ZIP mein gradlew, settings.gradle aur app/build.gradle hone chahiye.")

def build_apk(root: Path, cancel: threading.Event):
    cmd = [
        "docker", "run", "--rm", "--network=bridge",
        "--memory=4g", "--memory-swap=4g", "--cpus=2",
        "--pids-limit=256", "--security-opt=no-new-privileges",
        "--cap-drop=ALL", "--tmpfs=/tmp:rw,nosuid,size=1g",
        "-e", "HOME=/tmp", "-e", "GRADLE_USER_HOME=/tmp/gradle",
        "-v", f"{root.resolve()}:/work", "-w", "/work",
        DOCKER_IMAGE, "bash", "-lc",
        "bash ./gradlew --no-daemon assembleDebug",
    ]
    log_path = root.parent / "gradle-build.log"
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as f:
            proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
            start_time = time.monotonic()
            while proc.poll() is None:
                if cancel.is_set() or time.monotonic() - start_time > BUILD_TIMEOUT_SECONDS:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    if cancel.is_set():
                        raise RuntimeError("Build cancel kar di gayi.")
                    raise RuntimeError("Build 10 minute ki time limit se zyada chali.")
            if proc.returncode:
                raise RuntimeError(
                    "Gradle build fail hui. Log ka aakhri hissa:\\n"
                    + log_path.read_text(errors="replace")[-2500:]
                )
    except FileNotFoundError as e:
        raise RuntimeError("Docker command nahi mila. GitHub Actions runner par Docker check karein.") from e
    apks = [p for p in root.rglob("*.apk") if "outputs" in p.parts and "apk" in p.parts and "debug" in p.parts]
    if not apks:
        raise RuntimeError("Build ke baad debug APK nahi mili.")
    apk = max(apks, key=lambda p: p.stat().st_mtime)
    if apk.stat().st_size > MAX_APK_BYTES:
        raise RuntimeError("APK 45 MB se badi hai.")
    return apk

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Aapko is bot ko use karne ki permission nahi hai.")
        return
    await update.message.reply_text("Android Gradle project ki ZIP bhejein. Bot debug APK build karke bhejega.\nZIP mein gradlew, settings.gradle aur app/build.gradle hona chahiye.", reply_markup=keyboard())

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        await q.answer("Permission nahi hai.", show_alert=True); return
    await q.answer()
    uid, action = update.effective_user.id, q.data
    if action == "help":
        await q.message.reply_text("Android project ZIP bhejein (APK nahi). ZIP mein gradlew, settings.gradle aur app/build.gradle hona chahiye.", reply_markup=keyboard())
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
    if doc.file_size and doc.file_size > MAX_ZIP_BYTES:
        await update.message.reply_text("ZIP 20 MB se badi hai."); return
    job = {"status": "ZIP download ho rahi hai…", "cancel": threading.Event()}
    JOBS[uid] = job
    await update.message.reply_text("ZIP mil gayi. Build shuru kar raha hoon…", reply_markup=keyboard())
    job["task"] = asyncio.create_task(run_build(update, context, doc, job))

async def run_build(update, context, doc, job):
    uid, chat_id = update.effective_user.id, update.effective_chat.id
    try:
        with tempfile.TemporaryDirectory(prefix="apk_bot_") as td:
            base = Path(td); zip_path = base / "project.zip"; src = base / "source"; src.mkdir()
            job["status"] = "ZIP download ho rahi hai…"
            tgfile = await context.bot.get_file(doc.file_id)
            await tgfile.download_to_drive(custom_path=str(zip_path))
            if job["cancel"].is_set(): raise RuntimeError("Build cancel kar di gayi.")
            job["status"] = "ZIP check/extract ho rahi hai…"
            await asyncio.to_thread(safe_extract, zip_path, src)
            root = find_root(src)
            if job["cancel"].is_set(): raise RuntimeError("Build cancel kar di gayi.")
            job["status"] = "Android APK build ho rahi hai…"
            apk = await asyncio.to_thread(build_apk, root, job["cancel"])
            job["status"] = "APK bheji ja rahi hai…"
            with apk.open("rb") as f:
                await context.bot.send_document(chat_id=chat_id, document=f, filename=apk.name, caption="Debug APK ready hai.")
    except zipfile.BadZipFile:
        await safe_send(context.bot, chat_id, "Valid ZIP nahi hai. Project ko dobara ZIP karein.", reply_markup=keyboard())
    except (ValueError, RuntimeError) as e:
        await safe_send(context.bot, chat_id, "APK build nahi hui:\n" + str(e)[-3000:], reply_markup=keyboard())
    except TelegramError:
        log.exception("Telegram file transfer failed")
        await safe_send(context.bot, chat_id, "Telegram se file transfer fail hua. Dobara try karein.", reply_markup=keyboard())
    except Exception:
        log.exception("Unexpected build error")
        await safe_send(context.bot, chat_id, "Unexpected error. GitHub Actions logs check karein.", reply_markup=keyboard())
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
    app.add_error_handler(errors)
    app.run_polling()

if __name__ == "__main__":
    main()
