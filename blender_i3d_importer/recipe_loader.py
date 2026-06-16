"""
Recipe loader for PBR debug materials.

Loads node-group snippets from the master blend (fs25_node_snippets.blend) and
combines them into a visually more accurate debug material per i3d material.

Architecture:
  - The master blend lives in the add-on subfolder lib.
  - Snippets are shader NodeTree datablocks (node groups) with documented
    sockets - user-maintainable.
  - Append logic with per-import cache: each snippet is appended only once.

Fallback strategy:
  - Standard maps (BaseColor, Normalmap, Glossmap) are wired up via snippets
    (Glossmap -> fs25_Specular_smooth_AO_metallic -> BSDF Roughness + Metallic).
  - Custom maps are CREATED as image-texture nodes (label = custom-map name)
    and then wired with a snippet or proammatic logic if known purpose.
    If unknown purpose (or not possible in Blender), added unwired.

Naming convention:
  Debug material:  <mat_name>_pbr_debug
  use_fake_user=True, NOT attached to the mesh by default 
  - lives only in bpy.data.materials.
"""

from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import bpy

from . import i3d_shader_parser


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SNIPPETS_BLEND_FILENAME = "fs25_node_snippets.blend"

# Snippet IDs - must match the node-group names in the master blend.
SN_SPECULAR_METALLIC      = "fs25_Specular_smooth_AO_metallic"
SN_SPECULAR_DIRTMASK      = "fs25_Specular_smooth_AO_dirtmask"
SN_SPECULAR_SNOWINTENSITY = "fs25_Specular_smooth_AO_snowintensity"
SN_ALPHAMAP_UNPACK        = "fs25_AlphaMap_unpack"
SN_DIRT                   = "fs25_Compositing_Dirt"
SN_MOSS                   = "fs25_Compositing_Moss"
SN_AO_OVERLAY             = "fs25_AO_overlay"
SN_PARALLAX               = "fs25_Parallax"
SN_SNOW                   = "fs25_Snow_overlay"
SN_PLACEABLE_TINT         = "fs25_PlaceableColorTint"
SN_COLOR_MULTITINT        = "fs25_ColorShader_Multitint"
SN_BUILDING_MULTITINT     = "fs25_BuildingShader_Multitint"
SN_VEHICLE_TRIPLANAR      = "fs25_VehicleTriplanar"
SN_VEHICLE_MASKS          = "fs25_VehicleMasks"

# Texture names that go through tri-planar projection for vehicleShader
# Passed to _make_image_chain as triplanar_texture_names.
VEHICLE_TRIPLANAR_TEXTURES = {'detailDiffuse', 'detailNormal', 'detailSpecular',
                              'dirtDiffuse',   'dirtNormal',   'dirtSpecular'}

# Default paths for FS25 vehicleShader dirt textures (from vehicleShader.xml
# lines 73-76). Loaded in block 7g as fallback when the user has not set the
# texture themselves as customTexture_* in the i3d material.
VEHICLE_DIRT_DEFAULT_PATHS = {
    'dirtDiffuse':  '$data/shared/detailLibrary/nonMetallic/dirt_diffuse.png',
    'dirtNormal':   '$data/shared/detailLibrary/nonMetallic/dirt_normal.png',
    'dirtSpecular': '$data/shared/detailLibrary/nonMetallic/dirt_specular.png',
}

# Default snippet for the standard glossmap. Across all relevant map-building
# shaders B=metallic is the most common case
DEFAULT_GLOSSMAP_SNIPPET = SN_SPECULAR_METALLIC

# Mapping Giants uvType (from shader XML <UvUsage uvType="...">) -> Blender layer name.
# For 'worldspace' we build a triplanar UV source from Geometry.Position
# (worldspace) + Geometry.Normal (worldspace), fed into the
# fs25_VehicleTriplanar snippet - see _make_worldspace_triplanar_uv().
# 'custom' = shader-internal computed coords - fallback to UVMap + warning.
GIANTS_UV_TO_BLENDER_UV = {
    'uv0': 'UVMap',
    'uv1': 'UV2',
    'uv2': 'UV3',
    'uv3': 'UV4',
}


def default_snippets_blend_path() -> Path:
    """Default path relative to the add-on module. Overridden by the add-on or
    the preference when the user moves the master blend elsewhere.
    """
    return Path(__file__).resolve().parent / SNIPPETS_BLEND_FILENAME


# ---------------------------------------------------------------------------
# Snippet append logic with cache
# ---------------------------------------------------------------------------

def append_node_group(name: str, blend_path: Path,
                      cache: Dict[str, Optional[bpy.types.NodeTree]],
                      report: Callable) -> Optional[bpy.types.NodeTree]:
    """Load a node group from blend_path. Per-import cached.

    Returns:
        bpy.types.NodeTree, or None on error (file missing, group missing).
        A None result is also cached so the load is not retried for every
        material.
    """
    if name in cache:
        return cache[name]

    # If already in the current blend (e.g. from an earlier import): use directly.
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        cache[name] = existing
        return existing

    if not blend_path.exists():
        report('WARNING',
               f"Master blend not found at:\n  {blend_path}\n"
               f"PBR debug materials will be built with skeleton fallback (no snippets).")
        cache[name] = None
        return None

    # bpy.data.libraries.load is the low-level append/link API from Python.
    # bpy.ops.wm.append needs an operator context (poll()) and fails when called
    # from the importer without UI context.
    try:
        with bpy.data.libraries.load(str(blend_path), link=False) as (data_from, data_to):
            if name not in data_from.node_groups:
                report('WARNING',
                       f"Snippet '{name}' not found in master blend:\n  {blend_path}")
                cache[name] = None
                return None
            data_to.node_groups = [name]
    except Exception as e:
        report('WARNING', f"Loading master blend failed: {e}")
        cache[name] = None
        return None

    appended = bpy.data.node_groups.get(name)
    if appended is None:
        report('WARNING',
               f"Snippet '{name}' not found in bpy.data.node_groups after append")
        cache[name] = None
        return None
    cache[name] = appended
    return appended


# ---------------------------------------------------------------------------
# Helper: worldspace -> triplanar UV source
# ---------------------------------------------------------------------------

def _make_worldspace_triplanar_uv(nt, snippets_blend_path, snippet_cache,
                                  report, location, label_suffix=""):
    """Build a worldspace-position-based triplanar UV source.

    For textures with uvType="worldspace" (e.g. dirt/moss/snow overlays in
    colorShader / buildingShader). Uses Geometry.Position + Geometry.Normal
    (both in world space) as inputs into the fs25_VehicleTriplanar snippet.

    Properties:
      - static in world space (does NOT follow camera or object motion),
      - unwarped (independent of the mesh's UV layout),
      - consistent world-space tile size (same texture density everywhere).

    Fallback when the snippet is missing: TexCoord.outputs['Object'] (better
    than 'Camera', because it does not follow the camera). The user will see
    a warning from append_node_group then.

    Args:
        nt:                  shader node tree.
        snippets_blend_path: path to fs25_node_snippets.blend.
        snippet_cache:       per-import snippet cache (dict).
        report:              callable(level, msg).
        location:            (x, y) for the triplanar group node.
        label_suffix:        optional extra text for the node labels.

    Returns:
        NodeSocket - the UV output that should be wired into a Mapping
        node (or directly into a texture input).
    """
    suffix = f" {label_suffix}" if label_suffix else ""
    tp_ng = append_node_group(SN_VEHICLE_TRIPLANAR, snippets_blend_path,
                              snippet_cache, report)
    geo = nt.nodes.new('ShaderNodeNewGeometry')
    geo.location = (location[0] - 300, location[1] - 80)
    geo.label = f"Worldspace geometry{suffix}"

    if tp_ng is not None:
        tp_grp = nt.nodes.new('ShaderNodeGroup')
        tp_grp.node_tree = tp_ng
        tp_grp.location = location
        tp_grp.label = f"Worldspace triplanar{suffix}"
        nt.links.new(geo.outputs['Position'], tp_grp.inputs['Position'])
        nt.links.new(geo.outputs['Normal'],   tp_grp.inputs['Normal'])
        return tp_grp.outputs['UV']

    # Snippet missing -> use the worldspace position directly as a 3D vector.
    # Better than TexCoord.Camera (which moves with the camera).
    return geo.outputs['Position']


# ---------------------------------------------------------------------------
# Helper: create image texture node
# ---------------------------------------------------------------------------

def _add_image_tex(nt, image, location, non_color=False, label=""):
    """Image texture node with image and optional label."""
    node = nt.nodes.new('ShaderNodeTexImage')
    node.image = image
    node.location = location
    if label:
        node.label = label
    if non_color and image is not None:
        try:
            image.colorspace_settings.name = 'Non-Color'
        except Exception:
            pass
    return node


def _parse_vec4(s, default):
    if not s:
        return default
    parts = str(s).split()
    if len(parts) < 4:
        return default
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# FS25 custom-parameter Value/RGB nodes (used by the PBR debug material).
#
# Each custom parameter from the i3d XML is materialised as a labeled
# Value/RGB node with `node.name = "fs25_param:<paramname>"`. The N-Panel
# scans the active material for that prefix and draws a slider/color picker
# on the node's default_value socket - changes propagate live through the
# link to the consumer input.
#
# Multi-component parameters (e.g. dirtMossMix is float2) get split into
# `<param>_x`, `<param>_y`, etc. following the i3d XML component order.
# The label keeps the semantic name ("Moss Intensity", "Dirt Intensity").
# ---------------------------------------------------------------------------

PARAM_FRAME_NAME = "FS25_Param_Frame"


def _ensure_param_frame(nt):
    """Return the existing 'FS25 Material Parameters' frame node or create it."""
    existing = nt.nodes.get(PARAM_FRAME_NAME)
    if existing is not None:
        return existing
    frame = nt.nodes.new('NodeFrame')
    frame.name = PARAM_FRAME_NAME
    frame.label = "FS25 Material Parameters"
    frame.use_custom_color = True
    frame.color = (0.30, 0.40, 0.55)
    return frame


def _make_param_value(nt, param_name, default_value, location,
                      label=None, target_input=None,
                      xml_param=None, xml_slot=None):
    """Labeled Value node for a single FS25 custom-parameter scalar.

    Marker: node.name = f"fs25_param:{param_name}" so the N-Panel can find
    it. When `target_input` is given, the Value output is linked to it -
    changing the slider updates the shader live.
    """
    node = nt.nodes.new('ShaderNodeValue')
    node.name = f"fs25_param:{param_name}"
    node.label = label or param_name
    node.outputs[0].default_value = float(default_value)
    node.location = location
    node.parent = _ensure_param_frame(nt)
    # Serialize hint for FS25_OT_sync_debug_to_export_material:
    # the slider value goes back to the customParameter_* IDProperty
    # as a plain float string.
    node['fs25_serialize'] = 'float'
    # Sync grouping hints. xml_param = the XML <CustomParameter name>;
    # xml_slot = which slice of its value this slider drives ('all'
    # for non-split, 'rgb'/'w'/'x'/'y'/'z'/'alpha' for split). Defaults
    # mean: this slider equals the whole XML param.
    node['fs25_xml_param'] = xml_param if xml_param is not None else param_name
    node['fs25_xml_slot'] = xml_slot if xml_slot is not None else 'all'
    if target_input is not None:
        nt.links.new(node.outputs[0], target_input)
    return node


def _make_param_rgb(nt, param_name, default_rgba, location,
                    label=None, target_input=None,
                    xml_param=None, xml_slot=None):
    """Labeled RGB node for an FS25 custom-parameter color (3 or 4 floats).

    If only 3 components are given, alpha defaults to 1.0.
    """
    if len(default_rgba) == 3:
        rgba = (float(default_rgba[0]), float(default_rgba[1]),
                float(default_rgba[2]), 1.0)
    else:
        rgba = tuple(float(c) for c in default_rgba[:4])
    node = nt.nodes.new('ShaderNodeRGB')
    node.name = f"fs25_param:{param_name}"
    node.label = label or param_name
    node.outputs[0].default_value = rgba
    node.location = location
    node.parent = _ensure_param_frame(nt)
    # Serialize hint for FS25_OT_sync_debug_to_export_material:
    # the slider value goes back to the customParameter_* IDProperty
    # as 4 space-separated floats 'r g b a'.
    node['fs25_serialize'] = 'rgba'
    # Sync grouping hints (see _make_param_value for details).
    node['fs25_xml_param'] = xml_param if xml_param is not None else param_name
    node['fs25_xml_slot'] = xml_slot if xml_slot is not None else 'all'
    if target_input is not None:
        nt.links.new(node.outputs[0], target_input)
    return node


def _make_param_value_inverted(nt, param_name, default_value, location,
                               label=None, target_input=None):
    """Value + Subtract(1-x) chain. Used for clearCoatSmoothness -> BSDF
    Coat Roughness (Roughness = 1 - Smoothness). The slider exposes the
    i3d semantic (smoothness)."""
    val = _make_param_value(nt, param_name, default_value, location, label)
    # The slider exposes the i3d-semantic value (e.g. smoothness),
    # but the shader uses 1-x internally (e.g. coat roughness).
    # Override the serialize hint set by _make_param_value to reflect
    # that the customParameter_* value is the slider value AS-IS
    # (NOT 1-x); the in-shader inversion is purely a visualization.
    val['fs25_serialize'] = 'float'
    sub = nt.nodes.new('ShaderNodeMath')
    sub.operation = 'SUBTRACT'
    sub.inputs[0].default_value = 1.0
    sub.location = (location[0] + 200, location[1])
    sub.label = f"1 - {label or param_name}"
    nt.links.new(val.outputs[0], sub.inputs[1])
    if target_input is not None:
        nt.links.new(sub.outputs[0], target_input)
    return val


# ---------------------------------------------------------------------------
# FS25 Debug Switch: route final material output through a
# switchable bypass that can show the BSDF, a mask image, or vertex colors.
#
# Architecture:
#   BSDF --> Switch group --> Material Output
#               ^    ^
#         mask_image  vertex_color   mode (Value 0/1/2)
#
# The group itself is a SINGLE shared node tree (created lazily on first
# use). Each material instantiates it as a ShaderNodeGroup plus three
# control nodes named fs25_debug:* (mode, mask_image, vertex_color)
# that the N-Panel manipulates.
# ---------------------------------------------------------------------------

DEBUG_SWITCH_TREE_NAME = "FS25_DebugSwitch"


