"""Browser-assisted authentication; never reads Microsoft passwords."""

import getpass
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
import webbrowser

AUTH_URL = "https://qh-api.corp.hertshtengroup.com/apis/auth/"
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
PROFILE_PATH = ENV_PATH.parent / "data" / "qh_api" / "edge_profile"


def load_token(token=None, env_path=ENV_PATH):
    value = token or os.environ.get("QH_ACCESS_TOKEN")
    if not value and Path(env_path).exists():
        for line in Path(env_path).read_text(encoding="utf-8-sig").splitlines():
            key, separator, candidate = line.strip().partition("=")
            if separator and key.strip() == "QH_ACCESS_TOKEN":
                value = candidate.strip().strip("\"'")
                break
    value = (value or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value or any(c.isspace() for c in value):
        raise ValueError("Supply QH_ACCESS_TOKEN, or run python -m API.probe --login")
    return value


def browser_token():
    print("Opening " + AUTH_URL)
    print("Sign in with Microsoft, then copy the access_token from the returned page.")
    webbrowser.open(AUTH_URL)
    return load_token(getpass.getpass("Paste access_token (hidden; not saved): "))


def save_token(token, env_path=ENV_PATH):
    """Atomically update only QH_ACCESS_TOKEN, keeping other .env entries."""
    token = load_token(token)
    path = Path(env_path)
    original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    lines = original.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in original else "\n"
    updated = []
    found = False
    for line in lines:
        key, separator, _ = line.strip().partition("=")
        if separator and key.strip() == "QH_ACCESS_TOKEN":
            if not found:
                updated.append("QH_ACCESS_TOKEN=" + token + newline)
                found = True
        else:
            updated.append(line)
    if not found:
        if updated and not updated[-1].endswith(("\n", "\r")):
            updated.append(newline)
        updated.append("QH_ACCESS_TOKEN=" + token + newline)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write("".join(updated))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    # Keep subsequent clients in this process consistent with the saved token.
    os.environ["QH_ACCESS_TOKEN"] = token


def automatic_browser_token(*, timeout_seconds=300):
    """Read QH's JSON auth response in a dedicated visible Edge profile."""
    try:
        from playwright.sync_api import Error, TimeoutError as BrowserTimeout, sync_playwright
    except ImportError:
        raise ValueError("Install automatic login support: .\\.venv\\Scripts\\python.exe -m pip install -r API/requirements.txt") from None

    origin = urlsplit(AUTH_URL)

    def is_auth_json(response):
        address = urlsplit(response.url)
        return (address.scheme == origin.scheme and address.netloc == origin.netloc
                and address.path.rstrip("/") in ("/apis/auth", "/apis/auth/callback")
                and response.status == 200
                and "json" in response.headers.get("content-type", "").lower())

    print("Opening Edge. Complete Microsoft sign-in/MFA if prompted.", flush=True)
    print("Waiting for the QH token; it will be saved to .env without printing it.", flush=True)
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(PROFILE_PATH), channel="msedge", headless=False)
            try:
                page = context.new_page()
                with context.expect_event("response", predicate=is_auth_json,
                                          timeout=timeout_seconds * 1000) as completed:
                    page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60000)
                payload = completed.value.json()
                value = (payload.get("access") or payload.get("access_token")) if isinstance(payload, dict) else None
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("QH auth returned JSON without an access token; use --login for manual entry")
                token = load_token(value)
            finally:
                context.close()
    except BrowserTimeout:
        raise ValueError("Login timed out. Run again and complete sign-in within five minutes, or use --login") from None
    except Error:
        # Browser exceptions can include callback URLs with authorization codes.
        raise ValueError("Automatic login failed. Ensure Edge is installed, close any previous API login window, or use --login") from None
    except (ValueError, TypeError) as error:
        if isinstance(error, ValueError) and str(error).startswith("QH auth returned"):
            raise
        raise ValueError("Could not read the QH authentication response; use --login") from None
    try:
        save_token(token)
    except OSError:
        raise ValueError("Token received, but .env could not be updated. Check that the file is writable and try again") from None
    print("Saved QH_ACCESS_TOKEN to .env.")
    return token


