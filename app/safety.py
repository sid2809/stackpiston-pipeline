"""Never let a secret reach the logs, the sheet or an email.

scrub() replaces the values of the secret Railway Variables (and anything shaped like an API key or a
private key) with *** in any text the pipeline prints, saves to the sheet or emails.
"""
from __future__ import annotations

import os
import re

SECRET_VARS = ("WP_APP_PASSWORD", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON_B64",
               "SMTP_APP_PASSWORD")
PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\"private_key\"\s*:\s*\"[^\"]*\""),
]


def _secret_values() -> list[str]:
    out = []
    for name in SECRET_VARS:
        v = os.environ.get(name, "").strip()
        if len(v) >= 8:
            out += [v, v.replace(" ", "")]  # app passwords are often pasted with spaces
    return sorted(set(out), key=len, reverse=True)


def scrub(text) -> str:
    text = "" if text is None else str(text)
    for v in _secret_values():
        if v and v in text:
            text = text.replace(v, "***")
    for p in PATTERNS:
        text = p.sub("***", text)
    return text
