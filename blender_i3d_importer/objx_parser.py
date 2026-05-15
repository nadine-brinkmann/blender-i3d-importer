"""
Parser for the OBJx format produced by i3d-to-objx (forked from VidhosticeSDK's I3DShapesTool-OBJx).

OBJx extends standard OBJ with:
  v X Y Z [R G B A]   - position + optional vertex color with alpha
  vt U V              - UV map 1
  vt2 U V             - UV map 2
  vt3 U V             - UV map 3
  vt4 U V             - UV map 4
  vn X Y Z            - normals
  usemtl <N>          - 1-based local subset index for the following faces
  f v/vt/vn ...       - face (1-based indices like OBJ)
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple


@dataclass
class MeshData:
    """A parsed OBJx file as raw lists."""
    name: str = ""
    vertices: List[Tuple[float, float, float]] = field(default_factory=list)
    uvs: List[Tuple[float, float]] = field(default_factory=list)
    normals: List[Tuple[float, float, float]] = field(default_factory=list)
    # faces: each face is a list of (vertex_idx, uv_idx, normal_idx) tuples,
    # 0-based; uv_idx/normal_idx may be None.
    faces: List[List[Tuple[int, int, int]]] = field(default_factory=list)
    # face_subsets: per face the 0-based subset index from the current usemtl
    # range. Faces before any usemtl line get subset 0 (default).
    face_subsets: List[int] = field(default_factory=list)
    # Number of subsets found (= max(face_subsets) + 1, or 0 if no faces).
    num_subsets: int = 0

    # Multi-UV: parallel to uvs (the same vt index in an f line applies to all
    # UV maps). In OBJx, vt, vt2, vt3, vt4 each have the same number of entries
    # when present.
    uvs2: List[Tuple[float, float]] = field(default_factory=list)
    uvs3: List[Tuple[float, float]] = field(default_factory=list)
    uvs4: List[Tuple[float, float]] = field(default_factory=list)

    # Vertex colors (RGBA): parallel to vertices, coming from the optional
    # 4 values in the v line. Either complete (len == len(vertices)) or empty.
    vertex_colors: List[Tuple[float, float, float, float]] = field(default_factory=list)


def parse(filepath: Path) -> MeshData:
    """Read an OBJx file and return it as MeshData."""
    md = MeshData(name=filepath.stem)
    current_subset = 0   # default for faces before any usemtl line

    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for line_num, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            cmd = parts[0]

            if cmd == 'v':
                # v X Y Z (with optional 4 extra values for vertex color RGBA in OBJx)
                if len(parts) < 4:
                    continue
                md.vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                if len(parts) >= 8:
                    try:
                        md.vertex_colors.append((
                            float(parts[4]), float(parts[5]),
                            float(parts[6]), float(parts[7]),
                        ))
                    except ValueError:
                        # Defensive: non-numeric color values -> ignore.
                        # Result: vertex_colors length will not match vertices,
                        # which the importer consistently discards.
                        pass

            elif cmd == 'vt':
                # vt U V (UV map 1)
                if len(parts) < 3:
                    continue
                md.uvs.append((float(parts[1]), float(parts[2])))

            elif cmd == 'vt2':
                if len(parts) < 3:
                    continue
                md.uvs2.append((float(parts[1]), float(parts[2])))

            elif cmd == 'vt3':
                if len(parts) < 3:
                    continue
                md.uvs3.append((float(parts[1]), float(parts[2])))

            elif cmd == 'vt4':
                if len(parts) < 3:
                    continue
                md.uvs4.append((float(parts[1]), float(parts[2])))

            elif cmd == 'vn':
                if len(parts) < 4:
                    continue
                md.normals.append((float(parts[1]), float(parts[2]), float(parts[3])))

            elif cmd == 'usemtl':
                # 1-based local subset index -> store 0-based
                if len(parts) >= 2:
                    try:
                        current_subset = int(parts[1]) - 1
                        if current_subset + 1 > md.num_subsets:
                            md.num_subsets = current_subset + 1
                    except ValueError:
                        # Non-numeric usemtl value (emergency robustness) - ignore
                        pass

            elif cmd == 'f':
                # f v/vt/vn v/vt/vn v/vt/vn  (1-based)
                # Also "f v//vn" or "f v" possible
                face_indices: List[Tuple[int, int, int]] = []
                for ref in parts[1:]:
                    fields_ = ref.split('/')
                    v_idx = int(fields_[0]) - 1 if fields_[0] else None
                    vt_idx = int(fields_[1]) - 1 if len(fields_) > 1 and fields_[1] else None
                    vn_idx = int(fields_[2]) - 1 if len(fields_) > 2 and fields_[2] else None
                    face_indices.append((v_idx, vt_idx, vn_idx))
                if len(face_indices) >= 3:
                    md.faces.append(face_indices)
                    md.face_subsets.append(current_subset)

            # g, s, o, mtllib etc. are intentionally ignored (irrelevant for mesh import).

    return md
