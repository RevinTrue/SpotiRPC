from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from html import unescape
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
from select import select
import socket
import struct
import subprocess
import time
from urllib.request import urlopen
from uuid import uuid4


def artist_names(value):
    if not isinstance(value, list):
        return []
    return [name for name in value if isinstance(name, str) and name.strip()]


def artist_credit(track, title):
    names = artist_names(track.get("xesam:artist")) or artist_names(track.get("xesam:albumArtist"))
    featured = re.search(
        r"(?:\(|\[|\s-\s|\s)(?:feat\.?|ft\.?|featuring)\s+([^()\[\]]+)",
        title, re.IGNORECASE,
    )
    if featured:
        names = [*names, *featured[1].split(",")]

    artists = {}
    for name in names:
        name = name.strip()
        if name:
            artists.setdefault(name.casefold(), name)
    return list(artists.values())


def public_artist_credit(url):
    page = bytearray()
    try:
        with urlopen(url, timeout=2) as response:
            while len(page) < 131_072:
                chunk = response.read(16_384)
                if not chunk:
                    break
                page.extend(chunk)
                match = re.search(rb'<meta name="music:musician_description" content="([^"]+)"', page)
                if match:
                    return unescape(match[1].decode("utf-8", errors="replace"))
                if b"</head>" in page:
                    break
    except (OSError, HTTPException):
        return None
    return ""


class PublicCredits:
    def __init__(self, executor):
        self.executor = executor
        self.cache = {}
        self.pending = None

    def get(self, url):
        if self.pending is not None:
            pending_url, future = self.pending
            if future.done():
                credit = future.result()
                if len(self.cache) >= 256:
                    del self.cache[next(iter(self.cache))]
                expires = time.monotonic() + 30 if credit is None else float("inf")
                self.cache[pending_url] = (credit or "", expires)
                self.pending = None

        if url in self.cache:
            credit, expires = self.cache[url]
            if time.monotonic() < expires:
                return credit
            del self.cache[url]
        if self.pending is None and url.startswith("https://open.spotify.com/track/"):
            self.pending = url, self.executor.submit(public_artist_credit, url)
        return ""


def dbus_properties(values):
    if not isinstance(values, dict):
        return {}
    return {key: value.get("data") for key, value in values.items() if isinstance(value, dict)}


def spotify_link(value):
    if not isinstance(value, str):
        return ""
    if value.startswith("spotify:"):
        parts = value.removeprefix("spotify:").replace(":", "/")
    else:
        prefix = "https://open.spotify.com/"
        if not value.startswith(prefix):
            return ""
        parts = value.removeprefix(prefix)
        parts = re.sub(r"^intl-[a-zA-Z-]+/", "", parts)
        parts = re.split(r"[?#]", parts, maxsplit=1)[0]
    if re.fullmatch(r"(track|episode)/[A-Za-z0-9]{22}", parts):
        return f"https://open.spotify.com/{parts}"
    return ""


def spotify_activity(credits):
    result = subprocess.run(
        ["busctl", "--user", "--auto-start=no", "--json=short", "call", "org.mpris.MediaPlayer2.spotify",
         "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties",
         "GetAll", "s", "org.mpris.MediaPlayer2.Player"],
        capture_output=True, timeout=2,
    )
    if result.returncode:
        return None

    sampled_at = time.time()
    try:
        reply = json.loads(result.stdout)
    except ValueError:
        return None
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, list) or not data:
        return None

    player = dbus_properties(data[0])
    status = player.get("PlaybackStatus")
    if status not in ("Playing", "Paused", "Stopped"):
        return None
    if status != "Playing":
        return status

    track = dbus_properties(player.get("Metadata"))
    title = track.get("xesam:title")
    source = track.get("xesam:url")
    if isinstance(source, str) and source.startswith("spotify:ad:"):
        return "Playing"
    if not isinstance(title, str) or not title.strip():
        return None
    url = spotify_link(source)

    album = track.get("xesam:album")
    album = album if isinstance(album, str) else ""
    release_name = " ".join(album.casefold().split())
    title_name = " ".join(title.casefold().split())
    if release_name == title_name or release_name.endswith(("- single", "(single)")):
        album = ""

    local_artists = artist_credit(track, title)
    public_credit = credits.get(url) if len(local_artists) < 2 else ""
    artists = public_credit or ", ".join(local_artists)

    activity = {"type": 2, "details": title[:128]}
    if url:
        activity["buttons"] = [{"label": "Play on Spotify", "url": url}]

    if artists:
        activity["state"] = artists[:128]

    cover = track.get("mpris:artUrl")
    if isinstance(cover, str) and cover.startswith("https://"):
        activity["assets"] = {"large_image": cover}
        if album:
            activity["assets"]["large_text"] = album[:128]

    length = track.get("mpris:length")
    length = length if type(length) is int else 0
    position = player.get("Position")
    position = position if type(position) is int else -1
    if 0 <= position < length:
        start = sampled_at - position / 1_000_000
        activity["timestamps"] = {"start": int(start), "end": int(start + length / 1_000_000)}

    track_id = track.get("mpris:trackid")
    if not isinstance(track_id, str) or not track_id:
        track_id = url
    return activity, track_id, position, length


def connect_discord(client):
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    for root in (runtime, runtime / "app/com.discordapp.Discord", Path("/tmp")):
        for path in sorted(root.glob("discord-ipc-*")):
            try:
                client.connect(str(path))
                return
            except OSError:
                continue
    raise ConnectionError("Discord desktop socket unavailable")