def _ensure_debug_switch_tree():
    """Create or retrieve the FS25_DebugSwitch shared node tree.

    Logic implemented inside the tree:
        is_debug = SourceMode > 0.5
        is_vc    = SourceMode > 1.5
        debug_color = mix(is_vc, MaskImage, VertexColor)
        debug_shader = Emission(debug_color)
        output = mix_shader(is_debug, Surface, debug_shader)

    So:
        mode = 0  -> Surface (BSDF) passes through unchanged
        mode = 1  -> Emission with MaskImage color
        mode = 2  -> Emission with VertexColor
    """
    tree = bpy.data.node_groups.get(DEBUG_SWITCH_TREE_NAME)
    if tree is not None:
        return tree

    tree = bpy.data.node_groups.new(DEBUG_SWITCH_TREE_NAME, 'ShaderNodeTree')

    # Interface (Blender 4.0+ API). Sockets defined here automatically
    # appear on any NodeGroupInput/Output node we add below.
    tree.interface.new_socket("Surface", in_out='INPUT',
                              socket_type='NodeSocketShader')
    tree.interface.new_socket("Mask Image", in_out='INPUT',
                              socket_type='NodeSocketColor')
    tree.interface.new_socket("Vertex Color", in_out='INPUT',
                              socket_type='NodeSocketColor')
    tree.interface.new_socket("Source Mode", in_out='INPUT',
                              socket_type='NodeSocketFloat')
    tree.interface.new_socket("Surface", in_out='OUTPUT',
                              socket_type='NodeSocketShader')

    nodes = tree.nodes
    links = tree.links

    grp_in = nodes.new('NodeGroupInput')
    grp_in.location = (-600, 0)
    grp_out = nodes.new('NodeGroupOutput')
    grp_out.location = (400, 0)

    # is_debug = mode > 0.5
    is_debug = nodes.new('ShaderNodeMath')
    is_debug.operation = 'GREATER_THAN'
    is_debug.inputs[1].default_value = 0.5
    is_debug.location = (-400, -120)
    is_debug.label = "is_debug"
    links.new(grp_in.outputs['Source Mode'], is_debug.inputs[0])

    # is_vc = mode > 1.5
    is_vc = nodes.new('ShaderNodeMath')
    is_vc.operation = 'GREATER_THAN'
    is_vc.inputs[1].default_value = 1.5
    is_vc.location = (-400, -240)
    is_vc.label = "is_vc"
    links.new(grp_in.outputs['Source Mode'], is_vc.inputs[0])

    # MixColor: factor=is_vc, A=MaskImage, B=VertexColor.
    # ShaderNodeMix with data_type='RGBA' uses inputs[6]=A, inputs[7]=B,
    # outputs[2]=Result.
    color_mix = nodes.new('ShaderNodeMix')
    color_mix.data_type = 'RGBA'
    color_mix.location = (-200, 120)
    color_mix.label = "Mask vs VC"
    links.new(is_vc.outputs[0], color_mix.inputs['Factor'])
    links.new(grp_in.outputs['Mask Image'], color_mix.inputs[6])
    links.new(grp_in.outputs['Vertex Color'], color_mix.inputs[7])

    # Emission shader for the debug bypass.
    emission = nodes.new('ShaderNodeEmission')
    emission.location = (0, 120)
    links.new(color_mix.outputs[2], emission.inputs['Color'])

    # MixShader: factor=is_debug, A=Surface (BSDF), B=Emission.
    shader_mix = nodes.new('ShaderNodeMixShader')
    shader_mix.location = (200, 0)
    links.new(is_debug.outputs[0], shader_mix.inputs['Fac'])
    links.new(grp_in.outputs['Surface'], shader_mix.inputs[1])
    links.new(emission.outputs['Emission'], shader_mix.inputs[2])

    links.new(shader_mix.outputs[0], grp_out.inputs['Surface'])

    return tree


def _add_debug_switch(mat, bsdf, output_node, debug_images=None,
                      debug_uv_types=None):
    """Insert FS25 Debug Switch group between bsdf and output_node.

    Side effects on the material:
      - Adds 5 nodes to mat.node_tree: switch (group), mode (Value),
        mask_image (TexImage), uv_node (UVMap), vc_attr (Attribute).
      - Repositions output_node further right to make room.
      - Re-routes any BSDF -> output_node.Surface link through the switch.
      - Stores list of available mask names in mat['_fs25_debug_masks']
        and per-mask UV types in mat['_fs25_debug_mask_uvs'] for the
        N-Panel dropdown.

    Args:
        debug_images: optional dict {name: bpy.types.Image} of images
            selectable as debug masks. First entry becomes the default.
        debug_uv_types: optional dict {name: uv_type} where uv_type is a
            string from GIANTS_UV_TO_BLENDER_UV keys ('uv0'..'uv3') or
            'worldspace'/'custom'. Used to set the UV map for the
            displayed debug mask. Worldspace falls back to 'UVMap'
            (uv0) since a full triplanar setup would be overkill for
            the debug view.
    """
    nt = mat.node_tree
    tree = _ensure_debug_switch_tree()

    sx = output_node.location.x - 200
    sy = output_node.location.y

    switch = nt.nodes.new('ShaderNodeGroup')
    switch.node_tree = tree
    switch.name = "fs25_debug:switch"
    switch.label = "FS25 Debug Switch"
    switch.location = (sx, sy)

    output_node.location = (sx + 400, sy)

    # Remove any direct BSDF -> output_node.Surface link.
    for link in list(nt.links):
        if (link.to_node == output_node
                and link.to_socket.name == 'Surface'):
            nt.links.remove(link)

    nt.links.new(bsdf.outputs['BSDF'], switch.inputs['Surface'])
    nt.links.new(switch.outputs['Surface'], output_node.inputs['Surface'])

    # Mode Value node (Source Mode driver).
    mode_node = nt.nodes.new('ShaderNodeValue')
    mode_node.name = "fs25_debug:mode"
    mode_node.label = "Debug Mode (0=off, 1=mask, 2=vc)"
    mode_node.outputs[0].default_value = 0.0
    mode_node.location = (sx - 250, sy - 160)
    nt.links.new(mode_node.outputs[0], switch.inputs['Source Mode'])

    # UV source node for the debug mask. Default depends on the first
    # mask's UV type, fallback "UVMap" (uv0). The N-Panel updates
    # uv_node.uv_map atomically when the user switches mask.
    uv_node = nt.nodes.new('ShaderNodeUVMap')
    uv_node.name = "fs25_debug:uv"
    uv_node.label = "Debug UV Source"
    uv_node.uv_map = "UVMap"
    uv_node.location = (sx - 450, sy - 260)

    # Mask Image node. UV is fed by uv_node above.
    mask_img = nt.nodes.new('ShaderNodeTexImage')
    mask_img.name = "fs25_debug:mask_image"
    mask_img.label = "Debug Mask Image"
    mask_img.location = (sx - 250, sy - 260)
    if debug_images:
        first_name = next(iter(debug_images))
        mask_img.image = debug_images[first_name]
        # Resolve first mask's UV (worldspace -> UVMap fallback).
        if debug_uv_types:
            uv_type = debug_uv_types.get(first_name, 'uv0')
            uv_node.uv_map = GIANTS_UV_TO_BLENDER_UV.get(uv_type, 'UVMap')
    nt.links.new(uv_node.outputs['UV'], mask_img.inputs['Vector'])
    nt.links.new(mask_img.outputs['Color'], switch.inputs['Mask Image'])

    # Vertex Color attribute node. No default attribute_name - Blender uses
    # the mesh's active color attribute as fallback.
    vc_attr = nt.nodes.new('ShaderNodeAttribute')
    vc_attr.name = "fs25_debug:vertex_color"
    vc_attr.label = "Vertex Color"
    vc_attr.attribute_type = 'GEOMETRY'
    vc_attr.location = (sx - 250, sy - 420)
    nt.links.new(vc_attr.outputs['Color'], switch.inputs['Vertex Color'])

    # Metadata for the N-Panel dropdown.
    mat['_fs25_debug_masks'] = list(debug_images.keys()) if debug_images else []
    mat['_fs25_debug_mask_uvs'] = dict(debug_uv_types) if debug_uv_types else {}
    # Image-name lookup so apply_debug_mode_to_material() can resolve
    # bpy.types.Image references without walking the node tree.
    mat['_fs25_debug_mask_images'] = {
        name: img.name for name, img in (debug_images or {}).items()
        if img is not None
    }

    return switch


DEBUG_FRAME_NAME = "FS25_Debug_Frame"


def _finalize_layout(mat, bsdf, output_node):
    """Last-step graph cleanup applied to every FS25 material:

    1. Stack all fs25_param:* nodes vertically inside the existing param
       frame at the far left (x = PARAM_X). Helper nodes downstream of an
       fs25_param (e.g. the SUBTRACT for inverted clearCoatSmoothness)
       move with the param.
    2. Move BSDF + (Debug Switch, if present) + Material Output behind
       all feature nodes on the far right.
    3. Wrap fs25_debug:* nodes in a 'FS25 Debug Switch' frame (reddish
       to contrast with the blue param frame).

    Safe to call on materials that have no fs25_param or no fs25_debug
    nodes - the corresponding step is then a no-op.
    """
    nt = mat.node_tree

    PARAM_X = -2200
    PARAM_TOP_Y = 1200
    PARAM_Y_GAP_VALUE = 100
    PARAM_Y_GAP_RGB   = 200

    param_frame = nt.nodes.get(PARAM_FRAME_NAME)

    # Collect nodes by category. Frames are skipped here - we deal with
    # them separately at the end.
    param_nodes = []
    debug_nodes = []
    debug_switch = None
    feature_nodes = []

    for n in nt.nodes:
        if n.type == 'FRAME':
            continue
        if n is bsdf or n is output_node:
            continue
        if n.name.startswith("fs25_param:"):
            param_nodes.append(n)
        elif n.name.startswith("fs25_debug:"):
            debug_nodes.append(n)
            if n.name == "fs25_debug:switch":
                debug_switch = n
        else:
            feature_nodes.append(n)

    # ---- 1. Stack param nodes vertically ----
    if param_nodes:
        # Try inventory sort; fall back to alphabetical if unavailable.
        try:
            from . import material_inventory

            def _sort_key(node):
                pname = node.name[len("fs25_param:"):]
                group, order = material_inventory.lookup_param(pname)
                groups = material_inventory.FS25_PARAM_GROUP_ORDER
                gidx = groups.index(group) if group in groups else 999
                return (gidx, order, pname)
        except Exception:
            def _sort_key(node):
                return node.name

        param_nodes.sort(key=_sort_key)

        y = PARAM_TOP_Y
        for pnode in param_nodes:
            pnode.location = (PARAM_X, y)
            # Helper nodes downstream of this param that live in the same
            # frame (e.g. SUBTRACT for inverted Value) ride along.
            for link in nt.links:
                if (link.from_node is pnode
                        and getattr(link.to_node, 'parent', None) is param_frame
                        and link.to_node not in param_nodes):
                    link.to_node.location = (PARAM_X + 200, y)
            y -= PARAM_Y_GAP_RGB if pnode.type == 'RGB' else PARAM_Y_GAP_VALUE

    # ---- 2. Find rightmost X of feature nodes (excl. param frame children) ----
    rightmost_x = -1000.0
    for n in feature_nodes:
        if getattr(n, 'parent', None) is param_frame:
            continue
        nx = n.location.x + 200  # approximate node width
        if nx > rightmost_x:
            rightmost_x = nx

    # ---- 3. BSDF -> [Debug Switch] -> Output to far right ----
    bsdf_x = rightmost_x + 100
    bsdf.location = (bsdf_x, 0)

    if debug_switch is not None:
        # Spacing taken from a hand-arranged reference material so the
        # framed block sits 300-ish px right of BSDF, leaves room for the
        # 3-column control layout (mode/vc/uv | mask | switch), and the
        # output is just past the frame on the right.
        switch_x = bsdf_x + 820
        debug_switch.location = (switch_x, 0)
        output_node.location = (switch_x + 280, 0)

        # Control nodes - 3 visual columns inside the frame:
        #   col 1 (mode/vc/uv) at switch_x - 480
        #   col 2 (mask_image) at switch_x - 280, mid-height
        #   col 3 (switch)     at switch_x
        ctrl_layout = {
            "fs25_debug:mode":         (switch_x - 480,    0),
            "fs25_debug:vertex_color": (switch_x - 480, -100),
            "fs25_debug:uv":           (switch_x - 480, -320),
            "fs25_debug:mask_image":   (switch_x - 280, -160),
        }
        for n in debug_nodes:
            if n is debug_switch:
                continue
            new_loc = ctrl_layout.get(n.name)
            if new_loc is not None:
                n.location = new_loc

        # ---- 4. Wrap debug nodes in a frame ----
        debug_frame = nt.nodes.get(DEBUG_FRAME_NAME)
        if debug_frame is None:
            debug_frame = nt.nodes.new('NodeFrame')
            debug_frame.name = DEBUG_FRAME_NAME
            debug_frame.label = "FS25 Debug Switch"
            debug_frame.use_custom_color = True
            debug_frame.color = (0.55, 0.30, 0.30)
        for n in debug_nodes:
            n.parent = debug_frame
    else:
        # No debug switch: BSDF -> Output directly.
        output_node.location = (bsdf_x + 300, 0)


# ---------------------------------------------------------------------------
# Public API: apply a debug-view mode to a single material.
# Used by the N-Panel update callback - scene-wide or per-material.
# ---------------------------------------------------------------------------

def apply_debug_mode_to_material(mat, mode_str):
    """Apply a debug-view mode to one material.

    mode_str:
        'NORMAL'         -> show the BSDF result (mode value = 0.0)
        'VERTEX_COLORS'  -> show vertex colors (mode value = 2.0)
        'MASK:<name>'    -> show the named mask. Sets mask_image.image
                            from mat['_fs25_debug_mask_images'][name]
                            and uv_node.uv_map from mat['_fs25_debug_mask_uvs'].
                            Materials that don't have the named mask stay
                            in NORMAL mode (silently skipped).

    Silently no-ops on materials without the FS25 debug switch (any of
    the fs25_debug:* nodes missing).
    """
    if not mat.use_nodes or mat.node_tree is None:
        return
    nt = mat.node_tree
    mode_value = nt.nodes.get("fs25_debug:mode")
    if mode_value is None:
        return  # not an FS25 material with debug switch

    if mode_str == 'NORMAL':
        mode_value.outputs[0].default_value = 0.0
        return

    if mode_str == 'VERTEX_COLORS':
        mode_value.outputs[0].default_value = 2.0
        return

    if mode_str.startswith('MASK:'):
        mask_name = mode_str[len('MASK:'):]
        mask_images = dict(mat.get('_fs25_debug_mask_images', {}))
        mask_uvs    = dict(mat.get('_fs25_debug_mask_uvs', {}))
        if mask_name not in mask_images:
            # Material lacks this mask - stay in NORMAL mode.
            mode_value.outputs[0].default_value = 0.0
            return
        mask_node = nt.nodes.get("fs25_debug:mask_image")
        uv_node   = nt.nodes.get("fs25_debug:uv")
        if mask_node is not None:
            img_name = mask_images[mask_name]
            mask_node.image = bpy.data.images.get(img_name)
        if uv_node is not None:
            uv_type = mask_uvs.get(mask_name, 'uv0')
            uv_node.uv_map = GIANTS_UV_TO_BLENDER_UV.get(uv_type, 'UVMap')
        mode_value.outputs[0].default_value = 1.0
        return

    # Unknown mode string: stay in NORMAL mode (safer than asserting).
    mode_value.outputs[0].default_value = 0.0


# ---------------------------------------------------------------------------
# Main API: build PBR debug material
# ---------------------------------------------------------------------------

def _build_colormat_palette_image(mat_name, custom_params):
    """Build an 8x1 lookup image from the FS22 vehicleShader colorMat0..7 palette.

    Pixel i = colorMat[i] RGB (paint colour, stored linear -> Non-Color image so
    the value reaches Base Color unchanged; i3d colours are linear) plus
    alpha = colorMat[i].w / 255 (the Giants material-library index, consumed by
    the detail-array pass). Missing entries default to mid-grey.
    """
    import bpy as _bpy
    img = _bpy.data.images.new(f"{mat_name}_colorMat", width=8, height=1, alpha=True)
    img.colorspace_settings.name = 'Non-Color'
    px = []
    for i in range(8):
        raw = custom_params.get(f'colorMat{i}')
        r = g = b = 0.5
        w = 0.0
        if raw:
            parts = str(raw).split()
            try:
                r, g, b = float(parts[0]), float(parts[1]), float(parts[2])
                w = float(parts[3]) if len(parts) > 3 else 0.0
            except (ValueError, IndexError):
                r = g = b = 0.5
                w = 0.0
        px.extend((r, g, b, min(1.0, max(0.0, w / 255.0))))
    img.pixels = px
    img.pack()
    return img


_DETAIL_ATLAS_CACHE = {}


