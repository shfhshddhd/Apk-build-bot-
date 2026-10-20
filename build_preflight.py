"""Non-invasive source preflight checks for Android APK projects.

Findings are advisory: this module never claims a project is unbuildable solely
because a heuristic did not recognize a configuration.
"""
from __future__ import annotations

import json
from pathlib import Path


MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def inspect_project(root: Path, engine: str) -> list[dict[str, str]]:
    """Return actionable warnings/errors without modifying uploaded source."""
    root = root.resolve()
    findings: list[dict[str, str]] = []

    def add(level: str, code: str, message: str) -> None:
        findings.append({"level": level, "code": code, "message": message})

    if engine == "gradle":
        settings = [root / "settings.gradle", root / "settings.gradle.kts"]
        builds = [root / "build.gradle", root / "build.gradle.kts"]
        if not any(p.is_file() for p in settings):
            add("error", "GRADLE_SETTINGS_MISSING", "Gradle settings file nahi mili.")
        if not any(p.is_file() for p in builds):
            add("warning", "ROOT_BUILD_MISSING", "Root build.gradle nahi mili; custom multi-module projects mein ye valid ho sakta hai.")
        wrapper_props = root / "gradle" / "wrapper" / "gradle-wrapper.properties"
        wrapper_jar = root / "gradle" / "wrapper" / "gradle-wrapper.jar"
        if wrapper_props.is_file() and not wrapper_jar.is_file():
            add("warning", "WRAPPER_JAR_MISSING", "Gradle wrapper JAR missing hai; builder ise restore karne ki koshish karega.")
        elif not wrapper_props.is_file() and not (root / "gradlew").is_file():
            add("warning", "WRAPPER_MISSING", "Gradle wrapper missing hai; build image mein compatible system Gradle hona zaroori hai.")
        manifests = list(root.glob("*/src/main/AndroidManifest.xml"))
        manifests += list(root.glob("*/src/main/**/AndroidManifest.xml"))
        if not manifests:
            add("warning", "MANIFEST_NOT_OBVIOUS", "Common module paths mein AndroidManifest.xml nahi mili; generated/dynamic manifests ke liye ye warning false positive ho sakti hai.")
    elif engine == "flutter":
        if not (root / "pubspec.yaml").is_file() or not (root / "lib" / "main.dart").is_file():
            add("error", "FLUTTER_ENTRY_MISSING", "Flutter project mein pubspec.yaml aur lib/main.dart expected hain.")
        if not (root / "android").is_dir():
            add("warning", "FLUTTER_ANDROID_FOLDER_MISSING", "android/ folder nahi hai; Flutter tooling ise generate kar sakti hai, lekin custom Android setup ki zaroorat ho sakti hai.")
    elif engine in {"expo", "react-native", "capacitor", "cordova"}:
        package_file = root / "package.json"
        if not package_file.is_file():
            add("error", "PACKAGE_JSON_MISSING", "JavaScript project ka package.json missing hai.")
        else:
            try:
                package = json.loads(package_file.read_text(encoding="utf-8"))
                if not isinstance(package, dict):
                    raise ValueError("package.json root object nahi hai")
                if not (package.get("dependencies") or package.get("devDependencies")):
                    add("warning", "DEPENDENCIES_EMPTY", "package.json mein dependencies/devDependencies nahi dikh rahi; project custom ho sakta hai.")
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                add("error", "PACKAGE_JSON_INVALID", "package.json valid JSON nahi hai.")
        lockfiles = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml")
        if not any((root / name).is_file() for name in lockfiles):
            add("warning", "LOCKFILE_MISSING", "JS lockfile nahi mili; dependency versions non-reproducible ho sakti hain.")
        if engine == "react-native" and not (root / "android").is_dir():
            add("error", "ANDROID_PROJECT_MISSING", "React Native Android native project folder missing hai.")
        if engine == "capacitor" and not any(root.glob("capacitor.config.*")):
            add("warning", "CAPACITOR_CONFIG_MISSING", "capacitor.config.* nahi mili; cap sync fail ho sakta hai.")
    elif engine == "telegram":
        for rel in ("TMessagesProj", "TMessagesProj_App", ".gitmodules", "gradlew"):
            if not (root / rel).exists():
                add("error", "TELEGRAM_SOURCE_INCOMPLETE", f"Official Telegram source mein {rel} missing hai.")

    # Flag oversized individual build manifests early without reading arbitrary files.
    for rel in ("package.json", "pubspec.yaml", "settings.gradle", "settings.gradle.kts",
                "build.gradle", "build.gradle.kts", "AndroidManifest.xml"):
        path = root / rel
        try:
            if path.is_file() and path.stat().st_size > MAX_MANIFEST_BYTES:
                add("warning", "MANIFEST_UNUSUALLY_LARGE", f"{rel} {MAX_MANIFEST_BYTES // (1024 * 1024)} MB se bada hai; file ko review karein.")
        except OSError:
            pass

    if not findings:
        add("ok", "PREFLIGHT_PASSED", "Basic source structure checks pass hue. Ye successful APK build ki guarantee nahi hai.")
    return findings


def format_findings(findings: list[dict[str, str]]) -> str:
    labels = {"error": "ERROR", "warning": "WARNING", "ok": "OK"}
    return "\n".join(
        f"• [{labels.get(item['level'], item['level'].upper())}] {item['message']}"
        for item in findings
    )
