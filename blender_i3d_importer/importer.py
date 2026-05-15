"""
Main logic for the FS25 i3d Importer.

Workflow:
  1. Parse .i3d XML -> I3DScene (hierarchy, materials, files)
  2. If inline shapes detected -> abort
  3. Run i3dToObjx.exe as subprocess -> produces temporary .objx or .spl.obj files
  4. .objx / .spl.obj filenames -> mapping shapeId -> Path
  5. Recursive walk over scene.roots:
       - TransformGroup/Light/Camera/ReferenceNode -> Empty
       - Shape -> mesh object (mesh sharing via (shapeId, materialIds))
       - Create materials + mesh slots + polygon.material_index
       - Set transforms (translation/rotation deg->rad/scale) + parenting
       - Optionally create debug materials
       - Optionally assign debug materials
       - Optionally rotate the scene (different up-axis between GE and Blender)
  6. Remove temporary files
"""

import math
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import bpy

from . import i3d_attr_mapping
from . import i3d_shader_parser
from . import i3d_xml_parser
from . import objx_parser
from . import recipe_loader

# Default paths. Overridden in import_i3d() when the add-on is invoked via
# the operator (which passes DEFAULT_TOOL_PATH plus the add-on preferences
# fs25_data_base and export_dir). Direct callers (e.g. test scripts) fall
# back to these module-level values.
TOOL_PATH      = str(Path(__file__).resolve().parent / "bin" / "i3dToObjx.exe")
FS25_DATA_BASE = ""
EXPORT_DIR     = ""

# Recipe loader for PBR debug materials.
# SNIPPETS_BLEND_PATH=None -> recipe_loader.default_snippets_blend_path() (add-on folder)
SNIPPETS_BLEND_PATH       = None
BUILD_PBR_DEBUG_MATERIALS = True
# when True, the debug material is attached to the mesh instead of
# the re-export material. Re-export is then not directly usable without manual swapping.
ATTACH_DEBUG_MATERIALS_TO_MESH = False

# Snippet cache: cleared per import (in import_i3d). Module variable instead
# of function parameter to avoid massive plumbing through 5 functions.
_snippet_cache: Dict[str, Optional["bpy.types.NodeTree"]] = {}

# Tool filename scheme: "NNN_<name>_<nodeId>.objx" (mesh) or
# "NNN_<name>_<nodeId>.spl.obj"
_OBJX_NAME_RE = re.compile(r'^(\d+)_(.+)_(\d+)\.objx$')
_SPL_NAME_RE  = re.compile(r'^(\d+)_(.+)_(\d+)\.spl\.obj$')

# mapping i3d Light type -> Blender bpy.data.lights.new(type=...)
_LIGHT_TYPE_MAP = {
    'directional': 'SUN',
    'point':       'POINT',
    'spot':        'SPOT',
}

# i3d Light attributes that flow into the bpy.data.lights datablock
# OR need light-specific handling in _set_meta_props. They are filtered out of
# raw_attrs there so they do NOT additionally land as _i3d_raw_<name> on the
# object via apply_attrs_to_object.
_LIGHT_DATABLOCK_ATTRS = {
    'type', 'color', 'range', 'castShadowMap',
    'coneAngle', 'dropOff',
    'iesProfileFileId',           # -> i3D_iesProfileFile via Files map (see _set_meta_props)
    'emitDiffuse', 'emitSpecular',  # -> _i3d_raw_* (Giants exporter does NOT read them)
}

# i3d Camera attributes that flow into the bpy.data.cameras datablock.
# Filtered out of raw_attrs in _set_meta_props.
_CAMERA_DATABLOCK_ATTRS = {
    'fov', 'nearClip', 'farClip',
    'orthographic', 'orthographicHeight',
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _create_objx_workdir():
    """Create a temporary working directory for the .objx and .spl.obj files
    produced by i3d-to-objx.

    Lives under extension_path_user (Blender 4.2+ extension API) or in the
    system tempdir (fallback for older Blender or legacy add-on installs that
    are not registered as extensions).

    Returns: Path to the fresh, empty subdirectory. The caller is responsible
    for cleanup via shutil.rmtree().
    """
    try:
        base = Path(bpy.utils.extension_path_user(
            __package__, path="objx_work", create=True))
    except (AttributeError, ValueError, RuntimeError):
        base = Path(tempfile.gettempdir()) / "blender_i3d_importer_objx_work"
        base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=str(base), prefix="import_"))


