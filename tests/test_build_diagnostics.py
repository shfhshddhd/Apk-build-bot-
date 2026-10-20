import os
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "123")

import bot


class BuildFailureDiagnosisTests(unittest.TestCase):
    def test_docker_runner_failure_is_not_blamed_on_source(self):
        result = bot.diagnose_build_failure(
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock", 1
        )
        self.assertIn("BOT RUNNER / DOCKER ENGINE", result)
        self.assertNotIn("SOURCE CODE / COMPILATION", result)

    def test_image_pull_failure_is_classified_as_builder_registry(self):
        result = bot.diagnose_build_failure(
            "failed to resolve reference: pull access denied for image", 1
        )
        self.assertIn("BUILDER IMAGE / REGISTRY", result)

    def test_flutter_dependency_failure_is_not_misclassified_as_compile_error(self):
        result = bot.diagnose_build_failure(
            "Because every version of sample depends on missing_pkg, version solving failed.", 1
        )
        self.assertIn("FLUTTER DEPENDENCIES", result)

    def test_manifest_errors_are_not_swallowed_by_generic_compiler_marker(self):
        result = bot.diagnose_build_failure(
            "error: resource style/Theme.App not found\nResource linking failed", 1
        )
        self.assertIn("ANDROID RESOURCES / MANIFEST", result)

    def test_compilation_errors_still_show_source_category(self):
        result = bot.diagnose_build_failure(
            "e: app/src/main/kotlin/Main.kt: unresolved reference: missingFunction", 1
        )
        self.assertIn("SOURCE CODE / COMPILATION", result)

    def test_unknown_failure_is_reported_as_uncertain(self):
        result = bot.diagnose_build_failure("an unexpected failure occurred", 17)
        self.assertIn("ROOT CAUSE NEEDS LOG REVIEW", result)
        self.assertIn("exit code 17", result)


if __name__ == "__main__":
    unittest.main()
