"""
Reader for external GIANTS .i3d.anim binary files.

The v7/v10 formats used by GIANTS store animation sets, clips and transform tracks
in a compact binary form:
  u32 version
  u32 animation_set_count
  repeated animation sets:
    string name (u32 byte length, bytes, 4-byte aligned)
    u32 clip_count
    repeated clips:
      string name
      f32 duration_ms
      u32 track_count
      repeated tracks:
        u32 node_id
        u32 key_count
        repeated keys:
          f32 time_ms
  u32 key_type bitmask (1=translation, 2=rotation, 4=scale)
  repeated f32 triples for the enabled transform components
"""

import math
import struct
from pathlib import Path
from typing import List

from .i3d_xml_parser import (
    I3DAnimationClip,
    I3DAnimationKey,
    I3DAnimationKeyframes,
    I3DAnimationSet,
)


class AnimReadError(RuntimeError):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def _require(self, size: int):
        if self.offset + size > len(self.data):
            raise AnimReadError(
                f"Unexpected EOF at 0x{self.offset:x}, need {size} byte(s)")

    def align4(self):
        while self.offset % 4:
            self.offset += 1

    def u32(self) -> int:
        self._require(4)
        value = struct.unpack_from('<I', self.data, self.offset)[0]
        self.offset += 4
        return value

    def f32(self) -> float:
        self._require(4)
        value = struct.unpack_from('<f', self.data, self.offset)[0]
        self.offset += 4
        return value

    def string(self) -> str:
        size = self.u32()
        if size > 1024 * 1024:
            raise AnimReadError(
                f"Invalid string length {size} at 0x{self.offset - 4:x}")
        self._require(size)
        raw = self.data[self.offset:self.offset + size]
        self.offset += size
        self.align4()
        return raw.decode('latin1', errors='replace')


def read_anim_file(path: Path) -> List[I3DAnimationSet]:
    reader = _Reader(Path(path).read_bytes())
    version = reader.u32()
    if version not in {7, 10}:
        raise AnimReadError(f"Unsupported .i3d.anim version {version}")

    animation_sets: List[I3DAnimationSet] = []
    set_count = reader.u32()
    for _ in range(set_count):
        anim_set = I3DAnimationSet(name=reader.string())
        clip_count = reader.u32()
        for _ in range(clip_count):
            clip = I3DAnimationClip(
                name=reader.string(),
                duration=reader.f32(),
            )
            track_count = reader.u32()
            for _ in range(track_count):
                node_id = reader.u32()
                key_count = reader.u32()
                keyframes = I3DAnimationKeyframes(node_id=node_id)
                for _ in range(key_count):
                    time_ms = reader.f32()
                    key_type = reader.u32()
                    translation = None
                    rotation = None
                    scale = None
                    if key_type & 1:
                        translation = (reader.f32(), reader.f32(), reader.f32())
                    if key_type & 2:
                        rotation = (
                            math.degrees(reader.f32()),
                            math.degrees(reader.f32()),
                            math.degrees(reader.f32()),
                        )
                    if key_type & 4:
                        scale = (reader.f32(), reader.f32(), reader.f32())
                    unknown_bits = key_type & ~0x7
                    if unknown_bits:
                        raise AnimReadError(
                            f"Unsupported key type bits 0x{unknown_bits:x} "
                            f"at 0x{reader.offset:x}")
                    keyframes.keys.append(I3DAnimationKey(
                        time=time_ms,
                        translation=translation,
                        rotation=rotation,
                        scale=scale,
                        raw_attrs={'binaryKeyType': str(key_type)},
                    ))
                clip.keyframes.append(keyframes)
            anim_set.clips.append(clip)
        animation_sets.append(anim_set)

    if reader.offset != len(reader.data):
        raise AnimReadError(
            f"Trailing data after .i3d.anim parse: "
            f"0x{reader.offset:x} of 0x{len(reader.data):x}")
    return animation_sets
