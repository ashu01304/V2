import json
import os
import time

import jwt
import requests
from dotenv import load_dotenv

from .config import PROJECT_ROOT, REFRESH_URL, TOKEN_FILE, TOKEN_URL


load_dotenv(PROJECT_ROOT / ".env")


def load_tokens():
    if not TOKEN_FILE.exists():
        return None
    with TOKEN_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_tokens(tokens):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TOKEN_FILE.open("w", encoding="utf-8") as file:
        json.dump(tokens, file, indent=2)


def is_token_expired(token):
    try:
        expiry = jwt.decode(token, options={"verify_signature": False})["exp"]
        return time.time() >= expiry - 60
    except Exception:
        return True


def login():
    username = os.getenv("QH_USERNAME")
    password = os.getenv("QH_PASSWORD")
    if not username or not password:
        raise RuntimeError("QH_USERNAME and QH_PASSWORD must be set in .env")

    response = requests.post(
        TOKEN_URL,
        json={"username": username, "password": password},
        timeout=30,
    )
    response.raise_for_status()
    tokens = response.json()
    save_tokens(tokens)
    return tokens["access"]


def refresh_token(refresh):
    response = requests.post(REFRESH_URL, json={"refresh": refresh}, timeout=30)
    if response.status_code != 200:
        return None

    refreshed = response.json()
    tokens = load_tokens() or {}
    tokens["access"] = refreshed["access"]
    if "refresh" in refreshed:
        tokens["refresh"] = refreshed["refresh"]
    save_tokens(tokens)
    return tokens["access"]


def get_access_token():
    tokens = load_tokens()
    if not tokens:
        return login()

    access = tokens.get("access")
    if access and not is_token_expired(access):
        return access

    refresh = tokens.get("refresh")
    return (refresh_token(refresh) if refresh else None) or login()
