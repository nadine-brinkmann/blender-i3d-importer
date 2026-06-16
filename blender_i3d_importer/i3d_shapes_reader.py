"""I3D .i3d.shapes binary reader — Python port of I3DShapesTool.Lib.

Stage 1 of the Python port: Cipher + FileHeader + Entity-level reader.
Shape-content parsing (vertices, UVs, blend weights etc.) comes in Stage 2.

Source reference: github.com/nadine-brinkmann/i3d-to-objx
- Container/Cipher/I3DCipher.cs
- Container/Cipher/CipherStream.cs
- Container/FileHeader.cs
- Container/ShapesFileReader.cs
- Container/Entity.cs
- Container/EntityType.cs
- Tools/EndianBinaryReader.cs
- Tools/Extensions/StreamExtensions.cs (Align)
"""

import struct
import io
from enum import IntEnum, IntFlag

try:
    from ._cipher_keys import KEY_CONST
except ImportError:
    from _cipher_keys import KEY_CONST


# ---------------------------------------------------------------------------
# Cipher
# ---------------------------------------------------------------------------


class I3DCipher:
    """XOR-based block cipher for .i3d.shapes files.

    Port of Container/Cipher/I3DCipher.cs.
    Block size = 64 bytes = 16 uint32. Cipher is symmetric (encrypt == decrypt).
    """

    CRYPT_BLOCK_SIZE = 64  # bytes
    KEY_LEN = 16            # uint32 words

    def __init__(self, seed: int):
        if not 0 <= seed <= 255:
            raise ValueError(f"seed must fit in a byte, got {seed}")
        start = seed << 4
        self.key = list(KEY_CONST[start:start + self.KEY_LEN])
        self.key[0x8] = 0  # block counter low
        self.key[0x9] = 0  # block counter high

    @staticmethod
    def _rol(val: int, bits: int) -> int:
        val &= 0xFFFFFFFF
        return ((val << bits) | (val >> (32 - bits))) & 0xFFFFFFFF

    @staticmethod
    def _ror(val: int, bits: int) -> int:
        val &= 0xFFFFFFFF
        return ((val >> bits) | (val << (32 - bits))) & 0xFFFFFFFF

    @staticmethod
    def _round_up_to(val: int, to_nearest: int) -> int:
        mod = val % to_nearest
        return val if mod == 0 else val + (to_nearest - mod)

    @staticmethod
    def _shuffle1(k, i1, i2, i3, i4):
        k[i3] = (k[i3] ^ I3DCipher._rol((k[i2] + k[i1]) & 0xFFFFFFFF, 7)) & 0xFFFFFFFF
        k[i4] = (k[i4] ^ I3DCipher._rol((k[i3] + k[i1]) & 0xFFFFFFFF, 9)) & 0xFFFFFFFF
        k[i2] = (k[i2] ^ I3DCipher._rol((k[i3] + k[i4]) & 0xFFFFFFFF, 13)) & 0xFFFFFFFF
        k[i1] = (k[i1] ^ I3DCipher._ror((k[i2] + k[i4]) & 0xFFFFFFFF, 14)) & 0xFFFFFFFF

    @staticmethod
    def _shuffle2(k, i1, i2, i3, i4):
        k[i3] = (k[i3] ^ I3DCipher._rol((k[i2] + k[i1]) & 0xFFFFFFFF, 7)) & 0xFFFFFFFF
        k[i4] = (k[i4] ^ I3DCipher._rol((k[i2] + k[i3]) & 0xFFFFFFFF, 9)) & 0xFFFFFFFF
        k[i1] = (k[i1] ^ I3DCipher._rol((k[i3] + k[i4]) & 0xFFFFFFFF, 13)) & 0xFFFFFFFF
        k[i2] = (k[i2] ^ I3DCipher._ror((k[i4] + k[i1]) & 0xFFFFFFFF, 14)) & 0xFFFFFFFF

    def _process_uint_blocks(self, buf: list, block_index: int):
        """In-place cipher on a list of uint32. len(buf) must be multiple of 16."""
        if len(buf) % self.KEY_LEN != 0:
            raise ValueError(f"Expecting {self.KEY_LEN}-uint blocks, got len={len(buf)}")

        # Indexed key copy (GetKeyByIndexBlock)
        key = list(self.key)
        key[8] = block_index & 0xFFFFFFFF
        key[9] = (block_index >> 32) & 0xFFFFFFFF

        block_counter = (key[8] | (key[9] << 32)) & 0xFFFFFFFFFFFFFFFF

        for i in range(0, len(buf), self.KEY_LEN):
            temp = list(key)
            for _ in range(10):
                self._shuffle1(temp, 0x0, 0xC, 0x4, 0x8)
                self._shuffle1(temp, 0x5, 0x1, 0x9, 0xD)
                self._shuffle1(temp, 0xA, 0x6, 0xE, 0x2)
                self._shuffle1(temp, 0xF, 0xB, 0x3, 0x7)
                self._shuffle2(temp, 0x3, 0x0, 0x1, 0x2)
                self._shuffle2(temp, 0x4, 0x5, 0x6, 0x7)
                self._shuffle1(temp, 0xA, 0x9, 0xB, 0x8)
                self._shuffle2(temp, 0xE, 0xF, 0xC, 0xD)

            for j in range(self.KEY_LEN):
                buf[i + j] = (buf[i + j] ^ ((key[j] + temp[j]) & 0xFFFFFFFF)) & 0xFFFFFFFF

            block_counter = (block_counter + 1) & 0xFFFFFFFFFFFFFFFF
            key[8] = block_counter & 0xFFFFFFFF
            key[9] = (block_counter >> 32) & 0xFFFFFFFF

    def process(self, buffer: bytearray, block_index: int) -> int:
        """Cipher the buffer in place. Returns the next block index.

        The buffer is padded internally to a multiple of CRYPT_BLOCK_SIZE bytes
        (= 16 uint32 words) but only the original-length bytes are written back.
        """
        rounded = self._round_up_to(len(buffer), self.CRYPT_BLOCK_SIZE)
        padded = bytearray(rounded)
        padded[:len(buffer)] = buffer

        # bytes (little-endian uint32) -> list[int]
        words = list(struct.unpack(f"<{rounded // 4}I", bytes(padded)))
        self._process_uint_blocks(words, block_index)
        ciphered = struct.pack(f"<{len(words)}I", *words)

        buffer[:] = ciphered[:len(buffer)]
        return block_index + (rounded // self.CRYPT_BLOCK_SIZE)


# ---------------------------------------------------------------------------
# File header
# ---------------------------------------------------------------------------


class FileHeader:
    """4-byte header at start of every .i3d.shapes file.

    Layout depends on version:
      version >= 4: bytes are [version, 0, seed, 0]
      version 2-3: bytes are [0, seed, 0, version]
    """

    def __init__(self, version: int, seed: int):
        self.version = version
        self.seed = seed

    @classmethod
    def read(cls, stream: io.BufferedReader) -> "FileHeader":
        b = stream.read(4)
        if len(b) != 4:
            raise ValueError("File too short to contain header")
        b1, b2, b3, b4 = b[0], b[1], b[2], b[3]

        if b1 >= 4:
            return cls(version=b1, seed=b3)
        elif b4 == 2 or b4 == 3:
            return cls(version=b4, seed=b2)
        else:
            raise ValueError(f"Unknown header byte pattern: {b1:02x} {b2:02x} {b3:02x} {b4:02x}")

    def __repr__(self):
        return f"FileHeader(version={self.version}, seed={self.seed})"


# ---------------------------------------------------------------------------
# Entity type + raw entity
# ---------------------------------------------------------------------------


class EntityType(IntEnum):
    UNKNOWN = 0
    SHAPE = 1
    SPLINE = 2   # cubic
    SPLINE_L = 6  # linear


def _type_to_enum(t: int) -> EntityType:
    if t == 1:
        return EntityType.SHAPE
    # Entity types 4 and 5 are tree-shape variants (e.g.
    # data/maps/trees/.../oak_stage05), both using the I3DShape layout:
    #   - type 4 = LOD0 trunk mesh (standard layout + trailing tree data we
    #     skip). Verified: oak LOD0Shape -> 1545 verts / 2410 tris.
    #   - type 5 = LOD attachments (leaf / small-branch planes). Same layout
    #     but with NO bounding-volume Vector4 at the body start;
    #     parse_shape_entity handles that via raw_entity.type. Verified: oak
    #     LOD0AttachmentsShape -> 31248 verts / 18732 tris (+ generic).
    # Both match the Giants Editor Mesh Viewer. Without this the detailed
    # trunk and the leaves are silently dropped (GitHub #22).
    if t in (4, 5):
        return EntityType.SHAPE
    if t == 2:
        return EntityType.SPLINE
    if t == 6:
        return EntityType.SPLINE_L
    return EntityType.UNKNOWN


class RawEntity:
    """Raw entity blob from the .i3d.shapes container. Decode later via I3DShape etc."""

    def __init__(self, type_int: int, data: bytes):
        self.type = type_int
        self.entity_type = _type_to_enum(type_int)
        self.size = len(data)
        self.data = data

    def __repr__(self):
        return f"RawEntity(type={self.type} {self.entity_type.name}, size={self.size})"


# ---------------------------------------------------------------------------
# Binary reader helper with chunked cipher behavior
# ---------------------------------------------------------------------------


class CipherBinReader:
    """Stream-like reader that decrypts data chunk-wise as it is consumed.

    Mirrors the C# behavior of CipherStream + BinaryReader: every Read call
    triggers a Cipher.Process pass on the just-read bytes, advancing the
    cipher block counter by ceil(read_size / 64) blocks. This is essential —
    decrypting all bytes in one big pass produces a different result because
    the cipher's XOR mask is keyed off the per-call block index.

    The reader also reproduces a quirk in the C# Stream Align extension:
    Align(4) computes bytesToRead as `4 - (pos % 4)` (hard-coded 4, not the
    requested word size). We replicate that behavior to stay byte-for-byte
    compatible with the C# tool.
    """

    def __init__(self, raw_after_header: bytes, cipher: "I3DCipher", little_endian: bool = True):
        self._data = raw_after_header
        self._pos = 0
        self._block_offset = 0
        self._cipher = cipher
        self._end = "<" if little_endian else ">"

    @property
    def pos(self) -> int:
        return self._pos

    @property
    def length(self) -> int:
        return len(self._data)

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    def read_bytes(self, n: int) -> bytes:
        if n == 0:
            return b""
        if self._pos + n > len(self._data):
            raise EOFError(f"Tried to read {n} bytes at pos {self._pos}, only {self.remaining} left")
        buf = bytearray(self._data[self._pos:self._pos + n])
        self._pos += n
        self._block_offset = self._cipher.process(buf, self._block_offset)
        return bytes(buf)

    def read_byte(self) -> int:
        return self.read_bytes(1)[0]

    def read_uint16(self) -> int:
        return struct.unpack(self._end + "H", self.read_bytes(2))[0]

    def read_int16(self) -> int:
        return struct.unpack(self._end + "h", self.read_bytes(2))[0]

    def read_uint32(self) -> int:
        return struct.unpack(self._end + "I", self.read_bytes(4))[0]

    def read_int32(self) -> int:
        return struct.unpack(self._end + "i", self.read_bytes(4))[0]

    def read_uint64(self) -> int:
        return struct.unpack(self._end + "Q", self.read_bytes(8))[0]

    def read_single(self) -> float:
        return struct.unpack(self._end + "f", self.read_bytes(4))[0]

    def read_double(self) -> float:
        return struct.unpack(self._end + "d", self.read_bytes(8))[0]

    def align(self, word_size: int = 4):
        """Advance past padding bytes to the next word_size-aligned position.

        Reads (and decrypts) the skipped bytes through the cipher so the block
        counter stays in sync — matches C# StreamExtension.Align which calls
        Stream.Read on the padding bytes (which goes through CipherStream).
        Note: the C# extension hard-codes `4 - mod` regardless of word_size,
        so we do the same.
        """
        mod = self._pos % word_size
        if mod == 0:
            return
        bytes_to_skip = 4 - mod  # C# hard-codes 4, even when word_size != 4
        if bytes_to_skip > 0 and self._pos + bytes_to_skip <= len(self._data):
            self.read_bytes(bytes_to_skip)


# Convenience alias to keep older call sites working in tests
BinReader = CipherBinReader


# ---------------------------------------------------------------------------
# Top-level reader
# ---------------------------------------------------------------------------


def read_shapes_file(path: str) -> "ShapesFile":
    """Open and parse a .i3d.shapes file. Returns a ShapesFile with raw entities."""
    with open(path, "rb") as f:
        raw = f.read()
    return parse_shapes_bytes(raw)


def parse_shapes_bytes(raw: bytes) -> "ShapesFile":
    """Parse already-loaded .i3d.shapes bytes."""
    header = FileHeader.read(io.BytesIO(raw))
    if header.version < 2 or header.version > 10:
        raise ValueError(f"Unsupported version: {header.version}")

    little_endian = header.version >= 4
    cipher = I3DCipher(header.seed)
    reader = CipherBinReader(raw[4:], cipher, little_endian=little_endian)

    count = reader.read_int32()
    if count < 0 or count > 1_000_000:
        raise ValueError(f"Invalid entity count {count} — likely wrong seed/decrypt")

    entities = []
    for _ in range(count):
        t = reader.read_int32()
        sz = reader.read_int32()
        if sz < 0 or sz > reader.remaining:
            raise ValueError(
                f"Invalid entity size {sz} at pos {reader.pos} "
                f"(count_so_far={len(entities)}/{count})"
            )
        data = reader.read_bytes(sz)
        entities.append(RawEntity(t, data))

    return ShapesFile(header=header, entities=entities)


class ShapesFile:
    def __init__(self, header: FileHeader, entities: list):
        self.header = header
        self.entities = entities

    def __repr__(self):
        by_type = {}
        for e in self.entities:
            by_type[e.entity_type.name] = by_type.get(e.entity_type.name, 0) + 1
        return (
            f"ShapesFile(v{self.header.version}, seed={self.header.seed}, "
            f"entities={len(self.entities)}, by_type={by_type})"
        )


# ---------------------------------------------------------------------------
# CLI for quick smoke test
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python i3d_shapes_reader.py <path/to/foo.i3d.shapes>")
        sys.exit(1)
    sf = read_shapes_file(sys.argv[1])
    print(sf)
    for i, e in enumerate(sf.entities[:10]):
        print(f"  [{i}] {e}")
    if len(sf.entities) > 10:
        print(f"  ... and {len(sf.entities) - 10} more")
