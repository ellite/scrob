import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from pydantic import ValidationError

from schemas import UserSettings


class UiLanguageValidationTests(unittest.TestCase):
    """ui_language only has to look like a language tag: the frontend decides
    which languages it ships, so adding one needs no backend change."""

    def test_accepts_language_tags(self) -> None:
        for code in ("en", "fr", "de", "pt-BR"):
            self.assertEqual(UserSettings(ui_language=code).ui_language, code)

    def test_accepts_null_for_browser_default(self) -> None:
        self.assertIsNone(UserSettings(ui_language=None).ui_language)
        self.assertNotIn("ui_language", UserSettings().model_dump(exclude_unset=True))

    def test_rejects_malformed_tags(self) -> None:
        for code in ("", "f", "FR", "fr_FR", "fr;q=1", "<script>", "zh-Hant-TWN"):
            with self.assertRaises(ValidationError):
                UserSettings(ui_language=code)


if __name__ == "__main__":
    unittest.main()
