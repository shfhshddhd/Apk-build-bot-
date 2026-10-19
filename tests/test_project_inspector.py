import json
import tempfile
import unittest
from pathlib import Path

from project_inspector import detect_project


class ProjectInspectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, relative, content=""):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_native_gradle_custom_module(self):
        self.write("settings.gradle.kts", 'rootProject.name = "demo"')
        self.write("build.gradle.kts", "plugins {}")
        self.write("mobile/build.gradle.kts", "plugins {}")
        root, engine, name = detect_project(self.root)
        self.assertEqual(root, self.root)
        self.assertEqual(engine, "gradle")

    def test_flutter_detected(self):
        self.write("pubspec.yaml", "name: demo")
        self.write("lib/main.dart", "void main() {}")
        self.assertEqual(detect_project(self.root)[1], "flutter")

    def test_expo_precedes_generated_android_gradle(self):
        self.write("package.json", json.dumps({
            "name": "demo",
            "dependencies": {"expo": "^52", "react-native": "^0.76"}
        }))
        self.write("android/settings.gradle", "include ':app'")
        self.write("android/app/build.gradle", "plugins {}")
        self.assertEqual(detect_project(self.root)[1], "expo")

    def test_react_native_precedes_android_gradle(self):
        self.write("package.json", json.dumps({
            "name": "demo", "dependencies": {"react-native": "^0.76"}
        }))
        self.write("index.js", " ")
        self.write("android/settings.gradle", "include ':app'")
        self.write("android/app/build.gradle", "plugins {}")
        self.assertEqual(detect_project(self.root)[1], "react-native")

    def test_capacitor_detected_without_generated_android_folder(self):
        self.write("package.json", json.dumps({
            "name": "demo", "dependencies": {"@capacitor/android": "^6"}
        }))
        self.write("capacitor.config.ts", "export default {}")
        self.assertEqual(detect_project(self.root)[1], "capacitor")

    def test_generated_folders_are_not_scanned(self):
        self.write("node_modules/fake/package.json", json.dumps({
            "dependencies": {"expo": "^52"}
        }))
        self.write("notes.txt", "not an app")
        with self.assertRaises(ValueError):
            detect_project(self.root)


if __name__ == "__main__":
    unittest.main()