def _decode_bc1_array_mip0(dds_bytes):
    """Decode a DX10 BC1 2D-array DDS (all layers, mip0) -> (list[(h,w,3) uint8], w, h).
    Verified against the FS22 detailArray_*.dds files. numpy is bundled with Blender.
    Returns (None, 0, 0) if the file is not a BC1 DX10 array."""
    import struct
    import numpy as np
    d = dds_bytes
    if d[:4] != b'DDS ' or d[84:88] != b'DX10':
        return None, 0, 0
    h = struct.unpack_from('<I', d, 12)[0]
    w = struct.unpack_from('<I', d, 16)[0]
    mips = struct.unpack_from('<I', d, 28)[0] or 1
    dxgi = struct.unpack_from('<I', d, 128)[0]
    arr = struct.unpack_from('<I', d, 140)[0] or 1
    if dxgi not in (71, 72):  # BC1_UNORM / BC1_UNORM_SRGB
        return None, 0, 0

    def mb(ww, hh):
        return max(1, ww // 4) * max(1, hh // 4) * 8

    slice_bytes = 0
    ww, hh = w, h
    for _ in range(mips):
        slice_bytes += mb(ww, hh)
        ww = max(1, ww // 2)
        hh = max(1, hh // 2)
    bw, bh = w // 4, h // 4

    def decode_mip0(buf):
        blk = np.frombuffer(buf[:bw * bh * 8], np.uint8).reshape(bw * bh, 8)
        c0 = blk[:, 0].astype(np.uint16) | (blk[:, 1].astype(np.uint16) << 8)
        c1 = blk[:, 2].astype(np.uint16) | (blk[:, 3].astype(np.uint16) << 8)
        idx = (blk[:, 4].astype(np.uint32) | (blk[:, 5].astype(np.uint32) << 8)
               | (blk[:, 6].astype(np.uint32) << 16) | (blk[:, 7].astype(np.uint32) << 24))

        def to888(c):
            r = (c >> 11) & 0x1f
            g = (c >> 5) & 0x3f
            b = c & 0x1f
            return np.stack([r * 255 // 31, g * 255 // 63, b * 255 // 31], -1).astype(np.uint16)

        a = to888(c0)
        b = to888(c1)
        gt = (c0 > c1)[:, None]
        c2 = np.where(gt, (2 * a + b) // 3, (a + b) // 2)
        c3 = np.where(gt, (a + 2 * b) // 3, np.zeros_like(a))
        pal = np.stack([a, b, c2, c3], 1).astype(np.uint8)
        n = bw * bh
        sel = np.stack([(idx >> (2 * pp)) & 3 for pp in range(16)], -1)
        px = pal[np.arange(n)[:, None], sel].reshape(n, 4, 4, 3)
        return px.reshape(bh, bw, 4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 3)

    data_off = 148
    layers = [decode_mip0(d[data_off + s * slice_bytes:data_off + s * slice_bytes + mb(w, h)])
              for s in range(arr)]
    return layers, w, h


def _get_detail_diffuse_atlas(i3d_dir, resolve_filepath, report):
    """Decode the mArrayDiffuse detail array (Giants material library) into a
    cached 8-column Blender atlas image (256 px/tile, sRGB). Tile (col,row) =
    library layer row*8+col, oriented bottom-up so model UV (floor(u),floor(v))
    maps straight onto it. Returns (image, row_count) or None."""
    import os
    import bpy as _bpy
    import numpy as np
    path = resolve_filepath('$data/shared/detailArray_diffuse.dds', i3d_dir)
    if not path or not os.path.exists(path):
        return None
    cached = _DETAIL_ATLAS_CACHE.get(path)
    if cached is not None:
        img, rows = cached
        if img is not None and img.name in _bpy.data.images:
            return cached
    try:
        with open(path, 'rb') as fh:
            layers, w, h = _decode_bc1_array_mip0(fh.read())
    except Exception as e:
        report('WARNING', f"detailArray_diffuse decode failed: {type(e).__name__}: {e}")
        _DETAIL_ATLAS_CACHE[path] = None
        return None
    if not layers:
        _DETAIL_ATLAS_CACHE[path] = None
        return None
    COLS, T = 8, 256
    rows = (len(layers) + COLS - 1) // COLS
    atlas = np.zeros((rows * T, COLS * T, 4), np.float32)
    atlas[:, :, 3] = 1.0
    sh = max(1, h // T)
    sw = max(1, w // T)
    for L, tile in enumerate(layers):
        col = L % COLS
        row = L // COLS
        ds = tile[::sh, ::sw][:T, :T].astype(np.float32) / 255.0
        atlas[row * T:(row + 1) * T, col * T:(col + 1) * T, :3] = np.flipud(ds)
    img = _bpy.data.images.new('FS_detailArray_diffuse', width=COLS * T, height=rows * T, alpha=True)
    img.colorspace_settings.name = 'sRGB'
    img.pixels.foreach_set(np.ascontiguousarray(atlas).ravel())
    img.pack()
    _DETAIL_ATLAS_CACHE[path] = (img, rows)
    return (img, rows)


def build_pbr_debug_material(
    mat_name: str,
    mat_attrs: dict,
    scene,
    image_cache: dict,
    snippet_cache: dict,
    shader_cache: dict,            # level 2: cached ShaderInfo per shader-XML path
    snippets_blend_path: Path,
    i3d_dir: Path,
    resolve_filepath: Callable,   # (filename, i3d_dir) -> Optional[Path]
    image_loader: Callable,        # (filepath, image_cache, report) -> Optional[bpy.types.Image]
    report: Callable,
) -> bpy.types.Material:
    """Build a debug material <mat_name>_pbr_debug from combined snippets.

    Level 1:
      1. Standard maps (Texture/Normalmap/Glossmap) -> correctly wired up
         (Glossmap via fs25_Specular_smooth_AO_metallic snippet -> Roughness + Metallic).
      2. Custom maps -> created as image-tex nodes with a label, NOT wired.
         The user can wire manually, or level 2+ does shader-specific wiring.

    Returns:
        bpy.types.Material - finished debug material (always non-None).
    """
    # Naming: <mat_name>_pbr_debug_<materialId>
    # Disambig via materialId, because multiple i3d materials can share the
    # same name (cube2.i3d has 4x "UnnamedMaterial"). Reimports of an i3d ->
    # same materialId -> existing is removed + rebuilt (deterministic).
    mat_id = mat_attrs.get('materialId', '')
    if mat_id:
        debug_name = f"{mat_name}_pbr_debug_{mat_id}"
    else:
        debug_name = f"{mat_name}_pbr_debug"

    existing = bpy.data.materials.get(debug_name)
    if existing is not None:
        bpy.data.materials.remove(existing)

    mat = bpy.data.materials.new(name=debug_name)
    mat.use_nodes = True
    mat.use_fake_user = True
    # Material identification for the switch operator (N-Panel "i3d Importer"):
    # _i3d_material_id matches the counterpart (re-export material) via the
    # same ID. _i3d_material_kind distinguishes "debug" vs "export". Robust
    # against material renames by the user.
    try:
        mat['_i3d_material_id'] = int(mat_attrs.get('materialId', 0))
    except (ValueError, TypeError):
        mat['_i3d_material_id'] = 0
    mat['_i3d_material_kind'] = 'debug'
    mat['_i3d_debug_pbr_for'] = mat_name
    mat['_i3d_debug_pbr_variation'] = mat_attrs.get('customShaderVariation', '') or ''
    # Stamp the current import's UUID so the switch operator can pair this
    # debug material with its export counterpart from the same import even
    # when multiple imports share material_id 0/1/2/...
    # Late-import importer here to avoid circular import at module load.
    from . import importer as _imp
    if _imp._CURRENT_IMPORT_UUID is not None:
        mat['_i3d_import_uuid'] = _imp._CURRENT_IMPORT_UUID

    nt = mat.node_tree
    # Use node type (locale-independent) instead of name (translated in non-English Blender).
    bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
    if bsdf is None or output_node is None:
        report('WARNING',
               f"PBR debug '{mat_name}': BSDF or Material Output missing "
               f"- material stays minimal.")
        return mat

    # Position BSDF (default is close to the output)
    bsdf.location = (200, 0)
    output_node.location = (500, 0)

    # Workaround for Giants exporter Emission-Color bug (also in the debug material)
    if 'Emission Color' in bsdf.inputs:
        bsdf.inputs['Emission Color'].default_value = (0, 0, 0, 1)

    # ---- 1. Load shader XML and resolve UV mapping per textureName.
    # uv_mapping = {textureName: ShaderUvUsage(uv_type, uv_scale)}.
    # Empty when customShader/customShaderVariation is missing or the XML cannot
    # be parsed - all textures then fall back to uv0 (UVMap).
    uv_mapping = _resolve_shader_uv_mapping(
        mat_attrs, scene, i3d_dir, shader_cache,
        resolve_filepath, report, mat_name,
    )

    # ---- 2.5. Parallax Occlusion Mapping setup----
    # If the Parallax Node extension (https://extensions.blender.org/add-ons/parallax-node/)
    # is installed AND the material has an mParallaxMap custom map, we build a
    # real parallax-occlusion-mapping setup here: ShaderNodeParallaxClosure +
    # closure zone (NodeClosureInput -> mParallaxMap tex -> NodeClosureOutput).
    # The POM output is a vector (modified UV) which in _make_image_chain
    # replaces all textures with the same uv_type as mParallaxMap - giving
    # visually real depth displacement instead of just bump.
    #
    # Extension detection: Blender 5.1 does NOT expose extension classes under
    # bpy.types (bl_ext.* namespace), but `nodes.new('ShaderNodeParallaxClosure')`
    # works via factory. Therefore: try/except around the first node create call
    # - on RuntimeError, block 7c falls back to the fs25_Parallax snippet (bump).
    pom_uv_source = None
    pom_target_uv_type = None
    pom_active = False

    parallax_fid = None
    for _cm in mat_attrs.get('_custommaps', []):
        if _cm.get('name') == 'mParallaxMap':
            try:
                parallax_fid = int(_cm.get('fileId'))
            except (TypeError, ValueError):
                parallax_fid = None
            break

    if parallax_fid is not None:
        _path_str = scene.files.get(parallax_fid)
        _resolved = resolve_filepath(_path_str, i3d_dir) if _path_str else None
        _pmap_image = image_loader(_resolved, image_cache, report) if _resolved else None

        if _pmap_image is not None:
            try:
                pc = nt.nodes.new('ShaderNodeParallaxClosure')
            except RuntimeError:
                pc = None  # extension not installed / not active

            if pc is not None:
                pc.location = (-1760, 360)
                pc.label = "Parallax: Closure"
                ci = nt.nodes.new('NodeClosureInput')
                ci.location = (-2540, 220)
                co = nt.nodes.new('NodeClosureOutput')
                co.location = (-1980, 300)
                ci.pair_with_output(co)
                # Items via 'FLOAT'/'VECTOR' enums (NOT 'VALUE' - verified in research).
                co.input_items.new('VECTOR', 'Vector')
                co.output_items.new('FLOAT', 'Value')

                pmap_node = nt.nodes.new('ShaderNodeTexImage')
                pmap_node.image = _pmap_image
                pmap_node.label = "mParallaxMap (POM)"
                pmap_node.location = (-2300, 300)
                try:
                    _pmap_image.colorspace_settings.name = 'Non-Color'
                except Exception:
                    pass

                nt.links.new(ci.outputs['Vector'], pmap_node.inputs['Vector'])
                nt.links.new(pmap_node.outputs['Color'], co.inputs['Value'])
                nt.links.new(co.outputs['Closure'], pc.inputs['Height Texture'])

                # Pass uvScale from the shader XML through to Texture Scale.
                # Texture Scale is a NodeSocketVector2D (default 1.0, 1.0).
                _pmap_usage = uv_mapping.get('mParallaxMap')
                if _pmap_usage and _pmap_usage.uv_scale and _pmap_usage.uv_scale != 1.0:
                    try:
                        pc.inputs['Texture Scale'].default_value = (
                            _pmap_usage.uv_scale, _pmap_usage.uv_scale)
                    except Exception:
                        pass

                pom_uv_source = pc.outputs['Vector']
                pom_target_uv_type = _pmap_usage.uv_type if _pmap_usage else 'uv0'
                pom_active = True

    # ---- 2.6. Vehicle triplanar setup ----
    # FS25 vehicleShader samples detailDiffuse/detailNormal/detailSpecular WITH
    # tri-planar projection (HLSL lines 1545-1556): the dominant |normal| axis
    # picks one of three object-space projections (texCoordsX=(z,y),
    # texCoordsY=(z,x), texCoordsZ=(x,y)). We replicate that via the
    # fs25_VehicleTriplanar snippet and pass the output vector as
    # `triplanar_uv_source` to _make_image_chain - which replaces the UV-Map
    # source for textures whose name is in VEHICLE_TRIPLANAR_TEXTURES.
    triplanar_uv_source = None
    triplanar_texture_names = None

    # Determine shader_name locally (code duplication with block 9.6/9.7;
    # accepted until a central helper exists).
    _tp_shader_name = ''
    _tp_csi = mat_attrs.get('customShaderId')
    if _tp_csi is not None:
        try:
            _tp_fid = int(_tp_csi)
            _tp_raw_path = scene.files.get(_tp_fid)
            if _tp_raw_path:
                _tp_resolved = resolve_filepath(_tp_raw_path, i3d_dir)
                if _tp_resolved is not None:
                    _tp_shader_name = _tp_resolved.name.lower()
        except (ValueError, TypeError):
            pass

    if _tp_shader_name == 'vehicleshader.xml':
        # Triplanar is ALWAYS built for vehicleShader - detail maps AND dirt
        # defaults both need tri-planar UVs. A _has_detail check on
        # custom maps is no longer needed because the dirt defaults are loaded
        # via VEHICLE_DIRT_DEFAULT_PATHS independently.
        tp_ng = append_node_group(SN_VEHICLE_TRIPLANAR, snippets_blend_path,
                                  snippet_cache, report)
        if tp_ng is not None:
                tp_grp = nt.nodes.new('ShaderNodeGroup')
                tp_grp.node_tree = tp_ng
                tp_grp.location = (-1900, -1300)
                tp_grp.label = "Vehicle Triplanar UV"

                # Position source: TexCoord.Object (object-local position).
                tp_texco = nt.nodes.new('ShaderNodeTexCoord')
                tp_texco.location = (-2200, -1200)
                tp_texco.label = "Object position"
                nt.links.new(tp_texco.outputs['Object'], tp_grp.inputs['Position'])

                # Normal source: Geometry.Normal (worldspace) -> VectorTransform
                # into object space (matches HLSL In.vs.localNormal).
                tp_geo = nt.nodes.new('ShaderNodeNewGeometry')
                tp_geo.location = (-2500, -1400)
                tp_geo.label = "Worldspace normal"
                tp_vt = nt.nodes.new('ShaderNodeVectorTransform')
                tp_vt.vector_type = 'NORMAL'
                tp_vt.convert_from = 'WORLD'
                tp_vt.convert_to = 'OBJECT'
                tp_vt.location = (-2200, -1400)
                tp_vt.label = "World -> Object normal"
                nt.links.new(tp_geo.outputs['Normal'], tp_vt.inputs['Vector'])
                nt.links.new(tp_vt.outputs['Vector'], tp_grp.inputs['Normal'])

                triplanar_uv_source = tp_grp.outputs['UV']
                triplanar_texture_names = VEHICLE_TRIPLANAR_TEXTURES

    # ---- 2. Diffuse color as fallback ----
    diffuse = mat_attrs.get('diffuseColor')
    if diffuse:
        rgba = _parse_vec4(diffuse, default=(1.0, 1.0, 1.0, 1.0))
        bsdf.inputs['Base Color'].default_value = rgba
        # Alpha < 1 -> enable BLEND method
        if rgba[3] < 1.0 and 'Alpha' in bsdf.inputs:
            bsdf.inputs['Alpha'].default_value = rgba[3]
            try:
                mat.blend_method = 'BLEND'
            except Exception:
                pass

    # ---- 3-5. Standard maps: Texture + Glossmap + Normalmap, with AO overlay ----
    # base_color_source: NodeSocketColor (final -> BSDF.BaseColor, possibly via AO/Snow)
    # ao_source: NodeSocketFloat (to wire into AO overlay snippet)
    # roughness_source/metallic_source: NodeSocketFloat - come from specular snippet,
    #   possibly rerouted via snow overlay, final wiring in block 10.
    # normal_source: NodeSocketVector - set by the normalmap block, possibly
    #   rerouted via the parallax block (7c), final wiring in block 10.
    base_color_source = None
    ao_source = None
    roughness_source = None
    metallic_source = None
    normal_source = None

    # 3. Texture (standard diffuse) - NOT directly to BSDF; via AO overlay (see 5b)
    tex_fid = mat_attrs.get('_texture_fileId')
    if tex_fid is not None:
        tex_node = _make_image_chain(
            nt, tex_fid, scene, i3d_dir,
            texture_name="baseMap", label="baseMap", non_color=False,
            location_x=-700, y_offset=350,
            uv_mapping=uv_mapping,
            image_cache=image_cache,
            resolve_filepath=resolve_filepath, image_loader=image_loader,
            report=report, mat_name=mat_name,
            pom_uv_source=pom_uv_source, pom_target_uv_type=pom_target_uv_type,
            snippets_blend_path=snippets_blend_path, snippet_cache=snippet_cache,
        )
        if tex_node is not None:
            base_color_source = tex_node.outputs['Color']
            # Alpha channel to BSDF.Alpha - for opaque textures (BC1/DXT1)
            # without a real alpha channel the output is constantly 1.0, so
            # effectively a no-op. Blender 4.2+/5.x renders alpha automatically
            # via surface_render_method = 'DITHERED' (default for new materials).
            # Verified with harborOfficeBricks_mat (4-channel DDS, no
            # 'alphaBlending' attribute).
            nt.links.new(tex_node.outputs['Alpha'], bsdf.inputs['Alpha'])

    # 4. Glossmap -> fs25_Specular_smooth_AO_metallic snippet -> BSDF Roughness + Metallic;
    #    AO output is remembered and multiplied onto BaseColor in 5b via fs25_AO_overlay.
    # gm_node is held outside the if block so block 7g (vehicle mask
    # compositing) can access the image-tex node (glossMap.r/.g/.b have
    # different channel semantics in vehicleShader: Scratches/AO/Dirt+Snow).
    gm_node = None
    gm_fid = mat_attrs.get('_glossmap_fileId')
    if gm_fid is not None:
        gm_node = _make_image_chain(
            nt, gm_fid, scene, i3d_dir,
            texture_name="glossMap", label="glossMap", non_color=True,
            location_x=-700, y_offset=-300,
            uv_mapping=uv_mapping,
            image_cache=image_cache,
            resolve_filepath=resolve_filepath, image_loader=image_loader,
            report=report, mat_name=mat_name,
            pom_uv_source=pom_uv_source, pom_target_uv_type=pom_target_uv_type,
            snippets_blend_path=snippets_blend_path, snippet_cache=snippet_cache,
        )
        if gm_node is not None:
            unpack_ng = append_node_group(
                DEFAULT_GLOSSMAP_SNIPPET, snippets_blend_path,
                snippet_cache, report,
            )
            if unpack_ng is not None:
                grp = nt.nodes.new('ShaderNodeGroup')
                grp.node_tree = unpack_ng
                grp.location = (-400, -300)
                grp.label = "Glossmap → PBR"
                nt.links.new(gm_node.outputs['Color'], grp.inputs['Specular'])
                # Remember in source variables - final BSDF wiring in block 10,
                # because the snow overlay (block 9) may override them.
                if 'Roughness' in grp.outputs:
                    roughness_source = grp.outputs['Roughness']
                if 'Metallic' in grp.outputs:
                    metallic_source = grp.outputs['Metallic']
                if 'AO' in grp.outputs:
                    ao_source = grp.outputs['AO']
            else:
                # Snippet unavailable -> skeleton fallback: glossmap directly as
                # the roughness source (matches re-export material behavior)
                roughness_source = gm_node.outputs['Color']
                report('INFO',
                       f"PBR debug '{mat_name}': glossmap snippet not available - "
                       f"glossmap wired directly to Roughness (skeleton fallback).")

    # 5. Normalmap → Normal Map Node (mit bumpDepth-Strength) → BSDF Normal
    nm_fid = mat_attrs.get('_normalmap_fileId')
    if nm_fid is not None:
        nm_node = _make_image_chain(
            nt, nm_fid, scene, i3d_dir,
            texture_name="normalMap", label="normalMap", non_color=True,
            location_x=-700, y_offset=0,
            uv_mapping=uv_mapping,
            image_cache=image_cache,
            resolve_filepath=resolve_filepath, image_loader=image_loader,
            report=report, mat_name=mat_name,
            pom_uv_source=pom_uv_source, pom_target_uv_type=pom_target_uv_type,
            snippets_blend_path=snippets_blend_path, snippet_cache=snippet_cache,
        )
        if nm_node is not None:
            nrm = nt.nodes.new('ShaderNodeNormalMap')
            nrm.location = (-400, 0)
            # bumpDepth aus <Normalmap bumpDepth="..."/> (GE-Default 1.0)
            bump_depth = mat_attrs.get('_normalmap_bumpDepth')
            if bump_depth is not None:
                try:
                    nrm.inputs['Strength'].default_value = float(bump_depth)
                except (ValueError, TypeError):
                    pass
            nt.links.new(nm_node.outputs['Color'], nrm.inputs['Color'])
            # Park the normal output in a source variable - final wiring to
            # BSDF in block 10. Block 7c may reroute the source via the parallax
            # snippet (combines normalmap + heightmap as bump).
            normal_source = nrm.outputs['Normal']

    # AO-Overlay followes AFTER custommap compositing (order: BaseColor → Moss → Dirt
    # → AO → BSDF). Do not plug anything into BSDF.BaseColor, Block 8 does that.

    # ---- 6. Create custom maps with correct UV map and collect
    # them for compositing ----
    custommaps = mat_attrs.get('_custommaps', [])
    cm_tex = {}  # cm_name -> tex_node (for compositing lookup)
    cm_count = 0
    y_offset = -700
    for cm in custommaps:
        cm_name = cm.get('name')
        fid_str = cm.get('fileId')
        if not cm_name or fid_str is None:
            continue
        # mParallaxMap: when POM is active, the heightmap lives in the closure
        # zone (block 2.5), not as a normal custom map in the tree. Otherwise
        # (bump fallback in block 7c) it stays here in cm_tex.
        if cm_name == 'mParallaxMap' and pom_active:
            continue
        # trackArray is a Vertex-Shader-Texture (vehicleShader):
        # delivers position/orientation data for motion path/vertex rotate animation
        # (rotating discs, scrolling tracks). Has NO visual effect on
        # albedo/roughness/metallic/normal. Re-export fidelity preserved via
        # _apply_material_custom_properties (customTexture_trackArray on the material).
        if cm_name == 'trackArray':
            report('INFO',
                   f"PBR debug '{mat_name}': trackArray is a vertex-shader texture "
                   f"(motion path / vertex rotate), not wired in the material build. "
                   f"Re-export fidelity preserved via customTexture_trackArray on the material.")
            continue
        try:
            fid = int(fid_str)
        except ValueError:
            continue
        # detailNormal is a normal map (Non-Color), detailDiffuse is sRGB.
        # Pragmatic: set non_color=True for all "*Normal" custommaps.
        _is_normal = cm_name.endswith('Normal') or cm_name.endswith('normalMap')
        cm_node = _make_image_chain(
            nt, fid, scene, i3d_dir,
            texture_name=cm_name, label=cm_name, non_color=_is_normal,
            location_x=-1300, y_offset=y_offset,
            uv_mapping=uv_mapping,
            image_cache=image_cache,
            resolve_filepath=resolve_filepath, image_loader=image_loader,
            report=report, mat_name=mat_name,
            pom_uv_source=pom_uv_source, pom_target_uv_type=pom_target_uv_type,
            triplanar_uv_source=triplanar_uv_source,
            triplanar_texture_names=triplanar_texture_names,
            snippets_blend_path=snippets_blend_path, snippet_cache=snippet_cache,
        )
        if cm_node is None:
            continue
        cm_tex[cm_name] = cm_node
        y_offset -= 300
        cm_count += 1

    # ---- 7. Level 3: compositing - moss + dirt + parallax (when custom maps fit) ----
    composited_features = []
    custom_params = {cp['name']: cp['value']
                     for cp in mat_attrs.get('_customparameters', [])
                     if cp.get('name') and cp.get('value') is not None}

    # ---- treeBranchShader (SEASONAL): leaf/branch debug visualization ------
    # The seasonal tree-branch shader packs 4 seasons into the baseMap atlas
    # (shader uvScale=0.5 -> a 0.5x0.5 quadrant) and derives the look from
    # mMaskMap (R=branches, G=leaves-grow, B=random-per-leaf). cShared3 (0..4)
    # picks the season at runtime; for a static debug we show SUMMER:
    #   summer leaf color : baseMap uv*0.5 + (0.5,0.5)
    #   branch color      : baseMap uv*0.5 + (0.5,0.0)   (mColorBranches)
    #   diffuse = mix(branch, summer, mask.G)            (G high = leaf -> green)
    #   alpha   = (mask.R + mask.G) > 0.25               (binary cutout)
    #   mMaskMap is a single image -> sampled at full uv (Mapping scale 1.0).
    # Re-export material is untouched; debug look only. Season switch can drive
    # the quadrant offsets later. See GitHub #22.
    if _tp_shader_name == 'treebranchshader.xml' and base_color_source is not None:
        def _ensure_mapping(tex_node, scale_xy, loc_xy, y):
            vec = tex_node.inputs.get('Vector') if hasattr(tex_node, 'inputs') else None
            mp = None
            if vec is not None and vec.is_linked:
                src = vec.links[0].from_node
                if src.type == 'MAPPING':
                    mp = src
            if mp is None:
                uvn = nt.nodes.new('ShaderNodeUVMap'); uvn.uv_map = 'UVMap'
                uvn.location = (tex_node.location.x - 600, y)
                mp = nt.nodes.new('ShaderNodeMapping')
                mp.location = (tex_node.location.x - 350, y)
                nt.links.new(uvn.outputs['UV'], mp.inputs['Vector'])
                if vec is not None:
                    nt.links.new(mp.outputs['Vector'], vec)
            mp.inputs['Scale'].default_value = (scale_xy[0], scale_xy[1], 1.0)
            mp.inputs['Location'].default_value = (loc_xy[0], loc_xy[1], 0.0)
            return mp

        _summer_tex = base_color_source.node
        _mask = cm_tex.get('mMaskMap')
        # leaf-diffuse quadrant on the existing baseMap node (default summer);
        # the N-Panel 'Tree Season' control drives this Mapping's offset.
        _leaf_map = _ensure_mapping(_summer_tex, (0.5, 0.5), (0.5, 0.5), 350)
        _leaf_map.label = 'i3d_tree_leaf_quadrant'

        if _mask is not None and getattr(_summer_tex, 'image', None) is not None:
            # mask channels (single image -> full uv)
            _ensure_mapping(_mask, (1.0, 1.0), (0.0, 0.0), -250)
            _sep = nt.nodes.new('ShaderNodeSeparateColor')
            _sep.location = (-300, -250)
            nt.links.new(_mask.outputs['Color'], _sep.inputs['Color'])

            # 'leaves enabled' (1 normally, 0 in winter) - driven by the
            # N-Panel 'Tree Season' control. G_eff = mask.G * leaf_enable
            # feeds BOTH the diffuse leaf/branch mix and the leaf alpha, so
            # winter -> all branches + branches-only cutout.
            _leaf_en = nt.nodes.new('ShaderNodeValue')
            _leaf_en.outputs[0].default_value = 1.0
            _leaf_en.label = 'i3d_tree_leaf_enable'
            _leaf_en.location = (-300, -120)
            _genable = nt.nodes.new('ShaderNodeMath')
            _genable.operation = 'MULTIPLY'
            _genable.location = (-120, -120)
            _genable.label = 'G_eff = leaves * season-enable'
            nt.links.new(_sep.outputs['Green'], _genable.inputs[0])
            nt.links.new(_leaf_en.outputs[0], _genable.inputs[1])

            # second baseMap sample at the branches quadrant (0.5, 0.0)
            _branch_tex = nt.nodes.new('ShaderNodeTexImage')
            _branch_tex.image = _summer_tex.image
            _branch_tex.interpolation = _summer_tex.interpolation
            _branch_tex.location = (_summer_tex.location.x, _summer_tex.location.y + 330)
            _branch_tex.label = 'baseMap (branches quadrant)'
            _ensure_mapping(_branch_tex, (0.5, 0.5), (0.5, 0.0), _branch_tex.location.y)

            # diffuse = mix(branch, summer, mask.G): G high -> leaf (summer green)
            _dmix = nt.nodes.new('ShaderNodeMix'); _dmix.data_type = 'RGBA'
            _dmix.location = (-60, 360); _dmix.label = 'branch/leaf diffuse (summer)'
            nt.links.new(_genable.outputs[0], _dmix.inputs['Factor'])
            nt.links.new(_branch_tex.outputs['Color'], _dmix.inputs[6])  # A = branches
            nt.links.new(_summer_tex.outputs['Color'], _dmix.inputs[7])  # B = summer leaves
            base_color_source = _dmix.outputs[2]

            # binary alpha cutout = (R + G) > 0.25 (ALPHA_TESTED, render-method
            # independent; blend_method is deprecated in 4.2+/5.x)
            _add = nt.nodes.new('ShaderNodeMath')
            _add.operation = 'ADD'; _add.use_clamp = True
            _add.location = (-100, -250)
            _add.label = 'R(branches)+G(leaves)'
            nt.links.new(_sep.outputs['Red'], _add.inputs[0])
            nt.links.new(_genable.outputs[0], _add.inputs[1])
            _bin = nt.nodes.new('ShaderNodeMath')
            _bin.operation = 'GREATER_THAN'
            _bin.inputs[1].default_value = 0.25
            _bin.location = (80, -250)
            _bin.label = 'leaf alpha cutout (binary)'
            nt.links.new(_add.outputs[0], _bin.inputs[0])
            for _l in list(bsdf.inputs['Alpha'].links):
                nt.links.remove(_l)
            nt.links.new(_bin.outputs[0], bsdf.inputs['Alpha'])
            mat.blend_method = 'CLIP'
            try:
                mat.alpha_threshold = 0.25
            except AttributeError:
                pass
            composited_features.append('treeBranchSeasonal')
            mat['_i3d_tree_branch_debug'] = True
        else:
            report('INFO',
                   f"PBR debug '{mat_name}': treeBranchShader without mMaskMap "
                   f"- leaf cutout / branch-color skipped.")

    # ---- 6b. FS22 vehicleShader colorMask palette (Teil 1) ----------------
    # FS22 vehicles have no diffuse <Texture>; the base colour comes from the
    # colorMat0..7 palette selected per-fragment by UV0: when uv.y < 0 the colour
    # is colorMat[floor(uv.x)] (fixes FS22 white vehicle materials). uv.y >= 0
    # regions use the Giants material-library detail array (handled in 6c).
    # cm_* refs are stashed for the detail-array pass.
    cm_palette_u = cm_is_palette = cm_mat_index = cm_uv_sep = cm_base_mix = None
    _cm_variation = mat_attrs.get('customShaderVariation', '') or ''
    if _tp_shader_name == 'vehicleshader.xml' and 'colorMask' in _cm_variation:
        _pal_img = _build_colormat_palette_image(mat_name, custom_params)
        _cm_uv = nt.nodes.new('ShaderNodeUVMap')
        _cm_uv.uv_map = 'UVMap'
        _cm_uv.location = (-1900, 700)
        cm_uv_sep = nt.nodes.new('ShaderNodeSeparateXYZ')
        cm_uv_sep.location = (-1700, 700)
        nt.links.new(_cm_uv.outputs['UV'], cm_uv_sep.inputs['Vector'])
        # palette_u = (floor(U) + 0.5) / 8
        _f = nt.nodes.new('ShaderNodeMath'); _f.operation = 'FLOOR'
        _f.location = (-1500, 800)
        nt.links.new(cm_uv_sep.outputs['X'], _f.inputs[0])
        _a = nt.nodes.new('ShaderNodeMath'); _a.operation = 'ADD'
        _a.inputs[1].default_value = 0.5; _a.location = (-1320, 800)
        nt.links.new(_f.outputs[0], _a.inputs[0])
        _d = nt.nodes.new('ShaderNodeMath'); _d.operation = 'DIVIDE'
        _d.inputs[1].default_value = 8.0; _d.location = (-1140, 800)
        nt.links.new(_a.outputs[0], _d.inputs[0])
        cm_palette_u = _d.outputs[0]
        _comb = nt.nodes.new('ShaderNodeCombineXYZ')
        _comb.inputs['Y'].default_value = 0.5; _comb.location = (-950, 800)
        nt.links.new(cm_palette_u, _comb.inputs['X'])
        _pal_tex = nt.nodes.new('ShaderNodeTexImage')
        _pal_tex.image = _pal_img
        _pal_tex.interpolation = 'Closest'
        _pal_tex.extension = 'EXTEND'
        _pal_tex.location = (-750, 800)
        nt.links.new(_comb.outputs['Vector'], _pal_tex.inputs['Vector'])
        cm_mat_index = _pal_tex.outputs['Alpha']  # material-library index / 255
        # is_palette = (uv.y < 0)
        _lt = nt.nodes.new('ShaderNodeMath'); _lt.operation = 'LESS_THAN'
        _lt.inputs[1].default_value = 0.0; _lt.location = (-1140, 560)
        nt.links.new(cm_uv_sep.outputs['Y'], _lt.inputs[0])
        cm_is_palette = _lt.outputs[0]
        # base colour: mix(grey placeholder, palette, is_palette).
        # The detail-array pass (6c) replaces the grey for uv.y >= 0 regions.
        _cm_mix = nt.nodes.new('ShaderNodeMix'); _cm_mix.data_type = 'RGBA'
        _cm_mix.location = (-500, 760); _cm_mix.label = 'colorMask palette'
        _cm_mix.inputs[6].default_value = (0.5, 0.5, 0.5, 1.0)
        nt.links.new(_pal_tex.outputs['Color'], _cm_mix.inputs[7])
        nt.links.new(cm_is_palette, _cm_mix.inputs['Factor'])
        base_color_source = _cm_mix.outputs[2]
        cm_base_mix = _cm_mix
        composited_features.append('colorMask')

    # ---- 6c. FS22 detail-array albedo for uv.y>=0 (Teil 2) ----------------
    # uv.y>=0 regions carry no diffuse texture; their albedo is the Giants
    # material-library tile mArrayDiffuse[floor(v)*8 + floor(u)]. Sample a decoded
    # atlas of that array and feed it into the colorMask base mix, replacing the
    # grey placeholder for the non-painted regions. Normal/spec stay as handled
    # by the material's own normal map + vmask (shader-internal combine not
    # reconstructable from the XML).
    if cm_base_mix is not None:
        _atlas = _get_detail_diffuse_atlas(i3d_dir, resolve_filepath, report)
        if _atlas is not None:
            _atlas_img, _atlas_rows = _atlas
            _COLS = 8
            _fu = nt.nodes.new('ShaderNodeMath'); _fu.operation = 'FLOOR'
            _fu.location = (-1500, 300)
            nt.links.new(cm_uv_sep.outputs['X'], _fu.inputs[0])
            _cu = nt.nodes.new('ShaderNodeClamp'); _cu.location = (-1320, 300)
            _cu.inputs['Min'].default_value = 0.0
            _cu.inputs['Max'].default_value = float(_COLS - 1)
            nt.links.new(_fu.outputs[0], _cu.inputs['Value'])
            _fv = nt.nodes.new('ShaderNodeMath'); _fv.operation = 'FLOOR'
            _fv.location = (-1500, 120)
            nt.links.new(cm_uv_sep.outputs['Y'], _fv.inputs[0])
            _cv = nt.nodes.new('ShaderNodeClamp'); _cv.location = (-1320, 120)
            _cv.inputs['Min'].default_value = 0.0
            _cv.inputs['Max'].default_value = float(_atlas_rows - 1)
            nt.links.new(_fv.outputs[0], _cv.inputs['Value'])
            _ru = nt.nodes.new('ShaderNodeMath'); _ru.operation = 'FRACT'
            _ru.location = (-1320, 460)
            nt.links.new(cm_uv_sep.outputs['X'], _ru.inputs[0])
            _rv = nt.nodes.new('ShaderNodeMath'); _rv.operation = 'FRACT'
            _rv.location = (-1320, -40)
            nt.links.new(cm_uv_sep.outputs['Y'], _rv.inputs[0])
            _aua = nt.nodes.new('ShaderNodeMath'); _aua.operation = 'ADD'
            _aua.location = (-1140, 380)
            nt.links.new(_cu.outputs[0], _aua.inputs[0])
            nt.links.new(_ru.outputs[0], _aua.inputs[1])
            _au = nt.nodes.new('ShaderNodeMath'); _au.operation = 'DIVIDE'
            _au.inputs[1].default_value = float(_COLS); _au.location = (-960, 380)
            nt.links.new(_aua.outputs[0], _au.inputs[0])
            _ava = nt.nodes.new('ShaderNodeMath'); _ava.operation = 'ADD'
            _ava.location = (-1140, 80)
            nt.links.new(_cv.outputs[0], _ava.inputs[0])
            nt.links.new(_rv.outputs[0], _ava.inputs[1])
            _av = nt.nodes.new('ShaderNodeMath'); _av.operation = 'DIVIDE'
            _av.inputs[1].default_value = float(_atlas_rows); _av.location = (-960, 80)
            nt.links.new(_ava.outputs[0], _av.inputs[0])
            _acomb = nt.nodes.new('ShaderNodeCombineXYZ'); _acomb.location = (-780, 240)
            nt.links.new(_au.outputs[0], _acomb.inputs['X'])
            nt.links.new(_av.outputs[0], _acomb.inputs['Y'])
            _atex = nt.nodes.new('ShaderNodeTexImage'); _atex.image = _atlas_img
            _atex.interpolation = 'Linear'; _atex.extension = 'EXTEND'
            _atex.location = (-560, 240)
            nt.links.new(_acomb.outputs['Vector'], _atex.inputs['Vector'])
            nt.links.new(_atex.outputs['Color'], cm_base_mix.inputs[6])
            composited_features.append('detailArrayDiffuse')

    mask_tex     = cm_tex.get('mMaskMap')
    dirt_tex     = cm_tex.get('mDirtDiffuse')
    moss_tex     = cm_tex.get('mMossDiffuse')
    parallax_tex = cm_tex.get('mParallaxMap')

    # mMaskMap.R = moss-mask, .G = AO, .B = dirt-mask (buildingShader-Konvention).
    # Create Separate Color once, plug in both compositing snippets.
    mask_sep = None
    if mask_tex is not None and (dirt_tex is not None or moss_tex is not None):
        mask_sep = nt.nodes.new('ShaderNodeSeparateColor')
        mask_sep.mode = 'RGB'
        mask_sep.location = (-100, -750)
        mask_sep.label = "mMaskMap (R=moss, G=AO, B=dirt)"
        nt.links.new(mask_tex.outputs['Color'], mask_sep.inputs['Color'])

    # dirtMossMix: float2 "X Y" — X=moss intensity, Y=dirt intensity
    dm_parts = (custom_params.get('dirtMossMix') or '1.0 1.0').split()
    try:
        moss_intensity = float(dm_parts[0]) if len(dm_parts) > 0 else 1.0
    except ValueError:
        moss_intensity = 1.0
    try:
        dirt_intensity = float(dm_parts[1]) if len(dm_parts) > 1 else 1.0
    except ValueError:
        dirt_intensity = 1.0

    # dirtMossTint: float3 "X Y Z" — X=DIRT tint, Y=MOSS tint (reverse to dirtMossMix!),
    # Z=parallax darkening (only heightBlend variation, ignored). Default per Shader: 0.0 0.0 0.8.
    # Applied in snippets as saturate(tint + diffuse).
    dt_parts = (custom_params.get('dirtMossTint') or '0.0 0.0 0.8').split()
    try:
        dirt_tint = float(dt_parts[0]) if len(dt_parts) > 0 else 0.0
    except ValueError:
        dirt_tint = 0.0
    try:
        moss_tint = float(dt_parts[1]) if len(dt_parts) > 1 else 0.0
    except ValueError:
        moss_tint = 0.0

    # 7a. Moss-Compositing (mMaskMap.R + mMossDiffuse + dirtMossMix.X)
    if (moss_tex is not None and mask_sep is not None
            and base_color_source is not None):
        moss_ng = append_node_group(SN_MOSS, snippets_blend_path, snippet_cache, report)
        if moss_ng is not None:
            moss_grp = nt.nodes.new('ShaderNodeGroup')
            moss_grp.node_tree = moss_ng
            moss_grp.location = (300, -500)
            moss_grp.label = "Moss Compositing"
            nt.links.new(base_color_source,         moss_grp.inputs['BaseColor'])
            nt.links.new(mask_sep.outputs['Red'],   moss_grp.inputs['MossMask'])
            nt.links.new(moss_tex.outputs['Color'], moss_grp.inputs['MossTexture'])
            _make_param_value(
                nt, "dirtMossMix_x", moss_intensity,
                location=(60, -480), label="Moss Intensity",
                target_input=moss_grp.inputs['MossIntensity'],
                xml_param="dirtMossMix", xml_slot="x",
            )
            # MossTint input only present in extended snippets.
            if 'MossTint' in moss_grp.inputs:
                _make_param_value(
                    nt, "dirtMossTint_y", moss_tint,
                    location=(60, -560), label="Moss Tint",
                    target_input=moss_grp.inputs['MossTint'],
                    xml_param="dirtMossTint", xml_slot="y",
                )
            base_color_source = moss_grp.outputs['BaseColor']
            composited_features.append('Moss')

    # 7b. Dirt-Compositing (mMaskMap.B + mDirtDiffuse + dirtMossMix.Y)
    if (dirt_tex is not None and mask_sep is not None
            and base_color_source is not None):
        dirt_ng = append_node_group(SN_DIRT, snippets_blend_path, snippet_cache, report)
        if dirt_ng is not None:
            dirt_grp = nt.nodes.new('ShaderNodeGroup')
            dirt_grp.node_tree = dirt_ng
            dirt_grp.location = (500, -500)
            dirt_grp.label = "Dirt Compositing"
            nt.links.new(base_color_source,         dirt_grp.inputs['BaseColor'])
            nt.links.new(mask_sep.outputs['Blue'],  dirt_grp.inputs['DirtMask'])
            nt.links.new(dirt_tex.outputs['Color'], dirt_grp.inputs['DirtTexture'])
            _make_param_value(
                nt, "dirtMossMix_y", dirt_intensity,
                location=(260, -480), label="Dirt Intensity",
                target_input=dirt_grp.inputs['DirtIntensity'],
                xml_param="dirtMossMix", xml_slot="y",
            )
            if 'DirtTint' in dirt_grp.inputs:
                _make_param_value(
                    nt, "dirtMossTint_x", dirt_tint,
                    location=(260, -560), label="Dirt Tint",
                    target_input=dirt_grp.inputs['DirtTint'],
                    xml_param="dirtMossTint", xml_slot="x",
                )
            base_color_source = dirt_grp.outputs['BaseColor']
            composited_features.append('Dirt')

    # 7c. Parallax - POM or bump fallback
    # POM was already built in block 2.5. Here we only add the logging.
    # The bump snippet (fs25_Parallax) is used ONLY when POM is not active and
    # mParallaxMap is present - fallback for users without the Parallax Node extension.
    if pom_active:
        composited_features.append('POM')
    elif parallax_tex is not None:
        para_ng = append_node_group(SN_PARALLAX, snippets_blend_path, snippet_cache, report)
        if para_ng is not None:
            para_grp = nt.nodes.new('ShaderNodeGroup')
            para_grp.node_tree = para_ng
            para_grp.location = (-100, -1100)
            para_grp.label = "Parallax → Normal (Bump fallback)"
            nt.links.new(parallax_tex.outputs['Color'], para_grp.inputs['ParallaxMap'])
            has_normal_input = 'Normal' in para_grp.inputs
            if normal_source is None:
                # Kein Normalmap — Bump-Parallax allein → normal_source.
                normal_source = para_grp.outputs['Normal']
                composited_features.append('Parallax (Bump)')
            elif has_normal_input:
                # Normalmap + Bump-Parallax kombiniert.
                nt.links.new(normal_source, para_grp.inputs['Normal'])
                normal_source = para_grp.outputs['Normal']
                composited_features.append('Parallax (Bump)')
            else:
                # Normalmap present but snippet without normal input - old
                # master-blend version. Bump parallax skipped, normalmap stays
                # alone (normal_source unchanged). Snippet node remains in the
                # graph without output wiring, serves as a hint.
                report('INFO',
                       f"PBR debug '{mat_name}': fs25_Parallax snippet has no "
                       f"normal input - parallax+normalmap combination skipped. "
                       f"Update master blend.")

    # ---- 7d. Emissive — mEmissiveMap -> BSDF.Emission Color ----
    # Trigger: Custommap 'mEmissiveMap' vorhanden. Strength aus lightControl-Parameter
    # (if present in the XML), otherwise default 1.0 (= visible emissive when
    # a map is present). Shader defaults for lightControl are inconsistent
    # (0.0 / 0 / 1.0 depending on shader); the 1.0 fallback is intentionally pragmatic.
    em_tex = cm_tex.get('mEmissiveMap')
    if em_tex is not None:
        nt.links.new(em_tex.outputs['Color'], bsdf.inputs['Emission Color'])
        lc_raw = custom_params.get('lightControl')
        try:
            light_ctrl = float(lc_raw) if lc_raw is not None else 1.0
        except (ValueError, TypeError):
            light_ctrl = 1.0
        _make_param_value(
            nt, "lightControl", light_ctrl,
            location=(-100, -100), label="Light Control (Emission Strength)",
            target_input=bsdf.inputs['Emission Strength'],
        )
        composited_features.append('Emissive')

    # ---- 7e. Vehicle Detail Combiner ----
    # FS25 vehicleShader: detailDiffuse/detailSpecular/detailNormal sampled with
    # Tri-planar Projection (UV-Source already in block 6 via
    # _make_image_chain → triplanar_uv_source).
    # Here we combine the detail maps with the standard PBR sources:
    #   - detailDiffuse -> multiplicative onto base_color_source (HLSL line 1582)
    #   - detailSpecular -> OVERRIDES roughness/AO/metallic (HLSL lines 1644-1646)
    #     Reason: glossMap in vehicleShader has different channel semantics
    #     (.r=Scratches mask, .g=AO, .b=Dirt/Snow mask) - the standard specular
    #     snippet from block 4 interprets that incorrectly. detailSpecular is the
    #     correct PBR source.
    #   - detailNormal → Image-Tex created, but NOT combined with normal_source
    detail_diff_tex = cm_tex.get('detailDiffuse')
    detail_spec_tex = cm_tex.get('detailSpecular')
    detail_norm_tex = cm_tex.get('detailNormal')

    if detail_diff_tex is not None and base_color_source is not None:
        vd_mix = nt.nodes.new('ShaderNodeMix')
        vd_mix.data_type = 'RGBA'
        vd_mix.blend_type = 'MULTIPLY'
        vd_mix.inputs['Factor'].default_value = 1.0
        vd_mix.location = (700, -1100)
        vd_mix.label = "Vehicle Detail × Base"
        # Mix RGBA: A=input[6], B=input[7], Result=output[2]
        nt.links.new(base_color_source,             vd_mix.inputs[6])
        nt.links.new(detail_diff_tex.outputs['Color'], vd_mix.inputs[7])
        base_color_source = vd_mix.outputs[2]
        composited_features.append('VehicleDetailDiffuse')

    # colorScale: float3 RGB multiply factor on BaseColor (HLSL Z.1582).
    # Default shader value is (1,1,1) → no effect; typically set in
    # Brand materials like AMAZONE_GREEN1 (via brandMaterialTemplates.xml
    # lookup in the Giants exporter). Also used for non-vehicle shaders
    # (e.g. colorShader/buildingShader have a colorScale array, that's block
    # 9.6/9.7) - here we only handle the vehicleShader single-RGB case.
    cs_raw = custom_params.get('colorScale')
    if (cs_raw is not None and base_color_source is not None
            and _tp_shader_name == 'vehicleshader.xml'):
        cs_parts = str(cs_raw).split()
        if len(cs_parts) >= 3:
            try:
                cs_r = float(cs_parts[0])
                cs_g = float(cs_parts[1])
                cs_b = float(cs_parts[2])
                cs_mix = nt.nodes.new('ShaderNodeMix')
                cs_mix.data_type = 'RGBA'
                cs_mix.blend_type = 'MULTIPLY'
                cs_mix.inputs['Factor'].default_value = 1.0
                cs_mix.location = (920, -1100)
                cs_mix.label = "× colorScale (brand)"
                nt.links.new(base_color_source, cs_mix.inputs[6])
                _make_param_rgb(
                    nt, "colorScale", (cs_r, cs_g, cs_b),
                    location=(720, -1100), label="Color Scale (brand tint)",
                    target_input=cs_mix.inputs[7],
                )
                base_color_source = cs_mix.outputs[2]
                composited_features.append('colorScale')
            except ValueError:
                pass

    if detail_spec_tex is not None:
        # detailSpecular: r=smoothness, g=AO (micro), b=metallic.
        # roughness = 1 - (smoothness × smoothnessScale)
        # metallic  = metallic × metalnessScale
        # ao        = micro_ao × existing macro_ao
        vd_sep = nt.nodes.new('ShaderNodeSeparateColor')
        vd_sep.mode = 'RGB'
        vd_sep.location = (700, -1350)
        vd_sep.label = "detailSpecular (R=smooth, G=AO, B=metal)"
        nt.links.new(detail_spec_tex.outputs['Color'], vd_sep.inputs['Color'])

        # Smoothness path: r × smoothnessScale (HLSL Z.1578) → 1-x → roughness
        smoothness_out = vd_sep.outputs['Red']
        ss_raw = custom_params.get('smoothnessScale')
        if ss_raw is not None:
            try:
                ss = float(str(ss_raw).split()[0])
            except (ValueError, IndexError):
                ss = 1.0
            ss_mul = nt.nodes.new('ShaderNodeMath')
            ss_mul.operation = 'MULTIPLY'
            ss_mul.location = (820, -1280)
            ss_mul.label = "× smoothnessScale"
            nt.links.new(vd_sep.outputs['Red'], ss_mul.inputs[0])
            _make_param_value(
                nt, "smoothnessScale", ss,
                location=(620, -1280), label="Smoothness Scale",
                target_input=ss_mul.inputs[1],
            )
            smoothness_out = ss_mul.outputs[0]

        vd_invert = nt.nodes.new('ShaderNodeMath')
        vd_invert.operation = 'SUBTRACT'
        vd_invert.location = (1020, -1300)
        vd_invert.label = "1 - smoothness"
        vd_invert.inputs[0].default_value = 1.0
        nt.links.new(smoothness_out, vd_invert.inputs[1])
        roughness_source = vd_invert.outputs[0]

        # Metallic path: b × metalnessScale (HLSL Z.1580) → metallic
        metallic_out = vd_sep.outputs['Blue']
        ms_raw = custom_params.get('metalnessScale')
        if ms_raw is not None:
            try:
                ms = float(str(ms_raw).split()[0])
            except (ValueError, IndexError):
                ms = 1.0
            ms_mul = nt.nodes.new('ShaderNodeMath')
            ms_mul.operation = 'MULTIPLY'
            ms_mul.location = (820, -1450)
            ms_mul.label = "× metalnessScale"
            nt.links.new(vd_sep.outputs['Blue'], ms_mul.inputs[0])
            _make_param_value(
                nt, "metalnessScale", ms,
                location=(620, -1450), label="Metalness Scale",
                target_input=ms_mul.inputs[1],
            )
            metallic_out = ms_mul.outputs[0]
        metallic_source = metallic_out

        if ao_source is not None:
            # micro AO (detailSpecular.g) × macro AO (existing)
            vd_ao_mul = nt.nodes.new('ShaderNodeMath')
            vd_ao_mul.operation = 'MULTIPLY'
            vd_ao_mul.location = (1020, -1500)
            vd_ao_mul.label = "micro × macro AO"
            nt.links.new(vd_sep.outputs['Green'], vd_ao_mul.inputs[0])
            nt.links.new(ao_source,               vd_ao_mul.inputs[1])
            ao_source = vd_ao_mul.outputs[0]
        else:
            ao_source = vd_sep.outputs['Green']
        composited_features.append('VehicleDetailSpecular')

    if detail_norm_tex is not None:
        # Interpret detailNormal as second normal map, then combine
        # with normal_source via Vector-Add + Normalize.
        # Approximation of HLSL logic Z.1593-1595 (only adds .xy-Offset);
        # Vector-Add is an established game engine approximation, very similar
        # visuell. If normal_source is None (no normalMap in material),
        # then the detailNormal is used as only normal source.
        detail_nrm = nt.nodes.new('ShaderNodeNormalMap')
        detail_nrm.location = (700, -1700)
        detail_nrm.label = "detailNormal → tangent"
        nt.links.new(detail_norm_tex.outputs['Color'], detail_nrm.inputs['Color'])

        if normal_source is not None:
            # Add + Normalize: result = normalize(normal + detailNormal)
            nrm_add = nt.nodes.new('ShaderNodeVectorMath')
            nrm_add.operation = 'ADD'
            nrm_add.location = (900, -1700)
            nrm_add.label = "normalMap + detailNormal"
            nt.links.new(normal_source,                nrm_add.inputs[0])
            nt.links.new(detail_nrm.outputs['Normal'], nrm_add.inputs[1])

            nrm_norm = nt.nodes.new('ShaderNodeVectorMath')
            nrm_norm.operation = 'NORMALIZE'
            nrm_norm.location = (1080, -1700)
            nrm_norm.label = "normalize"
            nt.links.new(nrm_add.outputs[0], nrm_norm.inputs[0])
            normal_source = nrm_norm.outputs[0]
        else:
            normal_source = detail_nrm.outputs['Normal']
        composited_features.append('VehicleDetailNormal')

    # ---- 7f. Vehicle ClearCoat ----
    # HLSL Z.1684-1689 (POST_CLEAR_COAT_FS):
    #   clearCoat = object.clearCoatIntensity.x  (with baseWeight multiply,
    #     simplified: pure intensity)
    #   clearCoatRoughness = 1.0 - object.clearCoatSmoothness.x
    #   clearCoatNormal = globals.gOriginalNormal  (stays default, no override)
    # Purely value-setting in Principled BSDF — no nodes required.
    if _tp_shader_name == 'vehicleshader.xml':
        cc_int_raw = custom_params.get('clearCoatIntensity')
        cc_sm_raw  = custom_params.get('clearCoatSmoothness')
        cc_applied = False
        if cc_int_raw is not None and 'Coat Weight' in bsdf.inputs:
            try:
                cc_int = float(str(cc_int_raw).split()[0])
                _make_param_value(
                    nt, "clearCoatIntensity", cc_int,
                    location=(-100, -200), label="Clear Coat Intensity",
                    target_input=bsdf.inputs['Coat Weight'],
                )
                cc_applied = True
            except (ValueError, IndexError):
                pass
        if cc_sm_raw is not None and 'Coat Roughness' in bsdf.inputs:
            try:
                cc_sm = float(str(cc_sm_raw).split()[0])
                _make_param_value_inverted(
                    nt, "clearCoatSmoothness", cc_sm,
                    location=(-300, -280), label="Clear Coat Smoothness",
                    target_input=bsdf.inputs['Coat Roughness'],
                )
                cc_applied = True
            except (ValueError, IndexError):
                pass
        if cc_applied:
            composited_features.append('clearCoat')

    # ---- 7g. Vehicle Mask-Compositing ----
    # FS25 vehicleShader (HLSL Z.1523-1615): glossMap-Channels have different
    # semantics as buildingShader (r=Scratches-Mask, g=AO, b=Dirt+Snow-Mask).
    # scratches_dirt_snow_wetness (float4) steers the mask threshholds.
    # Combine BaseColor/Roughness/Metallic with constant Snow/Dirt/Scratches-
    # values based on the 3 weights from the fs25_VehicleMasks snippet.
    if _tp_shader_name == 'vehicleshader.xml' and gm_node is not None:
        sdsw_raw = custom_params.get('scratches_dirt_snow_wetness')
        sa, da, sna, wa = 0.0, 0.0, 0.0, 0.0
        if sdsw_raw is not None:
            parts = str(sdsw_raw).split()
            try:
                if len(parts) >= 1: sa  = float(parts[0])
                if len(parts) >= 2: da  = float(parts[1])
                if len(parts) >= 3: sna = float(parts[2])
                if len(parts) >= 4: wa  = float(parts[3])
            except ValueError:
                pass

        masks_ng = append_node_group(SN_VEHICLE_MASKS, snippets_blend_path,
                                      snippet_cache, report)
        if masks_ng is not None:
            masks_grp = nt.nodes.new('ShaderNodeGroup')
            masks_grp.node_tree = masks_ng
            masks_grp.location = (300, -2000)
            masks_grp.label = "Vehicle Masks (sds_w)"
            nt.links.new(gm_node.outputs['Color'], masks_grp.inputs['GlossMap'])
            _make_param_value(
                nt, "scratches_dirt_snow_wetness_x", sa,
                xml_param="scratches_dirt_snow_wetness", xml_slot="x",
                location=(60, -1950), label="Scratches Amount",
                target_input=masks_grp.inputs['ScratchesAmount'],
            )
            _make_param_value(
                nt, "scratches_dirt_snow_wetness_y", da,
                xml_param="scratches_dirt_snow_wetness", xml_slot="y",
                location=(60, -2030), label="Dirt Amount",
                target_input=masks_grp.inputs['DirtAmount'],
            )
            _make_param_value(
                nt, "scratches_dirt_snow_wetness_z", sna,
                xml_param="scratches_dirt_snow_wetness", xml_slot="z",
                location=(60, -2110), label="Snow Amount",
                target_input=masks_grp.inputs['SnowAmount'],
            )

            snow_w = masks_grp.outputs['SnowWeight']
            dirt_w = masks_grp.outputs['DirtWeight']
            scr_w  = masks_grp.outputs['ScratchesWeight']

            # dirtColor (Custom-Parameter, Shader-Default 0.20 0.14 0.08).
            dc_raw = custom_params.get('dirtColor') or '0.20 0.14 0.08'
            dc_parts = str(dc_raw).split()
            try:
                dc_r = float(dc_parts[0]) if len(dc_parts) > 0 else 0.20
                dc_g = float(dc_parts[1]) if len(dc_parts) > 1 else 0.14
                dc_b = float(dc_parts[2]) if len(dc_parts) > 2 else 0.08
            except ValueError:
                dc_r, dc_g, dc_b = 0.20, 0.14, 0.08
            dc_node = _make_param_rgb(
                nt, "dirtColor", (dc_r, dc_g, dc_b),
                location=(400, -1900), label="Dirt Color",
            )

            # BaseColor: snow → dirt → scratches (sequential mix multiplies)
            # Dirt textures below are assigned inside this base-color block but
            # are also read by the roughness/metallic/normal blocks further down.
            # Pre-init so a material whose base_color_source is None does not
            # raise UnboundLocalError (FS22 vehicle debug materials).
            dirt_norm_tex = None
            dirt_spec_tex_def = None

            if base_color_source is not None:
                m_snow = nt.nodes.new('ShaderNodeMix')
                m_snow.data_type = 'RGBA'
                m_snow.location = (600, -1900); m_snow.label = "× SnowMask"
                nt.links.new(snow_w, m_snow.inputs['Factor'])
                nt.links.new(base_color_source, m_snow.inputs[6])
                m_snow.inputs[7].default_value = (0.7300, 0.7668, 0.8356, 1.0)
                base_color_source = m_snow.outputs[2]

                # dirtDiffuse: custom override (cm_tex) or load FS25 default path.
                # When texture available: dirtTex * dirtColor (Mix Multiply) replaces the
                # pure dirtColor constant.
                dirt_diff_tex = cm_tex.get('dirtDiffuse')
                if dirt_diff_tex is None:
                    _dd_default = VEHICLE_DIRT_DEFAULT_PATHS.get('dirtDiffuse')
                    _dd_resolved = resolve_filepath(_dd_default, i3d_dir) if _dd_default else None
                    _dd_img = image_loader(_dd_resolved, image_cache, report) if _dd_resolved else None
                    if _dd_img is not None:
                        dirt_diff_tex = nt.nodes.new('ShaderNodeTexImage')
                        dirt_diff_tex.image = _dd_img
                        dirt_diff_tex.label = "dirtDiffuse (FS25 default)"
                        dirt_diff_tex.location = (-1300, -2250)
                        if triplanar_uv_source is not None:
                            _dd_map = nt.nodes.new('ShaderNodeMapping')
                            _dd_map.location = (-800, -2250)
                            nt.links.new(triplanar_uv_source, _dd_map.inputs['Vector'])
                            nt.links.new(_dd_map.outputs['Vector'], dirt_diff_tex.inputs['Vector'])

                dirt_color_src = None
                if dirt_diff_tex is not None:
                    dd_mul = nt.nodes.new('ShaderNodeMix')
                    dd_mul.data_type = 'RGBA'
                    dd_mul.blend_type = 'MULTIPLY'
                    dd_mul.inputs['Factor'].default_value = 1.0
                    dd_mul.location = (620, -2080); dd_mul.label = "dirtTex × dirtColor"
                    nt.links.new(dirt_diff_tex.outputs['Color'], dd_mul.inputs[6])
                    nt.links.new(dc_node.outputs[0], dd_mul.inputs[7])
                    dirt_color_src = dd_mul.outputs[2]

                # dirtNormal: custom override (cm_tex) or load FS25 default path.
                dirt_norm_tex = cm_tex.get('dirtNormal')
                if dirt_norm_tex is None:
                    _dn_default = VEHICLE_DIRT_DEFAULT_PATHS.get('dirtNormal')
                    _dn_resolved = resolve_filepath(_dn_default, i3d_dir) if _dn_default else None
                    _dn_img = image_loader(_dn_resolved, image_cache, report) if _dn_resolved else None
                    if _dn_img is not None:
                        try:
                            _dn_img.colorspace_settings.name = 'Non-Color'
                        except Exception:
                            pass
                        dirt_norm_tex = nt.nodes.new('ShaderNodeTexImage')
                        dirt_norm_tex.image = _dn_img
                        dirt_norm_tex.label = "dirtNormal (FS25 default)"
                        dirt_norm_tex.location = (-1300, -2500)
                        if triplanar_uv_source is not None:
                            _dn_map = nt.nodes.new('ShaderNodeMapping')
                            _dn_map.location = (-800, -2500)
                            nt.links.new(triplanar_uv_source, _dn_map.inputs['Vector'])
                            nt.links.new(_dn_map.outputs['Vector'], dirt_norm_tex.inputs['Vector'])

                # dirtSpecular: custom override (cm_tex) or load FS25 default path.
                dirt_spec_tex_def = cm_tex.get('dirtSpecular')
                if dirt_spec_tex_def is None:
                    _ds_default = VEHICLE_DIRT_DEFAULT_PATHS.get('dirtSpecular')
                    _ds_resolved = resolve_filepath(_ds_default, i3d_dir) if _ds_default else None
                    _ds_img = image_loader(_ds_resolved, image_cache, report) if _ds_resolved else None
                    if _ds_img is not None:
                        try:
                            _ds_img.colorspace_settings.name = 'Non-Color'
                        except Exception:
                            pass
                        dirt_spec_tex_def = nt.nodes.new('ShaderNodeTexImage')
                        dirt_spec_tex_def.image = _ds_img
                        dirt_spec_tex_def.label = "dirtSpecular (FS25 default)"
                        dirt_spec_tex_def.location = (-1300, -2700)
                        if triplanar_uv_source is not None:
                            _ds_map = nt.nodes.new('ShaderNodeMapping')
                            _ds_map.location = (-800, -2700)
                            nt.links.new(triplanar_uv_source, _ds_map.inputs['Vector'])
                            nt.links.new(_ds_map.outputs['Vector'], dirt_spec_tex_def.inputs['Vector'])

                m_dirt = nt.nodes.new('ShaderNodeMix')
                m_dirt.data_type = 'RGBA'
                m_dirt.location = (820, -1900); m_dirt.label = "× DirtMask"
                nt.links.new(dirt_w, m_dirt.inputs['Factor'])
                nt.links.new(base_color_source, m_dirt.inputs[6])
                if dirt_color_src is not None:
                    nt.links.new(dirt_color_src, m_dirt.inputs[7])
                else:
                    nt.links.new(dc_node.outputs[0], m_dirt.inputs[7])
                base_color_source = m_dirt.outputs[2]

                m_scr = nt.nodes.new('ShaderNodeMix')
                m_scr.data_type = 'RGBA'
                m_scr.location = (1040, -1900); m_scr.label = "× ScratchesMask"
                nt.links.new(scr_w, m_scr.inputs['Factor'])
                nt.links.new(base_color_source, m_scr.inputs[6])
                m_scr.inputs[7].default_value = (0.98, 0.98, 0.98, 1.0)
                base_color_source = m_scr.outputs[2]

            # dirtNormal-Combine: in dirt areas dirtNormal dominates.
            # ShaderNodeMix(VECTOR) between normal_source and dirt normalMap output with
            # Factor=dirt_w. approximation of HLSL weighted sum (Z.1593).
            if dirt_norm_tex is not None and normal_source is not None:
                dn_nrm = nt.nodes.new('ShaderNodeNormalMap')
                dn_nrm.location = (900, -2500)
                dn_nrm.label = "dirtNormal → tangent"
                nt.links.new(dirt_norm_tex.outputs['Color'], dn_nrm.inputs['Color'])

                n_dirt = nt.nodes.new('ShaderNodeMix')
                n_dirt.data_type = 'VECTOR'
                n_dirt.location = (1100, -2500); n_dirt.label = "normal × DirtMask"
                nt.links.new(dirt_w, n_dirt.inputs['Factor'])
                nt.links.new(normal_source, n_dirt.inputs[4])
                nt.links.new(dn_nrm.outputs['Normal'], n_dirt.inputs[5])
                normal_source = n_dirt.outputs[1]

            # Roughness: snow → 0.81 (1-0.19), scratches → 0.15 (1-0.85)
            if roughness_source is not None:
                r_snow = nt.nodes.new('ShaderNodeMix')
                r_snow.data_type = 'FLOAT'
                r_snow.location = (600, -2150); r_snow.label = "rough × SnowMask"
                nt.links.new(snow_w, r_snow.inputs['Factor'])
                nt.links.new(roughness_source, r_snow.inputs[2])
                r_snow.inputs[3].default_value = 0.81
                roughness_source = r_snow.outputs[0]

                # dirtSpec → Roughness: 1-dirtSpec.r in Dirt-Areas.
                if dirt_spec_tex_def is not None:
                    ds_sep_r = nt.nodes.new('ShaderNodeSeparateColor')
                    ds_sep_r.mode = 'RGB'
                    ds_sep_r.location = (700, -2700)
                    ds_sep_r.label = "dirtSpec → R/G/B"
                    nt.links.new(dirt_spec_tex_def.outputs['Color'], ds_sep_r.inputs['Color'])

                    ds_inv = nt.nodes.new('ShaderNodeMath')
                    ds_inv.operation = 'SUBTRACT'
                    ds_inv.inputs[0].default_value = 1.0
                    ds_inv.location = (900, -2700)
                    ds_inv.label = "1 - dirtSpec.r"
                    nt.links.new(ds_sep_r.outputs['Red'], ds_inv.inputs[1])

                    r_dirt = nt.nodes.new('ShaderNodeMix')
                    r_dirt.data_type = 'FLOAT'
                    r_dirt.location = (720, -2200); r_dirt.label = "rough × DirtMask"
                    nt.links.new(dirt_w, r_dirt.inputs['Factor'])
                    nt.links.new(roughness_source, r_dirt.inputs[2])
                    nt.links.new(ds_inv.outputs[0], r_dirt.inputs[3])
                    roughness_source = r_dirt.outputs[0]

                r_scr = nt.nodes.new('ShaderNodeMix')
                r_scr.data_type = 'FLOAT'
                r_scr.location = (820, -2150); r_scr.label = "rough × ScratchesMask"
                nt.links.new(scr_w, r_scr.inputs['Factor'])
                nt.links.new(roughness_source, r_scr.inputs[2])
                r_scr.inputs[3].default_value = 0.15
                roughness_source = r_scr.outputs[0]

            # Metallic: snow → 0 (non-metallic)
            if metallic_source is not None:
                me_snow = nt.nodes.new('ShaderNodeMix')
                me_snow.data_type = 'FLOAT'
                me_snow.location = (600, -2380); me_snow.label = "metal × SnowMask"
                nt.links.new(snow_w, me_snow.inputs['Factor'])
                nt.links.new(metallic_source, me_snow.inputs[2])
                me_snow.inputs[3].default_value = 0.0
                metallic_source = me_snow.outputs[0]

                # dirtSpec → Metallic: dirtSpec.b in Dirt-Areas.
                # Re-use of ds_sep_r if exists, otherwise new.
                if dirt_spec_tex_def is not None:
                    _ds_sep_m = nt.nodes.new('ShaderNodeSeparateColor')
                    _ds_sep_m.mode = 'RGB'
                    _ds_sep_m.location = (700, -2900)
                    _ds_sep_m.label = "dirtSpec → R/G/B (metal)"
                    nt.links.new(dirt_spec_tex_def.outputs['Color'], _ds_sep_m.inputs['Color'])

                    me_dirt = nt.nodes.new('ShaderNodeMix')
                    me_dirt.data_type = 'FLOAT'
                    me_dirt.location = (720, -2440); me_dirt.label = "metal × DirtMask"
                    nt.links.new(dirt_w, me_dirt.inputs['Factor'])
                    nt.links.new(metallic_source, me_dirt.inputs[2])
                    nt.links.new(_ds_sep_m.outputs['Blue'], me_dirt.inputs[3])
                    metallic_source = me_dirt.outputs[0]

            composited_features.append('VehicleMasks')

    # ---- 7h. Vehicle Static Light ----
    # vehicleShader STATIC_LIGHT-Variation: lightsIntensity-Custommap (RGBA, sRGB)
    # to BSDF.Emission Color. Strength default 1.0 (demo mode).
    # Simplification: no bitmask extraction from lightTypeBitMask/lightUvOffsetBitMask
    # therefore no blinking
    # animation (blinkSimple/blinkMulti, possibly later via driver or animation
    # track). lightIds custom param is ignored in v1 - strength stays at 1.0 so
    # the emission is always visible in the debug material (i3d values are
    # often 0 because lights are switched dynamically in-game via LUA).
    _ct_variation = mat_attrs.get('customShaderVariation', '') or ''
    if (_tp_shader_name == 'vehicleshader.xml' and 'staticLight' in _ct_variation):
        light_tex = cm_tex.get('lightsIntensity')
        if light_tex is not None and 'Emission Color' in bsdf.inputs:
            nt.links.new(light_tex.outputs['Color'], bsdf.inputs['Emission Color'])
            if 'Emission Strength' in bsdf.inputs:
                bsdf.inputs['Emission Strength'].default_value = 1.0
            composited_features.append('staticLight (Emission)')

    # ---- 8. AO-Overlay (modifies BaseColor only) ----
    if base_color_source is not None and ao_source is not None:
        ao_ng = append_node_group(SN_AO_OVERLAY, snippets_blend_path, snippet_cache, report)
        if ao_ng is not None:
            ao_grp = nt.nodes.new('ShaderNodeGroup')
            ao_grp.node_tree = ao_ng
            ao_grp.location = (700, 250)
            ao_grp.label = "AO Overlay"
            nt.links.new(base_color_source, ao_grp.inputs['BaseColor'])
            nt.links.new(ao_source,         ao_grp.inputs['AO'])
            base_color_source = ao_grp.outputs['BaseColor']
            composited_features.append('AO')

    # ---- 9. Snow-Overlay — AFTER AO; modifies BaseColor + Roughness + Metallic ----
    # Trigger: mCustomSnowMask Custommap exists OR 'customSnowMask' in Variation.
    # SnowIntensity stays Default 0.0 (demo mode — user can use slider for visualization).
    # SnowMask is Float-Input -> grab R channel of mCustomSnowMask from Separate Color.
    snow_mask_tex = cm_tex.get('mCustomSnowMask')
    variation = mat_attrs.get('customShaderVariation', '') or ''
    snow_active = (snow_mask_tex is not None) or ('customSnowMask' in variation)
    if snow_active:
        snow_ng = append_node_group(SN_SNOW, snippets_blend_path, snippet_cache, report)
        if snow_ng is not None:
            snow_grp = nt.nodes.new('ShaderNodeGroup')
            snow_grp.node_tree = snow_ng
            snow_grp.location = (900, 100)
            snow_grp.label = "Snow Overlay"
            if base_color_source is not None:
                nt.links.new(base_color_source, snow_grp.inputs['BaseColor'])
            if roughness_source is not None:
                nt.links.new(roughness_source, snow_grp.inputs['Roughness'])
            if metallic_source is not None:
                nt.links.new(metallic_source, snow_grp.inputs['Metallic'])
            if snow_mask_tex is not None:
                snow_sep = nt.nodes.new('ShaderNodeSeparateColor')
                snow_sep.mode = 'RGB'
                snow_sep.location = (700, 100)
                snow_sep.label = "mCustomSnowMask -> R"
                nt.links.new(snow_mask_tex.outputs['Color'], snow_sep.inputs['Color'])
                nt.links.new(snow_sep.outputs['Red'], snow_grp.inputs['SnowMask'])
            base_color_source = snow_grp.outputs['BaseColor']
            roughness_source  = snow_grp.outputs['Roughness']
            metallic_source   = snow_grp.outputs['Metallic']
            composited_features.append('Snow')

            # SnowIntensity slider. Default reads from i3d snowScale custom
            # parameter when present, else 0.0 (no snow visible). The raw
            # value is also stored as custom property for re-export fidelity.
            sn_scale_raw = custom_params.get('snowScale')
            if sn_scale_raw is not None:
                mat['_i3d_pbr_snowScale'] = str(sn_scale_raw)
            try:
                sn_intensity = float(str(sn_scale_raw).split()[0]) \
                               if sn_scale_raw is not None else 0.0
            except (ValueError, IndexError):
                sn_intensity = 0.0
            if 'SnowIntensity' in snow_grp.inputs:
                _make_param_value(
                    nt, "snowIntensity", sn_intensity,
                    location=(700, 200), label="Snow Intensity (debug)",
                    target_input=snow_grp.inputs['SnowIntensity'],
                )

    # ---- 9.5. PlaceableColorTint at the very end of the BaseColor-Chain ---- 
    # Trigger: customParameter_placeableColorScale exists in i3d XML (= not
    # default '0.0 1.0 0.0 0.0'). Create also if W=0, so that user can play with
    # BlendFactor in the material editor. XYZ = TintColor, W = BlendFactor.
    pcs_raw = custom_params.get('placeableColorScale')
    if pcs_raw is not None and base_color_source is not None:
        pcs_parts = str(pcs_raw).split()
        if len(pcs_parts) >= 4:
            try:
                tint_r = float(pcs_parts[0])
                tint_g = float(pcs_parts[1])
                tint_b = float(pcs_parts[2])
                blend_factor = float(pcs_parts[3])
            except ValueError:
                tint_r, tint_g, tint_b, blend_factor = 1.0, 1.0, 1.0, 0.0
            tint_ng = append_node_group(SN_PLACEABLE_TINT, snippets_blend_path, snippet_cache, report)
            if tint_ng is not None:
                tint_grp = nt.nodes.new('ShaderNodeGroup')
                tint_grp.node_tree = tint_ng
                tint_grp.location = (1100, 100)
                tint_grp.label = "PlaceableColorTint"
                nt.links.new(base_color_source, tint_grp.inputs['BaseColor'])
                _make_param_rgb(
                    nt, "placeableColorScale_rgb", (tint_r, tint_g, tint_b),
                    xml_param="placeableColorScale", xml_slot="rgb",
                    location=(900, 50), label="Placeable Color (tint)",
                    target_input=tint_grp.inputs['TintColor'],
                )
                _make_param_value(
                    nt, "placeableColorScale_w", blend_factor,
                    xml_param="placeableColorScale", xml_slot="w",
                    location=(900, -30), label="Placeable Blend Factor",
                    target_input=tint_grp.inputs['BlendFactor'],
                )
                base_color_source = tint_grp.outputs['BaseColor']
                composited_features.append('Tint')

    # ---- 9.6. ColorShader Multi-Tint -------------------
    # Trigger: customShader file is colorShader.xml AND variation contains
    # 'colorScale'. In the shader: at uv.x<0 tint is active, Slot-Index =
    # floor(uv.y+8) mod 8 [0..7], colorScale[idx] is float4 (RGB + per-Slot
    # BlendFactor). Mask = mCustomMask.R. Missing slots stay on
    # shader defaults (from the snippet).
    csi = mat_attrs.get('customShaderId')
    shader_name = ''
    if csi is not None:
        try:
            fid = int(csi)
            raw_path = scene.files.get(fid)
            if raw_path:
                resolved = resolve_filepath(raw_path, i3d_dir)
                if resolved is not None:
                    shader_name = resolved.name.lower()
        except (ValueError, TypeError):
            pass

    ct_variation = mat_attrs.get('customShaderVariation', '') or ''
    if (shader_name == 'colorshader.xml' and 'colorScale' in ct_variation
            and base_color_source is not None):
        mt_ng = append_node_group(SN_COLOR_MULTITINT, snippets_blend_path,
                                  snippet_cache, report)
        if mt_ng is not None:
            mt_grp = nt.nodes.new('ShaderNodeGroup')
            mt_grp.node_tree = mt_ng
            mt_grp.location = (1300, -200)
            mt_grp.label = "ColorShader Multitint"

            # Per slot defaults from i3d-CustomParameters. Unset slots
            # stay on shader defaults (from snippet).
            for i in range(8):
                v = custom_params.get(f'colorScale{i}')
                if not v:
                    continue
                parts = str(v).split()
                if len(parts) < 4:
                    continue
                try:
                    r, g, b, a = (float(parts[0]), float(parts[1]),
                                  float(parts[2]), float(parts[3]))
                except ValueError:
                    continue
                tc = mt_grp.inputs.get(f'TintColor{i}')
                bf = mt_grp.inputs.get(f'BlendFactor{i}')
                if tc is not None:
                    _make_param_rgb(
                        nt, f"colorScale{i}_rgb", (r, g, b),
                        location=(1080, -200 - i * 80),
                        label=f"ColorMultitint Slot {i} Tint",
                        target_input=tc,
                        xml_param=f"colorScale{i}", xml_slot="rgb",
                    )
                if bf is not None:
                    _make_param_value(
                        nt, f"colorScale{i}_w", a,
                        location=(1080, -240 - i * 80),
                        label=f"ColorMultitint Slot {i} Blend",
                        target_input=bf,
                        xml_param=f"colorScale{i}", xml_slot="w",
                    )

            # contrastLuminiosity: pass to MaskContrast/MaskLuminosity 
            # sockets from snippet. Snippet implements the
            # sharedFunctions.gsl-Formula:
            #   mask_final = saturate((mask − 0.5) * contrast + 0.5 + luminosity)
            cl = custom_params.get('contrastLuminiosity')
            if cl is not None:
                parts = str(cl).split()
                if len(parts) >= 2:
                    try:
                        contrast = float(parts[0])
                        luminosity = float(parts[1])
                    except ValueError:
                        contrast, luminosity = 1.0, 0.0
                    mc = mt_grp.inputs.get('MaskContrast')
                    ml = mt_grp.inputs.get('MaskLuminosity')
                    if mc is not None:
                        _make_param_value(
                            nt, "contrastLuminiosity_x", contrast,
                            location=(1080, -900), label="Mask Contrast",
                            target_input=mc,
                            xml_param="contrastLuminiosity", xml_slot="x",
                        )
                    if ml is not None:
                        _make_param_value(
                            nt, "contrastLuminiosity_y", luminosity,
                            location=(1080, -980), label="Mask Luminosity",
                            target_input=ml,
                            xml_param="contrastLuminiosity", xml_slot="y",
                        )
                mat['_i3d_pbr_contrastLuminiosity'] = str(cl)

            # BaseColor Input
            nt.links.new(base_color_source, mt_grp.inputs['BaseColor'])

            # Mask: mCustomMask.R via Separate Color
            cmask_tex = cm_tex.get('mCustomMask')
            if cmask_tex is not None:
                mt_sep = nt.nodes.new('ShaderNodeSeparateColor')
                mt_sep.mode = 'RGB'
                mt_sep.location = (1100, -400)
                mt_sep.label = "mCustomMask -> R"
                nt.links.new(cmask_tex.outputs['Color'], mt_sep.inputs['Color'])
                nt.links.new(mt_sep.outputs['Red'], mt_grp.inputs['Mask'])
            # else: Mask stays default 1.0 (Snippet) -> Tint active everywhere

            # UVSource: own UVMap source based on baseMap UV setting from
            # Shader XML. Raw without mapping scale — shader code checks tile
            # position before each texture transformation.
            base_usage = uv_mapping.get('baseMap')
            u_type = base_usage.uv_type if base_usage else 'uv0'
            if u_type == 'worldspace':
                # worldspace -> triplanar (static in world space,
                # unwarped, consistent tile size).
                uv_out = _make_worldspace_triplanar_uv(
                    nt, snippets_blend_path, snippet_cache, report,
                    location=(1100, -550), label_suffix="(ColorMultitint)")
            else:
                blender_uv = ('UVMap' if u_type == 'custom'
                              else GIANTS_UV_TO_BLENDER_UV.get(u_type, 'UVMap'))
                uv_src = nt.nodes.new('ShaderNodeUVMap')
                uv_src.uv_map = blender_uv
                uv_src.location = (1100, -550)
                uv_src.label = f"UV {u_type} ({blender_uv})"
                uv_out = uv_src.outputs['UV']
            nt.links.new(uv_out, mt_grp.inputs['UVSource'])

            base_color_source = mt_grp.outputs['BaseColor']
            composited_features.append('ColorMultitint')

    # ---- 9.7. BuildingShader colorScale Multi-Tint ---------
    # Trigger: shader_name == buildingshader.xml AND 'colorScale' in variation.
    # Logic analoguous to Block 9.6 but modulate instead of Replace, NO mask,
    # NO contrastLuminiosity (snippet has clamp_result=True for saturate).
    if (shader_name == 'buildingshader.xml' and 'colorScale' in ct_variation
            and base_color_source is not None):
        bm_ng = append_node_group(SN_BUILDING_MULTITINT, snippets_blend_path,
                                  snippet_cache, report)
        if bm_ng is not None:
            bm_grp = nt.nodes.new('ShaderNodeGroup')
            bm_grp.node_tree = bm_ng
            bm_grp.location = (1500, -200)
            bm_grp.label = "BuildingShader Multitint"

            for i in range(8):
                v = custom_params.get(f'colorScale{i}')
                if not v:
                    continue
                parts = str(v).split()
                if len(parts) < 4:
                    continue
                try:
                    r, g, b, a = (float(parts[0]), float(parts[1]),
                                  float(parts[2]), float(parts[3]))
                except ValueError:
                    continue
                tc = bm_grp.inputs.get(f'TintColor{i}')
                bf = bm_grp.inputs.get(f'BlendFactor{i}')
                if tc is not None:
                    _make_param_rgb(
                        nt, f"colorScale{i}_rgb", (r, g, b),
                        location=(1280, -200 - i * 80),
                        label=f"BuildingMultitint Slot {i} Tint",
                        target_input=tc,
                        xml_param=f"colorScale{i}", xml_slot="rgb",
                    )
                if bf is not None:
                    _make_param_value(
                        nt, f"colorScale{i}_w", a,
                        location=(1280, -240 - i * 80),
                        label=f"BuildingMultitint Slot {i} Blend",
                        target_input=bf,
                        xml_param=f"colorScale{i}", xml_slot="w",
                    )

            nt.links.new(base_color_source, bm_grp.inputs['BaseColor'])

            # UVSource: own UVMap source based on baseMap UV.
            base_usage = uv_mapping.get('baseMap')
            u_type = base_usage.uv_type if base_usage else 'uv0'
            if u_type == 'worldspace':
                # worldspace -> triplanar (static in world space,
                # unwarped, consistent tile size).
                uv_out = _make_worldspace_triplanar_uv(
                    nt, snippets_blend_path, snippet_cache, report,
                    location=(1300, -400), label_suffix="(BuildingMultitint)")
            else:
                blender_uv = ('UVMap' if u_type == 'custom'
                              else GIANTS_UV_TO_BLENDER_UV.get(u_type, 'UVMap'))
                uv_src = nt.nodes.new('ShaderNodeUVMap')
                uv_src.uv_map = blender_uv
                uv_src.location = (1300, -400)
                uv_src.label = f"UV {u_type} ({blender_uv})"
                uv_out = uv_src.outputs['UV']
            nt.links.new(uv_out, bm_grp.inputs['UVSource'])

            base_color_source = bm_grp.outputs['BaseColor']
            composited_features.append('BuildingMultitint')

    # ---- 10. Final BSDF wiring ----
    if base_color_source is not None:
        nt.links.new(base_color_source, bsdf.inputs['Base Color'])
    if roughness_source is not None:
        nt.links.new(roughness_source, bsdf.inputs['Roughness'])
    if metallic_source is not None:
        nt.links.new(metallic_source, bsdf.inputs['Metallic'])
    if normal_source is not None:
        nt.links.new(normal_source, bsdf.inputs['Normal'])

    # ---- 11. Debug Switch ----
    # Wrap the BSDF -> Material Output link through a switch group that
    # can bypass the BSDF with an Emission of a mask or vertex color.
    # UV types come from uv_mapping (resolved from the shader XML in block
    # 1), so the debug mask displays with the correct UV channel.
    _debug_images = {name: tex.image for name, tex in cm_tex.items()
                     if getattr(tex, 'image', None) is not None}
    _debug_uv_types = {}
    for _name in _debug_images:
        _u = uv_mapping.get(_name)
        _debug_uv_types[_name] = _u.uv_type if _u is not None else 'uv0'
    _add_debug_switch(mat, bsdf, output_node,
                      debug_images=_debug_images,
                      debug_uv_types=_debug_uv_types)

    # ---- 12. Layout finalize ----
    # Stack params on the far left, push BSDF + Switch + Output to the
    # far right, frame the debug nodes.
    _finalize_layout(mat, bsdf, output_node)

    # Status-Log
    if cm_count > 0:
        variation = mat_attrs.get('customShaderVariation', '') or '(none)'
        if composited_features:
            features_str = ', '.join(composited_features)
            report('INFO',
                   f"PBR debug '{mat_name}': {cm_count} custom map(s), variation "
                   f"'{variation}' - composited: {features_str}")
        else:
            report('INFO',
                   f"PBR debug '{mat_name}': {cm_count} custom map(s), variation "
                   f"'{variation}' - no compositing rule applied "
                   f"(custom maps: {sorted(cm_tex.keys())}). Wire manually.")

    return mat


# ---------------------------------------------------------------------------
# Internal Helper: UV based on shader XML
# ---------------------------------------------------------------------------

def _resolve_shader_uv_mapping(
    mat_attrs: dict, scene, i3d_dir: Path,
    shader_cache: Dict[Path, "i3d_shader_parser.ShaderInfo"],
    resolve_filepath: Callable, report: Callable, mat_name: str,
) -> Dict[str, "i3d_shader_parser.ShaderUvUsage"]:
    """Load + parse the material's shader XML and resolve the customShaderVariation
    - returns {textureName: ShaderUvUsage}.

    On error or missing shader info: empty dict (-> caller falls back to UVMap
    default). shader_cache is reused per import - any shader file is parsed at
    most once per import.
    """
    csi = mat_attrs.get('customShaderId')
    if csi is None:
        return {}
    try:
        fid = int(csi)
    except (ValueError, TypeError):
        return {}
    raw_path = scene.files.get(fid)
    if not raw_path:
        return {}
    resolved = resolve_filepath(raw_path, i3d_dir)
    if resolved is None:
        return {}

    info = shader_cache.get(resolved)
    if info is None and resolved not in shader_cache:
        try:
            info = i3d_shader_parser.parse_shader(resolved)
        except Exception as e:
            report('WARNING',
                   f"PBR debug '{mat_name}': shader XML '{resolved.name}' "
                   f"not parsable - UV mapping falls back to UVMap: {e}")
            shader_cache[resolved] = None
            return {}
        shader_cache[resolved] = info

    if info is None:
        return {}

    variation = mat_attrs.get('customShaderVariation') or ''
    return i3d_shader_parser.resolve_uv_mapping(info, variation, report)


def _make_image_chain(
    nt, file_id, scene, i3d_dir,
    texture_name: str, label: str, non_color: bool,
    location_x: int, y_offset: int,
    uv_mapping: Dict[str, "i3d_shader_parser.ShaderUvUsage"],
    image_cache, resolve_filepath, image_loader, report, mat_name,
    pom_uv_source=None, pom_target_uv_type: Optional[str] = None,
    triplanar_uv_source=None, triplanar_texture_names=None,
    snippets_blend_path: Optional[Path] = None,
    snippet_cache: Optional[dict] = None,
):
    """Creates UV-Source (UVMap/TexCoord) + Mapping + Image-Tex wires them.

    UV choice is based on uv_mapping[texture_name] (from the shader XML).
    Default 'uv0' (UVMap) when texture_name is not in the mapping or uv_mapping
    is empty.

    POM-aware: when `pom_uv_source` and
    `pom_target_uv_type` are set AND this texture's uv_type matches
    (e.g. both 'uv0'), the POM vector replaces the UV-Map node as the
    mapping source. The mParallaxMap itself does NOT go through this path
    (handled separately in block 2.5 inside the closure zone).

    Triplanar-aware: when `triplanar_uv_source` and
    `triplanar_texture_names` are set AND `texture_name` exists in the
    collection, replace UV source with triplanar-Vector. Used for
    vehicleShader detail maps (uv_type='custom').

    Returns the image-tex node or None on error.
    """
    if file_id is None:
        return None
    path_str = scene.files.get(file_id)
    if not path_str:
        report('WARNING',
               f"PBR debug '{mat_name}': fileId {file_id} not in <Files>")
        return None
    resolved = resolve_filepath(path_str, i3d_dir)
    if resolved is None:
        report('WARNING',
               f"PBR debug '{mat_name}': file '{path_str}' not resolvable")
        return None
    img = image_loader(resolved, image_cache, report)
    if img is None:
        return None

    usage = uv_mapping.get(texture_name)
    uv_type = usage.uv_type if usage else 'uv0'
    uv_scale = (usage.uv_scale if (usage and usage.uv_scale is not None) else 1.0)

    # Triplanar path: when texture_name is in the set,
    # triplanar_uv_source replaces the UV source. Takes precedence over POM
    # because detail maps in vehicleShader have uv_type='custom' (not in the
    # POM set at all).
    use_triplanar = (triplanar_uv_source is not None
                     and triplanar_texture_names is not None
                     and texture_name in triplanar_texture_names)

    # POM path: pom_uv_source replaces the UV-Map node when uv_type matches.
    # Worldspace textures are excluded (POM is tangent-space-based).
    use_pom = (not use_triplanar
               and pom_uv_source is not None
               and pom_target_uv_type is not None
               and uv_type == pom_target_uv_type
               and uv_type != 'worldspace')

    if use_triplanar:
        src_out = triplanar_uv_source
    elif use_pom:
        src_out = pom_uv_source
    elif uv_type == 'worldspace':
        # worldspace -> triplanar UV from Geometry.Position +
        # Geometry.Normal (both worldspace), fed into fs25_VehicleTriplanar.
        # Static in world space, unwarped, consistent tile size - better than
        # the previous TexCoord.Camera (which moved with the camera).
        src_out = _make_worldspace_triplanar_uv(
            nt, snippets_blend_path, snippet_cache, report,
            location=(location_x - 800, y_offset),
            label_suffix=f"for {texture_name}")
    else:
        if uv_type == 'custom':
            # 'custom' = the shader computes these UVs internally (e.g.
            # mSeasonalCurve sampled by season, not a mesh UV). We can't
            # reproduce that from a mesh UV layer, so we fall back to
            # UVMap as a placeholder. This is an expected limitation, not
            # an error -> INFO, so it doesn't inflate the warning count.
            report('INFO',
                   f"PBR debug '{mat_name}': '{texture_name}' uvType='custom' "
                   f"(shader-computed) - using UVMap as a placeholder.")
            blender_uv = 'UVMap'
        else:
            blender_uv = GIANTS_UV_TO_BLENDER_UV.get(uv_type, 'UVMap')
        src_node = nt.nodes.new('ShaderNodeUVMap')
        src_node.uv_map = blender_uv
        src_node.location = (location_x - 800, y_offset)
        src_node.label = f"{uv_type} ({blender_uv}) for {texture_name}"
        src_out = src_node.outputs['UV']

    mapping = nt.nodes.new('ShaderNodeMapping')
    mapping.location = (location_x - 500, y_offset)
    if uv_scale != 1.0:
        try:
            mapping.inputs['Scale'].default_value = (uv_scale, uv_scale, uv_scale)
        except Exception:
            pass
    nt.links.new(src_out, mapping.inputs['Vector'])

    tex_node = _add_image_tex(nt, img, (location_x, y_offset),
                              non_color=non_color, label=label)
    nt.links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

    return tex_node