def import_i3d(i3d_filepath: str, report: Callable = None,
               apply_axis_correction: bool = True,
               auto_hide_invisible_shapes: bool = True,
               build_pbr_debug_materials: bool = True,
               attach_debug_materials_to_mesh: bool = False,
               tool_path: Optional[str] = None,
               fs25_data_base: Optional[str] = None,
               export_dir: Optional[str] = None,
               snippets_blend_path: Optional[str] = None) -> Tuple[int, int]:
    """Import an .i3d file. Returns: (shape_count, warning_count).

    apply_axis_correction: if True, after the tree walk an X+90 deg rotation is
    baked into all top-level objects and their geometry via the wrapper-empty
    trick (Y-up -> Z-up). Default True, paired with the inverse conversion in
    the Giants exporter for a correct roundtrip.

    auto_hide_invisible_shapes: If True (default), objects with
    visibility="false" or nonRenderable="true" (except terrainDecal="true")
    are hidden in the outliner via hide_set(True). Collected during the tree
    walk, applied AFTER axis correction (avoids any interaction with
    transform_apply).

    fs25_data_base / export_dir: When not None they
    override the module constants FS25_DATA_BASE / EXPORT_DIR for
    the duration of the import (internal helpers like _resolve_filepath read
    the globals). Filled by the operator from add-on preferences; direct
    callers may leave them None to fall back to the module defaults.
    """
    global TOOL_PATH, FS25_DATA_BASE, EXPORT_DIR, SNIPPETS_BLEND_PATH, BUILD_PBR_DEBUG_MATERIALS, ATTACH_DEBUG_MATERIALS_TO_MESH
    if tool_path:
        TOOL_PATH = tool_path
    if fs25_data_base:
        FS25_DATA_BASE = fs25_data_base
    if export_dir:
        EXPORT_DIR = export_dir
    if snippets_blend_path:
        SNIPPETS_BLEND_PATH = snippets_blend_path
    BUILD_PBR_DEBUG_MATERIALS = build_pbr_debug_materials
    ATTACH_DEBUG_MATERIALS_TO_MESH = attach_debug_materials_to_mesh

    # Reset snippet cache per import
    _snippet_cache.clear()

    def _report(level, msg: str):
        if report:
            report({level}, msg)
        else:
            print(f"[{level}] {msg}")

    i3d = Path(i3d_filepath)
    i3d_dir = i3d.parent

    # 1. Parse XML
    try:
        scene = i3d_xml_parser.parse_i3d(i3d)
    except Exception as e:
        raise RuntimeError(f"Could not parse .i3d XML: {e}") from e

    _report('INFO', f"XML: {len(scene.roots)} root node(s), "
                    f"{len(scene.materials)} material(s), {len(scene.files)} file(s)")

    # 2. Inline shape data: in practice only in data/sky/*.i3d
    # as <Precipitation> (weather particle spawner, no mesh geometry).
    # Instead of aborting: WARNING + skip. Re-export is lost for these special
    # shapes; hierarchy / lights / cameras / ReferenceNodes still come through.
    if scene.has_inline_shapes:
        _report('WARNING',
                "Inline shape data detected in XML (e.g. <Precipitation>) - "
                "will NOT be imported. Hierarchy/lights/cameras come through.")

    # 2b. Shape presence check: tool + .i3d.shapes only needed when
    # the scene actually contains <Shape> nodes. Hierarchy-only files (e.g.
    # *Props.i3d with only ReferenceNodes/TransformGroups) would otherwise abort
    # with FileNotFoundError.
    workdir = None  # objx workdir, removed in finally block
    try:
        has_shapes = any(_has_any_shape_nodes(r) for r in scene.roots)
        warning_count = 0
        objx_map: Dict[int, Path] = {}
        spline_map: Dict[int, Path] = {}

        if has_shapes:
            # Check tool and .shapes file
            shapes_file = i3d_dir / (i3d.name + ".shapes")
            if not shapes_file.exists():
                raise FileNotFoundError(f"No .i3d.shapes file found next to {i3d.name}.")

            tool = Path(TOOL_PATH)
            if not tool.exists():
                raise FileNotFoundError(
                    f"i3d-to-objx not found at:\n  {TOOL_PATH}\n"
                    f"The add-on installation appears incomplete - "
                    f"please reinstall the add-on."
                )

            # 3. Tool call -> .objx files in a temporary workdir
            # (removed in the finally block).
            workdir = _create_objx_workdir()
            call_start_time = time.time()
            _report('INFO', f"Calling i3d-to-objx: {shapes_file.name}")
            # i3d-to-objx ignores cwd and uses
            # Path.GetDirectoryName(options.File) as the output folder by default
            # (Program.cs lines 365-366). Steer explicitly with --out
            result = subprocess.run(
                [str(tool), "--out", str(workdir), str(shapes_file)],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"i3d-to-objx exit code {result.returncode}\n"
                    f"stdout: {result.stdout[:500]}\n"
                    f"stderr: {result.stderr[:500]}"
                )

            for line in result.stdout.splitlines():
                if line.startswith('[WARNING]'):
                    _report('WARNING', f"Tool: {line}")
                    warning_count += 1

            new_objx = sorted(workdir.glob("*.objx"))
            new_spl = sorted(workdir.glob("*.spl.obj"))
            if not new_objx and not new_spl:
                _report('WARNING', "No new .objx/.spl.obj files produced. Tool output:")
                _report('WARNING', result.stdout[:500])
                return 0, warning_count + 1

            # 4. shapeId -> Path mapping (Mesh OBJx)
            for p in new_objx:
                m = _OBJX_NAME_RE.match(p.name)
                if not m:
                    _report('WARNING', f"Unexpected tool filename, skipped: {p.name}")
                    continue
                shape_id = int(m.group(1))
                if shape_id in objx_map:
                    _report('WARNING', f"Duplicate shapeId {shape_id} in .objx files, "
                                       f"keeping first found: {objx_map[shape_id].name}")
                    continue
                objx_map[shape_id] = p

            # 4b. shapeId -> Path mapping (Spline .spl.obj - via tool patch 03)
            for p in new_spl:
                m = _SPL_NAME_RE.match(p.name)
                if not m:
                    _report('WARNING', f"Unexpected spline filename, skipped: {p.name}")
                    continue
                shape_id = int(m.group(1))
                if shape_id in spline_map:
                    _report('WARNING', f"Duplicate shapeId {shape_id} in .spl.obj files, "
                                       f"keeping first found: {spline_map[shape_id].name}")
                    continue
                spline_map[shape_id] = p

            _report('INFO',
                    f"{len(objx_map)} OBJx + {len(spline_map)} spline file(s) parsed (shapeId -> Path)")
        else:
            _report('INFO',
                    "No shape nodes in scene - skipping tool call "
                    "(hierarchy-only import).")

        # 5. Top-level collection
        collection_name = i3d.stem
        if collection_name in bpy.data.collections:
            n = 1
            while f"{collection_name}.{n:03d}" in bpy.data.collections:
                n += 1
            collection_name = f"{collection_name}.{n:03d}"
        import_collection = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(import_collection)

        # 6. Per-import caches (NOT global - see architecture decision)
        mesh_cache: Dict[Tuple[int, Tuple[int, ...]], bpy.types.Mesh] = {}
        material_cache: Dict[int, bpy.types.Material] = {}
        image_cache: Dict[Path, bpy.types.Image] = {}
        shader_cache: Dict[Path, i3d_shader_parser.ShaderInfo] = {}

        counts = {'shapes_imported': 0, 'empties': 0, 'lights': 0, 'cameras': 0,
                  'splines': 0, 'shapes_missing_objx': 0, 'hidden': 0}

        # collect hides during tree walk, apply AFTER axis correction.
        objects_to_hide: list = []

        for root in scene.roots:
            _build_node(root, parent=None, collection=import_collection,
                        scene=scene,
                        mesh_cache=mesh_cache, material_cache=material_cache,
                        image_cache=image_cache, shader_cache=shader_cache,
                        objx_map=objx_map, spline_map=spline_map,
                        i3d_dir=i3d_dir, counts=counts,
                        objects_to_hide=objects_to_hide, report=_report)

        # distribute skinBindNodeIds -> i3D_mergeGroup properties.
        # Re-export fidelity goes via the Giants exporter's mergeGroup path
        # (i3d_export.py:505-518) - generates skinBindNodeIds from the
        # i3D_mergeGroup/i3D_mergeGroupRoot properties on the objects.
        _process_skin_bindings(import_collection, _report)

        # axis correction Y-up -> Z-up after the complete hierarchy is
        # built (so all top-level objects exist and the wrapper-empty trick works).
        if apply_axis_correction:
            top_level = [obj for obj in import_collection.objects if obj.parent is None]
            if top_level:
                _apply_axis_correction(top_level, _report)

        # apply hide AFTER axis correction. hide_set matches the H
        # shortcut (view-layer eye). On Giants re-export this leads to
        # visibility="false" in the XML because the exporter reads
        # visible_in_viewport_get() - re-export-true for source visibility="false",
        # drift for nonRenderable->visibility is intentionally accepted
        # (see _should_hide_for_visibility).
        if auto_hide_invisible_shapes and objects_to_hide:
            for o in objects_to_hide:
                try:
                    o.hide_set(True)
                    counts['hidden'] += 1
                except RuntimeError as e:
                    _report('WARNING', f"hide_set for '{o.name}' failed: {e}")

        # Clear selection - user request: nothing selected after import.
        # (With axis correction active, all top-level objects + children would
        # otherwise stay selected because the wrapper-empty trick must select
        # everything for transform_apply and does not reset the selection on unparent.)
        try:
            bpy.ops.object.select_all(action='DESELECT')
        except RuntimeError:
            for obj in list(bpy.context.selected_objects):
                obj.select_set(False)
        bpy.context.view_layer.objects.active = None

        _report('INFO',
                f"Import done: {counts['shapes_imported']} shapes, "
                f"{counts['empties']} empties, "
                f"{counts['lights']} lights, "
                f"{counts['cameras']} cameras, "
                f"{counts['splines']} splines, "
                f"{counts['hidden']} hidden, "
                f"{counts['shapes_missing_objx']} shapes without .objx, "
                f"{len(material_cache)} material(s), {len(image_cache)} image(s) loaded")

        if counts['shapes_missing_objx'] > 0:
            warning_count += counts['shapes_missing_objx']

        # configure the Giants i3d Exporter automatically so re-export
        # works without manual UI setup.
        #   - i3D_gameLocationDisplay needs a trailing backslash (see
        #     io_export_i3d_10_0_2/util/i3d_shaderUtil.py:21 - string concat without sep)
        #   - i3D_exportUseSoftwareFileName=False so the "Use Blender Filename"
        #     checkbox is off and exportFileLocation takes effect
        #   - i3D_exportFileLocation = EXPORT_DIR + basename of the imported i3d
        try:
            settings = bpy.context.scene.I3D_UIexportSettings
            settings.i3D_gameLocationDisplay = FS25_DATA_BASE + "\\"
            settings.i3D_exportUseSoftwareFileName = False
            if EXPORT_DIR:
                settings.i3D_exportFileLocation = str(Path(EXPORT_DIR) / i3d.name)
                _report('INFO',
                        f"Giants exporter configured: "
                        f"gameLocation={FS25_DATA_BASE}, exportFile={i3d.name}")
            else:
                _report('INFO',
                        "Export Directory in the Giants i3d Exporter was not set "
                        "due to missing preference setting for export directory")
                _report('INFO',
                        f"Giants exporter gameLocation set to: {FS25_DATA_BASE}")
        except AttributeError:
            _report('INFO',
                    "Giants i3d Exporter not installed - "
                    "skipping export property setup")

        return counts['shapes_imported'], warning_count
    finally:
        if workdir is not None:
            shutil.rmtree(str(workdir), ignore_errors=True)


