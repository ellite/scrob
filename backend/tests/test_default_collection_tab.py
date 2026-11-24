import unittest

from schemas import UserSettings
from models.users import UserSettings as SettingsModel


class DefaultCollectionTabTests(unittest.TestCase):
    def test_preference_is_included_in_settings_updates(self):
        for value in (True, False):
            with self.subTest(value=value):
                update = UserSettings(default_collection_tab=value)
                self.assertEqual(update.model_dump(exclude_unset=True), {"default_collection_tab": value})

    def test_model_defaults_to_explore(self):
        column = SettingsModel.__table__.c.default_collection_tab
        self.assertFalse(column.default.arg)
        self.assertFalse(column.nullable)
