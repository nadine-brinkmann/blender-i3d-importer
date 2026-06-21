"""
Parser for the .i3d XML file (scene hierarchy, materials, files).

Returns values 1:1 as written in the XML (especially rotation in DEGREES).
The degrees -> radians conversion is done in the importer, since only there
the Blender context is known.

Phase C.1: hierarchy, transforms, material IDs, file refs only.
- Material sub-elements (Texture, Normalmap, ...) are added in Phase C.2.
- Inline shapes (<Shapes>...) are detected but not evaluated (Phase C.4).
- ReferenceNode is captured as its own node kind but not loaded recursively.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional, Tuple


# Node kinds we take from the <Scene> subtree.
# Everything else is skipped with a warning.
# Tags that are valid scene-tree nodes (children of <Scene>).
# TerrainTransformGroup is special: it's a leaf for our purposes — its XML
# children (<OccluderLods>, <Layers>, ...) are NOT scene-tree nodes but
# terrain-configuration sub-elements. _parse_node() does not recurse into
# them; the importer reads them via node.raw_attrs / via separate lookup.
VALID_KINDS = {'TransformGroup', 'Shape', 'Light', 'Camera', 'ReferenceNode',
               'Note', 'TerrainTransformGroup'}

# Kinds for which _parse_node() must NOT recurse into XML children, because
# those children are configuration data (handled by the importer separately),
# not scene-tree nodes.
_LEAF_KINDS = {'TerrainTransformGroup'}


@dataclass
class I3DTerrainLayer:
    """Ein einzelner Sub-Layer aus <Layers>/<Layer> innerhalb eines
    <TerrainTransformGroup>-Knotens."""
    name: str
    detail_map_id:       Optional[int] = None
    normal_map_id:       Optional[int] = None
    height_map_id:       Optional[int] = None
    displacement_map_id: Optional[int] = None
    weight_map_id:       Optional[int] = None
    unit_size:               float = 2.0    # tiling scale (m)
    displacement_max_height: float = 0.25
    blend_contrast:          float = 0.2
    raw_attrs: Dict[str, str] = field(default_factory=dict)  # Physik etc.


@dataclass
class I3DCombinedLayer:
    """Ein CombinedLayer aus <Layers>/<CombinedLayer>: kombiniert genau zwei
    Sub-Layer (z.B. 'asphalt01;asphalt02') ueber noise-basiertes Variant-
    Blending."""
    name: str
    sub_layer_names: List[str]     # gesplittet aus "asphalt01;asphalt02"
    noise_frequency: float = 2.0
    raw_attrs: Dict[str, str] = field(default_factory=dict)


@dataclass
class I3DSceneNode:
    """A node from the <Scene> subtree."""
    nodeId: int
    name: str
    kind: str                                              # one of VALID_KINDS
    translation: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation:    Tuple[float, float, float] = (0.0, 0.0, 0.0)   # in degrees (raw from XML)
    scale:       Tuple[float, float, float] = (1.0, 1.0, 1.0)
    shapeId:     Optional[int] = None                       # only for Shape
    materialIds: List[int] = field(default_factory=list)    # only for Shape
    children:    List['I3DSceneNode'] = field(default_factory=list)
    raw_attrs:   Dict[str, str] = field(default_factory=dict)  # everything else (for C.4)
    # Terrain-only: <Layers>-Sub-Element von <TerrainTransformGroup>.
    # Leer fuer alle anderen Node-Kinds. _parse_node fuellt diese fuer
    # TerrainTransformGroup-Knoten, _LEAF_KINDS verhindert dass die XML-
    # Kinder als Scene-Tree gelesen werden, deshalb hier separat.
    terrain_layers:          List['I3DTerrainLayer']  = field(default_factory=list)
    terrain_combined_layers: List['I3DCombinedLayer'] = field(default_factory=list)


@dataclass
class I3DAnimationKey:
    time: float
    translation: Optional[Tuple[float, float, float]] = None
    rotation: Optional[Tuple[float, float, float]] = None
    scale: Optional[Tuple[float, float, float]] = None
    raw_attrs: Dict[str, str] = field(default_factory=dict)


@dataclass
class I3DAnimationKeyframes:
    node_id: int
    keys: List[I3DAnimationKey] = field(default_factory=list)


@dataclass
class I3DAnimationClip:
    name: str
    duration: float = 0.0
    keyframes: List[I3DAnimationKeyframes] = field(default_factory=list)
    raw_attrs: Dict[str, str] = field(default_factory=dict)


@dataclass
class I3DAnimationSet:
    name: str
    clips: List[I3DAnimationClip] = field(default_factory=list)
    raw_attrs: Dict[str, str] = field(default_factory=dict)


@dataclass
class I3DScene:
    """Parsed i3d file."""
    roots: List[I3DSceneNode] = field(default_factory=list)
    materials: Dict[int, Dict[str, str]] = field(default_factory=dict)
    # materialId -> dict of all attributes of the <Material> tag.
    # Sub-elements (Texture, Normalmap, CustomParameter, ...) 

    files: Dict[int, str] = field(default_factory=dict)
    # fileId -> filename (path as written in the XML, often with $data variable).

    has_inline_shapes: bool = False
    external_shapes_file: Optional[str] = None
    # If has_inline_shapes is True, mesh data is embedded in the XML directly
    # and external_shapes_file is None. Importer does not support this
    # (will abort or warn).

    user_attributes: Dict[int, List[Tuple[str, str, str]]] = field(default_factory=dict)
    # nodeId -> list of (name, type, value_str) tuples from <UserAttributes>.
    # type is one of: boolean, integer, float, string, scriptCallback.
    # value_str is the raw XML value; conversion happens in the importer.

    external_anim_file: Optional[str] = None
    animation_sets: List[I3DAnimationSet] = field(default_factory=list)
    # Inline <Animation><AnimationSets> data, when the i3d is not split into
    # a binary externalAnimFile.


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_i3d(filepath: Path) -> I3DScene:
    """Parse an .i3d XML file and return an I3DScene."""
    tree = ET.parse(str(filepath))
    root = tree.getroot()  # <i3D>

    scene = I3DScene()

    files_elem     = root.find('Files')
    materials_elem = root.find('Materials')
    shapes_elem    = root.find('Shapes')
    scene_elem     = root.find('Scene')
    animation_elem = root.find('Animation')

    if files_elem is not None:
        for f in files_elem.findall('File'):
            file_id_str = f.get('fileId')
            filename    = f.get('filename')
            if file_id_str is None or filename is None:
                continue
            try:
                scene.files[int(file_id_str)] = filename
            except ValueError:
                print(f"[i3d_xml_parser] Warning: invalid fileId '{file_id_str}'")

    if materials_elem is not None:
        for m in materials_elem.findall('Material'):
            mat_id_str = m.get('materialId')
            if mat_id_str is None:
                continue
            try:
                mat_id = int(mat_id_str)
            except ValueError:
                print(f"[i3d_xml_parser] Warning: invalid materialId '{mat_id_str}'")
                continue

            mat_dict: Dict[str, object] = dict(m.attrib)

            # Sub-elements: Texture, Normalmap, Glossmap (with fileId).
            # Underscore prefix avoids collision with real XML attributes.
            for sub_tag, key in (('Texture',   '_texture_fileId'),
                                 ('Normalmap', '_normalmap_fileId'),
                                 ('Glossmap',  '_glossmap_fileId')):
                sub = m.find(sub_tag)
                if sub is not None:
                    file_id_str = sub.get('fileId')
                    if file_id_str is not None:
                        try:
                            mat_dict[key] = int(file_id_str)
                        except ValueError:
                            print(f"[i3d_xml_parser] Warning: invalid fileId '{file_id_str}' "
                                  f"in <{sub_tag}> of material {mat_id}")

            # Normalmap-specific: bumpDepth attribute (float, GE default 1.0).
            # Passed through to ShaderNodeNormalMap.Strength in the re-export material.
            nm_sub = m.find('Normalmap')
            if nm_sub is not None:
                bd_str = nm_sub.get('bumpDepth')
                if bd_str is not None:
                    try:
                        mat_dict['_normalmap_bumpDepth'] = float(bd_str)
                    except ValueError:
                        print(f"[i3d_xml_parser] Warning: invalid bumpDepth "
                              f"'{bd_str}' in <Normalmap> of material {mat_id}")

            # Custom maps: list of all <Custommap> sub-elements (for Phase C.4).
            custommaps = [dict(cm.attrib) for cm in m.findall('Custommap')]
            if custommaps:
                mat_dict['_custommaps'] = custommaps

            # CustomParameter: list of all <CustomParameter> sub-elements.
            # Each entry: {"name": "<param_name>", "value": "<value_as_string>"}.
            # Value can be 1-4 floats (e.g. "0.5" or "0.0603 0.2059 0.0300") -
            # passed through 1:1 as string; the Giants exporter formats on re-export.
            customparameters = [dict(cp.attrib) for cp in m.findall('CustomParameter')]
            if customparameters:
                mat_dict['_customparameters'] = customparameters

            scene.materials[mat_id] = mat_dict

    if shapes_elem is not None:
        ext = shapes_elem.get('externalShapesFile')
        if ext:
            scene.external_shapes_file = ext
        # Direct child elements present? Then inline shape data is contained.
        if len(list(shapes_elem)) > 0:
            scene.has_inline_shapes = True

    if scene_elem is not None:
        for child in scene_elem:
            node = _parse_node(child)
            if node is not None:
                scene.roots.append(node)

    if animation_elem is not None:
        scene.external_anim_file = animation_elem.get('externalAnimFile')
        scene.animation_sets = _parse_animation_sets(animation_elem)

    # <UserAttributes> sits at root level, after <Scene>. Each <UserAttribute
    # nodeId="N"> contains one or more <Attribute name="X" type="Y" value="Z"/>.
    user_attrs_elem = root.find('UserAttributes')
    if user_attrs_elem is not None:
        for ua in user_attrs_elem.findall('UserAttribute'):
            nid_str = ua.get('nodeId')
            if nid_str is None:
                continue
            try:
                nid = int(nid_str)
            except ValueError:
                continue
            attrs = []
            for a in ua.findall('Attribute'):
                a_name = a.get('name')
                a_type = a.get('type')
                a_value = a.get('value')
                if a_name is None or a_type is None or a_value is None:
                    continue
                attrs.append((a_name, a_type, a_value))
            if attrs:
                scene.user_attributes[nid] = attrs

    return scene


def _parse_animation_sets(animation_elem: ET.Element) -> List[I3DAnimationSet]:
    sets: List[I3DAnimationSet] = []
    sets_elem = animation_elem.find('AnimationSets')
    if sets_elem is None:
        return sets

    for set_elem in sets_elem.findall('AnimationSet'):
        anim_set = I3DAnimationSet(
            name=set_elem.get('name', ''),
            raw_attrs=dict(set_elem.attrib),
        )
        for clip_elem in set_elem.findall('Clip'):
            clip = I3DAnimationClip(
                name=clip_elem.get('name', ''),
                duration=_to_float(clip_elem.get('duration'), default=0.0),
                raw_attrs=dict(clip_elem.attrib),
            )
            for keyframes_elem in clip_elem.findall('Keyframes'):
                node_id = _to_int(keyframes_elem.get('nodeId'), default=-1)
                keyframes = I3DAnimationKeyframes(node_id=node_id)
                for key_elem in keyframes_elem.findall('Keyframe'):
                    raw_attrs = dict(key_elem.attrib)
                    keyframes.keys.append(I3DAnimationKey(
                        time=_to_float(key_elem.get('time'), default=0.0),
                        translation=_parse_optional_vec3(key_elem.get('translation')),
                        rotation=_parse_optional_vec3(key_elem.get('rotation')),
                        scale=_parse_optional_vec3(key_elem.get('scale')),
                        raw_attrs=raw_attrs,
                    ))
                clip.keyframes.append(keyframes)
            anim_set.clips.append(clip)
        sets.append(anim_set)
    return sets


def _parse_node(elem: ET.Element) -> Optional[I3DSceneNode]:
    """Recursive. Unknown tags -> None (skip, with print warning)."""
    kind = elem.tag
    if kind not in VALID_KINDS:
        print(f"[i3d_xml_parser] Skipping unknown scene tag: <{kind}>")
        return None

    name      = elem.get('name', '')
    node_id   = _to_int(elem.get('nodeId'), default=-1)
    shape_id  = _to_int(elem.get('shapeId'), default=None)

    translation = _parse_vec3(elem.get('translation'), default=(0.0, 0.0, 0.0))
    rotation    = _parse_vec3(elem.get('rotation'),    default=(0.0, 0.0, 0.0))
    scale       = _parse_vec3(elem.get('scale'),       default=(1.0, 1.0, 1.0))

    material_ids: List[int] = []
    mat_str = elem.get('materialIds')
    if mat_str:
        # Giants uses whitespace OR commas as separator depending on the shape.
        # Examples:  materialIds="48"  |  materialIds="19 20"  |  materialIds="2,3,4,5,6,7"
        for tok in mat_str.replace(',', ' ').split():
            v = _to_int(tok, default=None)
            if v is not None:
                material_ids.append(v)

    # raw_attrs: all attributes except the ones explicitly read above
    consumed = {'name', 'nodeId', 'shapeId', 'translation', 'rotation', 'scale', 'materialIds'}
    raw_attrs = {k: v for k, v in elem.attrib.items() if k not in consumed}

    node = I3DSceneNode(
        nodeId      = node_id,
        name        = name,
        kind        = kind,
        translation = translation,
        rotation    = rotation,
        scale       = scale,
        shapeId     = shape_id,
        materialIds = material_ids,
        raw_attrs   = raw_attrs,
    )

    # TerrainTransformGroup-spezifisch: <Layers>-Sub-Element extrahieren.
    # _LEAF_KINDS verhindert dass das als Scene-Tree gelesen wird, deshalb
    # hier explizit. <CombinedOverlayLayer> erstmal ausgeklammert (groundDetail-
    # Overlays - kommen ggf. spaeter, nicht im PoC-Scope).
    if kind == 'TerrainTransformGroup':
        layers_elem = elem.find('Layers')
        if layers_elem is not None:
            for layer_elem in layers_elem.findall('Layer'):
                node.terrain_layers.append(_parse_terrain_layer(layer_elem))
            for combined_elem in layers_elem.findall('CombinedLayer'):
                node.terrain_combined_layers.append(_parse_combined_layer(combined_elem))

    # Leaf kinds (e.g. TerrainTransformGroup) do not recurse — their XML
    # children are config sub-elements (OccluderLods/Layers/...), handled
    # by the importer separately, not part of the scene tree.
    if kind not in _LEAF_KINDS:
        for child in elem:
            child_node = _parse_node(child)
            if child_node is not None:
                node.children.append(child_node)

    return node


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_vec3(s, default):
    """'x y z' -> (x,y,z); s empty/None/invalid -> default."""
    if not s:
        return default
    parts = s.split()
    if len(parts) < 3:
        return default
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return default


def _parse_optional_vec3(s):
    if not s:
        return None
    return _parse_vec3(s, default=None)


def _to_int(s, default=0):
    if s is None:
        return default
    try:
        return int(str(s).strip(), 0)
    except (ValueError, TypeError):
        return default


def _to_float(s, default):
    if s is None:
        return default
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _parse_terrain_layer(elem: ET.Element) -> I3DTerrainLayer:
    """Parst ein einzelnes <Layer>-Element aus <TerrainTransformGroup>/<Layers>."""
    a = elem.attrib
    consumed = {'name', 'detailMapId', 'normalMapId', 'heightMapId',
                'displacementMapId', 'weightMapId', 'unitSize',
                'displacementMaxHeight', 'blendContrast'}
    return I3DTerrainLayer(
        name                    = a.get('name', ''),
        detail_map_id           = _to_int(a.get('detailMapId'),       default=None),
        normal_map_id           = _to_int(a.get('normalMapId'),       default=None),
        height_map_id           = _to_int(a.get('heightMapId'),       default=None),
        displacement_map_id     = _to_int(a.get('displacementMapId'), default=None),
        weight_map_id           = _to_int(a.get('weightMapId'),       default=None),
        unit_size               = _to_float(a.get('unitSize'),               2.0),
        displacement_max_height = _to_float(a.get('displacementMaxHeight'),  0.25),
        blend_contrast          = _to_float(a.get('blendContrast'),          0.2),
        raw_attrs               = {k: v for k, v in a.items() if k not in consumed},
    )


def _parse_combined_layer(elem: ET.Element) -> I3DCombinedLayer:
    """Parst ein einzelnes <CombinedLayer>-Element aus <TerrainTransformGroup>/<Layers>."""
    a = elem.attrib
    consumed = {'name', 'layers', 'noiseFrequency'}
    layers_str = a.get('layers', '')
    sub_names = [s.strip() for s in layers_str.split(';') if s.strip()]
    return I3DCombinedLayer(
        name             = a.get('name', ''),
        sub_layer_names  = sub_names,
        noise_frequency  = _to_float(a.get('noiseFrequency'), 2.0),
        raw_attrs        = {k: v for k, v in a.items() if k not in consumed},
    )