# ---------------------------------------------------------------------------
# Tree-Walk + Object-Erzeugung
# ---------------------------------------------------------------------------

def _has_any_shape_nodes(node) -> bool:
    """Recursively check whether `node` or any descendant has kind=='Shape'.

    When the scene contains no shape nodes at all (typical for
    *Props.i3d / mission spots / similar hierarchy-only files) we need neither
    i3d-to-objx nor an .i3d.shapes file - the tool call is skipped in
    import_i3d.
    """
    if node.kind == 'Shape':
        return True
    return any(_has_any_shape_nodes(c) for c in node.children)


def _build_node(node, parent, collection, scene, mesh_cache, material_cache,
                image_cache, shader_cache, objx_map, spline_map, i3d_dir, counts,
                objects_to_hide, report):
    """Recursively create the object for `node`, parent it, then walk children."""
    if node.kind == 'Shape':
        obj = _create_mesh_object(node, scene, mesh_cache, material_cache,
                                  image_cache, shader_cache, objx_map, spline_map,
                                  i3d_dir, counts, report)
    elif node.kind == 'Light':
        obj = _create_light_object(node, report)
        if obj is not None:
            counts['lights'] += 1
    elif node.kind == 'Camera':
        obj = _create_camera_object(node, report)
        if obj is not None:
            counts['cameras'] += 1
    else:
        obj = _create_empty(node)
        if obj is not None:
            counts['empties'] += 1

    if obj is None:
        next_parent = parent
    else:
        collection.objects.link(obj)
        _apply_transform(obj, node)
        _set_meta_props(obj, node, scene, report)
        if parent is not None:
            obj.parent = parent
        # collect hide check (applied later, after axis correction).
        if _should_hide_for_visibility(node):
            objects_to_hide.append(obj)
        next_parent = obj

    for child in node.children:
        _build_node(child, parent=next_parent, collection=collection, scene=scene,
                    mesh_cache=mesh_cache, material_cache=material_cache,
                    image_cache=image_cache, shader_cache=shader_cache,
                    objx_map=objx_map, spline_map=spline_map,
                    i3d_dir=i3d_dir, counts=counts,
                    objects_to_hide=objects_to_hide, report=report)


def _create_mesh_object(node, scene, mesh_cache, material_cache, image_cache,
                        shader_cache, objx_map, spline_map, i3d_dir, counts, report):
    """Mesh OR curve object for a Shape node.
    Mesh path: sharing via (shapeId, materialIds tuple).
    Spline path: no sharing - each spline gets its own curve datablock.
    """
    shape_id = node.shapeId
    if shape_id is None:
        report('WARNING', f"Shape '{node.name}' without shapeId - skipped")
        counts['shapes_missing_objx'] += 1
        return None

    # spline path preferred when the shapeId points to a spline.
    if shape_id in spline_map:
        try:
            obj = _create_curve_object(node, spline_map[shape_id], report)
        except Exception as e:
            report('ERROR',
                   f"Spline build for '{node.name}' (shapeId {shape_id}) failed: {e}")
            return None
        if obj is not None:
            counts['splines'] += 1
        return obj

    cache_key = (shape_id, tuple(node.materialIds))
    mesh = mesh_cache.get(cache_key)
    if mesh is None:
        objx_path = objx_map.get(shape_id)
        if objx_path is None:
            report('WARNING', f"No .objx for shapeId {shape_id} (Shape '{node.name}')")
            counts['shapes_missing_objx'] += 1
            return None
        try:
            mesh = _build_mesh_datablock(
                objx_path, datablock_name=node.name,
                node=node, scene=scene,
                material_cache=material_cache, image_cache=image_cache,
                shader_cache=shader_cache,
                i3d_dir=i3d_dir, report=report,
            )
        except Exception as e:
            report('ERROR',
                   f"Mesh build for '{node.name}' (shapeId {shape_id}) failed: {e}")
            return None
        mesh_cache[cache_key] = mesh

    obj = bpy.data.objects.new(name=node.name, object_data=mesh)
    # propagate snow-heap flag from mesh materials to the object,
    # so the N-Panel Show/Hide operators can find them via a flat scan.
    for _slot_mat in mesh.materials:
        if _slot_mat is not None and _slot_mat.get('_i3d_is_snow_heap'):
            obj['_i3d_is_snow_heap'] = True
            break
    counts['shapes_imported'] += 1
    return obj


