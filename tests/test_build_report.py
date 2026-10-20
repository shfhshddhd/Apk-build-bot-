import unittest

from build_report import redact_sensitive_text


class BuildLogRedactionTests(unittest.TestCase):
    def test_redacts_telegram_bot_token(self):
        token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        self.assertNotIn(token, redact_sensitive_text("token=" + token))

    def test_redacts_github_token(self):
        token = "ghp_" + "a" * 36
        self.assertNotIn(token, redact_sensitive_text("auth " + token))

    def test_redacts_authorization_and_secret_assignments(self):
        result = redact_sensitive_text("Authorization: Bearer abcdef123 secret=my-secret-value")
        self.assertNotIn("abcdef123", result)
        self.assertNotIn("my-secret-value", result)
        self.assertIn("[REDACTED]", result)

    def test_redacts_url_query_values_but_keeps_keys(self):
        result = redact_sensitive_text("https://example.com/download?token=abc&expires=123")
        self.assertIn("https://example.com/download?token=[REDACTED]&expires=[REDACTED]", result)
        self.assertNotIn("token=abc", result)


if __name__ == "__main__":
    unittest.main()
