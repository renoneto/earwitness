"""Clip input names and clip durations (audio clips design 4.2, 4.3).

One canonical name for a clip input in import_dir:
`<W>-<REQ>-clip-<episode_id>-<start_ms>-<end_ms>.mp3`. `parse` is the only
code that reads it: the pilot import, the inventory and `pilot_clips.py
remove` all use it. The two numbers are fixed at the end, so an episode ID
holding hyphens parses one way only.

`mp3_seconds` measures a clip the way the host can without ffprobe: it
walks the bytes as MPEG-1/2 Layer III frames from offset 0 to the end. Any
byte outside a valid frame (a tag, a stray byte, a cut frame) refuses.
Standard library only.
"""
from __future__ import annotations

import re

_NAME = re.compile(r"(?P<w>[a-z0-9][a-z0-9-]{0,63})-(?P<req>REQ-[0-9]{8}-[0-9]+)-clip-"
                   r"(?P<ep>[A-Za-z0-9_.-]{1,64})-(?P<s>[0-9]{1,9})-(?P<e>[0-9]{1,9})\.mp3")


def parse(basename: str) -> dict | None:
    """The parts of a canonical clip name, or None (start must be before end)."""
    m = _NAME.fullmatch(basename) if isinstance(basename, str) else None
    if m is None or int(m["s"]) >= int(m["e"]):
        return None
    return {"workspace_id": m["w"], "request_id": m["req"], "episode_id": m["ep"],
            "start_ms": int(m["s"]), "end_ms": int(m["e"])}


def name(workspace_id: str, request_id: str, episode_id: str, start_ms: int, end_ms: int) -> str:
    """The canonical name `parse` reads."""
    return f"{workspace_id}-{request_id}-clip-{episode_id}-{start_ms}-{end_ms}.mp3"


# Layer III bitrates (kbps) by index; 0 (free format) and 15 are refused.
_BITRATES = {3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),   # MPEG-1
             2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)}       # MPEG-2
_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000)}
# (samples per frame, frame-length coefficient) for Layer III.
_FRAME = {3: (1152, 144), 2: (576, 72)}


def mp3_seconds(data: bytes) -> float:
    """Duration of frame-only MPEG-1/2 Layer III bytes; ValueError otherwise."""
    if not data:
        raise ValueError("no audio frames")
    seconds, at, size = 0.0, 0, len(data)
    while at < size:
        if size - at < 4:
            raise ValueError(f"bytes after the last frame at {at}")
        b1, b2 = data[at + 1], data[at + 2]
        version, layer = (b1 >> 3) & 0b11, (b1 >> 1) & 0b11
        if data[at] != 0xFF or (b1 & 0xE0) != 0xE0 or version not in _BITRATES or layer != 0b01:
            raise ValueError(f"no MPEG-1/2 Layer III frame header at {at}")
        bitrate_index, rate_index, padding = b2 >> 4, (b2 >> 2) & 0b11, (b2 >> 1) & 1
        if not 0 < bitrate_index < 15 or rate_index == 3:
            raise ValueError(f"unsupported bitrate or sample rate at {at}")
        rate = _RATES[version][rate_index]
        samples, coefficient = _FRAME[version]
        length = coefficient * _BITRATES[version][bitrate_index] * 1000 // rate + padding
        if at + length > size:
            raise ValueError(f"frame at {at} is cut off")
        seconds += samples / rate
        at += length
    return seconds