def _build_mesh_datablock(objx_path, datablock_name, node, scene, material_cache,
                          image_cache, shader_cache, i3d_dir, report):
    """Parse OBJx, create Blender mesh datablock, assign materials."""
    md = objx_parser.parse(objx_path)

    mesh = bpy.data.meshes.new(name=datablock_name)
    face_v_indices = [tuple(idx[0] for idx in face) for face in md.faces]
    mesh.from_pydata(md.vertices, [], face_v_indices)
    mesh.update(calc_edges=True)

    # UV1
    if md.uvs and md.faces:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        loop_idx = 0
        for face in md.faces:
            for vert_ref in face:
                uv_idx = vert_ref[1]
                if uv_idx is not None and 0 <= uv_idx < len(md.uvs):
                    u, v = md.uvs[uv_idx]
                    uv_layer.data[loop_idx].uv = (u, v)
                loop_idx += 1

    # Multi-UV - UV2/UV3/UV4. The index in the f line is the same as for UV1.
    for uvs_n, layer_name in ((md.uvs2, "UV2"), (md.uvs3, "UV3"), (md.uvs4, "UV4")):
        if not uvs_n or not md.faces:
            continue
        uv_layer = mesh.uv_layers.new(name=layer_name)
        loop_idx = 0
        for face in md.faces:
            for vert_ref in face:
                uv_idx = vert_ref[1]
                if uv_idx is not None and 0 <= uv_idx < len(uvs_n):
                    u, v = uvs_n[uv_idx]
                    uv_layer.data[loop_idx].uv = (u, v)
                loop_idx += 1

    # Vertex colors as a color attribute on CORNER domain.
    # CORNER (= per loop / face-corner), not POINT - that's what the Giants
    # exporter expects on later re-export. Per loop, the color from the
    # corresponding vertex is replicated.
    if md.vertex_colors and len(md.vertex_colors) == len(md.vertices) and md.faces:
        color_layer = mesh.color_attributes.new(
            name="Color", type='FLOAT_COLOR', domain='CORNER'
        )
        loop_idx = 0
        for face in md.faces:
            for vert_ref in face:
                v_idx = vert_ref[0]
                if v_idx is not None and 0 <= v_idx < len(md.vertex_colors):
                    color_layer.data[loop_idx].color = md.vertex_colors[v_idx]
                loop_idx += 1

    # Material slots: one slot per materialId from the XML node.
    for mat_id in node.materialIds:
        mat = _get_or_create_material(
            mat_id, scene, material_cache, image_cache, shader_cache, i3d_dir, report
        )
        mesh.materials.append(mat)

    # Set polygon.material_index per face.
    if node.materialIds and md.face_subsets:
        max_slot = len(node.materialIds) - 1
        n_polys = len(mesh.polygons)
        for poly_idx, subset_idx in enumerate(md.face_subsets):
            if poly_idx >= n_polys:
                break
            mesh.polygons[poly_idx].material_index = min(subset_idx, max_slot)

    return mesh


# ---------------------------------------------------------------------------
# Empty / transform / meta
# ---------------------------------------------------------------------------

def _should_hide_for_visibility(node) -> bool:
    """check whether the node should be hidden in Blender based on
    GE visibility attributes (via hide_set(True) after axis correction).

    Hide rules (boolean OR, applies to all node kinds):
      visibility="false"  -> hide. Re-export-true (the exporter writes it back
                          automatically via visible_in_viewport_get()).
      nonRenderable="true" AND NOT terrainDecal="true"  -> hide. Re-export
                          additionally writes visibility="false" to the XML -
                          drift is harmless because the object was not
                          renderable anyway.

    Defaults in the i3d schema: visibility=true, nonRenderable=false,
    terrainDecal=false.

    renderedInViewports and lambert1 materials are INTENTIONALLY not in the
    logic: renderedInViewports is purely a GE editor visibility flag (the
    object is still visible in-game!) - hide_set would hide it in-game.
    lambert1 is also excluded because the XML visibility attributes cover the
    use case more cleanly.
    """
    raw = node.raw_attrs
    if not raw:
        return False
    vis = str(raw.get('visibility', 'true')).strip().lower()
    if vis in ('false', '0', 'no'):
        return True
    non_renderable = str(raw.get('nonRenderable', 'false')).strip().lower() in ('true', '1', 'yes')
    terrain_decal  = str(raw.get('terrainDecal',  'false')).strip().lower() in ('true', '1', 'yes')
    if non_renderable and not terrain_decal:
        return True
    return False


def _create_empty(node):
    """Empty object for TransformGroup/Camera/ReferenceNode.

    Lights are handled separately via _create_light_object.
    """
    obj = bpy.data.objects.new(name=node.name, object_data=None)
    if node.kind == 'TransformGroup':
        obj.empty_display_type = 'PLAIN_AXES'
    elif node.kind == 'Camera':
        obj.empty_display_type = 'CUBE'
    elif node.kind == 'ReferenceNode':
        obj.empty_display_type = 'ARROWS'
    obj.empty_display_size = 0.5
    return obj


def _create_light_object(node, report):
    """Light object with a real bpy.data.lights datablock.

    Datablock properties from i3d attributes (verified in
    io_export_i3d_10_0_2/dcc/dccBlender.py:getLightData - import is the inverse
    of those export mappings):
      type        directional/point/spot   -> SUN/POINT/SPOT
      color       "r g b" (linear)         -> color
      range       float (m)                -> cutoff_distance
      castShadowMap "true/false"           -> use_shadow
      coneAngle   deg (spot only)          -> spot_size (math.radians)
      dropOff     float (spot only)        -> spot_blend (dropOff / 5.0)

    Re-export loss:
      emitDiffuse/emitSpecular are NOT read back from the Blender object by
      the Giants exporter (see getLightData). _set_meta_props stores them as
      _i3d_raw_* so the value stays documented.

    Other i3d attributes (softShadows*, scattering*, iesProfileFile, ...) are
    set as object custom properties in _set_meta_props.
    """
    raw = node.raw_attrs

    i3d_type = raw.get('type', 'directional')
    blender_type = _LIGHT_TYPE_MAP.get(i3d_type)
    if blender_type is None:
        report('WARNING',
               f"Light '{node.name}' unknown type='{i3d_type}' "
               f"- falling back to SUN")
        blender_type = 'SUN'

    light_data = bpy.data.lights.new(name=node.name, type=blender_type)

    color_str = raw.get('color')
    if color_str:
        parts = color_str.split()
        if len(parts) >= 3:
            try:
                light_data.color = (float(parts[0]), float(parts[1]), float(parts[2]))
            except ValueError:
                report('WARNING',
                       f"Light '{node.name}' invalid color='{color_str}'")

    range_str = raw.get('range')
    if range_str is not None:
        try:
            light_data.cutoff_distance = float(range_str)
        except ValueError:
            report('WARNING',
                   f"Light '{node.name}' invalid range='{range_str}'")

    shadow_str = raw.get('castShadowMap')
    if shadow_str is not None:
        light_data.use_shadow = str(shadow_str).strip().lower() in ('true', '1', 'yes')

    if blender_type == 'SPOT':
        cone_str = raw.get('coneAngle')
        if cone_str is not None:
            try:
                light_data.spot_size = math.radians(float(cone_str))
            except ValueError:
                report('WARNING',
                       f"Light '{node.name}' invalid coneAngle='{cone_str}'")
        drop_str = raw.get('dropOff')
        if drop_str is not None:
            try:
                # Inverse of the export formula: exporter writes dropOff = 5.0 * spot_blend
                light_data.spot_blend = float(drop_str) / 5.0
            except ValueError:
                report('WARNING',
                       f"Light '{node.name}' invalid dropOff='{drop_str}'")

    return bpy.data.objects.new(name=node.name, object_data=light_data)


