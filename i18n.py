"""I18n localization support."""

import json
from functools import lru_cache
from pathlib import Path

from fastapi import Request

LOCALES_DIR = Path(__file__).parent / "locales"
SUPPORTED_LANGUAGES = {"en", "ru"}
DEFAULT_LANGUAGE = "en"


@lru_cache(maxsize=len(SUPPORTED_LANGUAGES))
def _load_locale(lang: str) -> dict:
    """Load locale file from disk."""
    locale_path = LOCALES_DIR / f"{lang}.json"
    try:
        return json.loads(locale_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def get_locale(lang: str) -> dict:
    """Get translations for a language."""
    if lang not in SUPPORTED_LANGUAGES:
        lang = DEFAULT_LANGUAGE
    return _load_locale(lang)


def detect_language(request: Request) -> str:
    """Detect language from Accept-Language header or cookie."""
    # Check cookie first
    cookie_lang = request.cookies.get("lang")
    if cookie_lang in SUPPORTED_LANGUAGES:
        return cookie_lang

    # Parse Accept-Language header
    accept_lang = request.headers.get("accept-language", "")
    if accept_lang:
        # Parse header like "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
        for part in accept_lang.split(","):
            lang = part.split(";")[0].split("-")[0].strip()
            if lang in SUPPORTED_LANGUAGES:
                return lang

    return DEFAULT_LANGUAGE
