"""I3D Shape data models — Python port of I3DShapesTool.Lib.Model.

Stage 2 of the Python port: decodes the raw entity bytes (from
i3d_shapes_reader.RawEntity) into typed shape objects with vertices,
indices, UVs, normals, blend weights/indices, etc.

Source reference: github.com/nadine-brinkmann/i3d-to-objx
- Model/I3DShape.cs
- Model/I3DShapeSubset.cs
- Model/I3DShapeOptions.cs
- Model/I3DShapeAttachment.cs
- Model/I3DPart.cs
- Model/I3DTri.cs, I3DUV.cs, I3DVector.cs, I3DVector4.cs

Usage:
    from i3d_shapes_reader import read_shapes_file
    from i3d_shapes_models import parse_shape_entity

    sf = read_shapes_file("foo.i3d.shapes")
    shapes = [parse_shape_entity(e, sf.header.version) for e in sf.entities
              if e.entity_type.name == "SHAPE"]
"""

import struct
from enum import IntFlag


# ---------------------------------------------------------------------------
# Options flags (Model/I3DShapeOptions.cs)
# ---------------------------------------------------------------------------


class ShapeOptions(IntFlag):
    NONE = 0
    HAS_NORMALS = 0x01
    HAS_UV1 = 0x02
    HAS_UV2 = 0x04
    HAS_UV3 = 0x08
    HAS_UV4 = 0x10
    HAS_VERTEX_COLOR = 0x20
    HAS_SKINNING_INFO = 0x40
    HAS_TANGENTS = 0x80
    SINGLE_BLEND_WEIGHTS = 0x100
    HAS_GENERIC = 0x200
    ALL = 0x3FF


# Engine-marker high bit in the .i3d.shapes options field. When set, the
# Giants Editor shows the shape's "CPU Mesh" checkbox as checked. Empirically
# verified roundtrip (biomassBale125 roundbale_vis/extra): Blender mesh-object
# with i3D_cpuMesh=True -> Giants exporter -> i3dConverter writes this bit
# into the new .i3d.shapes binary -> GE shows CPU Mesh checked again.
# Other observed high bits (0x02000000 occluder, 0x80000000/0xC0000000
# MergedMesh, 0x00040000 bee-emitter, 0x00010000 unknown) are documented in
# the "Blender-Addon i3d-Import - Analyse vorhandener LS 25 Shapes-Dateien"
# note; only the CPU Mesh and occluder bits have a verified XML roundtrip
# path so far.
SHAPE_HIGH_BIT_CPU_MESH = 0x01000000


# ---------------------------------------------------------------------------
# Small reader operating on already-decrypted Shape entity bytes
# ---------------------------------------------------------------------------


