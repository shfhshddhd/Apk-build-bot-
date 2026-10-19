"""Framework-specific, testable build command adapters."""
from __future__ import annotations

SDK_PREPARE = r"""
prepare_android_sdk() {
  echo '=== Android build environment ==='
  java -version 2>&1 || true
  df -h / /work /tmp || true
  free -h || true
  command -v sdkmanager >/dev/null 2>&1 || { echo 'sdkmanager unavailable in this image.'; return 2; }
  yes | sdkmanager --licenses >/dev/null 2>&1 || true
  while IFS= read -r package; do
    [ -n "$package" ] || continue
    echo "Ensuring Android SDK package: $package"
    sdkmanager "$package"
  done < <(python3 - <<'PY'
from pathlib import Path
import os, re
ignored = {".git", ".gradle", "build", "node_modules", ".dart_tool", "Pods", "dist"}
packages = set()
gradle_files = []
cmake_present = False
for base, dirs, files in os.walk("/work"):
    dirs[:] = [d for d in dirs if d not in ignored]
    if "CMakeLists.txt" in files:
        cmake_present = True
    for name in files:
        if name in {"build.gradle", "build.gradle.kts", "gradle.properties", "libs.versions.toml"}:
            path = Path(base) / name
            gradle_files.append(path)
            try: text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError: continue
            for pattern in (
                r"\bcompileSdk(?:Version)?\s*(?:=\s*)?(?:\(\s*)?['\"]?(\d{2,3})",
                r"\btargetSdk(?:Version)?\s*(?:=\s*)?['\"]?(\d{2,3})",
                r"\b(?:androidCompileSdk|compileSdk|compile_sdk)\s*=\s*['\"]?(\d{2,3})",
            ):
                for value in re.findall(pattern, text):
                    packages.add(f"platforms;android-{value}")
            for value in re.findall(r"\bbuildToolsVersion\s*(?:=\s*)?['\"]([^'\"]+)['\"]", text):
                if re.fullmatch(r"\d+(?:\.\d+){1,2}", value): packages.add(f"build-tools;{value}")
            for value in re.findall(r"\bndkVersion\s*(?:=\s*)?['\"]([^'\"]+)['\"]", text):
                if re.fullmatch(r"\d+(?:\.\d+){2,3}", value): packages.add(f"ndk;{value}")
            if "externalNativeBuild" in text:
                for value in re.findall(r"\bversion\s+['\"](\d+\.\d+(?:\.\d+)?)['\"]", text):
                    packages.add(f"cmake;{value}")
if cmake_present:
    all_gradle = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in gradle_files if p.exists())
    if "externalNativeBuild" in all_gradle:
        packages.add("cmake;3.22.1")
        if not re.search(r"\bndkVersion\s*(?:=\s*)?['\"]", all_gradle):
            packages.add("ndk;27.2.12479018")
for package in sorted(packages): print(package)
PY
)
}

restore_gradle_wrapper() {
  if [ -f ./gradlew ] && [ ! -s gradle/wrapper/gradle-wrapper.jar ]; then
    [ -s gradle/wrapper/gradle-wrapper.properties ] || {
      echo 'Gradle wrapper is incomplete: gradle-wrapper.properties is missing.'
      return 2
    }
    VERSION=$(python3 - <<'PY'
from pathlib import Path
import re
text = Path("gradle/wrapper/gradle-wrapper.properties").read_text(encoding="utf-8", errors="ignore")
match = re.search(r"gradle-([0-9][A-Za-z0-9.+-]*)-(?:bin|all)\.zip", text)
if not match: raise SystemExit("Could not determine Gradle version from distributionUrl.")
version = match.group(1)
# Release tags use a patch component (e.g. v8.7.0), but distributionUrl may say 8.7.
if re.fullmatch(r"\d+\.\d+", version):
    version += ".0"
print(version)
PY
) || return 2
    echo "Restoring wrapper JAR for declared Gradle $VERSION"
    mkdir -p gradle/wrapper
    rm -f gradle/wrapper/gradle-wrapper.jar
    if ! curl --fail --silent --show-error --location --max-time 45 \
      "https://raw.githubusercontent.com/gradle/gradle/v\${VERSION}/gradle/wrapper/gradle-wrapper.jar" \
      --output gradle/wrapper/gradle-wrapper.jar; then
      rm -f gradle/wrapper/gradle-wrapper.jar
      echo "Wrapper JAR download failed for Gradle tag v\${VERSION}."
      return 2
    fi
    test -s gradle/wrapper/gradle-wrapper.jar || { echo 'Wrapper JAR download returned an empty file.'; return 2; }
  fi
  [ ! -f ./gradlew ] || chmod +x ./gradlew || true
}

run_gradle_debug() {
  local gradle_cmd=()
  if [ -f ./gradlew ]; then
    restore_gradle_wrapper || return $?
    gradle_cmd=(bash ./gradlew)
  elif command -v gradle >/dev/null 2>&1; then
    gradle_cmd=(gradle)
  else
    echo 'No Gradle wrapper or system Gradle is available for this project.'
    return 2
  fi
  export GRADLE_OPTS="${GRADLE_OPTS:--Xmx3g -XX:MaxMetaspaceSize=768m -Dfile.encoding=UTF-8}"
  local common_args=(--no-daemon --max-workers=2 --stacktrace --console=plain)
  local task_file selected_task
  task_file=$(mktemp)
  if "${gradle_cmd[@]}" "${common_args[@]}" tasks --all >"$task_file" 2>&1; then
    if grep -Eq '(^|[[:space:]])assembleDebug([[:space:]]|$)' "$task_file"; then
      selected_task="assembleDebug"
    else
      selected_task=$(sed -nE 's/^[[:space:]]*(assemble[A-Za-z0-9]*Debug)[[:space:]]+-.*$/\1/p' "$task_file" | sort -u | head -n 1)
    fi
  else
    cat "$task_file"
    rm -f "$task_file"
    echo 'Gradle task discovery failed; inspect the output above.'
    return 2
  fi
  rm -f "$task_file"
  if [ -z "$selected_task" ]; then
    echo 'No assembleDebug or flavor-specific assemble*Debug task was found.'
    echo 'This project may not define a standard Android debug variant.'
    return 2
  fi
  echo "Selected Android debug task: $selected_task"
  "${gradle_cmd[@]}" "${common_args[@]}" "$selected_task"
}
"""