def send(client, opcode, payload):
    body = json.dumps(payload).encode()
    send_frame(client, opcode, body)


def send_frame(client, opcode, body):
    client.sendall(struct.pack("<II", opcode, len(body)) + body)


def read_exactly(client, size):
    data = bytearray()
    while len(data) < size:
        chunk = client.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Discord disconnected")
        data.extend(chunk)
    return data


def read_frame(client):
    opcode, size = struct.unpack("<II", read_exactly(client, 8))
    if size > 1_048_576:
        raise ConnectionError("Discord sent an oversized IPC frame")
    payload = read_exactly(client, size)
    if opcode == 3:
        send_frame(client, 4, payload)
        return {}
    body = json.loads(payload)
    if not isinstance(body, dict):
        raise ConnectionError("Discord sent an invalid IPC frame")
    if opcode == 2 or body.get("evt") == "ERROR":
        error = body.get("data")
        if not isinstance(error, dict):
            error = {}
        raise ConnectionError(body.get("message") or error.get("message") or "Discord rejected the request")
    return body


def receive(client, nonce=None):
    while True:
        body = read_frame(client)
        if nonce is None and body.get("evt") == "READY":
            return
        if nonce is not None and body.get("nonce") == nonce:
            return


def set_activity(client, activity):
    nonce = uuid4().hex
    send(client, 1, {
        "cmd": "SET_ACTIVITY",
        "args": {"pid": os.getpid(), "activity": activity},
        "nonce": nonce,
    })
    receive(client, nonce)


def wait_for_next_poll(client):
    deadline = time.monotonic() + 1
    while (remaining := deadline - time.monotonic()) > 0:
        if not select([client], [], [], remaining)[0]:
            return
        read_frame(client)


def changed(previous, current):
    if previous is None or current is None:
        return previous != current

    before = previous.get("timestamps", {})
    after = current.get("timestamps", {})
    if before.keys() != after.keys() or any(abs(before[key] - after[key]) > 2 for key in before):
        return True

    return {k: v for k, v in previous.items() if k != "timestamps"} != {
        k: v for k, v in current.items() if k != "timestamps"
    }


def idle_card():
    return {
        "type": 2,
        "details": "Idle",
        "assets": {"large_image": "https://storage.googleapis.com/pr-newsroom-wp/1/2021/02/Spotify_Icon_RGB_Green.png"},
    }


class SessionPlays:
    def __init__(self):
        self.counts = {}
        self.last_track = None
        self.last_position = -1

    def activity(self, playback):
        activity, track_id, position, length = playback
        if not track_id:
            return activity

        restarted = (
            0 <= position <= 5_000_000
            and length > 0
            and self.last_position >= max(length - 8_000_000, length // 2)
        )
        if track_id != self.last_track or restarted:
            self.counts[track_id] = self.counts.get(track_id, 0) + 1
        self.last_track = track_id
        self.last_position = position

        count = self.counts[track_id]
        album = activity.get("assets", {}).get("large_text")
        if count > 1:
            suffix = f" · {count}×"
            if album:
                activity["assets"]["large_text"] = album[:128 - len(suffix)] + suffix
            else:
                activity["details"] = activity["details"][:128 - len(suffix)] + suffix
        return activity

    def observe_inactive(self, playback):
        if playback != "Paused":
            self.last_track = None
            self.last_position = -1


def application_id():
    path = Path(__file__).with_name(".env")
    if path.is_file():
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "DISCORD_APPLICATION_ID":
                return value.strip().strip('"').strip("'")
    return None


def main(credits):
    client_id = application_id()
    if not client_id or not client_id.isascii() or not client_id.isdecimal():
        raise SystemExit("Set DISCORD_APPLICATION_ID to your numeric Application ID in .env.")

    plays = SessionPlays()
    last_error = None
    while True:
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(5)
                connect_discord(client)
                send(client, 0, {"v": 1, "client_id": client_id})
                receive(client)
                print("Connected to Discord.", flush=True)
                last_error = None

                previous = None
                missed_read = False
                try:
                    while True:
                        try:
                            playback = spotify_activity(credits)
                        except subprocess.TimeoutExpired:
                            playback = None
                        if playback is None and not missed_read:
                            missed_read = True
                            wait_for_next_poll(client)
                            continue
                        missed_read = playback is None
                        if isinstance(playback, tuple):
                            activity = plays.activity(playback)
                        else:
                            plays.observe_inactive(playback)
                            activity = idle_card() if playback in ("Paused", "Stopped") else None
                        if changed(previous, activity):
                            set_activity(client, activity)
                            if playback in ("Paused", "Stopped") and (
                                previous is None or previous.get("details") != "Idle"
                            ):
                                print(f"Discord accepted the {playback.lower()} card.", flush=True)
                            previous = activity
                        wait_for_next_poll(client)
                except KeyboardInterrupt:
                    if previous is not None:
                        with suppress(OSError, ValueError):
                            set_activity(client, None)
                    return
        except FileNotFoundError:
            raise SystemExit("busctl is required to read Spotify playback.") from None
        except (OSError, ValueError) as error:
            if str(error) != last_error:
                print(f"{error}. Retrying in 5 seconds.", flush=True)
                last_error = str(error)
            time.sleep(5)


if __name__ == "__main__":
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            main(PublicCredits(executor))
    except KeyboardInterrupt:
        pass
