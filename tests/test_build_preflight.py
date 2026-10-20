import tempfile
import unittest
from pathlib import Path

from build_preflight import format_findings, inspect_project


class BuildPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text=""):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_gradle_wrapper_jar_is_advisory_not_fatal(self):
        self.write("settings.gradle", "rootProject.name='demo'")
        self.write("build.gradle", "plugins {}")
        self.write("gradle/wrapper/gradle-wrapper.properties", "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.8-bin.zip")
        codes = {x["code"] for x in inspect_project(self.root, "gradle")}
        self.assertIn("WRAPPER_JAR_MISSING", codes)

    def test_invalid_package_json_is_reported(self):
        self.write("package.json", "{broken")
        result = inspect_project(self.root, "expo")
        self.assertIn("PACKAGE_JSON_INVALID", {x["code"] for x in result})
        self.assertIn("[ERROR]", format_findings(result))

    def test_clean_flutter_structure_has_success_marker(self):
        self.write("pubspec.yaml", "name: demo")
        self.write("lib/main.dart", "void main() {}")
        self.write("android/app/src/main/AndroidManifest.xml", "<manifest/>")
        result = inspect_project(self.root, "flutter")
        self.assertIn("PREFLIGHT_PASSED", {x["code"] for x in result})

    def test_missing_react_native_android_is_error(self):
        self.write("package.json", '{"dependencies":{"react-native":"1"}}')
        result = inspect_project(self.root, "react-native")
        self.assertIn("ANDROID_PROJECT_MISSING", {x["code"] for x in result})


if __name__ == "__main__":
    unittest.main()