def _create_camera_object(node, report):
    """Camera object with a real bpy.data.cameras datablock.

    Datablock properties from i3d attributes (verified as the inverse of
    io_export_i3d_10_0_2/dcc/dccBlender.py:getCameraData):
      fov                  -> lens (mm, 1:1)  WARNING: the attribute is named
                                             "fov" but is the focal length in
                                             mm - the exporter writes data.lens
                                             directly into the fov XML field.
      nearClip             -> clip_start
      farClip              -> clip_end
      orthographic="true"  -> type = 'ORTHO' (otherwise 'PERSP' = default)
      orthographicHeight   -> ortho_scale (ORTHO only)
    """
    raw = node.raw_attrs
    camera_data = bpy.data.cameras.new(name=node.name)

    fov_str = raw.get('fov')
    if fov_str is not None:
        try:
            camera_data.lens = float(fov_str)
        except ValueError:
            report('WARNING',
                   f"Camera '{node.name}' invalid fov='{fov_str}'")

    near_str = raw.get('nearClip')
    if near_str is not None:
        try:
            camera_data.clip_start = float(near_str)
        except ValueError:
            report('WARNING',
                   f"Camera '{node.name}' invalid nearClip='{near_str}'")

    far_str = raw.get('farClip')
    if far_str is not None:
        try:
            camera_data.clip_end = float(far_str)
        except ValueError:
            report('WARNING',
                   f"Camera '{node.name}' invalid farClip='{far_str}'")

    ortho_str = raw.get('orthographic')
    if ortho_str is not None and str(ortho_str).strip().lower() in ('true', '1', 'yes'):
        camera_data.type = 'ORTHO'
        height_str = raw.get('orthographicHeight')
        if height_str is not None:
            try:
                camera_data.ortho_scale = float(height_str)
            except ValueError:
                report('WARNING',
                       f"Camera '{node.name}' invalid orthographicHeight='{height_str}'")

    return bpy.data.objects.new(name=node.name, object_data=camera_data)


def _apply_transform(obj, node):
    """Translation 1:1, rotation deg -> rad, scale 1:1."""
    obj.location = node.translation
    obj.rotation_mode = 'XYZ'
    obj.rotation_euler = (
        math.radians(node.rotation[0]),
        math.radians(node.rotation[1]),
        math.radians(node.rotation[2]),
    )
    obj.scale = node.scale


def _set_meta_props(obj, node, scene, report):
    """Identity info + re-export-relevant XML attributes as custom properties.

    Identity (for debug): _i3d_kind, _i3d_nodeId.
    Re-export fidelity:
      - Generic mapping via i3d_attr_mapping.apply_attrs_to_object
        -> typed i3D_<name> properties per Giants exporter schema.
      - Special case ReferenceNode: referenceId -> Files-map lookup
        -> i3D_referenceFilename (path as expected by the exporter).
    """
    # Identity info (debug aid, NOT used for re-export)
    obj['_i3d_kind'] = node.kind
    obj['_i3d_nodeId'] = node.nodeId

    # ReferenceNode-specific: referenceId -> i3D_referenceFilename
    # (referenceRuntimeLoaded handled generically via apply_attrs_to_object)
    if node.kind == 'ReferenceNode':
        ref_id_str = node.raw_attrs.get('referenceId')
        if ref_id_str is not None:
            try:
                ref_id = int(ref_id_str)
                filename = scene.files.get(ref_id)
                if filename is not None:
                    obj['i3D_referenceFilename'] = filename
                else:
                    report('WARNING',
                           f"{obj.name}: referenceId {ref_id} not in <Files>")
            except ValueError:
                report('WARNING',
                       f"{obj.name}: invalid referenceId '{ref_id_str}'")

        # i3D_referenceChildPath MUST exist on the object, otherwise KeyError
        # in the Giants exporter (i3d_export.py:900 uses data["..."] without
        # try/except). An empty string means "no ChildPath" - the exporter then
        # skips the XML output (len check in checkRefChildPathFormat).
        ref_child_path = node.raw_attrs.get('referenceChildPath', '')
        obj['i3D_referenceChildPath'] = str(ref_child_path)

    # Light-specific:
    #   - iesProfileFileId → i3D_iesProfileFile via Files-Map (analog
    #     referenceId -> i3D_referenceFilename for ReferenceNode)
    #   - emitDiffuse/emitSpecular: no re-export path in the Giants exporter
    #     (see dcc/dccBlender.py:getLightData) -> stored as _i3d_raw_* so the
    #     value is not silently lost.
    #   - Datablock attributes (type/color/range/castShadowMap/coneAngle/dropOff)
    #     are filtered out - they live on the bpy.data.lights datablock and
    #     should NOT additionally end up as _i3d_raw_* on the object.
    raw = node.raw_attrs
    if node.kind == 'Light':
        ies_id_str = raw.get('iesProfileFileId')
        if ies_id_str is not None:
            try:
                ies_id = int(ies_id_str)
                ies_path = scene.files.get(ies_id)
                if ies_path is not None:
                    obj['i3D_iesProfileFile'] = ies_path
                else:
                    report('WARNING',
                           f"{obj.name}: iesProfileFileId {ies_id} not in <Files>")
            except ValueError:
                report('WARNING',
                       f"{obj.name}: invalid iesProfileFileId '{ies_id_str}'")
        for k in ('emitDiffuse', 'emitSpecular'):
            if k in raw:
                obj[f'_i3d_raw_{k}'] = str(raw[k])
        raw = {k: v for k, v in raw.items() if k not in _LIGHT_DATABLOCK_ATTRS}

    # Camera-specific: filter out datablock attributes (fov/nearClip/
    # farClip/orthographic/orthographicHeight) - they live on the
    # bpy.data.cameras datablock and should NOT additionally end up as
    # _i3d_raw_* on the object.
    if node.kind == 'Camera':
        raw = {k: v for k, v in raw.items() if k not in _CAMERA_DATABLOCK_ATTRS}

    # Generic attribute mapping (automatically skips
    # name/nodeId/shapeId/translation/rotation/scale/materialIds and the SPECIAL_ATTRS)
    i3d_attr_mapping.apply_attrs_to_object(obj, raw, report)


# ---------------------------------------------------------------------------
# Material builder
# ---------------------------------------------------------------------------

def _get_or_create_material(material_id, scene, material_cache, image_cache,
                            shader_cache, i3d_dir, report):
    """Fetch material from cache or build + cache it."""
    mat = material_cache.get(material_id)
    if mat is not None:
        return mat
    mat = _build_material(material_id, scene, image_cache, shader_cache, i3d_dir, report)
    material_cache[material_id] = mat
    return mat


def _is_snow_heap_material(mat_attrs, scene, i3d_dir):
    """True if the material's customShader points to snowHeapShader.xml.

    Used by Step 5 to flag snow-heap meshes for the N-Panel Show/Hide
    buttons. Detection is purely path-based - works for FS25 and FS22
    since both use the same shader filename.
    """
    csi = mat_attrs.get('customShaderId')
    if csi is None:
        return False
    try:
        fid = int(csi)
    except (ValueError, TypeError):
        return False
    raw = scene.files.get(fid)
    if not raw:
        return False
    resolved = _resolve_filepath(raw, i3d_dir)
    if resolved is None:
        return False
    return resolved.name.lower() == 'snowheapshader.xml'