def build_command_for(engine: str, android_image: str, flutter_image: str) -> tuple[str, str]:
    """Return the selected container image and its framework-specific command."""
    common = "set -e; trap 'chmod -R a+rwX /work 2>/dev/null || true' EXIT; "
    sdk = SDK_PREPARE
    node_setup = (
        "if ! command -v npm >/dev/null 2>&1 || [ \"$(node -p 'Number(process.versions.node.split(\".\")[0])' 2>/dev/null || echo 0)\" -lt 18 ]; then "
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates && "
        "curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs; fi; "
        "install_js_dependencies() { "
        "if [ -f pnpm-lock.yaml ]; then corepack enable && pnpm install --frozen-lockfile; "
        "elif [ -f yarn.lock ]; then corepack enable && (yarn install --immutable || yarn install --frozen-lockfile); "
        "elif [ -f package-lock.json ]; then npm ci; else npm install; fi; }; "
    )
    if engine == "flutter":
        return flutter_image, common + sdk + "prepare_android_sdk; flutter --version; flutter pub get; flutter build apk --debug"
    if engine == "telegram":
        return android_image, (
            common + "echo '=== Telegram Android pinned toolchain ==='; java -version 2>&1 || true; "
            "df -h / /work /tmp; free -h || true; yes | sdkmanager --licenses >/dev/null 2>&1 || true; "
            "sdkmanager 'platforms;android-36' 'build-tools;36.0.0' 'ndk;27.2.12479018' 'cmake;3.22.1'; "
            "export GRADLE_OPTS='-Xmx2g -XX:MaxMetaspaceSize=512m -Dfile.encoding=UTF-8'; "
            "bash ./gradlew --no-daemon --max-workers=1 --no-parallel "
            "-Dorg.gradle.jvmargs='-Xmx2g -XX:MaxMetaspaceSize=512m -Dfile.encoding=UTF-8' "
            "-Dkotlin.compiler.execution.strategy=in-process assembleAfatDebug"
        )
    if engine == "gradle":
        return android_image, common + sdk + "prepare_android_sdk; run_gradle_debug"
    if engine == "react-native":
        node = node_setup
        setup = (
            "if [ ! -d node_modules ]; then install_js_dependencies; fi; "
            "mkdir -p android/app/src/main/assets; ENTRY=index.js; [ -f index.js ] || ENTRY=index.android.js; "
            "npx react-native bundle --platform android --dev false --entry-file \"$ENTRY\" "
            "--bundle-output android/app/src/main/assets/index.android.bundle "
            "--assets-dest android/app/src/main/res; cd android; prepare_android_sdk; run_gradle_debug"
        )
        return android_image, common + sdk + node + setup
    if engine == "capacitor":
        setup = (
            "if [ ! -d node_modules ]; then install_js_dependencies; fi; "
            "if node -e \"process.exit(require('./package.json').scripts?.build ? 0 : 1)\"; then npm run build; fi; "
            "npx cap sync android; cd android; prepare_android_sdk; run_gradle_debug"
        )
        return android_image, common + sdk + node_setup + setup
    if engine == "expo":
        setup = (
            "install_js_dependencies; "
            "npx expo prebuild --platform android --no-install; cd android; "
            "prepare_android_sdk; run_gradle_debug"
        )
        return android_image, common + sdk + node_setup + setup
    if engine == "cordova":
        setup = (
            "if [ -f package-lock.json ]; then npm ci; else npm install; fi; "
            "npx --yes cordova platform add android; cd platforms/android; "
            "prepare_android_sdk; restore_gradle_wrapper; cd ../..; npx --yes cordova build android"
        )
        return android_image, common + sdk + node_setup + setup
    raise ValueError(f"Unsupported build adapter: {engine}")
