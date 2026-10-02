#!/usr/bin/env bash
# Ставит окружение плагина рядом с ним же и показывает, что делать дальше.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 -m venv "$here/.venv"
"$here/.venv/bin/pip" install --upgrade pip >/dev/null
"$here/.venv/bin/pip" install -r "$here/requirements.txt"

cat <<MSG

Окружение готово. Осталось два шага:

  1) авторизация (один раз, откроется страница Яндекса):
     $here/.venv/bin/python $here/scripts/ym_auth.py

  2) включить плагин в Noctalia:
     noctalia msg plugins source add local path "$(dirname "$here")"
     noctalia msg plugins enable alex/yandex-music

Нужен mpv: он и воспроизводит звук.
MSG
