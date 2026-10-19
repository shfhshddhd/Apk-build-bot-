"""Bounded, deterministic project fingerprinting for the APK build bot."""
from __future__ import annotations

import json
import os
from pathlib import Path

IGNORED_DIRS = {
    ".git", ".gradle", ".idea", ".dart_tool", ".next", ".expo",
    "node_modules", "build", "dist", "target", "vendor", "Pods",
    "__pycache__", ".pytest_cache",
}
MAX_INSPECTED_FILES = 100_000


def _inventory(folder: Path):
    """Return relative file paths while avoiding generated/dependency trees."""
    files = []
    for current, dirs, names in os.walk(folder, topdown=True, followlinks=False):
        dirs[:] = sorted(
            d for d in dirs
            if d not in IGNORED_DIRS and not (Path(current) / d).is_symlink()
        )
        base = Path(current)
        for name in sorted(names):
            path = base / name
            if path.is_file() and not path.is_symlink():
                files.append(path)
                if len(files) > MAX_INSPECTED_FILES:
                    raise ValueError(
                        "Category: PROJECT INSPECTION LIMIT. Project mein bahut zyada files hain; "
                        "generated folders (node_modules/build/.git) hata kar dobara ZIP karein."
                    )
    return files


def detect_project(folder: Path):
    """Return (project_root, engine, human-readable name) for a supported project."""
    folder = folder.resolve()

    # Check the special source build before inventorying it: its submodules can
    # contain many files, and the generic inspection cap should not block it.
    if (
        (folder / "TMessagesProj").is_dir()
        and (folder / "TMessagesProj_App").is_dir()
        and (folder / "settings.gradle").is_file()
        and (folder / ".gitmodules").is_file()
    ):
        return folder, "telegram", "Telegram Android (with submodules)"

    files = _inventory(folder)
    by_name = {}
    for path in files:
        by_name.setdefault(path.name.lower(), []).append(path)

    # Prefer actual Flutter application roots over incidental nested manifests.
    for pubspec in by_name.get("pubspec.yaml", []):
        root = pubspec.parent
        if (root / "lib" / "main.dart").is_file():
            return root, "flutter", "Flutter"

    # Detect JavaScript framework projects before their generated android/ Gradle
    # subprojects. Otherwise React Native/Expo/Capacitor can be misclassified
    # as plain native Gradle projects and miss framework-specific preparation.
    for package_file in by_name.get("package.json", []):
        root = package_file.parent
        try:
            package = json.loads(package_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(package, dict):
            continue
        dependencies = package.get("dependencies") or {}
        dev_dependencies = package.get("devDependencies") or {}
        if not isinstance(dependencies, dict):
            dependencies = {}
        if not isinstance(dev_dependencies, dict):
            dev_dependencies = {}
        deps = {**dependencies, **dev_dependencies}
        names = set(deps)

        if "expo" in names or (root / "app.json").is_file() and "expo" in str(package.get("name", "")).lower():
            return root, "expo", "Expo (React Native)"

        if "cordova" in names or (root / "config.xml").is_file():
            if (root / "config.xml").is_file() or "cordova" in names:
                return root, "cordova", "Apache Cordova"

        if "react-native" in names:
            android = root / "android"
            if any((android / n).is_file() for n in ("settings.gradle", "settings.gradle.kts")):
                return root, "react-native", "React Native"
            raise ValueError(
                "Category: PROJECT STRUCTURE. React Native project mila, lekin Android native "
                "project configuration missing hai. Android platform generate/add karke ZIP karein."
            )

        if any(n in names for n in ("@capacitor/android", "@ionic/angular", "@ionic/react", "@ionic/vue")):
            config_exists = any(root.glob("capacitor.config.*"))
            if config_exists or (root / "android").is_dir():
                return root, "capacitor", "Ionic/Capacitor"

    # Native Gradle roots may use a custom module name and may not include a
    # wrapper. The build stage decides whether a compatible system Gradle exists.
    settings_names = {"settings.gradle", "settings.gradle.kts"}
    settings_roots = {p.parent for p in files if p.name.lower() in settings_names}
    candidates = [folder] + sorted(
        (p for p in settings_roots if p != folder),
        key=lambda p: (len(p.parts), str(p)),
    )
    for root in candidates:
        if not any((root / name).is_file() for name in settings_names):
            continue
        has_build_script = any(
            p.parent == root or root in p.parents
            for name in ("build.gradle", "build.gradle.kts")
            for p in by_name.get(name, [])
        )
        if has_build_script:
            return root, "gradle", "Native Android / Multi-module Gradle"

    raise ValueError(
        "Category: PROJECT STRUCTURE. Supported Android project detect nahi hua. "
        "Native Android ke liye settings.gradle(.kts) aur build.gradle(.kts), "
        "Flutter ke liye pubspec.yaml + lib/main.dart, ya supported framework ka "
        "complete project chahiye. Generated folders (node_modules/build/.git) ko ZIP mein "
        "include karne ki zaroorat nahi."
    )
