import unittest

from build_adapters import SDK_PREPARE, build_command_for


class BuildAdapterTests(unittest.TestCase):
    def test_native_gradle_uses_project_wrapper_and_sdk_preflight(self):
        image, command = build_command_for("gradle", "android:test", "flutter:test")
        self.assertEqual(image, "android:test")
        self.assertIn("prepare_android_sdk", command)
        self.assertIn("run_gradle_debug", command)
        self.assertIn("gradle-wrapper.properties", command)
        self.assertIn("gradle-([0-9]", command)

    def test_missing_wrapper_uses_declared_distribution_version(self):
        self.assertIn("distributionUrl", SDK_PREPARE)
        self.assertIn("gradle-([0-9]", SDK_PREPARE)
        self.assertNotIn("v8.14.3", SDK_PREPARE)

    def test_sdk_preflight_reads_compile_sdk_and_native_toolchain(self):
        self.assertIn("compileSdk", SDK_PREPARE)
        self.assertIn("buildToolsVersion", SDK_PREPARE)
        self.assertIn("ndkVersion", SDK_PREPARE)
        self.assertIn("CMakeLists.txt", SDK_PREPARE)

    def test_flutter_uses_flutter_image(self):
        image, command = build_command_for("flutter", "android:test", "flutter:test")
        self.assertEqual(image, "flutter:test")
        self.assertIn("flutter pub get", command)
        self.assertIn("flutter build apk --debug", command)

    def test_javascript_adapters_prepare_android_sdk(self):
        for engine in ("react-native", "capacitor", "expo", "cordova"):
            with self.subTest(engine=engine):
                image, command = build_command_for(engine, "android:test", "flutter:test")
                self.assertEqual(image, "android:test")
                self.assertIn("prepare_android_sdk", command)
                self.assertIn("nodejs", command)

    def test_unknown_adapter_is_rejected(self):
        with self.assertRaises(ValueError):
            build_command_for("unknown", "android:test", "flutter:test")


if __name__ == "__main__":
    unittest.main()
