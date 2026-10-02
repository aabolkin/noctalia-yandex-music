# Yandex Music — плагин Noctalia

Плеер Яндекс Музыки для [Noctalia](https://github.com/noctalia-dev/noctalia-shell)
(plugin_api 24). Этот ПК и есть плеер: треки берутся из API Яндекс Музыки,
звук играет mpv, которым владеет демон плагина.

- **Карточка на рабочем столе** — обложка, трек и исполнитель, перемотка по
  клику на полосе, кнопки назад / пауза / вперёд и лайк.
- **Поиск** — подсказки на лету, навигация стрелками, исполнители, альбомы,
  плейлисты и треки. Лайки ищутся локально и находятся мгновенно, даже когда
  Яндекс отвечает плохо.
- **Коллекции** — «Мне нравится», альбом, плейлист или исполнитель
  открываются списком по порядку с фильтром внутри; можно включить всё
  подряд или начать с выбранного трека.
- **Моя волна** — очередь из рекомендаций с фидбэком станции, либо радио по
  конкретному треку или исполнителю.
- **MPRIS** — трек виден и управляется в системной панели «Медиа».

## Требования

`python3`, `mpv`, Noctalia v5.

## Установка

```bash
git clone https://github.com/aabolkin/noctalia-yandex-music.git \
  ~/.local/share/noctalia-plugins/yandex-music
~/.local/share/noctalia-plugins/yandex-music/install.sh
```

Скрипт поднимает `.venv` рядом с плагином — системный Python не трогается —
и ставит `yandex-music` и `dbus-next` из `requirements.txt`.

Дальше разовая авторизация: скрипт напечатает ссылку и код, дождётся входа и
положит токен в `~/.config/noctalia/yandex-music-token` с правами 600.

```bash
~/.local/share/noctalia-plugins/yandex-music/.venv/bin/python \
  ~/.local/share/noctalia-plugins/yandex-music/scripts/ym_auth.py
```

Остаётся зарегистрировать каталог как источник плагинов и включить плагин:

```bash
noctalia msg plugins source add local path ~/.local/share/noctalia-plugins
noctalia msg plugins enable alex/yandex-music
```

Карточку на рабочий стол добавляют в редакторе виджетов
(`noctalia msg desktop-widgets-edit`), подходящий размер — 480×144.

## Настройки

`noctalia msg settings-open-plugin alex/yandex-music` — источник по умолчанию
(волна или лайки), стартовая громкость, обложка, полоса прогресса и кнопка
лайка. Перевыпустить токен — тот же `ym_auth.py` с `--force`.

## Лицензия

MIT, см. [LICENSE](LICENSE).
