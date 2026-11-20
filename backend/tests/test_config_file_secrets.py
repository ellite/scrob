"""#500: NAME_FILE variables (Docker/Compose secrets) populate settings."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.config import Settings, load_file_secrets

FIELDS = list(Settings.model_fields)


class FileSecretsTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def _secret(self, content: str, name: str = "secret") -> str:
        path = Path(self._dir.name) / name
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_file_value_is_loaded_and_trailing_newline_stripped(self):
        env = {"SECRET_KEY_FILE": self._secret("s3cret-value\n")}
        self.assertEqual(load_file_secrets(FIELDS, env), ["secret_key"])
        self.assertEqual(env["SECRET_KEY"], "s3cret-value")

    def test_inner_whitespace_and_leading_space_are_kept(self):
        env = {"POSTGRES_PASSWORD_FILE": self._secret(" pass word \r\n")}
        load_file_secrets(FIELDS, env)
        self.assertEqual(env["POSTGRES_PASSWORD"], " pass word ")

    def test_explicit_variable_wins_over_file(self):
        env = {"SECRET_KEY": "from-env", "SECRET_KEY_FILE": self._secret("from-file")}
        self.assertEqual(load_file_secrets(FIELDS, env), [])
        self.assertEqual(env["SECRET_KEY"], "from-env")

    def test_unreadable_file_stops_startup_with_a_clear_message(self):
        env = {"SECRET_KEY_FILE": "/nonexistent/run/secrets/secret_key"}
        with self.assertRaises(RuntimeError) as ctx:
            load_file_secrets(FIELDS, env)
        self.assertIn("SECRET_KEY_FILE", str(ctx.exception))
        self.assertNotIn("SECRET_KEY", env)

    def test_only_real_settings_are_considered(self):
        env = {"NOT_A_SETTING_FILE": self._secret("x"), "PATH_FILE": self._secret("y")}
        self.assertEqual(load_file_secrets(FIELDS, env), [])
        self.assertEqual(set(env), {"NOT_A_SETTING_FILE", "PATH_FILE"})

    def test_several_secrets_in_one_pass(self):
        env = {
            "SECRET_KEY_FILE": self._secret("k", "a"),
            "SMTP_PASSWORD_FILE": self._secret("m", "b"),
            "OIDC_CLIENT_SECRET_FILE": self._secret("o", "c"),
        }
        self.assertEqual(set(load_file_secrets(FIELDS, env)), {"secret_key", "smtp_password", "oidc_client_secret"})

    def test_loaded_secret_reaches_settings(self):
        env = {"SECRET_KEY_FILE": self._secret("file-key\n")}
        load_file_secrets(FIELDS, env)
        os_env_backup = dict(os.environ)
        try:
            os.environ.pop("SECRET_KEY", None)
            os.environ["SECRET_KEY"] = env["SECRET_KEY"]
            self.assertEqual(Settings().secret_key, "file-key")
        finally:
            os.environ.clear()
            os.environ.update(os_env_backup)
