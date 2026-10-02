#!/usr/bin/env python3
"""MPRIS2 face for the Yandex Music daemon.

The panel "Медиа" in Noctalia lists MPRIS players, and mpv alone cannot be one
here: mpv-mpris is not installed, and the Yandex direct stream carries no tags,
so even with the script mpv would publish a nameless entry. The daemon knows
the track anyway - it got it from the API - so it publishes its own player on
the session bus and routes Play/Pause/Next back into the same command path the
desktop widget uses.

The bus lives in its own thread with its own asyncio loop; `publish()` is the
only thread-safe entry point and hops onto that loop.
"""

import asyncio
import pathlib
import re
import sys
import threading

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method, signal

BUS_NAME = "org.mpris.MediaPlayer2.yandexmusic"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
TRACK_PATH = "/ru/yandex/music/track/"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"


def log(message):
    print(f"[ym/mpris] {message}", file=sys.stderr, flush=True)


class RootInterface(ServiceInterface):
    """org.mpris.MediaPlayer2 - the bits every client reads before the player."""

    def __init__(self):
        super().__init__("org.mpris.MediaPlayer2")

    @method()
    def Raise(self):
        pass

    @method()
    def Quit(self):
        pass

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":
        return "Yandex Music"

    @dbus_property(access=PropertyAccess.READ)
    def DesktopEntry(self) -> "s":
        return "yandex-music"

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> "as":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> "as":
        return []


class PlayerInterface(ServiceInterface):
    """org.mpris.MediaPlayer2.Player - status, metadata and transport."""

    def __init__(self, on_command):
        super().__init__("org.mpris.MediaPlayer2.Player")
        self._on_command = on_command
        self._status = "Stopped"
        self._metadata = {"mpris:trackid": Variant("o", NO_TRACK)}
        self._position = 0
        self._volume = 1.0
        self._can_control = True

    # ------------------------------------------------------------- transport

    @method()
    def Play(self):
        self._on_command("resume")

    @method()
    def Pause(self):
        self._on_command("pause")

    @method()
    def PlayPause(self):
        self._on_command("toggle")

    @method()
    def Stop(self):
        self._on_command("pause")

    @method()
    def Next(self):
        self._on_command("next")

    @method()
    def Previous(self):
        self._on_command("prev")

    @method()
    def Seek(self, offset: "x"):
        self._on_command("seek", offset / 1_000_000.0)

    @method()
    def SetPosition(self, track_id: "o", position: "x"):
        self._on_command("position", position / 1_000_000.0)

    @method()
    def OpenUri(self, uri: "s"):
        pass

    @signal()
    def Seeked(self) -> "x":
        return self._position

    # ------------------------------------------------------------ properties

    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":
        return self._status

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":
        return self._metadata

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":
        return self._position

    @dbus_property(access=PropertyAccess.READWRITE)
    def Volume(self) -> "d":
        return self._volume

    @Volume.setter
    def Volume(self, value: "d"):
        value = max(0.0, min(1.0, float(value)))
        self._volume = value
        self._on_command("volume", value)

    @dbus_property(access=PropertyAccess.READ)
    def Rate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MinimumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MaximumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":
        return self._can_control

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":
        return self._can_control

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":
        return self._can_control

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":
        return self._can_control

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":
        return self._can_control

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":
        return True


def _metadata_of(state):
    track_id = str(state.get("trackId") or "")
    clean = re.sub(r"[^A-Za-z0-9_]", "_", track_id)
    meta = {"mpris:trackid": Variant("o", TRACK_PATH + clean if clean else NO_TRACK)}

    length = int(state.get("durationMs") or 0) * 1000
    if length > 0:
        meta["mpris:length"] = Variant("x", length)

    cover = str(state.get("cover") or "")
    if cover:
        try:
            meta["mpris:artUrl"] = Variant("s", pathlib.Path(cover).as_uri())
        except ValueError:
            pass

    meta["xesam:title"] = Variant("s", str(state.get("title") or ""))
    artists = [a.strip() for a in str(state.get("artist") or "").split(",") if a.strip()]
    meta["xesam:artist"] = Variant("as", artists)
    album = str(state.get("album") or "")
    if album:
        meta["xesam:album"] = Variant("s", album)
    return meta


class Mpris:
    """Owns the bus thread; `publish` feeds it the daemon's state dict."""

    def __init__(self, on_command):
        self.on_command = on_command
        self.player = PlayerInterface(on_command)
        self.root = RootInterface()
        self.bus = None
        self.loop = None
        self.ready = threading.Event()
        self._last_track = None

    def start(self):
        threading.Thread(target=self._run, name="mpris", daemon=True).start()
        # Not fatal if the bus is slow or missing: the widget works without it.
        self.ready.wait(timeout=5)

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            loop.run_until_complete(self._serve())
        except Exception as e:  # noqa: BLE001
            log(f"шина недоступна: {e}")
            self.loop = None
            self.ready.set()
            return
        self.ready.set()
        try:
            loop.run_forever()
        except Exception as e:  # noqa: BLE001
            log(f"цикл шины остановлен: {e}")

    async def _serve(self):
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        self.bus.export(OBJECT_PATH, self.root)
        self.bus.export(OBJECT_PATH, self.player)
        await self.bus.request_name(BUS_NAME)
        log(f"опубликован {BUS_NAME}")

    def publish(self, state):
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._apply, state)
        except RuntimeError:
            pass

    def _apply(self, state):
        player = self.player
        changed = {}

        if state.get("active"):
            status = "Paused" if state.get("paused") else "Playing"
        else:
            status = "Stopped"
        if status != player._status:
            player._status = status
            changed["PlaybackStatus"] = status

        track_id = str(state.get("trackId") or "")
        metadata = _metadata_of(state)
        # Rebuild only on a real change: PropertiesChanged fires once a second
        # otherwise, and every listening panel would re-render with it.
        if track_id != self._last_track or not player._metadata:
            self._last_track = track_id
            player._metadata = metadata
            changed["Metadata"] = metadata

        position = int(state.get("progressMs") or 0) * 1000
        jumped = abs(position - player._position) > 2_000_000
        player._position = position

        volume = float(state.get("volume") or 0.0)
        if abs(volume - player._volume) > 0.001:
            player._volume = volume
            changed["Volume"] = volume

        if changed:
            player.emit_properties_changed(changed)
        if jumped:
            player.Seeked()

    def stop(self):
        loop, bus = self.loop, self.bus
        if loop is None or loop.is_closed():
            return
        self.loop = None

        def close():
            # Only a disconnect releases the bus name; a stopped loop would
            # leave a ghost player in every media panel.
            if bus is not None:
                bus.disconnect()
            loop.stop()

        try:
            loop.call_soon_threadsafe(close)
        except RuntimeError:
            pass
