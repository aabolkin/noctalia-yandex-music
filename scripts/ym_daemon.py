#!/usr/bin/env python3
"""Local Yandex Music player for the Noctalia widget.

This machine is the player: tracks come from the Yandex Music API (My Wave or
liked tracks) and are played by an mpv instance the daemon owns. Ynison remote
control is deliberately not used - the web player never registers as a device,
so transport commands had nothing to steer.

  state.json    what is playing (the widget reads it)
  command.json  what the widget wants done (the widget writes it)
  mpv.sock      mpv's JSON IPC socket
"""

import argparse
import collections
import fcntl
import json
import os
import pathlib
import random
import signal
import socket
import subprocess
import sys
import threading
import time

import requests
from yandex_music import Client

try:
    import ym_mpris
except Exception as _mpris_error:  # noqa: BLE001 - the player works without a bus
    ym_mpris = None
    print(f"[ym] MPRIS выключен: {_mpris_error}", file=sys.stderr, flush=True)

DEFAULT_DIR = pathlib.Path(
    os.environ.get("XDG_STATE_HOME", "~/.local/state")
).expanduser() / "noctalia/plugins/data/alex/yandex-music"

WAVE_STATION = "user:onyourwave"
POLL_SECONDS = 0.05   # how long a press can sit in command.json before we see it
LINK_TTL = 300        # seconds a stream link stays worth reusing
PREFETCH_INTERVAL = 5 # how often to check that the next track is ready
HINT_DEBOUNCE = 0.2   # seconds of quiet typing before asking for suggestions
SERVICE_GRACE = 20    # seconds without the service mark before we give up
SEARCH_TIMEOUT = 10   # the library default of 5 is short for this link
LIKES_FILE = "likes.json"      # titles of liked tracks, for searching offline
COLLECTION_FILE = "collection.json"   # the opened collection, read by the panel
HISTORY_FILE = "history.json"  # recent queries
HISTORY_KEEP = 8
STATE_INTERVAL = 1.0


LOG_FILE = None   # set once the data dir is known


def log(message):
    line = f"[ym] {message}"
    print(line, file=sys.stderr, flush=True)
    # The shell swallows the daemon's stderr, so without this there is no way
    # to find out why a background thread gave up.
    if LOG_FILE is not None:
        try:
            with LOG_FILE.open("a", encoding="utf-8") as out:
                out.write(f"{time.strftime('%F %T')} {line}\n")
        except OSError:
            pass


def artist_links(track):
    """Every credited artist, so each name on screen can open its own page."""
    return [{"id": str(a.id), "name": a.name}
            for a in (track.artists or []) if a.name and a.id]


_sessions = threading.local()


def http():
    """One keep-alive session per thread."""
    session = getattr(_sessions, "session", None)
    if session is None:
        session = _sessions.session = requests.Session()
    return session


def enable_connection_pooling():
    """Make the library reuse connections.

    It calls `requests.request` for every API call, which opens a fresh
    connection each time, and a TLS handshake to Yandex costs from half a
    second to two and a half here. Reusing one session per thread makes a
    warm call about five times faster, and every button press is a few of them.
    """
    requests.request = lambda method, url, **kwargs: http().request(
        method=method, url=url, **kwargs
    )