def _build_material(material_id, scene, image_cache, shader_cache, i3d_dir, report):
    """
    Blender material with Principled BSDF node graph from i3d XML material data.
    Maps: diffuse (Texture), normal (Normalmap), roughness (Glossmap, inverted).
    """
    mat_attrs = scene.materials.get(material_id, {})
    mat_name = mat_attrs.get('name', f'i3d_material_{material_id}')

    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    # Set fake_user on every re-export material - when the debug material is
    # attached to the mesh via ATTACH_DEBUG_MATERIALS_TO_MESH, the re-export
    # material would otherwise have no user and be lost on file save/cleanup.
    mat.use_fake_user = True
    # Material identification for the switch operator (N-Panel "i3d Importer"):
    # _i3d_material_id matches the counterpart (debug material) via the same ID.
    # _i3d_material_kind distinguishes "debug" vs "export". Robust against
    # material renames by the user.
    mat['_i3d_material_id'] = material_id
    mat['_i3d_material_kind'] = 'export'
    mat['_i3d_materialId'] = material_id  # legacy, kept for backward compat

    nt = mat.node_tree
    bsdf = nt.nodes.get('Principled BSDF')
    if bsdf is None:
        # Very unusual, but fallback: empty material
        return mat

    # Workaround for Giants exporter bug: Blender 4.x+ has default Emission
    # Color (1,1,1,1) with Strength=0 (no light), but the exporter only checks
    # color and incorrectly exports emissiveColor="1 1 1 1". Explicitly set to 0
    # - safe even independently of exporter patch 02.
    if 'Emission Color' in bsdf.inputs:
        bsdf.inputs['Emission Color'].default_value = (0, 0, 0, 1)

    # 1. Diffuse color (default base color when no texture overrides it).
    # i3d values are linear; Blender's default_value also expects linear.
    # Direct assignment - NO sRGB conversion (the color picker shows an sRGB
    # display, but that is only the display; internally + on Giants re-export
    # the values stay linear correct).
    diffuse = mat_attrs.get('diffuseColor')
    if diffuse:
        rgba = _parse_vec4(diffuse, default=(1.0, 1.0, 1.0, 1.0))
        bsdf.inputs['Base Color'].default_value = rgba
        if rgba[3] < 1.0 and 'Alpha' in bsdf.inputs:
            bsdf.inputs['Alpha'].default_value = rgba[3]
            mat.blend_method = 'BLEND'

    # UV-Map + Mapping node - lazily initialized only when at least one image
    # texture is actually created. This makes it visible in the shader editor
    # which UV map the textures use, and the user can change it if needed.
    uv_state = {'mapping_node': None}

    def _ensure_uv_mapping():
        if uv_state['mapping_node'] is None:
            uv_node = nt.nodes.new('ShaderNodeUVMap')
            uv_node.uv_map = "UVMap"
            uv_node.location = (-1500, 0)
            mapping = nt.nodes.new('ShaderNodeMapping')
            mapping.location = (-1200, 0)
            nt.links.new(uv_node.outputs['UV'], mapping.inputs['Vector'])
            uv_state['mapping_node'] = mapping
        return uv_state['mapping_node']

    def _add_image_node(file_id, location, non_color: bool):
        """Create an image texture node (or None when the image is not available)."""
        if file_id is None:
            return None
        path_str = scene.files.get(file_id)
        if not path_str:
            report('WARNING',
                   f"Material {material_id} '{mat_name}': "
                   f"fileId {file_id} not in <Files>")
            return None
        resolved = _resolve_filepath(path_str, i3d_dir)
        if resolved is None:
            if path_str.lower().endswith('.png'):
                report('WARNING',
                       f"Material {material_id} '{mat_name}': "
                       f"file '{path_str}' not found - no same-name .dds variant either")
            else:
                report('WARNING',
                       f"Material {material_id} '{mat_name}': "
                       f"file '{path_str}' not resolvable / not found")
            return None
        img = _get_or_load_image(resolved, image_cache, report)
        if img is None:
            return None
        node = nt.nodes.new('ShaderNodeTexImage')
        node.image = img
        node.location = location
        if non_color:
            try:
                img.colorspace_settings.name = 'Non-Color'
            except Exception:
                pass
        # Create UV-Map + Mapping node (if not yet present) and connect Vector.
        map_node = _ensure_uv_mapping()
        nt.links.new(map_node.outputs['Vector'], node.inputs['Vector'])
        return node

    # 2. Diffuse texture -> Base Color
    tex_node = _add_image_node(mat_attrs.get('_texture_fileId'),
                               location=(-700, 300), non_color=False)
    if tex_node is not None:
        nt.links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])

    # 3. Normalmap -> Normal Map node -> Normal
    nm_node = _add_image_node(mat_attrs.get('_normalmap_fileId'),
                              location=(-700, 0), non_color=True)
    if nm_node is not None:
        normal_map = nt.nodes.new('ShaderNodeNormalMap')
        normal_map.location = (-300, 0)
        nt.links.new(nm_node.outputs['Color'], normal_map.inputs['Color'])
        nt.links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])

    # 4. Glossmap -> Roughness (direct, without invert)
    # In FS25 the "Glossmap" is semantically a specular/roughness map
    # (default: default_specular.png) - connecting it directly to Roughness is correct.
    gm_node = _add_image_node(mat_attrs.get('_glossmap_fileId'),
                              location=(-700, -300), non_color=True)
    if gm_node is not None:
        nt.links.new(gm_node.outputs['Color'], bsdf.inputs['Roughness'])

    # Debug Switch - same group as the PBR debug material gets,
    # but only baseTexture / normalMap / glossMap are exposed as masks here
    # since the re-export material doesn't load custom maps. All three use
    # the default UVMap (uv0) in the re-export material.
    output_node = nt.nodes.get('Material Output')
    if output_node is not None:
        _debug_images = {}
        if tex_node is not None and tex_node.image is not None:
            _debug_images['baseTexture'] = tex_node.image
        if nm_node is not None and nm_node.image is not None:
            _debug_images['normalMap'] = nm_node.image
        if gm_node is not None and gm_node.image is not None:
            _debug_images['glossMap'] = gm_node.image
        _debug_uv_types = {name: 'uv0' for name in _debug_images}
        recipe_loader._add_debug_switch(mat, bsdf, output_node,
                                        debug_images=_debug_images,
                                        debug_uv_types=_debug_uv_types)
        # Finalize layout: BSDF + Switch + Output far right, framed.
        recipe_loader._finalize_layout(mat, bsdf, output_node)

    # Re-export relevant material properties
    _apply_material_custom_properties(mat, mat_attrs, scene, report, mat_name)

    # PBR debug material 
    # Default behavior: lives only in bpy.data.materials with use_fake_user=True,
    # NOT attached to the mesh. The re-export material is attached to the mesh.
    # ATTACH_DEBUG_MATERIALS_TO_MESH=True: the debug material is
    # attached to the mesh instead of the re-export material. The re-export
    # material stays in bpy.data.materials with use_fake_user=True for manual
    # swapping when re-export is needed.
    debug_mat = None
    if BUILD_PBR_DEBUG_MATERIALS:
        snippets_path = (Path(SNIPPETS_BLEND_PATH) if SNIPPETS_BLEND_PATH
                         else recipe_loader.default_snippets_blend_path())
        try:
            debug_mat = recipe_loader.build_pbr_debug_material(
                mat_name=mat_name,
                mat_attrs=mat_attrs,
                scene=scene,
                image_cache=image_cache,
                snippet_cache=_snippet_cache,
                shader_cache=shader_cache,
                snippets_blend_path=snippets_path,
                i3d_dir=i3d_dir,
                resolve_filepath=_resolve_filepath,
                image_loader=_get_or_load_image,
                report=report,
            )
        except Exception as e:
            report('WARNING', f"PBR debug material for '{mat_name}' failed: {e}")

    # snow-heap flag. Marked on BOTH the re-export and debug
    # material so the object-level check (after material assignment) works
    # regardless of which material kind is on the slot.
    _is_snow = _is_snow_heap_material(mat_attrs, scene, i3d_dir)
    mat['_i3d_is_snow_heap'] = _is_snow
    if debug_mat is not None:
        debug_mat['_i3d_is_snow_heap'] = _is_snow

    # when ATTACH_DEBUG_MATERIALS_TO_MESH is set, swap the returned
    # material (debug to the mesh). The re-export material gets fake_user for
    # later manual swapping.
    if ATTACH_DEBUG_MATERIALS_TO_MESH and debug_mat is not None:
        mat.use_fake_user = True
        return debug_mat

    return mat


