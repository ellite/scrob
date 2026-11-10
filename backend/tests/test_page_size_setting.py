import unittest

from pydantic import ValidationError
from schemas import UserSettings


class PageSizeSettingTests(unittest.TestCase):
    def test_sizes_and_reset_survive_settings_update(self):
        for size in (None, 20, 30, 40, 50, 100):
            with self.subTest(size=size):
                self.assertEqual(UserSettings(page_size=size).model_dump(exclude_unset=True), {"page_size": size})

    def test_invalid_sizes_are_rejected(self):
        for size in (0, 101, -1, 25):
            with self.subTest(size=size), self.assertRaises(ValidationError):
                UserSettings(page_size=size)