class Mpv:
    """Thin JSON-IPC wrapper around an mpv process we own."""

    def __init__(self, socket_path, volume, on_end):
        self.socket_path = str(socket_path)
        self.volume = volume
        self.on_end = on_end
        self.process = None
        self.sock = None
        self.reader = None
        self.lock = threading.Lock()
        self.request_id = 0
        self.replies = {}
        self.replies_lock = threading.Lock()

    def kill_orphans(self):
        """Terminate an mpv left behind by a previous daemon on our socket."""
        for entry in pathlib.Path("/proc").iterdir():
            if not entry.name.isdigit() or entry.name == str(os.getpid()):
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
            except OSError:
                continue
            if "mpv" in cmdline and self.socket_path in cmdline:
                try:
                    os.kill(int(entry.name), 15)
                    log(f"убит осиротевший mpv (pid {entry.name})")
                except OSError:
                    pass

    def start(self):
        self.kill_orphans()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.process = subprocess.Popen(
            [
                "mpv", "--idle=yes", "--no-video", "--really-quiet",
                f"--volume={self.volume}",
                f"--input-ipc-server={self.socket_path}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if os.path.exists(self.socket_path):
                break
            time.sleep(0.1)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)
        self.reader = threading.Thread(target=self._reader, name="mpv-reader", daemon=True)
        self.reader.start()
        log("mpv запущен")

    def alive(self):
        # Without the reader nobody drains mpv's replies: its socket fills up
        # and the next sendall blocks the main loop for good.
        return (
            self.process is not None and self.process.poll() is None
            and self.reader is not None and self.reader.is_alive()
        )

    def _reader(self):
        buffer = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "request_id" in message:
                    with self.replies_lock:
                        self.replies[message["request_id"]] = message
                elif message.get("event") == "end-file":
                    # "eof" means the track finished on its own, "error" that
                    # the stream broke; a manual skip reports "stop" and is
                    # already handled by the caller.
                    reason = message.get("reason")
                    if reason in ("eof", "error"):
                        self.on_end(reason)

    def command(self, *args, timeout=2.0):
        if self.sock is None:
            return None
        with self.lock:
            self.request_id += 1
            request_id = self.request_id
            payload = json.dumps({"command": list(args), "request_id": request_id}) + "\n"
            try:
                self.sock.sendall(payload.encode())
            except OSError as e:
                log(f"mpv недоступен: {e}")
                return None

        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.replies_lock:
                reply = self.replies.pop(request_id, None)
            if reply is not None:
                return reply.get("data")
            time.sleep(0.002)
        return None

    def play(self, url):
        self.command("loadfile", url, "replace")

    def set_pause(self, paused):
        self.command("set_property", "pause", bool(paused))

    def get(self, prop, default=None):
        value = self.command("get_property", prop)
        return default if value is None else value

    def set_volume(self, percent):
        self.volume = max(0, min(100, int(percent)))
        self.command("set_property", "volume", self.volume)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()


class Player:
    def __init__(self, token, data_dir, source, volume):
        self.dir = data_dir
        self.state_file = data_dir / "state.json"
        self.command_file = data_dir / "command.json"
        self.covers = data_dir / "covers"
        self.covers.mkdir(parents=True, exist_ok=True)
        self.cache = data_dir / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)

        self.client = Client(token).init()
        self.source = source
        # The handler goes to the network and waits on mpv's replies, which
        # only the reader thread delivers, so it must not run on that thread.
        self.mpv = Mpv(
            data_dir / "mpv.sock", volume,
            lambda reason: self.background(lambda: self.on_track_end(reason), "конец трека"),
        )

        self.queue = []          # upcoming tracks
        self.prefetched = {}     # track id -> ready stream link
        self.fill_lock = threading.Lock()   # queue top-ups run off the main loop
        self.history = []        # for "previous"
        self.links = {}          # track id -> (link, when) so "назад" is instant
        self.retried = None      # track whose link we already re-fetched once
        self.prefetching = False
        self.stopping = False
        self.started_ms = time.time() * 1000
        self.alive_file = data_dir / "service.alive"
        self.saw_service = False
        self.station = WAVE_STATION   # "моя волна" или радио по конкретному треку
        self.found = {"query": "", "status": "idle", "items": []}
        self.hints = {"query": "", "items": []}
        self.known = {}          # id -> Track из выдачи, чтобы не ходить за ним снова
        self.known_artists = {}
        self.known_albums = {}
        self.known_playlists = {}
        self.likes_index = []    # [{id,title,artist}] - поиск по лайкам без сети
        self.liked_order = []    # те же id в порядке Яндекса: свежие сверху
        self.queries = []        # недавние запросы
        # Открытая коллекция: сами треки лежат в collection.json (их бывают
        # сотни), в состоянии - только описание, иначе state.json распух бы.
        self.collection = {"kind": "", "id": "", "title": "",
                           "count": 0, "status": "idle", "rev": 0}
        self.collection_ids = []   # порядок треков в ней
        self.collection_seq = 0
        self.ordered = []        # что ещё осталось доиграть по порядку
        # Ответы приходят вразнобой, поэтому у каждого запроса свой номер: всё,
        # что не на последний, выбрасывается, иначе выдача отстаёт на слово.
        self.search_seq = 0
        self.hint_seq = 0
        self.hint_pending = None   # (часть запроса, когда набрали) - для дебаунса
        self.current = None
        self.batch_id = None
        self.started = False     # has playback ever been started
        self.liked = set()
        self.last_error = ""
        self.last_error_at = 0
        self.last_command_nonce = self.resume_nonce()
        self.lock = threading.Lock()

        # Buttons pressed in the system media panel arrive on the MPRIS thread;
        # they are executed by the main loop, like the widget's own commands.
        self.remote = collections.deque()
        self.mpris = ym_mpris.Mpris(self.remote_command) if ym_mpris else None
        if self.mpris:
            self.mpris.start()

        self.load_side_files()
        self.refresh_likes()
        # 836 лайков с названиями едут около двух секунд - не в главном потоке.
        self.background(self.refresh_likes_index, "названия лайков")

    def remote_command(self, action, value=None):
        self.remote.append({"action": action, "value": value})

    def resume_nonce(self):
        """Pick up where the previous daemon left off.

        Every daemon records the last command it ran, so a button pressed while
        this one was still starting is honoured, while one the predecessor had
        already executed is not. Without a state file the leftover command is
        treated as executed: it belongs to a run whose state is gone.
        """
        for path, key in ((self.state_file, "lastNonce"), (self.command_file, "nonce")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get(key):
                try:
                    return int(data[key])
                except (TypeError, ValueError):
                    continue
        return 0

    # ---------------------------------------------------------------- catalogue

    def refresh_likes(self, attempts=3):
        """The likes endpoint times out now and then; it is worth a retry."""
        for attempt in range(1, attempts + 1):
            try:
                likes = self.client.users_likes_tracks()
                # Yandex hands them back newest first, the way the site shows
                # them, so the order has to be kept: a set would lose it.
                self.liked_order = [str(item.id) for item in (likes.tracks if likes else [])]
                self.liked = set(self.liked_order)
                return
            except Exception as e:  # noqa: BLE001
                log(f"лайки не загрузились (попытка {attempt}/{attempts}): {e}")
                time.sleep(2)

    def load_side_files(self):
        """Likes and query history survive a restart, so the panel is useful
        before the first request to Yandex comes back."""
        for path, attr in ((self.dir / LIKES_FILE, "likes_index"),
                           (self.dir / HISTORY_FILE, "queries")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, list):
                setattr(self, attr, data)
        log(f"из кеша: лайков {len(self.likes_index)}, запросов {len(self.queries)}")

    def save_side_file(self, name, data):
        path = self.dir / name
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            log(f"{name} не сохранился: {e}")

    def refresh_likes_index(self):
        """Titles for the liked tracks, so they can be searched without network.

        The order is the one Yandex gave: newest first, as on the site. Batches
        come back in whatever order they like, so each one is mapped by id.
        """
        ids = list(self.liked_order)
        if not ids:
            return
        same = [item.get("id") for item in self.likes_index] == ids
        if same and all("artists" in item for item in self.likes_index):
            return                      # ничего не изменилось с прошлого раза
        index = []
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            by_id = {str(t.id): t for t in self.client.tracks(chunk)}
            for track_id in chunk:
                track = by_id.get(track_id)
                if track is None:
                    continue
                index.append({
                    "id": track_id,
                    "title": track.title or "",
                    "artist": ", ".join(a.name for a in (track.artists or []) if a.name),
                    "artistId": str(track.artists[0].id) if track.artists else "",
                    "artists": artist_links(track),
                    "durationMs": int(track.duration_ms or 0),
                })
        self.likes_index = index
        self.save_side_file(LIKES_FILE, index)
        log(f"названия лайков собраны: {len(index)}")

    def local_matches(self, query, limit=5):
        """Substring match over the liked tracks - no network, no waiting."""
        needle = query.casefold()
        found = []
        for item in self.likes_index:
            haystack = f"{item.get('title', '')} {item.get('artist', '')}".casefold()
            if needle in haystack:
                found.append(dict(item, liked=True))
                if len(found) >= limit:
                    break
        return found

    def remember_query(self, query):
        self.queries = [q for q in self.queries if q.casefold() != query.casefold()]
        self.queries.insert(0, query)
        del self.queries[HISTORY_KEEP:]
        self.save_side_file(HISTORY_FILE, self.queries)

    def forget_query(self, query):
        query = str(query or "")
        self.queries = [q for q in self.queries if q.casefold() != query.casefold()]
        self.save_side_file(HISTORY_FILE, self.queries)

    def fill_queue(self):
        """Top the queue up from the configured source."""
        if self.queue:
            return
        try:
            if self.source == "collection":
                # An opened collection plays in its own order, twenty at a time.
                ids, self.ordered = self.ordered[:20], self.ordered[20:]
                if not ids:
                    log("коллекция доиграла, возвращаюсь к волне")
                    self.source = "wave"
                    self.fill_queue()
                    return
                have = {str(t.id): t for t in self.client.tracks(ids)}
                for track_id in ids:
                    track = self.known.get(track_id) or have.get(track_id)
                    if track is not None:
                        self.queue.append(track)
            elif self.source == "likes":
                # The index already holds every liked id, so this needs no call
                # of its own - and still works while that endpoint times out.
                ids = [item["id"] for item in self.likes_index]
                if not ids:
                    likes = self.client.users_likes_tracks()
                    ids = [str(item.id) for item in (likes.tracks if likes else [])]
                random.shuffle(ids)
                for track in self.client.tracks(ids[:20]):
                    self.queue.append(track)
            else:
                last = self.current.id if self.current else None
                batch = self.client.rotor_station_tracks(
                    self.station, settings2=True, queue=last
                )
                self.batch_id = batch.batch_id
                if not self.started:
                    self.client.rotor_station_feedback_radio_started(
                        self.station, f"desktop-noctalia-{int(time.time())}"
                    )
                    self.started = True
                for item in batch.sequence:
                    if item.track:
                        self.queue.append(item.track)
        except Exception as e:  # noqa: BLE001
            self.fail(f"не удалось получить треки: {e}")

    def stream_url(self, track):
        # get_direct_links=True resolves a link for every bitrate variant, one
        # HTTP request each, and the low-bitrate one is often the slow one. We
        # play exactly one of them, so only that one is worth resolving.
        variants = self.client.tracks_download_info(track.track_id)
        best = max(variants, key=lambda i: i.bitrate_in_kbps or 0, default=None)
        return best.get_direct_link() if best else ""

    def cover_path(self, track):
        uri = getattr(track, "cover_uri", None)
        if not uri:
            return ""
        path = self.covers / f"{track.id}.jpg"
        if path.exists():
            return str(path)
        try:
            response = http().get("https://" + uri.replace("%%", "400x400"), timeout=10)
            response.raise_for_status()
            path.write_bytes(response.content)
            return str(path)
        except Exception as e:  # noqa: BLE001
            log(f"обложка не скачалась: {e}")
            return ""

    # -------------------------------------------------------------------- кеш

    def cache_path(self, track):
        return self.cache / f"{track.id}.mp3"

    def download(self, track, url):
        """Pull the track to disk before it is needed.

        mpv spends three to seven seconds opening a Yandex stream - that is the
        silence after a button press - while the whole file arrives in under two
        and opens instantly from disk.
        """
        path = self.cache_path(track)
        if path.exists() and path.stat().st_size > 0:
            return path
        tmp = path.with_suffix(".part")
        with http().get(url, timeout=30, stream=True) as response:
            response.raise_for_status()
            with tmp.open("wb") as out:
                for chunk in response.iter_content(65536):
                    out.write(chunk)
        tmp.replace(path)
        self.trim_cache()
        return path

    def trim_cache(self, keep=8):
        files = sorted(self.cache.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
        for path in files[:-keep]:
            try:
                path.unlink()
            except OSError:
                pass

    # ----------------------------------------------------------------- playback

    def play_track(self, track, prepare=False):
        # Everything the API has to say about this track is either already in
        # hand (prefetch) or can wait: only the stream link stands between the
        # press and the sound.
        key = str(track.id)
        cached = self.cache_path(track)
        if cached.exists() and cached.stat().st_size > 0:
            source = str(cached)
        else:
            url = self.prefetched.pop(key, "") or self.cached_link(key) or self.stream_url(track)
            if not url:
                self.fail("нет ссылки на поток")
                return
            self.links[key] = (url, time.time())
            for old in list(self.links)[:-20]:
                del self.links[old]
            source = url
            if prepare:
                try:
                    source = str(self.download(track, url))
                except Exception as e:  # noqa: BLE001
                    log(f"скачать не вышло, играю потоком: {e}")
        self.current = track
        self.mpv.play(source)
        self.mpv.set_pause(False)
        if self.source == "wave":
            batch, station = self.batch_id, self.station
            self.background(
                lambda: self.client.rotor_station_feedback_track_started(
                    station, track.id, batch),
                "фидбэк волны",
            )
        self.background(lambda: self.cover_path(track), "обложка")
        self.background(self.prefetch_next, "предзагрузка")

    def prefetch_next(self):
        """Resolve the upcoming track while the current one is still playing.

        This is what makes "вперёд" instant: by the time the button is pressed
        the track is already on disk, and only mpv has to move.
        """
        if self.prefetching:
            return
        self.prefetching = True
        try:
            with self.fill_lock:
                if not self.queue:
                    self.fill_queue()
                track = self.queue[0] if self.queue else None
            if track is None:
                return
            self.cover_path(track)
            path = self.cache_path(track)
            if path.exists() and path.stat().st_size > 0:
                return
            # The API times out now and then; giving up silently would leave
            # the next press waiting on the stream for several seconds.
            for attempt in (1, 2):
                try:
                    url = self.stream_url(track)
                except Exception as e:  # noqa: BLE001
                    log(f"предзагрузка, попытка {attempt}: {e}")
                    time.sleep(1)
                    continue
                if url:
                    self.prefetched.clear()   # one track ahead is enough
                    self.prefetched[str(track.id)] = url
                    self.download(track, url)
                    return
        finally:
            self.prefetching = False

    # ----------------------------------------------------------------- поиск

    def cover_path_for(self, key):
        path = self.covers / f"{key}.jpg"
        return str(path) if path.exists() and path.stat().st_size > 0 else ""

    def cover_of(self, key, uri, wanted, size="200x200"):
        """Path to a cover already on disk; a missing one is queued, never
        waited for - twenty covers ahead of the results would be four seconds
        of nothing on screen."""
        path = self.cover_path_for(key)
        if path:
            return path
        if uri:
            wanted.append((key, uri, size))
        return ""

    def fetch_cover(self, key, uri, size):
        if self.cover_path_for(key):
            return
        path = self.covers / f"{key}.jpg"
        try:
            response = http().get("https://" + uri.replace("%%", size), timeout=10)
            response.raise_for_status()
            tmp = path.with_suffix(".part")
            tmp.write_bytes(response.content)
            tmp.replace(path)
        except Exception as e:  # noqa: BLE001
            log(f"обложка {key}: {e}")

    def fill_covers(self, wanted, seq):
        """Download what the results are missing, then hand the paths over."""
        for key, uri, size in wanted:
            if seq != self.search_seq:
                return
            self.fetch_cover(key, uri, size)
        if seq != self.search_seq:
            return
        for block in ("items", "artists", "albums", "playlists"):
            for row in self.found.get(block, []):
                if not row.get("cover"):
                    row["cover"] = self.cover_path_for(row.get("coverKey", ""))
        with self.lock:
            self.write_state()

    def track_row(self, track, wanted):
        self.known[str(track.id)] = track
        key = str(track.id)
        return {
            "id": key,
            "title": track.title or "",
            "artist": ", ".join(a.name for a in (track.artists or []) if a.name),
            "artistId": str(track.artists[0].id) if track.artists else "",
            "artists": artist_links(track),
            "album": (track.albums[0].title if track.albums else "") or "",
            "durationMs": int(track.duration_ms or 0),
            "liked": key in self.liked,
            "coverKey": key,
            # 400x400 - тот же файл, что потом покажет карточка плеера.
            "cover": self.cover_of(key, getattr(track, "cover_uri", None),
                                   wanted, "400x400"),
        }

    def search(self, query):
        """Search the catalogue. Liked tracks are matched locally and shown at
        once; artists, albums, playlists and tracks follow from the API."""
        query = str(query or "").strip()
        if not query:
            self.found = {"query": "", "status": "idle", "items": []}
            return
        self.search_seq += 1
        seq = self.search_seq
        self.remember_query(query)
        self.found = {
            "query": query, "status": "loading", "best": "",
            "likes": self.local_matches(query),
            "items": [], "artists": [], "albums": [], "playlists": [],
        }

        def run():
            result = None
            # This API times out often enough that a single failure must not
            # look like "search is broken" - one retry covers nearly all of it.
            for attempt in (1, 2, 3):
                try:
                    # Пять секунд по умолчанию мало: одно рукопожатие к
                    # Яндексу отсюда стоит до двух с половиной.
                    result = self.client.search(query, type_="all", timeout=SEARCH_TIMEOUT)
                    break
                except Exception as e:  # noqa: BLE001
                    log(f"поиск {query!r}, попытка {attempt}: {e}")
                    if seq != self.search_seq:
                        return
                    if attempt == 3:
                        self.found = dict(self.found, status="error", message=str(e))
                        with self.lock:
                            self.write_state()
                        return
                    time.sleep(1)
            if seq != self.search_seq:
                return   # пока ходили, набрали дальше - эта выдача уже не нужна

            found = {
                "query": query, "status": "ok",
                "best": str(getattr(getattr(result, "best", None), "type", "") or ""),
                "likes": self.local_matches(query),
                "items": [], "artists": [], "albums": [], "playlists": [],
            }

            wanted = []
            tracks = result.tracks.results if result.tracks else []
            for track in tracks[:12]:
                if getattr(track, "available", True) is False:
                    continue
                found["items"].append(self.track_row(track, wanted))

            for artist in (result.artists.results if result.artists else [])[:3]:
                self.known_artists[str(artist.id)] = artist
                key = f"artist-{artist.id}"
                uri = getattr(getattr(artist, "cover", None), "uri", None)
                found["artists"].append({
                    "id": str(artist.id),
                    "title": artist.name or "",
                    "coverKey": key,
                    "cover": self.cover_of(key, uri, wanted),
                })

            for album in (result.albums.results if result.albums else [])[:3]:
                self.known_albums[str(album.id)] = album
                key = f"album-{album.id}"
                found["albums"].append({
                    "id": str(album.id),
                    "title": album.title or "",
                    "artist": ", ".join(a.name for a in (album.artists or []) if a.name),
                    "year": album.year or "",
                    "count": album.track_count or 0,
                    "coverKey": key,
                    "cover": self.cover_of(key, getattr(album, "cover_uri", None), wanted),
                })

            for pl in (result.playlists.results if result.playlists else [])[:2]:
                uid = str(getattr(getattr(pl, "owner", None), "uid", "") or "")
                ident = f"{uid}:{pl.kind}"
                key = f"playlist-{uid}-{pl.kind}"
                self.known_playlists[ident] = pl
                uri = getattr(getattr(pl, "cover", None), "uri", None)
                found["playlists"].append({
                    "id": ident,
                    "title": pl.title or "",
                    "artist": getattr(getattr(pl, "owner", None), "name", "") or "",
                    "count": getattr(pl, "track_count", 0) or 0,
                    "coverKey": key,
                    "cover": self.cover_of(key, uri, wanted),
                })

            for store in (self.known, self.known_artists, self.known_albums,
                          self.known_playlists):
                for old in list(store)[:-60]:
                    del store[old]

            self.found = found
            with self.lock:
                self.write_state()
            if wanted:
                self.background(lambda: self.fill_covers(wanted, seq), "обложки выдачи")

        self.background(run, "поиск")

    def suggest(self, part):
        """Hints while typing. The call itself takes about 0.1s, so the only
        thing worth adding is a debounce - fired from the main loop."""
        part = str(part or "").strip()
        if not part:
            self.hints = {"query": "", "items": []}
            self.hint_pending = None
            return
        self.hint_pending = (part, time.time())

    def run_pending_hint(self):
        pending = self.hint_pending
        if pending is None or time.time() - pending[1] < HINT_DEBOUNCE:
            return
        self.hint_pending = None
        part = pending[0]
        self.hint_seq += 1
        seq = self.hint_seq

        def run():
            try:
                result = self.client.search_suggest(part)
            except Exception as e:  # noqa: BLE001
                log(f"подсказки: {e}")
                return
            if seq != self.hint_seq:
                return
            items = [s for s in (result.suggestions or []) if s][:6] if result else []
            self.hints = {"query": part, "items": items}
            with self.lock:
                self.write_state()

        self.background(run, "подсказки")

    def fetch_track(self, track_id):
        track = self.known.get(track_id)
        if track is not None:
            return track
        tracks = self.client.tracks([track_id])
        return tracks[0] if tracks else None

    def play_by_id(self, track_id):
        track_id = str(track_id or "").strip()
        track = self.fetch_track(track_id) if track_id else None
        if track is None:
            self.fail("трек не найден")
            return
        if self.current:
            self.history.append(self.current)
            del self.history[:-20]
        # A track picked by hand is never prefetched, and downloading it is
        # still quicker than letting mpv open the stream.
        self.play_track(track, prepare=True)

    def play_all(self, tracks, label):
        """Start the first one and line the rest up in front of the queue."""
        tracks = [t for t in tracks if t is not None]
        if not tracks:
            self.fail(f"{label}: треков нет")
            return
        if self.current:
            self.history.append(self.current)
            del self.history[:-20]
        self.queue[:0] = tracks[1:]
        self.prefetched.clear()
        self.play_track(tracks[0], prepare=True)

    def play_artist(self, artist_id):
        """Top tracks of the artist, which is what clicking a name should do."""
        artist_id = str(artist_id or "").strip()
        if not artist_id:
            return
        result = self.client.artists_tracks(artist_id, page_size=20)
        self.play_all(list(result.tracks if result else []), "исполнитель")

    def artist_radio(self, artist_id):
        artist_id = str(artist_id or "").strip()
        if not artist_id:
            return
        self.station = f"artist:{artist_id}"
        self.started = False
        self.source = "wave"
        self.queue.clear()
        self.prefetched.clear()
        self.advance(remember=True)

    def play_album(self, album_id):
        album_id = str(album_id or "").strip()
        if not album_id:
            return
        album = self.client.albums_with_tracks(album_id)
        tracks = [t for volume in (album.volumes or []) for t in volume]
        self.play_all(tracks, "альбом")

    def play_playlist(self, ident):
        """`ident` is "<uid>:<kind>", the only way to name a playlist."""
        playlist = self.known_playlists.get(str(ident or ""))
        if playlist is None:
            uid, _, kind = str(ident or "").partition(":")
            if not kind:
                return
            playlist = self.client.users_playlists(kind, uid)
        short = playlist.fetch_tracks() if playlist else []
        self.play_all([item.track for item in short], "плейлист")

    def play_wave(self):
        """«Моя волна» right now, whatever was playing before it."""
        self.station = WAVE_STATION
        self.started = False
        self.source = "wave"
        self.queue.clear()
        self.prefetched.clear()
        self.advance(remember=True)

    def play_likes(self):
        """«Мне нравится» as a collection: the liked tracks, shuffled."""
        self.source = "likes"
        self.station = WAVE_STATION   # из радио возвращаемся к обычной очереди
        self.queue.clear()
        self.prefetched.clear()
        self.advance(remember=True)

    # ------------------------------------------------------- открытая коллекция

    def light_row(self, track):
        """A row for a collection: hundreds of them, so no cover is fetched -
        only the ones already on disk are shown."""
        key = str(track.id)
        self.known[key] = track
        return {
            "id": key,
            "title": track.title or "",
            "artist": ", ".join(a.name for a in (track.artists or []) if a.name),
            "artistId": str(track.artists[0].id) if track.artists else "",
            "artists": artist_links(track),
            "durationMs": int(track.duration_ms or 0),
            "liked": key in self.liked,
            "cover": self.cover_path_for(key),
        }

    def collection_items(self, kind, ident):
        if kind == "likes":
            items = [dict(item, liked=True, cover=self.cover_path_for(item["id"]))
                     for item in self.likes_index]
            return items, "likes"
        if kind == "album":
            album = self.client.albums_with_tracks(ident)
            tracks = [t for volume in (album.volumes or []) for t in volume]
            return [self.light_row(t) for t in tracks], (album.title or "")
        if kind == "playlist":
            playlist = self.known_playlists.get(ident)
            if playlist is None:
                uid, _, number = ident.partition(":")
                playlist = self.client.users_playlists(number, uid)
            short = playlist.fetch_tracks() if playlist else []
            tracks = [item.track for item in short if item.track]
            return [self.light_row(t) for t in tracks], (playlist.title if playlist else "")
        if kind == "artist":
            result = self.client.artists_tracks(ident, page_size=50)
            tracks = result.tracks if result else []
            artist = self.known_artists.get(ident)
            # Opened from a playing track rather than from search, the artist
            # was never looked up; its own tracks carry the name anyway.
            name = artist.name if artist else next(
                (a.name for t in tracks for a in (t.artists or [])
                 if str(a.id) == ident and a.name), "")
            return [self.light_row(t) for t in tracks], name
        return [], ""

    def artist_albums(self, ident, wanted):
        """The artist's own albums, newest first; without them the tracks still show."""
        try:
            result = self.client.artists_direct_albums(ident, page_size=50)
        except Exception as e:  # noqa: BLE001
            log(f"альбомы исполнителя {ident}: {e}")
            return []
        rows = []
        for album in (result.albums if result else []) or []:
            self.known_albums[str(album.id)] = album
            key = f"album-{album.id}"
            rows.append({
                "id": str(album.id),
                "title": album.title or "",
                "year": album.year or "",
                "count": album.track_count or 0,
                "coverKey": key,
                "cover": self.cover_of(key, getattr(album, "cover_uri", None), wanted),
            })
        return rows

    def fill_album_covers(self, wanted, seq):
        # Each cover lands in the state as soon as it is on disk; the main loop
        # writes the state every second anyway.
        for key, uri, size in wanted:
            if seq != self.collection_seq:
                return
            self.fetch_cover(key, uri, size)
            path = self.cover_path_for(key)
            if not path or seq != self.collection_seq:
                continue
            self.collection = dict(self.collection, albums=[
                dict(row, cover=path) if row["coverKey"] == key else row
                for row in self.collection.get("albums", [])
            ])

    def open_collection(self, kind, ident=""):
        """Hand the panel a collection to browse instead of starting it."""
        ident = str(ident or "")
        self.collection_seq += 1
        seq = self.collection_seq
        self.collection = dict(self.collection, kind=kind, id=ident, title="",
                               count=0, status="loading", albums=[],
                               rev=self.collection["rev"] + 1)

        def run():
            try:
                items, title = self.collection_items(kind, ident)
                wanted = []
                albums = self.artist_albums(ident, wanted) if kind == "artist" else []
            except Exception as e:  # noqa: BLE001
                log(f"коллекция {kind} {ident}: {e}")
                if seq == self.collection_seq:
                    self.collection = dict(self.collection, status="error",
                                           message=str(e),
                                           rev=self.collection["rev"] + 1)
                    with self.lock:
                        self.write_state()
                return
            if seq != self.collection_seq:
                return
            self.save_side_file(COLLECTION_FILE, items)
            self.collection_ids = [item["id"] for item in items]
            self.collection = {"kind": kind, "id": ident, "title": title,
                               "count": len(items), "status": "ok",
                               "albums": albums,
                               "rev": self.collection["rev"] + 1}
            with self.lock:
                self.write_state()
            if wanted:
                self.fill_album_covers(wanted, seq)

        self.background(run, "коллекция")

    def collection_play(self, track_id=""):
        """Play the collection in order, starting at the chosen track."""
        if not self.collection_ids:
            return
        track_id = str(track_id or "")
        try:
            start = self.collection_ids.index(track_id)
        except ValueError:
            start = 0
        self.source = "collection"
        self.station = WAVE_STATION
        self.ordered = list(self.collection_ids[start:])
        self.queue.clear()
        self.prefetched.clear()
        self.advance(remember=True)

    def queue_by_id(self, track_id):
        """Play this one after the current track instead of interrupting it."""
        track = self.fetch_track(str(track_id or "").strip())
        if track is None:
            self.fail("трек не найден")
            return
        self.queue.insert(0, track)
        self.prefetched.clear()
        self.background(self.prefetch_next, "предзагрузка")

    def radio_by_id(self, track_id):
        """Switch the station to one built around this track and start it."""
        track_id = str(track_id or "").strip()
        if not track_id:
            return
        self.station = f"track:{track_id}"
        self.started = False       # новой станции нужен свой radio_started
        self.source = "wave"
        self.queue.clear()
        self.prefetched.clear()
        self.play_by_id(track_id)

    def like_by_id(self, track_id):
        """The heart flips at once and the request follows; a failed one is
        taken back, which is rarer than the wait would be annoying."""
        track_id = str(track_id or "").strip()
        if not track_id:
            return
        adding = track_id not in self.liked
        self.mark_liked(track_id, adding)

        def call():
            try:
                if adding:
                    self.client.users_likes_tracks_add(track_id)
                else:
                    self.client.users_likes_tracks_remove(track_id)
            except Exception as e:  # noqa: BLE001
                self.mark_liked(track_id, not adding)
                self.fail(f"лайк не прошёл: {e}")

        self.background(call, "лайк")

    def index_row(self, track_id):
        track = self.known.get(track_id)
        if track is None and self.current is not None and str(self.current.id) == track_id:
            track = self.current
        if track is None:
            return None
        return {
            "id": track_id,
            "title": track.title or "",
            "artist": ", ".join(a.name for a in (track.artists or []) if a.name),
            "artistId": str(track.artists[0].id) if track.artists else "",
            "artists": artist_links(track),
            "durationMs": int(track.duration_ms or 0),
        }

    def mark_liked(self, track_id, liked):
        """Keep the set, the order and the searchable index in step.

        A freshly liked track belongs at the top, where the site puts it, so
        «Мне нравится» shows it straight away without another round trip.
        """
        if liked:
            if track_id not in self.liked:
                self.liked.add(track_id)
                self.liked_order.insert(0, track_id)
                row = self.index_row(track_id)
                if row is not None:
                    self.likes_index.insert(0, row)
        else:
            self.liked.discard(track_id)
            self.liked_order = [i for i in self.liked_order if i != track_id]
            self.likes_index = [i for i in self.likes_index if i.get("id") != track_id]
        self.save_side_file(LIKES_FILE, self.likes_index)
        for item in self.found.get("items", []):
            if item.get("id") == track_id:
                item["liked"] = liked

    def cached_link(self, key):
        """A link we used minutes ago is still good; an old one is not worth it."""
        link, when = self.links.get(key, ("", 0))
        return link if time.time() - when < LINK_TTL else ""

    def background(self, fn, label):
        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - never take the loop down
                log(f"{label}: {e}")

        threading.Thread(target=run, daemon=True).start()

    def advance(self, remember=True):
        if self.current and remember:
            self.history.append(self.current)
            del self.history[:-20]
        with self.fill_lock:
            self.fill_queue()
            track = self.queue.pop(0) if self.queue else None
        if track is None:
            return
        self.play_track(track)

    def go_back(self):
        if not self.history:
            # No history yet: restart the current track, like every player does.
            self.mpv.command("seek", 0, "absolute")
            return
        track = self.history.pop()
        if self.current:
            self.queue.insert(0, self.current)
        self.play_track(track)

    def on_track_end(self, reason="eof"):
        with self.lock:
            track = self.current
            if reason == "error" and track is not None and self.retried != track.id:
                # An expired link or a half-written cache file: throw both away,
                # try once more, and only give up on the track if that fails too.
                self.retried = track.id
                self.links.pop(str(track.id), None)
                self.cache_path(track).unlink(missing_ok=True)
                log("поток оборвался, беру ссылку заново")
                self.play_track(track)
                return
            self.retried = None
            self.advance()

    def fail(self, message):
        log(message)
        self.last_error, self.last_error_at = message, time.time()

    # ------------------------------------------------------------------- state

    def check_service(self, now):
        """Stop when the service that owns us is gone.

        The plugin can be disabled, reloaded or the whole shell restarted
        without our exit callback ever running, and a daemon nobody owns keeps
        playing music and holds the lock against its own replacement.
        """
        try:
            age = now - self.alive_file.stat().st_mtime
        except OSError:
            return          # старый сервис отметок не ставит - не наше дело
        if age < SERVICE_GRACE:
            self.saw_service = True
        elif self.saw_service:
            log(f"сервис молчит {int(age)} с, останавливаюсь")
            self.stopping = True

    def build_state(self):
        track = self.current
        position = float(self.mpv.get("time-pos", 0) or 0)
        duration = float(self.mpv.get("duration", 0) or 0)
        paused = bool(self.mpv.get("pause", True))
        idle = bool(self.mpv.get("idle-active", True))

        state = {
            "ok": True,
            "active": track is not None,
            "playing": track is not None and not paused and not idle,
            "paused": paused or idle,
            "hasDevice": self.mpv.alive(),
            "source": self.source,
            "volume": self.mpv.volume / 100.0,
            "commandError": self.last_error if time.time() - self.last_error_at < 20 else "",
            "lastNonce": self.last_command_nonce,
            "search": self.found,
            "suggest": self.hints,
            "station": self.station,
            "history": self.queries,
            # Only a handful: the whole index would be rewritten every second.
            "likesPreview": self.likes_index[:6],
            "likesCount": len(self.likes_index) or len(self.liked),
            "collection": self.collection,
            "updatedAt": int(time.time()),
        }
        if track is not None:
            track_id = str(track.id)
            state.update({
                "trackId": track_id,
                "title": track.title or "",
                "artist": ", ".join(a.name for a in (track.artists or []) if a.name),
                "artistId": str(track.artists[0].id) if track.artists else "",
                "artists": artist_links(track),
                "album": (track.albums[0].title if track.albums else "") or "",
                "cover": str(self.covers / f"{track_id}.jpg")
                if (self.covers / f"{track_id}.jpg").exists() else "",
                "liked": track_id in self.liked,
                "progressMs": int(position * 1000),
                "durationMs": int((duration or (track.duration_ms or 0) / 1000) * 1000),
                "progress": (position / duration) if duration else 0.0,
            })
        return state

    def write_state(self):
        state = self.build_state()
        state["heartbeat"] = int(time.time())
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_file)
        if self.mpris:
            self.mpris.publish(state)
        return state

    # ---------------------------------------------------------------- commands

    def handle(self, command):
        action = command.get("action", "")
        if action in ("toggle", "playpause"):
            if self.current is None:
                self.advance(remember=False)
            else:
                self.mpv.set_pause(not bool(self.mpv.get("pause", True)))
        elif action == "pause":
            self.mpv.set_pause(True)
        elif action in ("resume", "play"):
            if self.current is None:
                self.advance(remember=False)
            else:
                self.mpv.set_pause(False)
        elif action == "next":
            if self.source == "wave" and self.current:
                skipped, at, batch = self.current.id, \
                    float(self.mpv.get("time-pos", 0) or 0), self.batch_id
                station = self.station
                self.background(
                    lambda: self.client.rotor_station_feedback_skip(
                        station, skipped, at, batch),
                    "фидбэк пропуска",
                )
            self.advance()
        elif action in ("prev", "previous"):
            self.go_back()
        elif action == "volume":
            self.mpv.set_volume(float(command.get("value", 0.7)) * 100)
        elif action == "seek":
            self.mpv.command("seek", float(command.get("value", 0)), "relative")
        elif action == "position":
            target = float(command.get("value", 0))
            # Seeking to where playback already is only interrupts it. Nobody
            # asks for that on purpose, so treat it as a stray command.
            if abs(target - float(self.mpv.get("time-pos", 0) or 0)) >= 1.0:
                self.mpv.command("seek", target, "absolute")
        elif action in ("like", "unlike", "like_toggle"):
            self.toggle_like(action)
        elif action == "source":
            self.source = "likes" if command.get("value") == "likes" else "wave"
            self.station = WAVE_STATION   # из радио по треку возвращаемся к волне
            self.queue.clear()
            self.prefetched.clear()
        elif action == "quit":
            # Disabling the plugin must stop the music too, and the lock would
            # otherwise keep the next daemon from ever taking over. A quit
            # written before we started belongs to our predecessor: obeying it
            # would kill every daemon the service brings up after it.
            if int(command.get("nonce") or 0) < self.started_ms:
                log("команда выхода от прошлого запуска, игнорирую")
            else:
                log("получена команда выхода")
                self.stopping = True
        elif action == "refresh":
            self.refresh_likes()
        elif action == "forget_query":
            self.forget_query(command.get("value"))
        elif action == "search":
            self.search(command.get("value"))
        elif action == "suggest":
            self.suggest(command.get("value"))
        elif action == "play_id":
            self.play_by_id(command.get("value"))
        elif action == "queue_id":
            self.queue_by_id(command.get("value"))
        elif action == "radio_id":
            self.radio_by_id(command.get("value"))
        elif action == "like_id":
            self.like_by_id(command.get("value"))
        elif action == "artist_play":
            self.play_artist(command.get("value"))
        elif action == "artist_radio":
            self.artist_radio(command.get("value"))
        elif action == "album_play":
            self.play_album(command.get("value"))
        elif action == "playlist_play":
            self.play_playlist(command.get("value"))
        elif action == "likes_play":
            self.play_likes()
        elif action == "wave_play":
            self.play_wave()
        elif action == "open":
            # "<kind>:<ident>"; a playlist ident is "<uid>:<kind>" itself, so
            # only the first colon separates.
            kind, _, ident = str(command.get("value") or "").partition(":")
            self.open_collection(kind, ident)
        elif action == "collection_play":
            self.collection_play(command.get("value"))
        else:
            log(f"неизвестная команда: {action!r}")

    def toggle_like(self, action):
        if self.current is None:
            return
        track_id = str(self.current.id)
        liked = track_id in self.liked
        if (action == "like" and liked) or (action == "unlike" and not liked):
            return
        self.like_by_id(track_id)

    def poll_commands(self):
        try:
            raw = self.command_file.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return
        try:
            command = json.loads(raw)
        except json.JSONDecodeError:
            return
        nonce = int(command.get("nonce") or 0)
        if nonce <= self.last_command_nonce:
            return
        self.last_command_nonce = nonce
        self.dispatch(command)

    def dispatch(self, command):
        with self.lock:
            try:
                self.handle(command)
                self.last_error, self.last_error_at = "", 0
            except Exception as e:  # noqa: BLE001
                self.fail(f"команда {command.get('action')!r}: {e}")
            self.write_state()

    # -------------------------------------------------------------------- loop

    def run(self):
        self.mpv.start()
        self.write_state()
        last_write = last_prefetch = 0.0
        while not self.stopping:
            if not self.mpv.alive():
                log("mpv умер, поднимаю заново")
                self.mpv.start()
                if self.current:
                    self.play_track(self.current)
            self.poll_commands()
            while self.remote:
                self.dispatch(self.remote.popleft())
            self.run_pending_hint()
            now = time.time()
            # A prefetch that failed on a timeout would otherwise stay failed
            # until the next track change, and that press would wait on it.
            if now - last_prefetch >= PREFETCH_INTERVAL and not self.prefetching:
                last_prefetch = now
                self.background(self.prefetch_next, "предзагрузка")
            if now - last_write >= STATE_INTERVAL:
                with self.lock:
                    self.write_state()
                last_write = now
                self.check_service(now)
            time.sleep(POLL_SECONDS)
        self.mpv.stop()
        if self.mpris:
            self.mpris.stop()


def service_alive(path):
    """True while the service keeps marking the file, None if it never did."""
    try:
        return time.time() - path.stat().st_mtime < SERVICE_GRACE
    except OSError:
        return None


def read_token(path):
    path = pathlib.Path(path).expanduser()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    return (os.environ.get("YM_TOKEN") or "").strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", default="~/.config/noctalia/yandex-music-token")
    parser.add_argument("--data-dir", default=str(DEFAULT_DIR))
    parser.add_argument("--source", default="wave", choices=("wave", "likes"))
    parser.add_argument("--volume", type=int, default=70)
    args = parser.parse_args()

    data_dir = pathlib.Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    state_file = data_dir / "state.json"

    global LOG_FILE
    LOG_FILE = data_dir / "daemon.log"
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
        LOG_FILE.unlink()   # держим один файл, без ротации
    threading.excepthook = lambda hook: log(
        f"поток {hook.thread.name if hook.thread else '?'} упал: {hook.exc_value!r}"
    )

    # Two daemons would fight over one mpv socket, so a second copy steps aside.
    # Open without truncating: the loser must not wipe the winner's pid.
    lock_fd = os.open(data_dir / "daemon.lock", os.O_RDWR | os.O_CREAT, 0o644)
    lock_file = os.fdopen(lock_fd, "r+", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("демон уже запущен, выхожу")
        return 0
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()

    enable_connection_pooling()
    token = read_token(args.token_file)
    if not token:
        state_file.write_text(json.dumps({
            "ok": False,
            "error": "no-token",
            "message": f"Нет токена. Запустите ym_auth.py, он положит его в {args.token_file}",
            "heartbeat": int(time.time()),
        }, ensure_ascii=False), encoding="utf-8")
        log("нет токена")
        return 1

    player = None

    def shutdown(_signum, _frame):
        # Without this a SIGTERM would leave mpv running and still audible.
        if player:
            player.mpv.stop()
            if player.mpris:
                player.mpris.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        try:
            player = Player(token, data_dir, args.source, args.volume)
            player.run()
            if player.stopping:
                return 0
        except KeyboardInterrupt:
            if player:
                player.mpv.stop()
                if player.mpris:
                    player.mpris.stop()
            return 0
        except Exception as e:  # noqa: BLE001 - keep the daemon alive
            log(f"плеер упал: {e}")
            if player:
                player.mpv.stop()
                # A fresh Player would otherwise fight the old one for the name.
                if player.mpris:
                    player.mpris.stop()
            state_file.write_text(json.dumps({
                "ok": False,
                "error": "player",
                "message": str(e),
                "heartbeat": int(time.time()),
            }, ensure_ascii=False), encoding="utf-8")
            # A crash loop must not outlive the plugin either: without this the
            # daemon kept retrying long after the service that owns it was gone.
            if service_alive(data_dir / "service.alive") is False:
                log("сервиса нет, не перезапускаюсь")
                return 0
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