def _apply_material_custom_properties(mat, mat_attrs, scene, report, mat_name):
    """Set re-export relevant material properties as custom properties on the
    bpy.data.materials datablock (schema: Giants i3d Exporter, verified in
    io_export_i3d_*/dcc/dccBlender.py 1742-1769).

    Standard Texture/Normalmap/Glossmap need NO custom property - the exporter
    derives them from the image-texture nodes in the shader graph.
    """
    # customShader - path from customShaderId via Files map
    csi = mat_attrs.get('customShaderId')
    if csi is not None:
        try:
            fid = int(csi)
            shader_path = scene.files.get(fid)
            if shader_path:
                mat['customShader'] = shader_path
            else:
                report('WARNING',
                       f"Material '{mat_name}': customShaderId {fid} not in <Files>")
        except ValueError:
            report('WARNING',
                   f"Material '{mat_name}': invalid customShaderId '{csi}'")

    # customShaderVariation - string 1:1
    csv = mat_attrs.get('customShaderVariation')
    if csv:
        mat['customShaderVariation'] = str(csv)

    # customParameter_<name> for each <CustomParameter name="..." value="..."/>
    for cp in mat_attrs.get('_customparameters', []):
        name = cp.get('name')
        value = cp.get('value')
        if name and value is not None:
            mat[f'customParameter_{name}'] = str(value)

    # customTexture_<name> for each <Custommap name="..." fileId="..."/>
    # - the original XML path ($data/...) is the source of truth for re-export.
    for cm in mat_attrs.get('_custommaps', []):
        name = cm.get('name')
        fid_str = cm.get('fileId')
        if not name or fid_str is None:
            continue
        try:
            fid = int(fid_str)
            path = scene.files.get(fid)
            if path:
                mat[f'customTexture_{name}'] = path
            else:
                report('WARNING',
                       f"Material '{mat_name}': custom map '{name}' fileId {fid} not in <Files>")
        except ValueError:
            report('WARNING',
                   f"Material '{mat_name}': invalid fileId '{fid_str}' for custom map '{name}'")


# ---------------------------------------------------------------------------
# Path resolution + image loading
# ---------------------------------------------------------------------------

def _resolve_filepath(filename: str, i3d_dir: Path) -> Optional[Path]:
    """
    Resolve i3d paths:
      "$..." (e.g. "$data/foo.png")  ->  FS25_DATA_BASE/... ($ is replaced)
      relativer Pfad                  →  i3d_dir / Pfad
      absoluter Pfad                  →  wie ist

    Special case: i3d files often reference .png even though the actual file is .dds.
    For a .png extension we therefore first look for a same-name .dds file;
    only when that is missing do we fall back to the .png.

    Returns None when neither the .dds (for .png) nor the original exists.
    """
    if not filename:
        return None
    if filename.startswith('$'):
        rest = filename[1:]
        candidate = Path(FS25_DATA_BASE) / rest
    else:
        p = Path(filename)
        if p.is_absolute():
            candidate = p
        else:
            candidate = i3d_dir / filename

    if candidate.suffix.lower() == '.png':
        dds_candidate = candidate.with_suffix('.dds')
        if dds_candidate.exists():
            return dds_candidate

    if candidate.exists():
        return candidate
    return None


def _get_or_load_image(filepath: Path, image_cache: Dict[Path, bpy.types.Image],
                      report) -> Optional[bpy.types.Image]:
    """Load image or fetch from cache."""
    cached = image_cache.get(filepath)
    if cached is not None:
        return cached
    try:
        img = bpy.data.images.load(str(filepath), check_existing=True)
    except RuntimeError as e:
        report('WARNING', f"Could not load image: {filepath} ({e})")
        return None
    image_cache[filepath] = img
    return img


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_vec4(s: str, default):
    """'r g b a' -> (r,g,b,a); empty/None/invalid -> default."""
    if not s:
        return default
    parts = s.split()
    if len(parts) < 4:
        return default
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Spline-Import (via i3d-to-objx Patch 03)
# ---------------------------------------------------------------------------

