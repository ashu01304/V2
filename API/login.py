"""Capture and save a QH token without querying market-data endpoints."""

from .auth import automatic_browser_token


def main():
    try:
        automatic_browser_token()
    except ValueError as error:
        print("ERROR:", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