class _ByteReader:
    """Plain little-endian binary reader over a bytes object.

    Used to decode the *already decrypted* per-entity bytes. The cipher has
    already been applied at the container level (i3d_shapes_reader).
    """

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    @property
    def pos(self) -> int:
        return self._pos

    @property
    def length(self) -> int:
        return len(self._data)

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    @property
    def at_end(self) -> bool:
        return self._pos >= len(self._data)

    def read_bytes(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise EOFError(f"Tried to read {n} bytes at pos {self._pos}, only {self.remaining} left")
        b = self._data[self._pos:self._pos + n]
        self._pos += n
        return b

    def peek_bytes(self, n: int) -> bytes:
        """Return up to n upcoming bytes without advancing the cursor.

        Returns fewer bytes if fewer remain - no EOFError. Caller must
        check the returned length if it matters.
        """
        return self._data[self._pos:self._pos + n]

    def read_uint8(self) -> int:
        return self.read_bytes(1)[0]

    def read_uint16(self) -> int:
        return struct.unpack("<H", self.read_bytes(2))[0]

    def read_int16(self) -> int:
        return struct.unpack("<h", self.read_bytes(2))[0]

    def read_uint32(self) -> int:
        return struct.unpack("<I", self.read_bytes(4))[0]

    def read_int32(self) -> int:
        return struct.unpack("<i", self.read_bytes(4))[0]

    def read_single(self) -> float:
        return struct.unpack("<f", self.read_bytes(4))[0]

    def align(self, word_size: int = 4):
        """Skip padding bytes to the next word_size-aligned position.

        After the cipher has been applied, alignment is just cursor-arithmetic;
        no extra cipher-state needs to be advanced.
        """
        mod = self._pos % word_size
        if mod != 0:
            self._pos += word_size - mod


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


class Vector3:
    __slots__ = ("x", "y", "z")

    def __init__(self, x: float, y: float, z: float):
        self.x = x
        self.y = y
        self.z = z

    @classmethod
    def read(cls, r: _ByteReader) -> "Vector3":
        return cls(r.read_single(), r.read_single(), r.read_single())

    def __repr__(self):
        return f"Vector3({self.x}, {self.y}, {self.z})"


class Vector4:
    __slots__ = ("x", "y", "z", "w")

    def __init__(self, x: float, y: float, z: float, w: float):
        self.x = x
        self.y = y
        self.z = z
        self.w = w

    @classmethod
    def read(cls, r: _ByteReader) -> "Vector4":
        return cls(r.read_single(), r.read_single(), r.read_single(), r.read_single())

    def __repr__(self):
        return f"Vector4({self.x}, {self.y}, {self.z}, {self.w})"


class UV:
    __slots__ = ("u", "v")

    def __init__(self, u: float, v: float):
        self.u = u
        self.v = v

    @classmethod
    def read(cls, r: _ByteReader, file_version: int) -> "UV":
        # File versions 4-5 stored V before U (per I3DUV.cs).
        if 4 <= file_version <= 5:
            v = r.read_single()
            u = r.read_single()
            return cls(u, v)
        return cls(r.read_single(), r.read_single())

    def __repr__(self):
        return f"UV({self.u}, {self.v})"


class Triangle:
    """Triangle = three indices into the vertex array.

    Note (from I3DTri.cs): the file stores indices as (real_index - 1) and the
    reader adds +1 back. We keep that here for byte-for-byte compatibility
    with the C# tool; downstream code that wants 0-based Blender indices needs
    to subtract 1.
    """

    __slots__ = ("p1", "p2", "p3")

    def __init__(self, p1: int, p2: int, p3: int):
        self.p1 = p1
        self.p2 = p2
        self.p3 = p3

    @classmethod
    def read(cls, r: _ByteReader, is_int: bool) -> "Triangle":
        if is_int:
            return cls(r.read_uint32() + 1, r.read_uint32() + 1, r.read_uint32() + 1)
        return cls(r.read_uint16() + 1, r.read_uint16() + 1, r.read_uint16() + 1)

    def __repr__(self):
        return f"Tri({self.p1}, {self.p2}, {self.p3})"


class Subset:
    """Material subset descriptor inside a Shape.

    Each subset covers [firstVertex, firstVertex+numVertices) and
    [firstIndex, firstIndex+numIndices) — one subset per material.
    """

    __slots__ = ("first_vertex", "num_vertices", "first_index", "num_indices",
                 "uv_density1", "uv_density2", "uv_density3", "uv_density4")

    def __init__(self, fv: int, nv: int, fi: int, ni: int,
                 uvd1=None, uvd2=None, uvd3=None, uvd4=None):
        self.first_vertex = fv
        self.num_vertices = nv
        self.first_index = fi
        self.num_indices = ni
        self.uv_density1 = uvd1
        self.uv_density2 = uvd2
        self.uv_density3 = uvd3
        self.uv_density4 = uvd4

    @classmethod
    def read(cls, r: _ByteReader, file_version: int, options: ShapeOptions) -> "Subset":
        fv = r.read_uint32()
        nv = r.read_uint32()
        fi = r.read_uint32()
        ni = r.read_uint32()
        uvd1 = uvd2 = uvd3 = uvd4 = None
        if file_version >= 6:
            if options & ShapeOptions.HAS_UV1:
                uvd1 = r.read_single()
            if options & ShapeOptions.HAS_UV2:
                uvd2 = r.read_single()
            if options & ShapeOptions.HAS_UV3:
                uvd3 = r.read_single()
            if options & ShapeOptions.HAS_UV4:
                uvd4 = r.read_single()
        return cls(fv, nv, fi, ni, uvd1, uvd2, uvd3, uvd4)

    def __repr__(self):
        return (f"Subset(fv={self.first_vertex} nv={self.num_vertices} "
                f"fi={self.first_index} ni={self.num_indices})")


class Attachment:
    """Optional shape attachment (I3DShapeAttachment.cs)."""

    __slots__ = ("flags", "floats", "data")

    def __init__(self, flags: int, floats, data: bytes):
        self.flags = flags
        self.floats = floats
        self.data = data

    @classmethod
    def read(cls, r: _ByteReader) -> "Attachment":
        flags = r.read_uint32()
        floats = None
        if (flags & 4) != 0:
            floats = (r.read_single(), r.read_single(), r.read_single())
        num_bytes = r.read_int32()
        data = r.read_bytes(num_bytes)
        return cls(flags, floats, data)


# ---------------------------------------------------------------------------
# The Shape itself
# ---------------------------------------------------------------------------


class Shape:
    """Decoded I3D shape — vertices, indices, attributes, optional skin data.

    Field naming matches the Python convention; original C# field names noted
    in comments where relevant.
    """

    def __init__(self):
        # Header (I3DPart.cs)
        self.name = ""
        self.id = 0  # shape id (referenced by <Shape shapeId="..."> in the i3d XML)

        # Body
        self.bounding_volume = None  # Vector4
        self.subsets = []            # list[Subset]
        self.material_slot_names = []  # list[str] (only if file_version >= 10)

        # Per-vertex / per-index arrays
        self.triangles = []  # list[Triangle]
        self.positions = []  # list[Vector3]
        self.normals = None  # list[Vector3] | None
        self.tangents = None  # list[Vector4] | None
        self.uv_sets = [None, None, None, None]  # 4 optional UV channels
        self.vertex_colors = None  # list[Vector4] | None

        # Skin data
        self.blend_weights = None  # list[tuple[float,float,float,float]] | None
        self.blend_indices = None  # list[tuple[int,...]] (1 or 4 bytes per vertex) | None
        self.is_single_blend_weights = False

        # Other
        self.generic_data = None
        self.attachments = []
        self.options = ShapeOptions.NONE
        self.options_high_bits = 0
        self.vtx_compression = None  # only set for file_version >= 10
        self.unread_bytes = 0  # set if the reader did not consume all input

    @property
    def vertex_count(self) -> int:
        return len(self.positions)

    @property
    def triangle_count(self) -> int:
        return len(self.triangles)

    @property
    def has_skin(self) -> bool:
        return self.blend_indices is not None

    @property
    def is_merge_group(self) -> bool:
        """True for MergeGroup-style shapes (1 bone index per vertex)."""
        return self.has_skin and self.is_single_blend_weights

    @property
    def is_armature_skin(self) -> bool:
        """True for Armature-style shapes (4 weighted bones per vertex)."""
        return self.has_skin and not self.is_single_blend_weights

    def __repr__(self):
        kind = "?"
        if self.is_merge_group:
            kind = "MergeGroup"
        elif self.is_armature_skin:
            kind = "Armature"
        elif self.has_skin:
            kind = "Skin?"
        else:
            kind = "Plain"
        return (f"Shape(id={self.id}, name={self.name!r}, "
                f"verts={self.vertex_count}, tris={self.triangle_count}, "
                f"subsets={len(self.subsets)}, kind={kind})")


VERSION_WITH_TANGENTS = 5


def _read_part_header(r: _ByteReader):
    """Read the I3DPart header (name + id). Returns (name, id_)."""
    name_len = r.read_int32()
    name_bytes = r.read_bytes(name_len)
    try:
        name = name_bytes.decode("ascii")
    except UnicodeDecodeError:
        name = name_bytes.decode("latin-1")
    r.align(4)
    id_ = r.read_uint32()
    return name, id_


def parse_shape_entity(raw_entity, file_version: int) -> Shape:
    """Decode a RawEntity (entity_type=SHAPE) into a Shape.

    `file_version` is `ShapesFile.header.version`.
    """
    if raw_entity.entity_type.name != "SHAPE":
        raise ValueError(f"Expected SHAPE entity, got {raw_entity.entity_type.name}")

    r = _ByteReader(raw_entity.data)
    sh = Shape()
    sh.name, sh.id = _read_part_header(r)
    sh.file_version = file_version

    # ----- ReadContents (I3DShape.cs:84+) -----
    sh.bounding_volume = Vector4.read(r)
    corner_count = r.read_uint32()
    num_subsets = r.read_uint32()
    vertex_count = r.read_uint32()

    options_raw = r.read_uint32()
    # Only keep the known low bits in our enum; preserve high bits separately.
    sh.options = ShapeOptions(options_raw & int(ShapeOptions.ALL))
    sh.options_high_bits = options_raw & ~int(ShapeOptions.ALL)
    # noBindPose flag (high bit 0x80000000): when set, merge-group vertices
    # are stored in each bound node's local space (FS25-Giants convention).
    # When cleared / missing, the vertices are in the merge-group root's
    # space (FS22 v7 AND files produced by the Giants Blender exporter, which
    # never writes this flag). Authoritative discriminator for the root-space
    # -> bone-local correction (GitHub #2/#8); verified empirically against
    # four sample files (Giants FS25, FS22 v7, Blender-export v10, Blender
    # re-export v10).
    sh.no_bind_pose = bool(sh.options_high_bits & 0x80000000)

    # 4-byte slot before the Subsets list. The C# reference (I3DShape.cs:97)
    # only reads this for file_version >= 10 and names it 'vtxCompression'.
    # Empirical FS25 / FS22 base-game scan shows the slot ALSO exists in v9
    # (always zero, effectively padding); skipping it shifts every subsequent
    # field by 4 bytes and turns v9 MergedChildren/MergeGroup shapes into
    # silently corrupt geometry. v7 (FS19-era) does NOT have this slot.
    if file_version >= 9:
        sh.vtx_compression = r.read_single()

    sh.subsets = [Subset.read(r, file_version, sh.options) for _ in range(num_subsets)]

    if file_version >= 10:
        # Per-subset material slot name (uint16 length-prefixed). After each
        # name a 2-byte alignment pad is needed when the name length is odd,
        # otherwise the next uint16 read lands on an odd position and reads
        # garbage (silently breaks decoding for the rest of the shape).
        # Verified on FS25 puma.i3d.shapes: name #1 = 23 bytes, followed by a
        # single 0x00 pad before the next length prefix.
        # The C# reference tool has the same off-by-pad issue and silently
        # fails on these shapes.
        for _ in range(num_subsets):
            n = r.read_uint16()
            sname = r.read_bytes(n)
            try:
                sh.material_slot_names.append(sname.decode("ascii"))
            except UnicodeDecodeError:
                sh.material_slot_names.append(sname.decode("latin-1"))
            r.align(2)
        r.align(4)

    # v9 (FS22-era) inserts 4 bytes of padding here, BEFORE the triangle
    # index block. The C# reference (I3DShape.cs:122) reads triangles
    # straight after subsets and silently consumes the pad as the first
    # triangle's three uint16 indices, producing a degenerate (1,1,1)
    # leading triangle and shifting every subsequent triangle by one
    # vertex. Verified on props02.i3d.shapes MergedChildren2: without
    # this skip 25/66 triangles spanned MergeChildren-slots (was the
    # cross-slot warning in the importer). v7 and v10 do not have the
    # extra 4 bytes here.
    if file_version == 9:
        r.read_bytes(4)
    num_triangles = corner_count // 3
    is_int_index = vertex_count > (0xFFFF + 1)
    sh.triangles = [Triangle.read(r, is_int_index) for _ in range(num_triangles)]
    r.align(4)

    sh.positions = [Vector3.read(r) for _ in range(vertex_count)]

    if sh.options & ShapeOptions.HAS_NORMALS:
        sh.normals = [Vector3.read(r) for _ in range(vertex_count)]

    if sh.options & ShapeOptions.HAS_TANGENTS:
        if file_version >= VERSION_WITH_TANGENTS:
            sh.tangents = [Vector4.read(r) for _ in range(vertex_count)]
        else:
            # Older files reused the bit for something else; we don't decode it.
            sh.options_high_bits |= int(ShapeOptions.HAS_TANGENTS)

    for uv_set_idx in range(4):
        flag = ShapeOptions(int(ShapeOptions.HAS_UV1) << uv_set_idx)
        if sh.options & flag:
            sh.uv_sets[uv_set_idx] = [UV.read(r, file_version) for _ in range(vertex_count)]

    if sh.options & ShapeOptions.HAS_VERTEX_COLOR:
        sh.vertex_colors = [Vector4.read(r) for _ in range(vertex_count)]

    if sh.options & ShapeOptions.HAS_SKINNING_INFO:
        sh.is_single_blend_weights = bool(sh.options & ShapeOptions.SINGLE_BLEND_WEIGHTS)
        num_indices_per_vertex = 1 if sh.is_single_blend_weights else 4

        if not sh.is_single_blend_weights:
            sh.blend_weights = [
                (r.read_single(), r.read_single(), r.read_single(), r.read_single())
                for _ in range(vertex_count)
            ]

        sh.blend_indices = [
            tuple(r.read_uint8() for _ in range(num_indices_per_vertex))
            for _ in range(vertex_count)
        ]

    if sh.options & ShapeOptions.HAS_GENERIC:
        sh.generic_data = [r.read_single() for _ in range(vertex_count)]

    # Attachments block is optional in practice — some FS25 shapes (v10) end
    # exactly at or near the entity boundary with no attachments section.
    # The C# reference tool fails on those too. We catch the EOF and continue
    # so the rest of the shape data (which IS valid) reaches Blender.
    try:
        num_attachments = r.read_uint32()
        sh.attachments = [Attachment.read(r) for _ in range(num_attachments)]
    except EOFError:
        sh.attachments = []

    # v9 (FS22-era) appended a Post-Attachments-Block slot to MergedMesh /
    # MergedChildren shapes — 2x uint32 (typically count=0, flag=0). v10
    # dropped this slot. The 8-byte trailer is always all-zeros where
    # present; consuming it silently brings ~40+ v9 files to clean
    # unread_bytes=0 decode. Peek+check first so we don't accidentally eat
    # a v9 Convex-Hull-Trailer (starts with 0x6d 0x04 ..., see Bales) or
    # the sky-shape 8-byte trailer (starts with 0x9d 0x7e 0x4a 0x3a ...).
    if file_version == 9 and r.remaining >= 8:
        if r.peek_bytes(8) == b"\x00" * 8:
            r.read_bytes(8)

    sh.unread_bytes = r.remaining
    return sh


# ---------------------------------------------------------------------------
# Spline (entity types 2 = SPLINE, 6 = SPLINE_L)
# ---------------------------------------------------------------------------


class Spline:
    """Decoded I3D spline - list of 3D points plus form flag.

    Ports I3DShapesTool.Lib.Model.Spline. Same binary layout for both
    EntityType.SPLINE (cubic) and EntityType.SPLINE_L (linear); the kind
    distinction lives in the parent entity type, not in the payload.
    """

    def __init__(self):
        self.name = ""
        self.id = 0
        self.kind = ""             # "SPLINE" or "SPLINE_L" - set by parser
        self.form_closed = False   # UnknownFlags1: 0=open, 1=closed
        self.points = []           # list[Vector3]
        self.attr_flags = 0        # UnknownFlags2 (v>=10 only); !=0 means
                                   # per-point attributes follow (not yet decoded)
        self.unread_bytes = 0

    @property
    def point_count(self) -> int:
        return len(self.points)

    def __repr__(self):
        return (f"Spline(id={self.id}, name={self.name!r}, kind={self.kind}, "
                f"points={self.point_count}, "
                f"{'closed' if self.form_closed else 'open'})")


def parse_spline_entity(raw_entity, file_version: int) -> Spline:
    """Decode a RawEntity (entity_type=SPLINE or SPLINE_L) into a Spline.

    `file_version` is `ShapesFile.header.version`.
    """
    kind = raw_entity.entity_type.name
    if kind not in ("SPLINE", "SPLINE_L"):
        raise ValueError(f"Expected SPLINE/SPLINE_L entity, got {kind}")

    r = _ByteReader(raw_entity.data)
    sp = Spline()
    sp.name, sp.id = _read_part_header(r)
    sp.kind = kind

    # UnknownFlags1 (uint32): 0=open, 1=closed
    flags1 = r.read_uint32()
    sp.form_closed = bool(flags1 & 1)

    point_count = r.read_uint32()
    sp.points = [Vector3.read(r) for _ in range(point_count)]

    # v10+ has an extra UnknownFlags2 (uint32) for per-point attributes.
    # All known samples have it == 0; non-zero would mean per-point
    # attribute payload follows, which we don't decode yet.
    if file_version >= 10:
        sp.attr_flags = r.read_uint32()

    sp.unread_bytes = r.remaining
    return sp
