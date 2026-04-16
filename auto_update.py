"""
Auto-update: fetch latest JS bundles and extract signature secret + API config.
Run periodically or when requests start failing.

Usage:
    python3 auto_update.py          # update config with latest secret
    python3 auto_update.py --check  # just check, don't write
"""

import re
import json
import sys
import os

from curl_cffi import requests


BASE_URL = "https://agent.minimax.io"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def fetch_page():
    """Fetch the main page to get JS bundle URLs."""
    resp = requests.get(f"{BASE_URL}/", impersonate="chrome", timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Failed to fetch page: {resp.status_code}")
    return resp.text


def extract_bundle_urls(html: str) -> list:
    """Extract JS bundle URLs from HTML."""
    return re.findall(r'(https://cdn\.hailuo\.ai[^"\'>\s]+\.js)', html)


def find_signature_secret(bundle_urls: list) -> dict:
    """Download bundles and extract signature secret + other config."""
    result = {
        "secret": None,
        "bundle_version": None,
        "base_url": None,
    }

    # Extract version from bundle URL
    for url in bundle_urls[:1]:
        m = re.search(r'prod-web-va-([0-9.]+)', url)
        if m:
            result["bundle_version"] = m.group(1)

    # The secret is in the axios interceptor bundle (has 'x-signature' and 'x-timestamp')
    for url in bundle_urls:
        try:
            resp = requests.get(url, impersonate="chrome", timeout=10)
            text = resp.text

            if 'x-signature' not in text:
                continue

            # Extract secret: pattern is `${a}<SECRET>${d}` in the x-signature line
            # The format: f()(`${a}SECRET${d}`)
            m = re.search(r'`\$\{[a-z]\}([^`$]{10,50})\$\{[a-z]\}`', text)
            if m:
                result["secret"] = m.group(1)

            # Extract base URL
            m = re.search(r'baseURL\s*:\s*[^"]*"(https://agent\.minimax\.io)"', text)
            if m:
                result["base_url"] = m.group(1)

            if result["secret"]:
                print(f"  Found secret in: {url.split('/')[-1]}")
                break

        except Exception as e:
            continue

    return result


def update_config(secret: str, version: str):
    """Update config.json with new secret."""
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}

    old_secret = config.get("signature_secret")
    config["signature_secret"] = secret
    config["bundle_version"] = version

    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=4)

    if old_secret and old_secret != secret:
        print(f"  Secret CHANGED: {old_secret} -> {secret}")
    else:
        print(f"  Secret: {secret}")


def main():
    check_only = "--check" in sys.argv

    print("Fetching agent.minimax.io...")
    html = fetch_page()

    urls = extract_bundle_urls(html)
    print(f"Found {len(urls)} JS bundles")

    print("Scanning for signature secret...")
    info = find_signature_secret(urls)

    if not info["secret"]:
        print("ERROR: Could not find signature secret!")
        sys.exit(1)

    print(f"  Secret: {info['secret']}")
    print(f"  Version: {info['bundle_version']}")
    print(f"  Base URL: {info['base_url']}")

    if check_only:
        print("\n[Check only, not writing]")
    else:
        update_config(info["secret"], info["bundle_version"])
        print(f"\nConfig updated: {CONFIG_PATH}")


if __name__ == "__main__":
    main()
