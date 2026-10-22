import os

# Set before any test module is imported (their own setdefault calls then no-op),
# so signing session tokens uses a key of a realistic length instead of
# triggering PyJWT's short-HMAC-key warning.
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-at-least-32-bytes-long")
