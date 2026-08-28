"""Helpers for serving static files."""

import re

# Vite asset names look like `index-D2Ka7Pkg.js` or `geist-latin-wght-normal-BgDaEnEv.woff2`.
_VITE_HASHED = re.compile(r"^.+-[A-Za-z0-9_-]{8}\.[A-Za-z0-9.]+$")


def vite_immutable_file(path: str, url: str) -> bool:
    """WHITENOISE_IMMUTABLE_FILE_TEST: hashed Vite assets can be cached forever."""
    return _VITE_HASHED.match(url.rsplit("/", 1)[-1]) is not None
