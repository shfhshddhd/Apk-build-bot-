import asyncio
import http.server
import logging
import os
import ipaddress
import hashlib
import re
import socket
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from project_inspector import detect_project

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_IDS = {int(x.strip()) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
DOCKER_IMAGE = os.environ.get("DOCKER_IMAGE", "ghcr.io/cirruslabs/android-sdk:35")
MAX_ZIP_BYTES = int(os.environ.get("MAX_ZIP_BYTES", str(200 * 1024 * 1024)))
TELEGRAM_HOSTED_DOWNLOAD_LIMIT = MAX_ZIP_BYTES
MAX_UNZIPPED_BYTES = int(os.environ.get("MAX_UNZIPPED_BYTES", str(2 * 1024 * 1024 * 1024)))
MAX_ZIP_FILES = int(os.environ.get("MAX_ZIP_FILES", "50000"))
MAX_APK_BYTES = int(os.environ.get("MAX_APK_BYTES", str(500 * 1024 * 1024)))
BUILD_TIMEOUT_SECONDS = int(os.environ.get("BUILD_TIMEOUT_SECONDS", "10800"))
MAX_ACTIVE_BUILDS = max(1, int(os.environ.get("MAX_ACTIVE_BUILDS", "1")))
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


def diagnose_build_failure(log_text: str, returncode: int) -> str:
    low = log_text.lower()
    lines = log_text.splitlines()
    markers = ("* what went wrong:", "execution failed for task", "failure:",
               "caused by:", "error:", "could not resolve", "could not find",
               "manifest merger failed", "resource linking failed",
               "outofmemoryerror", "no space left on device", "unresolved reference",
               "cannot find symbol")
    important = []
    seen = set()
    for i, line in enumerate(lines):
        if any(marker in line.lower() for marker in markers):
            for j in range(max(0, i - 1), min(len(lines), i + 4)):
                item = lines[j].strip()
                if item and item not in seen:
                    seen.add(item)
                    important.append(item)
            if len(important) >= 45:
                break
    diagnostic = "\n".join(important)[-3200:] or "(Log mein recognizable error marker nahi mila.)"
    tail = log_text[-1800:].strip() or "(Build log khaali hai.)"
    excerpt = "Important error lines:\n" + diagnostic + "\n\nFinal log tail:\n" + tail
    rules = [
        (("cannot connect to the docker daemon", "docker daemon is not running", "error during connect", "docker: command not found"), "BOT RUNNER / DOCKER ENGINE", "Docker engine/CLI available nahi hai ya runner Docker daemon se connect nahi kar pa raha. Ye project ZIP ke source error ka proof nahi."),
        (("pull access denied", "manifest unknown", "failed to resolve reference", "error pulling image", "toomanyrequests: rate exceeded"), "BUILDER IMAGE / REGISTRY", "Build container image download nahi hui ya registry ne access/rate limit issue diya. DOCKER_IMAGE/FLUTTER_IMAGE, registry access aur network check karein."),
        (("pub get failed", "version solving failed", "because every version of", "could not find package", "failed to resolve dependencies"), "FLUTTER DEPENDENCIES", "Flutter/Dart package resolution fail hui. pubspec.yaml version constraints, package repository aur network log check karein; ise automatically source-code bug nahi maana ja sakta."),
        (("npm err!", "npm error", "enoent.*package.json", "could not resolve dependency", "eresolve"), "JAVASCRIPT DEPENDENCIES", "npm/package manager dependency install fail hua. package.json, lockfile, engine constraints aur registry/network check karein."),
        (("cmake error", "ninja: build stopped", "ndk is not installed", "no version of ndk matched", "c++ compiler"), "NATIVE ANDROID TOOLCHAIN", "Native C/C++ build ke liye NDK/CMake/compiler missing ya incompatible ho sakta hai. Declared versions aur SDK setup check karein."),
        (("aapt2", "daemon startup failed", "failed to start daemon"), "BUILD ENVIRONMENT / ANDROID TOOLCHAIN", "Android build tool ya AAPT2 daemon start nahi hua. SDK/toolchain, permissions ya runner resources issue ho sakta hai; source ZIP ko bina proof dosh nahi diya ja sakta."),
        (("permission denied", "operation not permitted", "read-only file system", "accessdeniedexception"), "BUILD ENVIRONMENT / FILE PERMISSIONS", "Build process ko file/folder access nahi mila. Permissions ya container filesystem check karein."),
        (("no space left on device", "outofmemoryerror", "cannot allocate memory"), "RUNNER RESOURCES", "Disk space ya memory khatam hui. Ye apne aap mein source-code error ka proof nahi."),
        (("unknownhostexception", "could not resolve host", "connection timed out", "network is unreachable", "failed to resolve"), "NETWORK / DEPENDENCY DOWNLOAD", "Dependency download/resolve nahi hui. Network, repository ya dependency server check karein."),
        (("sdk location not found", "licenses not accepted", "failed to find target", "failed to install the following android sdk"), "ANDROID SDK CONFIGURATION", "Required Android SDK package, SDK path ya license configuration missing hai."),
        (("could not resolve all files", "could not find", "could not resolve", "failed to transform", "could not get resource"), "DEPENDENCY / PROJECT CONFIGURATION", "Gradle dependency/plugin resolve nahi hua. Versions, repositories, network aur dependency declarations check karein."),
        (("wrapper jar download failed", "wrapper jar download returned", "could not determine gradle version from distributionurl"), "GRADLE WRAPPER / TOOLCHAIN DOWNLOAD", "Gradle wrapper JAR download nahi hua. Ye compiler/source-code error nahi hai; wrapper URL/version tag aur network response check karein."),
        (("no assembledebug or flavor-specific assemble*debug task was found", "task 'assembledebug' not found", "task not found"), "GRADLE / BUILD VARIANT", "Gradle task discovery ko project ka debug task nahi mila. Module-qualified task names aur available tasks check karein."),
        (("manifest merger failed", "resource linking failed", "error: resource"), "ANDROID RESOURCES / MANIFEST", "Manifest, resource reference ya dependency compatibility issue mila."),
        (("compilation error", "e: ", "error: ", "cannot find symbol", "unresolved reference", "failed to compile"), "SOURCE CODE / COMPILATION", "Compiler ne source code mein error report kiya. Neeche actual compiler log dekhein."),
        (("gradle-wrapper", "unsupported class file major version", "minimum supported gradle version", "requires java", "this version of the android gradle plugin requires"), "GRADLE / JAVA TOOLCHAIN", "Gradle wrapper, Java version ya Android Gradle Plugin compatibility issue ho sakta hai. Project-pinned versions aur runner Java version compare karein."),
        (("node: command not found", "npm: command not found", "unsupported engine", "engine node"), "JAVASCRIPT TOOLCHAIN", "Node.js/npm missing ya incompatible hai. Project ke package.json mein required Node version check karein."),
        (("sdkmanager: command not found", "failed to find target", "platforms;android-"), "ANDROID SDK CONFIGURATION", "Required Android SDK platform/build-tools unavailable hain. SDK package aur compileSdk configuration check karein."),
        (("http 429", "too many requests", "status code 429", "rate limit"), "NETWORK / REPOSITORY RATE LIMIT", "Package repository ne rate limit ki. Thodi der baad retry karein; repeated automatic retries nahi kiye gaye.")
    ]
    for needles, category, reason in rules:
        if any(n in low for n in needles):
            return f"Category: {category}\nAssessment: {reason}\n\n{excerpt}"
    return f"Category: BUILD FAILED — ROOT CAUSE NEEDS LOG REVIEW\nAssessment: Build command exit code {returncode} ke saath fail hui. Available log se source ZIP aur builder ki galti ko certainty se alag nahi kiya ja saka. Neeche raw log hai.\n\n{excerpt}"


def build_apk(root: Path, engine: str, cancel: threading.Event, job=None):
    from build_adapters import build_command_for

    image, shell_command = build_command_for(engine, DOCKER_IMAGE, FLUTTER_IMAGE)
    container_name = "apk-build-" + uuid.uuid4().hex[:12]
    cmd = [
        "docker", "run", "--rm", "--name", container_name, "--network=bridge",
        "--memory=6g", "--memory-swap=6g", "--cpus=2",
        "--pids-limit=512", "--security-opt=no-new-privileges",
        "--tmpfs=/tmp:rw,exec,nosuid,size=1g",
        "-e", "HOME=/tmp", "-e", "GRADLE_USER_HOME=/work/.gradle-cache",
        "-v", f"{root.resolve()}:/work", "-w", "/work",
        image, "bash", "-lc", shell_command,
    ]
    log_path = root.parent / "build.log"
    # Remove only generated debug APK outputs so a stale/pre-uploaded artifact
    # cannot be mistaken for the APK produced by this build.
    flutter_output = root / "build" / "app" / "outputs" / "flutter-apk"
    if engine == "flutter" and flutter_output.is_dir():
        for stale_apk in flutter_output.glob("*.apk"):
            stale_apk.unlink(missing_ok=True)
    for output_dir in root.rglob("outputs/apk"):
        if output_dir.is_dir():
            for stale_apk in output_dir.rglob("*.apk"):
                stale_apk.unlink(missing_ok=True)
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as f:
            proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
            started = time.monotonic()
            timeout_seconds = BUILD_TIMEOUT_SECONDS
            last_task_count = 0
            while proc.poll() is None:
                if cancel.is_set() or time.monotonic() - started > timeout_seconds:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    # Stopping the Docker CLI alone may leave the container running.
                    try:
                        subprocess.run(
                            ["docker", "rm", "-f", container_name],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=15, check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        log.exception("Could not force-remove cancelled build container")
                    if cancel.is_set():
                        raise RuntimeError("Build cancel kar di gayi.")
                    raise RuntimeError(
                        f"Category: BUILD TIMEOUT / RUNNER RESOURCES. Build "
                        f"{timeout_seconds // 60} minute se zyada chali; ye source ZIP ke kharab hone ka proof nahi."
                    )
                if job is not None:
                    try:
                        log_text = log_path.read_text(encoding="utf-8", errors="replace")
                        task_count = len(re.findall(r"(?m)^> Task ", log_text))
                        last_task_count = max(last_task_count, task_count)
                        progress = min(95, 5 + int(last_task_count * (0.25 if engine == "telegram" else 1.5)))
                        job["progress"] = max(job.get("progress", 0), progress)
                        job["status"] = f"Build chal rahi hai — lagbhag {job['progress']}%"
                        lines = [line.strip() for line in log_text.splitlines() if line.strip()]
                        task_lines = [line for line in lines if line.startswith("> Task ")]
                        if task_lines:
                            job["detail"] = task_lines[-1][:100]
                        elif lines:
                            job["detail"] = lines[-1][:100]
                    except OSError:
                        pass
                time.sleep(2)
            if proc.returncode:
                raise RuntimeError(diagnose_build_failure(log_path.read_text(errors="replace"), proc.returncode))
    except FileNotFoundError as e:
        raise RuntimeError(
            "Category: BOT / RUNNER CONFIGURATION. Docker executable runner par nahi mila. "
            "GitHub Actions workflow aur Docker setup check karein."
        ) from e
    if engine == "flutter":
        apks = list((root / "build" / "app" / "outputs" / "flutter-apk").glob("*.apk"))
    else:
        apks = [
            p for p in root.rglob("*.apk")
            if "outputs" in p.parts and "apk" in p.parts
            and any("debug" in part.lower() for part in p.parts)
            and p.is_file()
        ]
    if not apks:
        raise RuntimeError(
            "Category: BUILD OUTPUT / BOT CONFIGURATION. Build command successful dikhi, lekin expected debug APK nahi mili. "
            "Output path aur build logs check karein."
        )
    apk = max(apks, key=lambda p: p.stat().st_mtime)
    if not apk.is_file() or apk.stat().st_size <= 0:
        raise RuntimeError("Category: ARTIFACT VERIFICATION. APK output empty ya missing hai.")
    if apk.stat().st_size > MAX_APK_BYTES:
        raise RuntimeError(
            f"Category: APK DELIVERY LIMIT. APK {apk.stat().st_size // (1024 * 1024)} MB hai; "
            f"configured limit {MAX_APK_BYTES // (1024 * 1024)} MB hai. "
            "MAX_APK_BYTES badha sakte hain agar Telegram API aur runner resources allow karein."
        )
    try:
        with zipfile.ZipFile(apk) as artifact:
            damaged_entry = artifact.testzip()
            if damaged_entry:
                raise RuntimeError(f"Category: ARTIFACT VERIFICATION. APK archive corrupt hai: {damaged_entry}")
            if "AndroidManifest.xml" not in artifact.namelist():
                raise RuntimeError("Category: ARTIFACT VERIFICATION. APK mein AndroidManifest.xml nahi mila.")
    except zipfile.BadZipFile as exc:
        raise RuntimeError("Category: ARTIFACT VERIFICATION. Output file valid APK/ZIP archive nahi hai.") from exc
    return apk


DOWNLOAD_LINK_TTL_SECONDS = max(300, int(os.environ.get("DOWNLOAD_LINK_TTL_SECONDS", "3600")))


def start_temporary_download_link(apk: Path):
    """Expose one APK through a random, expiring Cloudflare quick-tunnel URL."""
    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        raise RuntimeError("Cloudflare Tunnel executable missing hai; workflow install step check karein.")
    token = uuid.uuid4().hex + uuid.uuid4().hex
    download_dir = Path(tempfile.mkdtemp(prefix="apk_download_"))
    saved_apk = download_dir / apk.name
    shutil.copy2(apk, saved_apk)
    expires_at = time.time() + DOWNLOAD_LINK_TTL_SECONDS

    class DownloadHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if time.time() > expires_at or self.path != "/" + token + "/" + urllib.parse.quote(saved_apk.name):
                self.send_error(404)
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.android.package-archive")
                self.send_header("Content-Length", str(saved_apk.stat().st_size))
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(saved_apk.name))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with saved_apk.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, fmt, *args):
            log.info("Temporary APK download server: " + fmt, *args)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DownloadHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tunnel_log = download_dir / "cloudflared.log"
    tunnel_output = tunnel_log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [cloudflared, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{server.server_port}"],
        stdout=tunnel_output, stderr=subprocess.STDOUT, text=True,
    )
    public_url = None
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                log_text = tunnel_log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                log_text = ""
            match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", log_text)
            if match:
                public_url = match.group(0)
                break
            time.sleep(0.5)
        if not public_url:
            details = tunnel_log.read_text(encoding="utf-8", errors="replace")[-800:]
            raise RuntimeError("Temporary public download tunnel start nahi hua. " + details)
        link = public_url.rstrip("/") + "/" + token + "/" + urllib.parse.quote(saved_apk.name)
    except Exception:
        process.terminate()
        server.shutdown()
        tunnel_output.close()
        shutil.rmtree(download_dir, ignore_errors=True)
        raise

    def cleanup():
        time.sleep(DOWNLOAD_LINK_TTL_SECONDS)
        try:
            process.terminate()
            process.wait(timeout=10)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        server.shutdown()
        server.server_close()
        tunnel_output.close()
        shutil.rmtree(download_dir, ignore_errors=True)

    threading.Thread(target=cleanup, daemon=True).start()
    return link, DOWNLOAD_LINK_TTL_SECONDS


