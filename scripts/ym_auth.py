#!/usr/bin/env python3
"""Interactive OAuth device-flow login for the Yandex Music widget.

Run it once in a terminal: it prints a URL and a code, waits for you to confirm
the login in a browser, then stores the token in the file the widget reads.
The token never leaves this machine.
"""

import os
import pathlib
import subprocess
import sys

from yandex_music import Client

TOKEN_FILE = pathlib.Path(
    os.environ.get("YM_TOKEN_FILE", "~/.config/noctalia/yandex-music-token")
).expanduser()


def save(access_token):
    """Check the token against the account, then store it."""
    account = Client(access_token).init().me
    name = getattr(getattr(account, "account", None), "display_name", "") or "?"

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(access_token + "\n", encoding="utf-8")
    TOKEN_FILE.chmod(0o600)

    print(f"\nГотово. Аккаунт: {name}")
    print(f"Токен записан в {TOKEN_FILE} (права 600)")
    print("Виджет подхватит его в течение минуты.")


def main():
    args = sys.argv[1:]

    # Already have a token from somewhere else? Skip the device flow.
    if "--token" in args:
        index = args.index("--token")
        value = args[index + 1] if len(args) > index + 1 else ""
        if not value:
            print("Укажите токен: ym_auth.py --token <значение>")
            return 1
        save(value.strip())
        return 0

    if TOKEN_FILE.exists() and "--force" not in args:
        print(f"Токен уже есть: {TOKEN_FILE}")
        print("Перевыпустить: ym_auth.py --force")
        return 0

    def on_code(code):
        print()
        print(f"  Ссылка: {code.verification_url}")
        print(f"  Код:    {code.user_code}")
        if "--no-open" not in args:
            try:
                subprocess.Popen(
                    ["xdg-open", code.verification_url],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                print("  (страница открыта в браузере)")
            except Exception:  # noqa: BLE001 - opening a browser is a nicety
                pass
        print()
        print("Жду подтверждения входа… Ctrl+C — отмена.")

    token = Client().device_auth(on_code=on_code)
    save(token.access_token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
