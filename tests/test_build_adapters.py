import subprocess
import unittest

from build_adapters import SDK_PREPARE, build_command_for


class BuildAdapterTests(unittest.TestCase):
    def test_native_gradle_uses_project_wrapper_and_sdk_preflight(self):
        image, command = build_command_for("gradle", "android:test", "flutter:test")
        self.assertEqual(image, "android:test")
        self.assertIn("prepare_android_sdk", command)
        self.assertIn("run_gradle_debug", command)
        self.assertIn("Selected Android debug task", command)
        self.assertIn("gradle-wrapper.properties", command)
        self.assertIn("Gradle launcher missing", command)
        self.assertIn("wrapper launcher could not be restored", command)
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
                self.assertIn("install_js_dependencies", command)
                self.assertIn("pnpm-lock.yaml", command)
                self.assertIn("yarn.lock", command)

    def test_generated_shell_commands_parse(self):
        for engine in ("gradle", "flutter", "react-native", "capacitor", "expo", "cordova", "telegram"):
            with self.subTest(engine=engine):
                _, command = build_command_for(engine, "android:test", "flutter:test")
                result = subprocess.run(
                    ["bash", "-n"], input=command, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_gradle_runner_supports_flavored_debug_variants(self):
        self.assertIn("assemble[A-Za-z0-9]*Debug", SDK_PREPARE)
        self.assertIn("No assembleDebug or flavor-specific", SDK_PREPARE)

    def test_gradle_retries_only_transient_network_failures_once(self):
        self.assertIn("run_gradle_with_network_retry", SDK_PREPARE)
        self.assertIn("retrying Gradle once (attempt 2/2)", SDK_PREPARE)
        self.assertIn("Temporary failure in name resolution", SDK_PREPARE)
        self.assertIn("Received status code 5[0-9][0-9]", SDK_PREPARE)
        self.assertIn('"$attempt" -ge 2', SDK_PREPARE)

    def test_unknown_adapter_is_rejected(self):
        with self.assertRaises(ValueError):
            build_command_for("unknown", "android:test", "flutter:test")


if __name__ == "__main__":
    unittest.main()
