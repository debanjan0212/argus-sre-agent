"""The one place Argus opens a URL, so the scheme check cannot be forgotten.

Backend and model URLs come from configuration. urllib would happily open file:// or ftp://
if one of them were set wrong, or set by someone hostile, so only http and https are allowed.
"""

from __future__ import annotations

import urllib.parse
import urllib.request

ALLOWED_SCHEMES = ("http", "https")


def open_http(req: urllib.request.Request, timeout: float):
    scheme = urllib.parse.urlsplit(req.full_url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"refusing to open a {scheme or 'schemeless'} URL: only http and https are allowed")
    # Bandit cannot see the check above, so its "audit url open for permitted schemes" finding is answered here.
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310