HELP_TEXT = (
    "📦 Universal APK Build Bot\n\n"
    "Complete supported project ZIP bhejein ya public HTTPS direct-download link paste karein.\n"
    "Supported build paths: Native Android/Gradle, Flutter, React Native, Expo, "
    "Ionic/Capacitor, Cordova, aur special Telegram Android source build.\n\n"
    "Commands: /help /status /cancel /frameworks /limits /about\n"
    "Note: project ko compatible SDKs, dependencies aur required credentials phir bhi chahiye ho sakte hain."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Aapko is bot ko use karne ki permission nahi hai.")
        return
    await update.message.reply_text(HELP_TEXT, reply_markup=keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Aapko is bot ko use karne ki permission nahi hai.")
        return
    await update.message.reply_text(HELP_TEXT, reply_markup=keyboard())


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Aapko is bot ko use karne ki permission nahi hai.")
        return
    job = JOBS.get(update.effective_user.id)
    if not job:
        await update.message.reply_text("Abhi koi active build nahi chal rahi.")
        return
    progress = job.get("progress")
    detail = job.get("detail", "")
    message = "Build status: " + job.get("status", "Working…")
    if progress is not None:
        message += f"\nEstimated progress: {progress}%"
    if detail:
        message += "\nCurrent stage: " + str(detail)[:180]
    await update.message.reply_text(message, reply_markup=keyboard())


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Aapko is bot ko use karne ki permission nahi hai.")
        return
    job = JOBS.get(update.effective_user.id)
    if not job:
        await update.message.reply_text("Koi active build nahi hai.")
        return
    job["cancel"].set()
    job["status"] = "Cancel ho rahi hai…"
    await update.message.reply_text("Cancel request bhej di. Running container ko stop hone mein kuch seconds lag sakte hain.")


async def frameworks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Aapko is bot ko use karne ki permission nahi hai.")
        return
    await update.message.reply_text(
        "Supported build paths:\n"
        "• Native Android Java/Kotlin + Gradle\n"
        "• Flutter\n"
        "• React Native\n"
        "• Expo (native prebuild)\n"
        "• Ionic / Capacitor\n"
        "• Apache Cordova\n"
        "• Official Telegram Android source (special build)\n\n"
        "C/C++ NDK modules are supported when integrated into a compatible Gradle Android project. "
        "Standalone Unity, Godot, .NET MAUI, Kivy/Buildozer and arbitrary website ZIPs are not yet guaranteed."
    )


async def limits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Aapko is bot ko use karne ki permission nahi hai.")
        return
    await update.message.reply_text(
        "Current configured limits:\n"
        f"• Source ZIP: {MAX_ZIP_BYTES // (1024 * 1024)} MB\n"
        f"• Extracted source: {MAX_UNZIPPED_BYTES // (1024 * 1024)} MB\n"
        f"• ZIP entries: {MAX_ZIP_FILES:,}\n"
        f"• APK output: {MAX_APK_BYTES // (1024 * 1024)} MB\n"
        f"• Normal build timeout: {BUILD_TIMEOUT_SECONDS // 60} minutes\n"
        f"• Maximum concurrent builds: {MAX_ACTIVE_BUILDS}\n"
        "Actual limits also depend on Telegram API, GitHub runner resources, network and disk space."
    )


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Aapko is bot ko use karne ki permission nahi hai.")
        return
    await update.message.reply_text(
        "Universal APK Build Bot\n"
        "Framework-aware project inspection, isolated Docker builds, diagnostic log summaries, "
        "APK archive verification and SHA-256 reporting.\n"
        "Build success depends on project completeness, toolchain compatibility, dependencies and available resources."
    )


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        await q.answer("Permission nahi hai.", show_alert=True); return
    await q.answer()
    uid, action = update.effective_user.id, q.data
    if action == "help":
        await q.message.reply_text(HELP_TEXT, reply_markup=keyboard())
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
    if len(JOBS) >= MAX_ACTIVE_BUILDS:
        await update.message.reply_text(
            f"Build server busy hai. Abhi {MAX_ACTIVE_BUILDS} active build limit hai; "
            "thodi der baad dobara try karein."
        )
        return
    if not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        await update.message.reply_text("Kripya .zip file bhejein."); return
    if doc.file_size and doc.file_size > TELEGRAM_HOSTED_DOWNLOAD_LIMIT:
        await update.message.reply_text(
            f"Category: SOURCE ZIP SIZE. ZIP {TELEGRAM_HOSTED_DOWNLOAD_LIMIT // (1024 * 1024)} MB upload limit se badi hai. "
            f"ZIP ko limit se chhota karein ya public HTTPS direct-download link bhejein (max {MAX_ZIP_BYTES // (1024 * 1024)} MB)."
        ); return
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
    if len(JOBS) >= MAX_ACTIVE_BUILDS:
        await update.message.reply_text(
            f"Build server busy hai. Abhi {MAX_ACTIVE_BUILDS} active build limit hai; "
            "thodi der baad dobara try karein."
        )
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
            job["progress"] = 1
            job["detail"] = "Build environment start ho raha hai"
            progress_message = await context.bot.send_message(
                chat_id=chat_id,
                text="🔧 **Build progress**\n[░░░░░░░░░░] 1%\nBuild environment start ho raha hai.\n*Percent estimate hai; Gradle exact overall percentage provide nahi karta.*",
                parse_mode="Markdown",
                reply_markup=keyboard(),
            )
            build_task = asyncio.create_task(asyncio.to_thread(build_apk, root, engine, job["cancel"], job))
            last_progress_text = ""
            while not build_task.done():
                progress = int(job.get("progress", 1))
                detail = job.get("detail", "Build chal rahi hai…")
                filled = min(10, max(0, progress // 10))
                bar = "█" * filled + "░" * (10 - filled)
                progress_text = (
                    "🔧 **Build progress**\n"
                    f"[{bar}] {progress}%\n"
                    f"Status: {detail}\n"
                    f"Baqi: lagbhag {100 - progress}%\n"
                    "*Percent estimate hai; Gradle exact overall percentage provide nahi karta.*"
                )
                if progress_text != last_progress_text:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id, message_id=progress_message.message_id,
                            text=progress_text, parse_mode="Markdown", reply_markup=keyboard(),
                        )
                        last_progress_text = progress_text
                    except TelegramError:
                        pass
                await asyncio.sleep(5)
            apk = await build_task
            job["progress"] = 100
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=progress_message.message_id,
                    text="✅ **Build progress**\n[██████████] 100%\nDebug APK ready hai; ab file bhej raha hoon.",
                    parse_mode="Markdown", reply_markup=keyboard(),
                )
            except TelegramError:
                pass
            job["status"] = "APK bheji ja rahi hai…"
            digest = hashlib.sha256()
            with apk.open("rb") as artifact_file:
                for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            size_mb = apk.stat().st_size / (1024 * 1024)
            caption = (
                "✅ Debug APK ready hai.\n"
                f"Framework: {project_name}\n"
                f"Size: {size_mb:.2f} MB\n"
                f"SHA-256: {digest.hexdigest()}"
            )
            try:
                with apk.open("rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id, document=f, filename=apk.name, caption=caption,
                    )
            except TelegramError as upload_error:
                if "413" not in str(upload_error) and "request entity too large" not in str(upload_error).lower():
                    raise
                job["status"] = "Badi APK ke liye temporary download link ban raha hai…"
                link, ttl = await asyncio.to_thread(start_temporary_download_link, apk)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "✅ APK build successful hai, lekin Telegram upload size limit ki wajah se file attach nahi ho saki.\n"
                        f"Framework: {project_name}\nSize: {size_mb:.2f} MB\n"
                        f"SHA-256: {digest.hexdigest()}\n\n"
                        f"⬇️ Temporary APK download link (lagbhag {ttl // 60} minute valid):\n{link}\n\n"
                        "Link random secret path ke saath hai; ise sirf trusted jagah share karein."
                    ),
                    disable_web_page_preview=True,
                )
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
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("frameworks", frameworks_command))
    app.add_handler(CommandHandler("limits", limits_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.Document.ALL, receive_zip))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
    app.add_error_handler(errors)
    app.run_polling()

if __name__ == "__main__":
    main()