def _create_curve_object(node, spl_obj_path, report):
    """Curve object from a .spl.obj file.

    .spl.obj format (see tool patch 03):
      # i3d Spline Export — type: cubic|linear, flags1: N, flags2: N
      # shapeId: N, name: ...
      v X Y Z
      v X Y Z
      ...
      l 1 2 3 ... N

    Mapping in Blender:
      bpy.data.curves.new(type='CURVE') + spline.type='POLY'
      use_cyclic_u = (flags1 == 1)   <- best guess; flags1 semantics not
                                       verifiziert, siehe Stolperstein 14.
      Custom Properties am Object:
        _i3d_isSpline=True, _i3d_splineType='cubic'/'linear',
        _i3d_raw_splineFlags1, _i3d_raw_splineFlags2
    """
    points = []
    spline_type = 'unknown'
    flags1 = 0
    flags2 = 0

    with open(spl_obj_path, 'r', encoding='utf-8', errors='ignore') as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith('#'):
                # Header comment — extract Type and Flags 
                # Format i.e.: "# i3d Spline Export — type: cubic, flags1: 0, flags2: 0"
                if 'type:' in line:
                    for token in ('type:', 'flags1:', 'flags2:'):
                        i = line.find(token)
                        if i < 0:
                            continue
                        rest = line[i + len(token):].strip()
                        val = rest.split(',', 1)[0].strip()
                        if token == 'type:':
                            spline_type = val
                        elif token == 'flags1:':
                            try: flags1 = int(val)
                            except ValueError: pass
                        elif token == 'flags2:':
                            try: flags2 = int(val)
                            except ValueError: pass
                continue
            parts = line.split()
            if parts[0] == 'v' and len(parts) >= 4:
                # Defensive: old Tool-Builds (before InvariantCulture-Fix) could
                # output "," instead of "." as decimal point on German systems.
                # Tolerant parser for both variants.
                try:
                    points.append((
                        float(parts[1].replace(',', '.')),
                        float(parts[2].replace(',', '.')),
                        float(parts[3].replace(',', '.')),
                    ))
                except ValueError:
                    pass
            # 'l'- ignore line — we use all points as polyline.

    if not points:
        report('WARNING', f"Spline '{node.name}': no points in .spl.obj")
        return None

    curve = bpy.data.curves.new(name=node.name, type='CURVE')
    curve.dimensions = '3D'
    spline = curve.splines.new('POLY')
    # spline.points has 1 point by default - we need len(points)-1 additional.
    spline.points.add(len(points) - 1)
    for i, (x, y, z) in enumerate(points):
        spline.points[i].co = (x, y, z, 1.0)  # 4D: (x, y, z, w) - w=1.0 for POLY splines
    spline.use_cyclic_u = (flags1 == 1)

    obj = bpy.data.objects.new(name=node.name, object_data=curve)
    obj['_i3d_isSpline'] = True
    obj['_i3d_splineType'] = spline_type
    obj['_i3d_raw_splineFlags1'] = flags1
    obj['_i3d_raw_splineFlags2'] = flags2
    return obj


# ---------------------------------------------------------------------------
# Skin Bind Node IDs (maybe later version, very complex) 
# ---------------------------------------------------------------------------

def _process_skin_bindings(import_collection, report):
    """skinBindNodeIds-processing (only research).

    HISTORY:
    - v1 (mergeGroup path): failed on re-export because mergeGroup members
      expect mesh data, but our "Bones" are TransformGroups
      (dccBlender.py:446: `item["Vertices"]['data']` → None).
    - v2 (now): no mergeGroup setup. We park only _i3d_skinBindObjects
      with resolved object names as info. Non re-export true (without
      skinBindNodeIds in the XML).
    - v3 (planned): full Armature-Setup with Bones + Vertex-Groups + Weights +
      ARMATURE modifier. May need another i3d-to-objx patch for blendweights.
    """
    # Lookup: nodeId -> object (for resolving skinBindNodeIds)
    node_id_to_obj = {}
    for obj in import_collection.objects:
        nid = obj.get('_i3d_nodeId')
        if nid is not None:
            node_id_to_obj[int(nid)] = obj

    # Collect all meshes with skinBindNodeIds_raw
    skin_meshes = [obj for obj in import_collection.objects
                   if '_i3d_skinBindNodeIds_raw' in obj.keys()]

    if not skin_meshes:
        return

    for mesh_obj in skin_meshes:
        raw = mesh_obj.get('_i3d_skinBindNodeIds_raw', '')
        # Parse: "2 7 11" → [2, 7, 11]
        try:
            bind_ids = [int(x) for x in str(raw).split() if x.strip()]
        except ValueError:
            report('WARNING', f"{mesh_obj.name}: skinBindNodeIds '{raw}' not parsable")
            del mesh_obj['_i3d_skinBindNodeIds_raw']
            continue

        if not bind_ids:
            del mesh_obj['_i3d_skinBindNodeIds_raw']
            continue

        # mergeGroup number: mesh.nodeId guarantees uniqueness
        mesh_node_id = int(mesh_obj.get('_i3d_nodeId', 0))
        if mesh_node_id == 0:
            report('WARNING',
                   f"{mesh_obj.name}: no _i3d_nodeId - skinBindNodeIds setup skipped")
            del mesh_obj['_i3d_skinBindNodeIds_raw']
            continue

        # v2 (no mergeGroup): save Object-Refs only as Info.
        # No i3D_mergeGroup-Setup anymore (Re-Export broken).
        resolved_names = []
        for bind_id in bind_ids:
            bound = node_id_to_obj.get(bind_id)
            if bound is None:
                resolved_names.append(f'<nodeId={bind_id} not found>')
            else:
                resolved_names.append(bound.name)

        mesh_obj['_i3d_skinBindObjects'] = ', '.join(resolved_names)
        report('INFO',
               f"{mesh_obj.name}: skinBindNodeIds {bind_ids} -> "
               f"_i3d_skinBindObjects={resolved_names} "
               f"(info only; re-export fidelity requires armature setup v3)")

        # Clean up intermediate property
        del mesh_obj['_i3d_skinBindNodeIds_raw']


# ---------------------------------------------------------------------------
# Axis Correction Y-up (Giants) → Z-up (Blender) 
# ---------------------------------------------------------------------------

def _apply_axis_correction(top_level_objs, report):
    """Wrapper Empty Trick: bakes X+90° rotation into all top level objects + 
    their rekursive children (mesh vertices, curve points, object transforms).

    Steps:
      1. Create wrapper empty with rotation_euler = (radians(90), 0, 0).
      2. Parent top level objects as children with matrix_parent_inverse=Identity,
         so they rotate with the wrapper (instead of staying in place).
      3. view_layer.update() to recompute matrices.
      4. Select wrapper + all children rekursively.
      5. bpy.ops.object.transform_apply(rotation=True) - bakes the wrapper
         rotation into the selected objects (mesh vertices, curve points,
         object rotations).
      6. Unparent children from wrapper.
      7. Delete wrapper empty.

    Result: mesh vertices and curve points in Z-up-coordinates,
    object rotations in Z-up world, no wrapper empty left in the scene.
    """
    import mathutils

    wrapper = bpy.data.objects.new("__fs25_axis_correction_temp__", None)
    bpy.context.scene.collection.objects.link(wrapper)
    wrapper.rotation_euler = (math.radians(90), 0, 0)

    # Parent with matrix_parent_inverse=Identity so children get rotated by the
    # wrapper rotation actually rotates them (instead of keeping their old
    # zu behalten).
    for obj in top_level_objs:
        obj.parent = wrapper
        obj.matrix_parent_inverse = mathutils.Matrix.Identity(4)

    bpy.context.view_layer.update()

    # Selection: Wrapper + all recursive children
    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.view_layer.objects.active = wrapper
    wrapper.select_set(True)
    for child in wrapper.children_recursive:
        child.select_set(True)

    # Transform apply: bakes the wrapper rotation into the geometry/transforms
    # of all selected objects.
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)

    # Unparent children — Save world matrix, then unparent, then set
    # again (otherwise they move).
    direct_children = list(wrapper.children)
    for child in direct_children:
        world = child.matrix_world.copy()
        child.parent = None
        child.matrix_world = world

    bpy.data.objects.remove(wrapper, do_unlink=True)

    report('INFO',
           f"Axis correction Y-up -> Z-up applied to {len(top_level_objs)} "
           f"top-level object(s) (X+90 deg rotation baked into geometry).")
