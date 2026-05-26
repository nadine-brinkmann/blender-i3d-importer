"""
Main logic for the FS25 i3d Importer.

Workflow:
  1. Parse .i3d XML -> I3DScene (hierarchy, materials, files)
  2. If inline shapes detected -> abort
  3. Decode .i3d.shapes binary directly via i3d_shapes_reader + i3d_shapes_models
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
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import bpy

from . import i3d_attr_mapping
from . import i3d_shader_parser
from . import i3d_xml_parser
from . import i3d_shapes_reader
from . import i3d_shapes_models
from . import i3d_shapes_to_meshdata
from . import recipe_loader

# Default paths. Overridden in import_i3d() when the add-on is invoked via
# the operator (which passes the add-on preferences fs25_data_base and
# export_dir). Direct callers (e.g. test scripts) fall back to these
# module-level values.
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

# Per-import UUID. Set at the start of every import_i3d() call. Both the
# re-export material in _build_material() and the debug material in
# recipe_loader stamp it on the materials they create as
# _i3d_import_uuid. The FS25_OT_switch_materials operator uses
# (material_id, import_uuid, kind) as the lookup key so material pairs
# from different imports do not collide on material_id alone.
_CURRENT_IMPORT_UUID: Optional[str] = None

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

def import_i3d(i3d_filepath: str, report: Callable = None,
               apply_axis_correction: bool = True,
               auto_hide_invisible_shapes: bool = True,
               build_pbr_debug_materials: bool = True,
               attach_debug_materials_to_mesh: bool = False,
               terrain_lod: str = 'OFF',
               terrain_base_color=(0.03434, 0.042311, 0.012286, 1.0),
               terrain_poc_layer_names: str = "ASPHALT,GRASS,MUD,FOREST_LEAVES,FOREST_GRASS",
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
    global FS25_DATA_BASE, EXPORT_DIR, SNIPPETS_BLEND_PATH, BUILD_PBR_DEBUG_MATERIALS, ATTACH_DEBUG_MATERIALS_TO_MESH
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

    # New UUID per import - stamped on every material pair (export + debug)
    # so the N-Panel "Switch i3d Materials" operator can disambiguate pairs
    # across multiple imports that share material_id 0/1/2/...
    global _CURRENT_IMPORT_UUID
    _CURRENT_IMPORT_UUID = uuid.uuid4().hex

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

    # 1b. Map-Only-Filter: Wenn auf Scene-Root-Ebene ein TerrainTransformGroup
    # existiert, ist das eine map.i3d. In dem Fall importieren wir NUR das
    # Terrain (kein sun, kein gameplay-TransformGroup, keine Placeables).
    # WICHTIG: Filter MUSS hier vor dem Shape-Decoding stehen, sonst werden
    # tausende Shapes (Placeables, Vegetation, ...) sinnlos dekodiert, bevor
    # sie verworfen werden. has_shapes (Schritt 2b) lookt auf roots_to_process,
    # nicht scene.roots - dadurch wird der ganze Shape-Binary-Read gespart.
    # Re-Export von map.i3d ist ohnehin nicht moeglich (Giants Exporter kennt
    # <TerrainTransformGroup> nicht), deshalb keine Loss-of-Fidelity-Bedenken.
    terrain_roots = [r for r in scene.roots
                     if r.kind == 'TerrainTransformGroup']
    if terrain_roots:
        skipped = len(scene.roots) - len(terrain_roots)
        _report('INFO',
                f"Map import erkannt (TerrainTransformGroup auf Root-Ebene) "
                f"- {skipped} Non-Terrain-Root-Node(s) uebersprungen.")
        roots_to_process = terrain_roots
    else:
        roots_to_process = scene.roots

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
    saved_empty_visibility = None
    try:
        # Force empties to be visible + selectable in all 3D viewports for
        # the duration of the import. The wrapper-empty trick in
        # _apply_axis_correction creates a temporary Empty and bakes its
        # rotation into all children via bpy.ops.object.transform_apply;
        # if empties are hidden in the viewport's "Selectability &
        # Visibility" filter, the wrapper is silently skipped and the
        # rotation is not applied (objects end up unrotated + with wrong
        # locations). The same applies to bpy.ops.view3d.view_selected in
        # _post_import_view_setup. We snapshot the per-space flags so the
        # user's choice is restored in the finally below.
        saved_empty_visibility = _force_empty_visibility_on_all_views(_report)

        has_shapes = any(_has_any_shape_nodes(r) for r in roots_to_process)
        warning_count = 0
        # Tracks which Blender mesh object got built for each shapeId.
        # Populated in _create_mesh_object, consumed by _process_merge_groups.
        shape_id_to_obj: Dict[int, "bpy.types.Object"] = {}
        # shape_map: shapeId -> decoded Shape (i3d_shapes_models.Shape) read
        # directly from the .i3d.shapes binary. Replaces the previous
        shape_map: Dict[int, "i3d_shapes_models.Shape"] = {}
        # spline_map: shapeId -> decoded Spline (i3d_shapes_models.Spline) read
        # directly from the .i3d.shapes binary. Same id-space as shape_map —
        # the i3d XML references both via <Shape shapeId="..."> and the
        # container entity_type (SHAPE vs SPLINE/SPLINE_L) decides which path
        # is taken in _create_mesh_object.
        spline_map: Dict[int, "i3d_shapes_models.Spline"] = {}

        if has_shapes:
            shapes_file = i3d_dir / (i3d.name + ".shapes")
            if not shapes_file.exists():
                raise FileNotFoundError(f"No .i3d.shapes file found next to {i3d.name}.")

            call_start_time = time.time()
            _report('INFO', f"Reading shapes binary: {shapes_file.name}")
            try:
                shapes_container = i3d_shapes_reader.read_shapes_file(str(shapes_file))
            except Exception as e:
                raise RuntimeError(
                    f"Failed to decode {shapes_file.name}: {type(e).__name__}: {e}"
                ) from e

            num_shape_entities = 0
            num_spline_entities = 0
            for entity in shapes_container.entities:
                etype = entity.entity_type.name
                if etype == 'SHAPE':
                    num_shape_entities += 1
                    try:
                        shape = i3d_shapes_models.parse_shape_entity(
                            entity, shapes_container.header.version
                        )
                    except Exception as e:
                        _report('WARNING',
                                f"Failed to decode SHAPE entity #{num_shape_entities}: "
                                f"{type(e).__name__}: {e}")
                        warning_count += 1
                        continue
                    if shape.id in shape_map:
                        _report('WARNING',
                                f"Duplicate shapeId {shape.id} in shapes binary, "
                                f"keeping first ({shape_map[shape.id].name!r})")
                        continue
                    shape_map[shape.id] = shape
                elif etype in ('SPLINE', 'SPLINE_L'):
                    num_spline_entities += 1
                    try:
                        spline = i3d_shapes_models.parse_spline_entity(
                            entity, shapes_container.header.version
                        )
                    except Exception as e:
                        _report('WARNING',
                                f"Failed to decode {etype} entity #{num_spline_entities}: "
                                f"{type(e).__name__}: {e}")
                        warning_count += 1
                        continue
                    if spline.id in spline_map:
                        _report('WARNING',
                                f"Duplicate spline shapeId {spline.id} in shapes binary, "
                                f"keeping first ({spline_map[spline.id].name!r})")
                        continue
                    if spline.attr_flags != 0:
                        _report('WARNING',
                                f"Spline '{spline.name}' (shapeId {spline.id}) has "
                                f"per-point attribute flags 0x{spline.attr_flags:08x} - "
                                f"not yet decoded, attributes will be lost.")
                        warning_count += 1
                    spline_map[spline.id] = spline
                else:
                    _report('WARNING',
                            f"Unknown entity type {entity.type} in shapes binary, skipped.")
                    warning_count += 1

            if not shape_map and not spline_map:
                _report('WARNING', "No shapes / splines decoded from the binary.")
                return 0, warning_count + 1

            elapsed = time.time() - call_start_time
            _report('INFO',
                    f"Decoded {len(shape_map)} shape(s) + "
                    f"{len(spline_map)} spline(s) "
                    f"in {elapsed:.2f}s")
        else:
            _report('INFO',
                    "No shape nodes in scene - skipping shapes binary "
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

        for root in roots_to_process:
            _build_node(root, parent=None, collection=import_collection,
                        scene=scene,
                        mesh_cache=mesh_cache, material_cache=material_cache,
                        image_cache=image_cache, shader_cache=shader_cache,
                        shape_map=shape_map, spline_map=spline_map,
                        shape_id_to_obj=shape_id_to_obj,
                        i3d_dir=i3d_dir, counts=counts,
                        terrain_lod=terrain_lod,
                        terrain_base_color=terrain_base_color,
                        terrain_poc_layer_names=terrain_poc_layer_names,
                        objects_to_hide=objects_to_hide, report=_report)

        # distribute skinBindNodeIds -> i3D_mergeGroup properties.
        # Re-export fidelity goes via the Giants exporter's mergeGroup path
        # (i3d_export.py:505-518) - generates skinBindNodeIds from the
        # i3D_mergeGroup/i3D_mergeGroupRoot properties on the objects.
        _process_merge_groups(import_collection, shape_map, shape_id_to_obj, _report)
        _process_merge_children(import_collection, shape_map, shape_id_to_obj, _report)
        _process_skin_weights(import_collection, shape_map, shape_id_to_obj, _report)
        _process_skin_bindings(import_collection, _report)

        # axis correction Y-up -> Z-up after the complete hierarchy is
        # built (so all top-level objects exist and the wrapper-empty trick works).
        if apply_axis_correction:
            top_level = [obj for obj in import_collection.objects if obj.parent is None]
            if top_level:
                _apply_axis_correction(top_level, _report)

        # Root-space merge-group slots (noBindPose flag absent on the shape)
        # are remapped to bone-local now that axis correction has finalized world
        # transforms. Covers FS22 v7 AND Giants-Blender-exporter v10 (#2 / #8).
        _fix_rootspace_mergegroup_local_space(import_collection, _report)

        # apply hide AFTER axis correction. hide_set matches the H
        # shortcut (view-layer eye). On Giants re-export this leads to
        # visibility="false" in the XML because the exporter reads
        # visible_in_viewport_get() - re-export-true for source visibility="false",
        # drift for nonRenderable->visibility is intentionally accepted
        # (see _should_hide_for_visibility).
        if auto_hide_invisible_shapes and objects_to_hide:
            for o in objects_to_hide:
                # The object may have been deleted by _process_merge_groups or
                # _process_merge_children when their Empty-to-Mesh replacement
                # removed the original Empty. In that case `o` is a stale
                # StructRNA wrapper; even reading `o.name` raises ReferenceError.
                try:
                    name = o.name  # access first, catches stale wrappers
                    o.hide_set(True)
                    counts['hidden'] += 1
                except (ReferenceError, RuntimeError) as e:
                    _report('INFO',
                            f"Skipped hide for a removed object "
                            f"(probably replaced by Merge-Group/MergeChildren split): {e}")

        # Post-import view setup: Material Preview shading, frame imported
        # objects, bump clip_end if anything is large/far. Runs AFTER
        # auto-hide so view_selected ignores hidden objects, and BEFORE the
        # deselect block (which cleanly resets selection afterwards).
        try:
            _post_import_view_setup(import_collection, _report)
        except Exception as e:
            _report('INFO', f"Post-import view setup skipped: {e}")

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
            settings.i3D_gameLocationDisplay = FS25_DATA_BASE.rstrip("\\/") + "\\"
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

        # Total imported objects: meshes (shapes) + curves (splines) +
        # empties (TransformGroup/Reference/...) + lights + cameras.
        # shapes_missing_objx are import failures and not counted; hidden is
        # a state flag and overlaps with the other categories.
        total_imported = (counts['shapes_imported'] + counts['splines']
                          + counts['empties'] + counts['lights']
                          + counts['cameras'])
        return total_imported, warning_count
    finally:
        # Restore per-viewport "Selectability & Visibility" flags for
        # empties (forced True at the start of try, see comment there).
        if saved_empty_visibility is not None:
            _restore_empty_visibility(saved_empty_visibility)


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
                image_cache, shader_cache, shape_map, spline_map, shape_id_to_obj,
                i3d_dir, counts, terrain_lod, terrain_base_color,
                terrain_poc_layer_names, objects_to_hide, report):
    """Recursively create the object for `node`, parent it, then walk children."""
    if node.kind == 'Shape':
        obj = _create_mesh_object(node, scene, mesh_cache, material_cache,
                                  image_cache, shader_cache, shape_map, spline_map,
                                  i3d_dir, counts, report)
        # Register Shape obj for later merge-group / skin-weight post-processing.
        # node.shapeId may be None for invalid shapes — guard against that.
        if obj is not None and node.shapeId is not None:
            shape_id_to_obj[node.shapeId] = obj
    elif node.kind == 'Light':
        obj = _create_light_object(node, report)
        if obj is not None:
            counts['lights'] += 1
    elif node.kind == 'Camera':
        obj = _create_camera_object(node, report)
        if obj is not None:
            counts['cameras'] += 1
    elif node.kind == 'TerrainTransformGroup':
        obj = _create_terrain_object(node, scene, i3d_dir, terrain_lod,
                                     terrain_base_color, terrain_poc_layer_names,
                                     image_cache, report)
        if obj is not None:
            counts['empties'] += 1  # bookkeeping: terrain counts as 1 object
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
        # Mark with a custom property so the N-Panel toggle
        # "FS25 Invisible GE-objects" can show/hide them later regardless
        # of the auto_hide_invisible_shapes setting on import.
        if _should_hide_for_visibility(node):
            objects_to_hide.append(obj)
            obj['_i3d_invisible_in_ge'] = True
        next_parent = obj

    for child in node.children:
        _build_node(child, parent=next_parent, collection=collection, scene=scene,
                    mesh_cache=mesh_cache, material_cache=material_cache,
                    image_cache=image_cache, shader_cache=shader_cache,
                    shape_map=shape_map, spline_map=spline_map,
                    shape_id_to_obj=shape_id_to_obj,
                    i3d_dir=i3d_dir, counts=counts,
                    terrain_lod=terrain_lod,
                    terrain_base_color=terrain_base_color,
                    terrain_poc_layer_names=terrain_poc_layer_names,
                    objects_to_hide=objects_to_hide, report=report)


def _create_mesh_object(node, scene, mesh_cache, material_cache, image_cache,
                        shader_cache, shape_map, spline_map, i3d_dir, counts, report):
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
    # spline_map[shape_id] is a decoded i3d_shapes_models.Spline.
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
        shape = shape_map.get(shape_id)
        if shape is None:
            report('WARNING', f"No shape data for shapeId {shape_id} (Shape '{node.name}')")
            counts['shapes_missing_objx'] += 1
            return None
        try:
            mesh = _build_mesh_datablock(
                shape, datablock_name=node.name,
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
    # Restore the CPU Mesh marker (high_bit 0x01000000) from the binary
    # options. The XML normally has no cpuMesh/meshUsage attribute when the
    # bit is set in the source .i3d.shapes binary (verified on FS25 base-game
    # bales — roundbale_vis/extra), so the only way to roundtrip it is via
    # this high-bit -> Blender property bridge. The Giants exporter then
    # writes the appropriate marker so i3dConverter regenerates the bit.
    _shape = shape_map.get(shape_id)
    if _shape is not None and (_shape.options_high_bits
                               & i3d_shapes_models.SHAPE_HIGH_BIT_CPU_MESH):
        obj['i3D_cpuMesh'] = True
    # propagate snow-heap flag from mesh materials to the object,
    # so the N-Panel Show/Hide operators can find them via a flat scan.
    for _slot_mat in mesh.materials:
        if _slot_mat is not None and _slot_mat.get('_i3d_is_snow_heap'):
            obj['_i3d_is_snow_heap'] = True
            break
    counts['shapes_imported'] += 1
    return obj


def _apply_custom_split_normals(mesh, vertex_normals):
    """Apply i3d per-vertex normals as Blender custom split normals.

    Without this, Blender falls back to flat per-face normals and the mesh
    looks blocky (GitHub #1, #7). Faces are set smooth so the custom corner
    normals are respected; the explicit normals reproduce the baked GE look
    (soft surfaces + hard edges) and survive re-export.

    vertex_normals: list of (x, y, z), one per mesh vertex (local order).
    No-op if absent or the count does not match the vertex count.
    """
    if not vertex_normals or len(vertex_normals) != len(mesh.vertices):
        return False
    if mesh.polygons:
        mesh.polygons.foreach_set('use_smooth', [True] * len(mesh.polygons))
    mesh.normals_split_custom_set_from_vertices(vertex_normals)
    return True


def _build_mesh_datablock(shape, datablock_name, node, scene, material_cache,
                          image_cache, shader_cache, i3d_dir, report):
    """Convert a decoded Shape into a Blender mesh datablock + assign materials."""
    md = i3d_shapes_to_meshdata.shape_to_mesh_data(shape, name=datablock_name)

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

    # Custom split normals from the i3d (GitHub #1/#7: avoid blocky look).
    _apply_custom_split_normals(mesh, md.normals)

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
    elif node.kind == 'Note':
        obj.empty_display_type = 'SPHERE'
    obj.empty_display_size = 0.5
    return obj


def _load_combined_layer_textures(node, scene_files, i3d_dir, image_cache,
                                  report, selected_names=None, max_layers=4):
    """PoC: Laedt fuer ausgewaehlte CombinedLayer die benoetigten Texturen
    (detail, weight) fuer beide Sub-Layer (01+02).

    Normal- und Displacement-Channel werden bewusst NICHT geladen — das
    spart pro CombinedLayer 4 Sampler (normal01, normal02, disp01, disp02)
    und haelt das Material unter dem Eevee-Sampler-Limit (Stand 2026-05-19:
    mit allen 4 Channels wurde das Terrain pink = Shader-Compile-Fail).

    selected_names: Liste von CombinedLayer-Namen (z.B. ['ASPHALT', 'GRASS']),
        case-sensitive Match auf <CombinedLayer name=...>. Reihenfolge in der
        Liste wird beibehalten. Namen die nicht existieren: Warning + skip.
        None -> die ersten max_layers im XML-Order.

    Returns: Liste von Dicts. Pro CombinedLayer ein Dict mit:
      - name, noise_frequency
      - sub_layers: Liste von Sub-Layer-Dicts mit Metadaten + bpy.Image-Refs.
    Bilder die nicht aufgeloest oder geladen werden konnten sind None.

    Verwendet _resolve_filepath fuer $data-Substitution und PNG->DDS-Fallback,
    sowie _get_or_load_image fuer Caching ueber image_cache (key: Path).
    Colorspace: detail bleibt sRGB (Color-Daten), weight wird auf 'Non-Color'
    gesetzt (Blender-Konvention fuer Non-Color-Daten).
    """
    if not node.terrain_combined_layers:
        report('INFO', f"Terrain '{node.name}': no CombinedLayer entries in XML")
        return []

    # Sub-Layer-Lookup nach Name fuer schnelles Anziehen aus
    # combined_layer.sub_layer_names (z.B. ['asphalt01', 'asphalt02']).
    sub_by_name = {sl.name: sl for sl in node.terrain_layers}

    # CombinedLayer-Auswahl: nach Namensliste oder nach Index.
    if selected_names is not None:
        cl_by_name = {cl.name: cl for cl in node.terrain_combined_layers}
        combined_iter = [cl_by_name[n] for n in selected_names if n in cl_by_name]
        missing = [n for n in selected_names if n not in cl_by_name]
        if missing:
            report('WARNING',
                   f"Terrain '{node.name}': CombinedLayer name(s) not found "
                   f"in XML: {missing}")
    else:
        combined_iter = node.terrain_combined_layers[:max_layers]

    plan = []
    total_ok = 0
    total_missing = 0
    i3d_dir_path = Path(i3d_dir)

    for combined in combined_iter:
        cl_dict = {
            'name': combined.name,
            'noise_frequency': combined.noise_frequency,
            'sub_layers': [],
        }
        for sub_name in combined.sub_layer_names:
            sub = sub_by_name.get(sub_name)
            if sub is None:
                report('WARNING',
                       f"Terrain '{node.name}': CombinedLayer '{combined.name}' "
                       f"references unknown sub-layer '{sub_name}'")
                continue
            sl_dict = {
                'name': sub.name,
                'unit_size': sub.unit_size,
                'blend_contrast': sub.blend_contrast,
                'displacement_max_height': sub.displacement_max_height,
                'detail_image':       None,
                'weight_image':       None,
            }
            # Zwei Map-Typen pro Sub-Layer (Normal/Displacement bewusst weggelassen,
            # siehe Docstring). non_color steuert colorspace_settings.
            for map_key, map_id, non_color in (
                ('detail_image',       sub.detail_map_id,       False),
                ('weight_image',       sub.weight_map_id,       True),
            ):
                map_type = map_key.replace('_image', '')
                if map_id is None:
                    total_missing += 1
                    continue
                rel = scene_files.get(map_id)
                if not rel:
                    report('WARNING',
                           f"Terrain '{node.name}': sub-layer '{sub.name}' "
                           f"{map_type}_map_id {map_id} not in <Files>")
                    total_missing += 1
                    continue
                resolved = _resolve_filepath(rel, i3d_dir_path)
                if resolved is None:
                    report('WARNING',
                           f"Terrain '{node.name}': sub-layer '{sub.name}' "
                           f"{map_type} not on disk: {rel}")
                    total_missing += 1
                    continue
                img = _get_or_load_image(resolved, image_cache, report)
                if img is None:
                    total_missing += 1
                    continue
                if non_color:
                    try:
                        img.colorspace_settings.name = 'Non-Color'
                    except Exception:
                        pass  # pipeline ohne colorspace_settings
                sl_dict[map_key] = img
                total_ok += 1
            cl_dict['sub_layers'].append(sl_dict)
        plan.append(cl_dict)

    sub_count = sum(len(p['sub_layers']) for p in plan)
    report('INFO',
           f"Terrain '{node.name}': PoC layer textures loaded - "
           f"{len(plan)} CombinedLayer(s), {sub_count} Sub-Layer(s), "
           f"{total_ok} image refs OK, {total_missing} missing.")
    return plan


def _build_combined_layer_node_group(cl_data, snippets_blend_path,
                                     snippet_cache, report):
    """Baut eine bpy NodeGroup fuer EINEN CombinedLayer.

    Outputs: Color
    KEIN UV-Input - intern wird via ShaderNodeTexCoord der Object-Output
    benutzt. Das ist ein einfacher Object-Space-UV ohne Triplanar-Trick;
    fuer den Debug-Preview-Use-Case der Terrain-Ansicht ausreichend.
    NUR fuer das Terrain-Material; die regulaere Mesh-Material-Pipeline
    (recipe_loader, _build_material) nutzt weiterhin Worldspace-Triplanar.

    Normal- und Displacement-Channels werden bewusst weggelassen
    (Stand 2026-05-19): bei allen 4 Channels x 4 CombinedLayers wurde
    das Eevee-Sampler-Limit ueberschritten -> Terrain wurde pink. Mit
    nur Detail-Channel: 2 Detail-Sampler pro CombinedLayer, was bei
    5 Layern komfortabel unter dem Limit bleibt.

    Logic:
      - ShaderNodeTexCoord, Output 'Object', als gemeinsame UV-Quelle.
      - Pro Sub-Layer (01 + 02) ein eigener Branch:
        Mapping(scale=1/unitSize) -> 1 ImageTex (detail).
      - Variant-Blending via ShaderNodeTexNoise mit cl_data['noise_frequency'],
        Noise-Vector ebenfalls aus dem Object-UV.
      - Mix(01, 02) via Noise-Faktor fuer Color.

    Edge: nur 1 Sub-Layer -> kein Noise-Mix, direkte 01-Werte.
    Edge: fehlende Image-Refs -> ImageTex bleibt leer (Blender zeigt magenta).

    Returns bpy.types.NodeTree, None wenn keine Sub-Layer.

    Caching: existiert eine NodeGroup gleichen Namens schon, wird sie
    wiederverwendet (Re-Import zeigt ggf. alte Image-Refs).

    snippets_blend_path, snippet_cache: no longer used (legacy from the
    earlier worldspace-triplanar version). Kept in the signature to
    avoid changing the call site.

    Mix-Node-API (Blender 4.x+): ShaderNodeMix mit data_type-Setting.
    Socket-Indices fuer 'RGBA': Factor=0, A=6, B=7, Result=2.
    """
    name = f"CombinedLayer_{cl_data['name']}"
    if name in bpy.data.node_groups:
        return bpy.data.node_groups[name]

    sub_layers = cl_data['sub_layers']
    if not sub_layers:
        report('WARNING', f"NodeGroup '{name}': no sub-layers - skipped")
        return None

    tree = bpy.data.node_groups.new(name=name, type='ShaderNodeTree')

    # Interface (Blender 4.0+ API). KEIN UV-Input - kommt intern aus
    # Geometry.Position via Worldspace-Triplanar-Snippet.
    tree.interface.new_socket(name='Color', in_out='OUTPUT',
                              socket_type='NodeSocketColor')

    nodes = tree.nodes
    links = tree.links

    out_node = nodes.new('NodeGroupOutput')
    out_node.location = (1200, 0)

    # Object-Space-UV via ShaderNodeTexCoord (Output 'Object'). Einfacher
    # als der frueher genutzte Worldspace-Triplanar-Snippet; reicht fuer
    # den Debug-Preview-Use-Case (Terrain als Vertex-Snap-Vorlage). Wird
    # von beiden Sub-Layer-Mapping-Nodes und vom Noise-Variant-Blender
    # geteilt. NUR fuer das Terrain-Material - andere Debug-Materialien
    # (recipe_loader, _build_material) nutzen weiterhin Triplanar.
    tex_coord = nodes.new('ShaderNodeTexCoord')
    tex_coord.location = (-1500, 0)
    tex_coord.label = "Object-space"
    object_uv = tex_coord.outputs['Object']

    def _branch(sub, y_base):
        """Baut Mapping + 1 ImageTex (detail) fuer einen Sub-Layer."""
        scale = 1.0 / max(sub['unit_size'], 0.001)
        mapping = nodes.new('ShaderNodeMapping')
        mapping.location = (-1200, y_base)
        mapping.inputs['Scale'].default_value = (scale, scale, scale)
        links.new(object_uv, mapping.inputs['Vector'])

        d_tex = nodes.new('ShaderNodeTexImage')
        d_tex.location = (-900, y_base)
        d_tex.image = sub['detail_image']
        d_tex.label = f"{sub['name']} detail"
        links.new(mapping.outputs['Vector'], d_tex.inputs['Vector'])

        return d_tex

    s1 = sub_layers[0]
    d01 = _branch(s1, 300)

    if len(sub_layers) > 1:
        s2 = sub_layers[1]
        d02 = _branch(s2, -300)

        # Noise als Variant-Blend-Faktor (object-space, scale=noiseFrequency).
        noise = nodes.new('ShaderNodeTexNoise')
        noise.location = (-1200, -900)
        noise.inputs['Scale'].default_value = cl_data['noise_frequency']
        links.new(object_uv, noise.inputs['Vector'])

        # Color-Mix (RGBA). ShaderNodeMix Sockets: Factor=0, A=6, B=7, Result=2.
        mix_c = nodes.new('ShaderNodeMix')
        mix_c.data_type = 'RGBA'
        mix_c.location = (-400, 0)
        links.new(noise.outputs['Fac'], mix_c.inputs[0])
        links.new(d01.outputs['Color'], mix_c.inputs[6])
        links.new(d02.outputs['Color'], mix_c.inputs[7])
        color_out = mix_c.outputs[2]
    else:
        # Nur 1 Sub-Layer: direkter Color-Wert ohne Mix.
        color_out = d01.outputs['Color']

    # Output verbinden.
    links.new(color_out, out_node.inputs['Color'])

    return tree


def _build_terrain_master_material(obj, terrain_layer_plan, snippets_blend_path,
                                   snippet_cache, report,
                                   base_color=(0.3, 0.3, 0.3, 1.0)):
    """Baut das Master-Material fuer das Terrain und liefert es zurueck.

    Struktur:
      - TexCoord -> UV (fuer weightMaps; weightMaps sind map-coverage-codiert,
        muessen 1:1 auf die Map-Flaeche projiziert werden).
      - Pro CombinedLayer im terrain_layer_plan:
        - 2x ImageTex(weightMap), UV-basiert (eine pro Sub-Layer 01/02)
        - SeparateColor -> R -> Math(Add, clamp) = combined_weight
        - CombinedLayer-NodeGroup-Instance (object-space UV intern)
        - Mix-Step im Stack: Color (RGBA)
      - Final: Principled BSDF (Base Color only) + Material Output.

    Normal- und Displacement-Mixing wurden weggelassen (Stand 2026-05-19):
    siehe _build_combined_layer_node_group Docstring. Damit fallen pro
    CombinedLayer 2 Mix-Nodes weg (VECTOR + FLOAT) plus der globale
    ShaderNodeDisplacement am Material-Output.

    base_color: RGBA-Tuple (linear) fuer die Grundfarbe vor dem ersten Layer
    (sichtbar wo alle PoC-Weights null sind). Default neutrales Grau, wird
    vom Importer aus den Add-on-Preferences ueberschrieben.

    Mix-Node Socket-Indices (Blender 4.x+):
      Factor=0; RGBA A=6/B=7/Result=2.

    Returns bpy.types.Material, oder None bei leerem Plan.
    """
    if not terrain_layer_plan:
        return None

    mat = bpy.data.materials.new(name=f"{obj.name}_material")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()

    # UV-Source fuer weightMaps.
    tex_coord = nt.nodes.new('ShaderNodeTexCoord')
    tex_coord.location = (-2200, 0)
    uv_socket = tex_coord.outputs['UV']

    # Material Output + Principled BSDF.
    output = nt.nodes.new('ShaderNodeOutputMaterial')
    output.location = (2000, 0)
    bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.location = (1600, 100)
    # Fully matte terrain - no specular highlights (Nadine request).
    bsdf.inputs['Roughness'].default_value = 1.0
    nt.links.new(bsdf.outputs[0], output.inputs['Surface'])

    # Stack-Init: nur Color (RGB Node) - Normal- und Disp-Stacks sind weg.
    base_rgb = nt.nodes.new('ShaderNodeRGB')
    base_rgb.location = (-300, 600)
    base_rgb.label = "Base color (uncovered)"
    # base_color is a 4-tuple (linear RGBA); cast to tuple in case Blender
    # passed a bpy_prop_array.
    base_rgb.outputs[0].default_value = tuple(base_color)
    color_stack = base_rgb.outputs[0]

    x_offset = 100
    layers_attached = 0
    for cl_data in terrain_layer_plan:
        cl_name = cl_data['name']
        ng_tree = bpy.data.node_groups.get(f"CombinedLayer_{cl_name}")
        if ng_tree is None:
            report('WARNING',
                   f"Master-Material: NodeGroup 'CombinedLayer_{cl_name}' "
                   f"missing - layer skipped")
            continue

        ng_inst = nt.nodes.new('ShaderNodeGroup')
        ng_inst.node_tree = ng_tree
        ng_inst.location = (x_offset, -400)
        ng_inst.label = f"CL: {cl_name}"

        # WeightMap-Loading (max 2 Sub-Layer pro CombinedLayer).
        weight_floats = []
        for i, sub in enumerate(cl_data['sub_layers']):
            w_img = sub.get('weight_image')
            if w_img is None:
                continue
            w_tex = nt.nodes.new('ShaderNodeTexImage')
            w_tex.image = w_img
            w_tex.location = (x_offset - 900, -700 - i * 250)
            w_tex.label = f"weight: {sub['name']}"
            nt.links.new(uv_socket, w_tex.inputs['Vector'])

            sep = nt.nodes.new('ShaderNodeSeparateColor')
            sep.mode = 'RGB'
            sep.location = (x_offset - 600, -700 - i * 250)
            nt.links.new(w_tex.outputs['Color'], sep.inputs['Color'])
            weight_floats.append(sep.outputs['Red'])

        if not weight_floats:
            report('WARNING',
                   f"Master-Material: CombinedLayer '{cl_name}' has no weight "
                   f"maps - layer skipped")
            continue

        # Weight-Summe (clamp 0..1).
        if len(weight_floats) == 1:
            combined_w = weight_floats[0]
        else:
            math_add = nt.nodes.new('ShaderNodeMath')
            math_add.operation = 'ADD'
            math_add.use_clamp = True
            math_add.location = (x_offset - 300, -800)
            math_add.label = f"w_{cl_name}"
            nt.links.new(weight_floats[0], math_add.inputs[0])
            nt.links.new(weight_floats[1], math_add.inputs[1])
            combined_w = math_add.outputs[0]

        # Color-Mix (RGBA). Indices: Factor=0, A=6, B=7, Result=2.
        mix_c = nt.nodes.new('ShaderNodeMix')
        mix_c.data_type = 'RGBA'
        mix_c.location = (x_offset + 400, 600)
        mix_c.label = f"Mix {cl_name} color"
        nt.links.new(combined_w, mix_c.inputs[0])
        nt.links.new(color_stack, mix_c.inputs[6])
        nt.links.new(ng_inst.outputs['Color'], mix_c.inputs[7])
        color_stack = mix_c.outputs[2]

        x_offset += 900
        layers_attached += 1

    if layers_attached == 0:
        report('WARNING',
               f"Master-Material: no layers attached - material is empty.")
        # Cleanup: leeres Material zurueckgeben ist OK, BSDF zeigt Default-Grau.

    # Final-Connect: nur Base Color (Normal und Displacement weggelassen).
    nt.links.new(color_stack, bsdf.inputs['Base Color'])

    return mat


def _create_terrain_object(node, scene, i3d_dir, terrain_lod,
                           terrain_base_color, terrain_poc_layer_names,
                           image_cache, report):
    """Build a Plane + Displace modifier from the <TerrainTransformGroup>.

    Returns a Blender Object with:
      - a flat grid mesh of (segments+1)^2 vertices in the XY plane, centered
        on the object origin,
      - a Displace modifier that samples the DEM PNG to set Z heights at render
        time (no Apply — see option C: snap on evaluated geometry),
      - all TerrainTransformGroup XML attributes preserved as Custom Properties
        ``i3D_terrain_<name>`` (informational — the Giants Blender Exporter does
        not write TerrainTransformGroup nodes; this is one-way import).

    terrain_base_color: RGBA tuple (linear) used as the base color shown where
    no PoC layer has any weight. Passed through from the operator/preferences.

    terrain_poc_layer_names: comma-separated string of <CombinedLayer> names
    to load (case-sensitive). Up to 5 entries; extras are dropped with a
    warning. Invalid names get substituted from a hardcoded default list.

    Returns None when terrain_lod == 'OFF' or when the heightmap cannot be
    resolved (missing fileId, file not on disk, etc.).
    """
    if terrain_lod == 'OFF':
        return None

    # One-way import warning: the Giants Blender Exporter cannot re-emit a
    # <TerrainTransformGroup>. Re-exporting a scene that contains an imported
    # terrain will silently drop the terrain (or fail later).
    report('WARNING',
           f"Terrain '{node.name}' import works one-way "
           f"only, it is designed for testing purposes. You cannot re-export "
           f"it with the Giants Blender i3d Exporter.")

    raw = node.raw_attrs
    height_map_id_str = raw.get('heightMapId')
    if height_map_id_str is None:
        report('WARNING', f"Terrain '{node.name}': heightMapId attribute missing")
        return None
    try:
        height_map_id = int(height_map_id_str)
    except (TypeError, ValueError):
        report('WARNING',
               f"Terrain '{node.name}': invalid heightMapId {height_map_id_str!r}")
        return None

    heightmap_relpath = scene.files.get(height_map_id)
    if not heightmap_relpath:
        report('WARNING',
               f"Terrain '{node.name}': heightMapId {height_map_id} not in <Files>")
        return None

    # Resolve the heightmap path. _resolve_filepath handles the $data prefix
    # (e.g. "$data/maps/mapUS/data/dem.png" -> FS25_DATA_BASE/maps/mapUS/...)
    # and falls back to .dds if a same-name .dds exists - acceptable for DEMs
    # since they are typically distributed as 16-bit PNG, not DDS.
    hm_path = _resolve_filepath(heightmap_relpath, Path(i3d_dir))
    if hm_path is None:
        report('WARNING',
               f"Terrain '{node.name}': heightmap could not be resolved: "
               f"{heightmap_relpath!r} (FS25_DATA_BASE={FS25_DATA_BASE!r})")
        return None

    # Load the heightmap. Blender handles 16-bit grayscale PNG natively;
    # tag it as Non-Color so the renderer / Displace doesn't apply sRGB.
    try:
        img = bpy.data.images.load(str(hm_path), check_existing=True)
    except Exception as e:
        report('WARNING',
               f"Terrain '{node.name}': failed to load heightmap {hm_path}: {e}")
        return None
    try:
        img.colorspace_settings.name = 'Non-Color'
    except Exception:
        pass  # some pipelines don't expose colorspace_settings
    width, height = img.size
    if width <= 1 or height <= 1:
        report('WARNING',
               f"Terrain '{node.name}': heightmap too small ({width}x{height})")
        return None
    if width != height:
        report('INFO',
               f"Terrain '{node.name}': non-square heightmap {width}x{height} - "
               "using width for both axes")

    # XML scalar properties (with safe defaults from Giants).
    try:
        units_per_pixel = float(raw.get('unitsPerPixel', 1.0))
    except (TypeError, ValueError):
        units_per_pixel = 1.0
    try:
        height_scale = float(raw.get('heightScale', 255.0))
    except (TypeError, ValueError):
        height_scale = 255.0

    # World size in meters: the heightmap has WIDTH pixels giving the heights
    # at WIDTH vertex positions; spacing between adjacent vertices is
    # unitsPerPixel meters, so the grid spans (WIDTH-1) * unitsPerPixel.
    world_size = (width - 1) * units_per_pixel

    # LOD choice -> number of grid subdivisions.
    lod_segments_map = {
        'FULL':    width - 1,
        'HALF':    max(1, (width - 1) // 2),
        'QUARTER': max(1, (width - 1) // 4),
    }
    segments = lod_segments_map.get(terrain_lod, width - 1)

    # Build the grid mesh via bmesh (C-speed for the vertex creation).
    # bmesh.ops.create_grid centers the grid on (0,0,0) in the XY plane and
    # creates segment+1 vertices per side. calc_uvs=True attaches a UV layer
    # going from (0,0) at one corner to (1,1) at the opposite — exactly what
    # the Displace modifier needs to sample the heightmap.
    import bmesh
    import time
    mesh = bpy.data.meshes.new(name=(node.name or "terrain") + "_mesh")
    bm = bmesh.new()
    t0 = time.perf_counter()
    try:
        bmesh.ops.create_grid(bm,
                              x_segments=segments,
                              y_segments=segments,
                              size=world_size / 2.0,
                              calc_uvs=True)
    except TypeError:
        # Older bmesh.ops.create_grid lacks calc_uvs - retry without it and
        # build UVs manually afterwards.
        bmesh.ops.create_grid(bm,
                              x_segments=segments,
                              y_segments=segments,
                              size=world_size / 2.0)
    bm.to_mesh(mesh)
    bm.free()
    elapsed = time.perf_counter() - t0

    # Shade smooth: Terrain soll glatt schattiert werden (keine sichtbaren
    # Facetten zwischen den Grid-Quads). foreach_set ist der schnellste Weg
    # fuer Bulk-Set ueber alle Polygone und braucht keinen Operator-Context.
    if mesh.polygons:
        mesh.polygons.foreach_set(
            'use_smooth', [True] * len(mesh.polygons)
        )

    # If create_grid didn't add UVs (older Blender), create them manually
    # from vertex XY positions normalised to [0,1].
    if not mesh.uv_layers:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        half = world_size / 2.0
        for loop_idx, loop in enumerate(mesh.loops):
            v = mesh.vertices[loop.vertex_index].co
            u = (v.x + half) / world_size if world_size else 0.0
            w = (v.y + half) / world_size if world_size else 0.0
            uv_layer.data[loop_idx].uv = (u, w)

    # Create the Object that hosts mesh + modifier.
    obj = bpy.data.objects.new(name=node.name or "terrain", object_data=mesh)

    # Displace modifier - drives the Z height per UV-sampled heightmap pixel.
    # strength = heightScale (meters at white pixel), mid_level = 0 so that
    # black pixel = 0 m. texture_coords = UV uses the layer we set up above.
    tex = bpy.data.textures.new(name=f"{obj.name}_heightmap", type='IMAGE')
    tex.image = img
    tex.extension = 'EXTEND'
    mod = obj.modifiers.new(name="Heightmap", type='DISPLACE')
    mod.texture = tex
    mod.strength = height_scale
    mod.mid_level = 0.0
    mod.texture_coords = 'UV'
    mod.direction = 'Z'

    # Preserve all XML attributes as Custom Properties (informational only —
    # the Giants Blender Exporter does not re-emit TerrainTransformGroup).
    obj['_i3d_kind'] = 'TerrainTransformGroup'
    # Opt out of Y-up -> Z-up axis correction: the heightmap-driven grid is
    # built in Blender's XY plane (Z-up) directly, so applying the wrapper's
    # X+90 rotation would tip it on its side. _apply_axis_correction reads
    # this flag and skips the object (and its descendants).
    obj['_i3d_skip_axis_correction'] = True
    if node.nodeId is not None and node.nodeId >= 0:
        obj['_i3d_nodeId'] = int(node.nodeId)
    for k, v in raw.items():
        obj[f'i3D_terrain_{k}'] = str(v)

    # Sub-3.2: PoC Layer-Texturen laden. Namen kommen vom Operator
    # (kommagetrennter String aus den Preferences) und werden hier geparst,
    # auf Max 5 gekappt, gegen die in dieser Map existierenden <CombinedLayer>
    # validiert. Ungueltige/doppelte Namen werden durch Defaults ersetzt.
    _DEFAULT_POC = ['ASPHALT', 'GRASS', 'MUD', 'FOREST_LEAVES', 'FOREST_GRASS']
    _MAX_POC = 5

    user_names = [n.strip() for n in terrain_poc_layer_names.split(',')
                  if n.strip()]

    # Cap to 5 (Eevee 32-sampler limit at detail+weight channels only).
    if len(user_names) > _MAX_POC:
        dropped = user_names[_MAX_POC:]
        user_names = user_names[:_MAX_POC]
        report('WARNING',
               f"Terrain '{node.name}': only the first {_MAX_POC} PoC "
               f"layer names are used (Eevee's 32 image-sampler per-material "
               f"limit caps the count); dropped: {dropped}")

    available = {cl.name for cl in node.terrain_combined_layers}

    poc_combined_layer_names = []
    seen = set()
    for name in user_names:
        if name in available and name not in seen:
            poc_combined_layer_names.append(name)
            seen.add(name)
            continue
        # Need a fallback from the defaults.
        fallback = next((dn for dn in _DEFAULT_POC
                         if dn in available and dn not in seen), None)
        if fallback is None:
            report('WARNING',
                   f"Terrain '{node.name}': PoC layer '{name}' not present "
                   f"in this map and no default fallback available - dropped")
            continue
        if name not in available:
            report('WARNING',
                   f"Terrain '{node.name}': PoC layer '{name}' not present "
                   f"in this map - substituting default '{fallback}'")
        else:
            report('WARNING',
                   f"Terrain '{node.name}': PoC layer '{name}' listed twice "
                   f"- substituting default '{fallback}' for the duplicate")
        poc_combined_layer_names.append(fallback)
        seen.add(fallback)

    terrain_layer_plan = _load_combined_layer_textures(
        node, scene.files, i3d_dir, image_cache, report,
        selected_names=poc_combined_layer_names,
    )
    # Inspection-Property: wie viele Bilder konnten aufgeloest werden?
    # Sichtbar im Object-Properties-Panel als Custom Property.
    obj['_i3d_terrain_poc_image_count'] = sum(
        sum(1 for k in ('detail_image', 'weight_image')
            if sl[k] is not None)
        for cl in terrain_layer_plan for sl in cl['sub_layers']
    )

    # Sub-3.3: NodeGroup pro CombinedLayer bauen (worldspace-triplanar).
    # Die NodeGroups sind unter Add -> Group im Shader-Editor verfuegbar.
    # Verbindung ans Material folgt in Sub-3.4.
    snippets_path = (Path(SNIPPETS_BLEND_PATH) if SNIPPETS_BLEND_PATH
                     else recipe_loader.default_snippets_blend_path())
    poc_node_group_names = []
    for cl_data in terrain_layer_plan:
        ng = _build_combined_layer_node_group(
            cl_data, snippets_path, _snippet_cache, report
        )
        if ng is not None:
            poc_node_group_names.append(ng.name)
    if poc_node_group_names:
        report('INFO',
               f"Terrain '{obj.name}': {len(poc_node_group_names)} NodeGroup(s) built: "
               f"{', '.join(poc_node_group_names)}")
    obj['_i3d_terrain_poc_node_groups'] = ', '.join(poc_node_group_names)

    # Sub-3.4 + 3.5: Master-Material bauen und an Terrain-Mesh anhaengen.
    # Verschaltet die CombinedLayer-NodeGroups via weightMap-Mixing-Stack.
    if terrain_layer_plan and poc_node_group_names:
        master_mat = _build_terrain_master_material(
            obj, terrain_layer_plan, snippets_path, _snippet_cache, report,
            base_color=terrain_base_color,
        )
        if master_mat is not None:
            obj.data.materials.append(master_mat)
            report('INFO',
                   f"Terrain '{obj.name}': master material "
                   f"'{master_mat.name}' attached")

    report('INFO',
           f"Terrain '{obj.name}': built {segments + 1}x{segments + 1} grid "
           f"({world_size:.0f}m x {world_size:.0f}m, heightScale={height_scale:.0f}m, "
           f"LOD={terrain_lod}, mesh build {elapsed:.2f}s)")
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
    """Translation 1:1, rotation Y-up -> Z-up converted, scale 1:1.

    XML rotation values come from the FS25 (Y-up) coordinate system. The
    same rotation expressed in Blender's Z-up world is
        R_blender = M @ R_xml @ M^-1,  with M = rotate +90 around X.
    This matches the X+90 vertex bake applied by _apply_axis_correction, so
    the round-trip through the Giants exporter (which does Z-up -> Y-up on
    rotation_euler) returns the original XML values unchanged.

    Sanity-checked: XML (0,-90,0) <-> Blender (0,0,-90),
    XML (0,90,0) <-> Blender (0,0,90).

    Light/Camera special case: the Giants exporter (dccBlender.py:1276) does
    `m_matrix = bake(matrix_local) @ R_x(-90)` for LIGHT/CAMERA types - a
    post-mult that compensates Blender's local-Z-forward light convention
    against FS25's Y-up local-Z-forward convention. The importer mirrors that
    with an extra @ R_x(+90) post-mult so the round-trip is symmetric.
    """
    import mathutils
    obj.location = node.translation
    obj.rotation_mode = 'XYZ'
    xml_euler = mathutils.Euler((
        math.radians(node.rotation[0]),
        math.radians(node.rotation[1]),
        math.radians(node.rotation[2]),
    ), 'XYZ')
    R_xml = xml_euler.to_matrix().to_4x4()
    M = mathutils.Matrix.Rotation(math.radians(90), 4, 'X')
    R_blender = M @ R_xml @ M.inverted()

    if obj.type in ('LIGHT', 'CAMERA'):
        R_x_plus90 = mathutils.Matrix.Rotation(math.radians(90), 4, 'X')
        R_blender = R_blender @ R_x_plus90

    obj.rotation_euler = R_blender.to_euler('XYZ')
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

    # Re-exportable copy of the original XML nodeId. Stored as a
    # userAttribute_* Custom Property so the Giants exporter writes it back
    # into the .i3d XML as an <UserAttribute> entry. A future GE LUA
    # collapse script uses this to match Wrapper-Armature bones (which carry
    # the original nodeId in their skinBoneMap string) against the original
    # TG that should host the skin-bind reference. Matching by ID is
    # immune to user-driven renames; matching by name is not.
    if node.nodeId is not None and node.nodeId >= 0:
        obj['userAttribute_integer_originalNodeId'] = int(node.nodeId)

    # UserAttributes from the i3d XML <UserAttributes> block - stored as
    # Custom Properties using the Giants exporter naming convention so they
    # survive re-export 1:1 (see dccBlender.py:getNodeUserAttributes).
    ua_list = scene.user_attributes.get(node.nodeId)
    if ua_list:
        for a_name, a_type, a_value in ua_list:
            key = f"userAttribute_{a_type}_{a_name}"
            try:
                if a_type == 'boolean':
                    obj[key] = (a_value.strip().lower() == 'true')
                elif a_type == 'integer':
                    obj[key] = int(a_value)
                elif a_type == 'float':
                    obj[key] = float(a_value)
                else:
                    # 'string', 'scriptCallback' or anything else - stored as-is
                    obj[key] = a_value
            except (ValueError, TypeError):
                report('WARNING',
                       f"{obj.name}: could not parse userAttribute "
                       f"{a_name!r} (type={a_type}, value={a_value!r}) - stored as string")
                obj[key] = a_value

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

    # Note-specific: text/color/fixedSize -> i3D_note_* Custom Properties.
    # The Giants Blender exporter does NOT know <Note> and won't re-emit
    # these — this is purely visualisation/orientation in the Outliner.
    # We still pop them out of raw so apply_attrs_to_object doesn't try to
    # store them as _i3d_raw_* duplicates.
    if node.kind == 'Note':
        raw = dict(raw)
        if 'text' in raw:
            obj['i3D_note_text'] = raw.pop('text')
        if 'color' in raw:
            c = raw.pop('color')
            try:
                obj['i3D_note_color'] = str(int(c, 0))  # uint32 as decimal string
            except (ValueError, TypeError):
                obj['i3D_note_color'] = str(c)
        if 'fixedSize' in raw:
            obj['i3D_note_fixedSize'] = (
                str(raw.pop('fixedSize')).strip().lower() == 'true')

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
    if _CURRENT_IMPORT_UUID is not None:
        mat['_i3d_import_uuid'] = _CURRENT_IMPORT_UUID

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

    # Re-export materials intentionally don't get the fs25_debug:* switch -
    # the Mix Shader before Material Output would break re-export through
    # the Giants i3d Exporter. Debug-view lives only on the PBR debug
    # material (built below if BUILD_PBR_DEBUG_MATERIALS is enabled).

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

def _create_curve_object(node, spline, report):
    """Curve object from a decoded Spline (i3d_shapes_models.Spline).

    Maps the FS25 spline representation to a Blender POLY curve:
      - spline.points -> spline.points[i].co (4D: x,y,z, w=1.0)
      - spline.form_closed -> bpy.types.Spline.use_cyclic_u
      - spline.kind ("SPLINE" or "SPLINE_L") preserved as custom property
        so the re-export can tell linear ("Lite") splines from cubic ones.
      - spline.attr_flags (UnknownFlags2 in the C# reference) is kept as a
        marker; non-zero indicates per-point attributes that we do not yet
        decode - a warning is emitted at decode time in import_i3d().

    The XML <Shape ... shapeId="N"> node carries translation/rotation/scale;
    those are applied by _apply_transform() in the caller. The point
    coordinates here stay in the shape's local space, identical to how the
    Giants exporter writes them.
    """
    if not spline.points:
        report('WARNING', f"Spline '{node.name}': no points decoded")
        return None

    curve = bpy.data.curves.new(name=node.name, type='CURVE')
    curve.dimensions = '3D'
    bspl = curve.splines.new('POLY')
    # POLY splines start with 1 point - add the rest.
    bspl.points.add(len(spline.points) - 1)
    for i, p in enumerate(spline.points):
        bspl.points[i].co = (p.x, p.y, p.z, 1.0)
    bspl.use_cyclic_u = spline.form_closed

    obj = bpy.data.objects.new(name=node.name, object_data=curve)
    obj['_i3d_isSpline'] = True
    obj['_i3d_splineKind'] = spline.kind        # "SPLINE" or "SPLINE_L"
    obj['_i3d_splineClosed'] = spline.form_closed
    obj['_i3d_splineAttrFlags'] = int(spline.attr_flags)
    return obj



# ---------------------------------------------------------------------------
# MergedChildren splitting (Phase 3d)
# ---------------------------------------------------------------------------


def _process_merge_children(import_collection, shape_map, shape_id_to_obj, report):
    """For each MergedChildren-Shape (HAS_GENERIC, NO skin):
      - Split the merged geometry by unique 'g' value
        (each Child of the original parent-TG got g = i/32767 at export time)
      - Convert the current Mesh object into a TransformGroup (Empty) with
        i3D_mergeChildren=True so the Giants exporter re-merges it on re-export
      - Create one Mesh child per g-slot, parented under that empty, carrying
        only its slot's vertices/triangles/uvs/etc.

    A future GE LUA collapse pass is NOT needed for MergedChildren: the
    re-export through the Giants exporter is naturally roundtrip-true because
    the children-list ordering preserves the g-value mapping.
    """
    import bpy as _bpy
    _bpy.context.view_layer.update()

    mc_counter = 0
    for shape_id, shape in shape_map.items():
        # MergedChildren = has generic data but no skin info.
        if shape.generic_data is None:
            continue
        if shape.has_skin:
            continue

        mesh_obj = shape_id_to_obj.get(shape_id)
        if mesh_obj is None:
            continue

        # Compute unique g slots from generic_data.
        # The exporter writes g = child_index / 32767, so round-trip gives the
        # original integer slot number.
        g_to_verts = {}
        for v_idx, g_val in enumerate(shape.generic_data):
            slot = int(round(g_val * 32767.0))
            g_to_verts.setdefault(slot, []).append(v_idx)

        # If there is only a single slot, this shape doesn't actually carry
        # multiple children. Leave it alone.
        if len(g_to_verts) <= 1:
            continue

        mc_counter += 1
        sorted_slots = sorted(g_to_verts.keys())

        # ---- Compute per-slot triangle lists + vertex remapping ----
        num_slots = len(sorted_slots)
        slot_index_of_g = {g: i for i, g in enumerate(sorted_slots)}

        per_slot_verts = [g_to_verts[g] for g in sorted_slots]   # global vert idxs
        global_to_local = [{} for _ in range(num_slots)]
        for slot_i, verts in enumerate(per_slot_verts):
            for local_idx, global_idx in enumerate(verts):
                global_to_local[slot_i][global_idx] = local_idx

        # Per-triangle slot assignment: all 3 vertices of a triangle should
        # share the same g-value (children don't overlap).
        global_face_subset = [0] * len(shape.triangles)
        for subset_idx, sub in enumerate(shape.subsets):
            first_tri = sub.first_index // 3
            last_tri = (sub.first_index + sub.num_indices) // 3
            for t in range(max(0, first_tri), min(len(shape.triangles), last_tri)):
                global_face_subset[t] = subset_idx

        per_slot_tris = [[] for _ in range(num_slots)]
        per_slot_face_subsets = [[] for _ in range(num_slots)]
        cross_tri_count = 0
        for tri_idx, tri in enumerate(shape.triangles):
            g1, g2, g3 = tri.p1 - 1, tri.p2 - 1, tri.p3 - 1
            sl1 = slot_index_of_g.get(int(round(shape.generic_data[g1] * 32767.0)))
            sl2 = slot_index_of_g.get(int(round(shape.generic_data[g2] * 32767.0)))
            sl3 = slot_index_of_g.get(int(round(shape.generic_data[g3] * 32767.0)))
            if sl1 == sl2 == sl3 and sl1 is not None:
                local = (global_to_local[sl1][g1],
                         global_to_local[sl1][g2],
                         global_to_local[sl1][g3])
                per_slot_tris[sl1].append(local)
                per_slot_face_subsets[sl1].append(global_face_subset[tri_idx])
            else:
                cross_tri_count += 1
                # Fall back to first vertex's slot
                target = sl1 if sl1 is not None else 0
                local = tuple(
                    global_to_local[target].setdefault(g, len(per_slot_verts[target]))
                    for g in (g1, g2, g3)
                )
                per_slot_tris[target].append(local)
                per_slot_face_subsets[target].append(global_face_subset[tri_idx])

        if cross_tri_count:
            report('INFO',
                   f"{mesh_obj.name}: {cross_tri_count} cross-slot triangle(s) "
                   f"in MergedChildren — assigned to first vertex's slot")

        # ---- Helper to build a sub-mesh datablock for one slot ----
        root_materials = list(mesh_obj.data.materials)

        def _build_sub(name, vert_idxs, tris, face_subsets):
            verts = [(shape.positions[i].x, shape.positions[i].y, shape.positions[i].z)
                     for i in vert_idxs]
            mdb = _bpy.data.meshes.new(name=name)
            mdb.from_pydata(verts, [], tris)
            mdb.update(calc_edges=True)
            used = sorted(set(face_subsets))
            remap = {s: i for i, s in enumerate(used)}
            for subset_idx in used:
                if 0 <= subset_idx < len(root_materials):
                    mdb.materials.append(root_materials[subset_idx])
                else:
                    mdb.materials.append(None)
            for poly_idx, subset_idx in enumerate(face_subsets):
                if poly_idx < len(mdb.polygons):
                    mdb.polygons[poly_idx].material_index = remap[subset_idx]
            # UV channels
            uv_layer_names = ("UVMap", "UV2", "UV3", "UV4")
            for uv_ch, uv_set in enumerate(shape.uv_sets):
                if uv_set is None:
                    continue
                uv_layer = mdb.uv_layers.new(name=uv_layer_names[uv_ch])
                for poly in mdb.polygons:
                    for loop_idx in poly.loop_indices:
                        loop = mdb.loops[loop_idx]
                        global_v = vert_idxs[loop.vertex_index]
                        uv = uv_set[global_v]
                        uv_layer.data[loop_idx].uv = (uv.u, uv.v)
            if shape.vertex_colors is not None:
                color_layer = mdb.color_attributes.new(
                    name="Color", type='FLOAT_COLOR', domain='CORNER'
                )
                for poly in mdb.polygons:
                    for loop_idx in poly.loop_indices:
                        loop = mdb.loops[loop_idx]
                        global_v = vert_idxs[loop.vertex_index]
                        c = shape.vertex_colors[global_v]
                        color_layer.data[loop_idx].color = (c.x, c.y, c.z, c.w)
            # Custom split normals (GitHub #1/#7), remapped to local verts.
            if shape.normals is not None:
                _apply_custom_split_normals(
                    mdb,
                    [(shape.normals[g].x, shape.normals[g].y, shape.normals[g].z)
                     for g in vert_idxs],
                )
            return mdb

        # ---- Convert the mesh_obj to an Empty (TransformGroup) ----
        # Capture state
        parent_name = mesh_obj.name
        old_world = mesh_obj.matrix_world.copy()
        old_parent = mesh_obj.parent
        old_children = list(mesh_obj.children)
        old_child_worlds = {c.name: c.matrix_world.copy() for c in old_children}
        old_props = {k: mesh_obj[k] for k in mesh_obj.keys()}
        old_mesh_data = mesh_obj.data

        # Remove the mesh object
        for coll in list(mesh_obj.users_collection):
            coll.objects.unlink(mesh_obj)
        mesh_obj.name = parent_name + "__to_remove"
        _bpy.data.objects.remove(mesh_obj, do_unlink=True)
        if old_mesh_data.users == 0:
            _bpy.data.meshes.remove(old_mesh_data, do_unlink=True)

        # Create new Empty
        empty_obj = _bpy.data.objects.new(name=parent_name, object_data=None)
        for k, v in old_props.items():
            if k == '_RNA_UI':
                continue
            try:
                empty_obj[k] = v
            except Exception:
                pass
        if old_parent is not None:
            empty_obj.parent = old_parent
        import_collection.objects.link(empty_obj)
        empty_obj.matrix_world = old_world
        empty_obj['i3D_mergeChildren'] = True
        empty_obj['_i3d_kind'] = 'TransformGroup'
        # Re-parent former children of the mesh (preserve world)
        for child in old_children:
            cw = old_child_worlds.get(child.name)
            child.parent = empty_obj
            if cw is not None:
                child.matrix_world = cw

        # ---- Create one Mesh child per slot ----
        for slot_i, g_slot in enumerate(sorted_slots):
            child_name = f"{parent_name}_part{slot_i:02d}"
            sub_mesh = _build_sub(child_name, per_slot_verts[slot_i],
                                  per_slot_tris[slot_i], per_slot_face_subsets[slot_i])
            sub_obj = _bpy.data.objects.new(name=child_name, object_data=sub_mesh)
            sub_obj.parent = empty_obj
            import_collection.objects.link(sub_obj)
            # The vertex positions are in the parent-TG's local space already
            # (Giants exporter bakes the child transform into the vertices when
            # freeze flags are off), so we DON'T set a local translation.
            sub_obj['_i3d_kind'] = 'Shape'
            sub_obj['_i3d_mergeChildrenSlot'] = g_slot

        report('INFO',
               f"{parent_name}: MergedChildren #{mc_counter} split into "
               f"{num_slots} child slot(s)")


# ---------------------------------------------------------------------------
# Skin-Weights / Armature setup (Phase 3c)
# ---------------------------------------------------------------------------


def _compute_xml_world_translation(obj):
    """Walk obj's parent chain accumulating the proper XML (Y-up) world
    translation.

    Background: the importer stores raw XML translations in obj.location
    (1:1, no axis conversion) but Z-up-converted rotations in
    obj.rotation_euler (R_blender = R_x(+90) @ R_xml @ R_x(-90)). Chaining
    these gives T1 + R_blender @ T2, which differs from the true XML-chain
    value T1 + R_xml @ T2 whenever T2 has non-X components (the R_x
    conjugation only commutes through X-axis vectors).

    For non-bone TGs this is masked because _apply_axis_correction's
    transform_apply step bakes R_x(+90) into every descendant's
    matrix_basis.location too, putting them at the proper Z-up world
    position. The Giants exporter's bakeTransformMatrix then undoes
    that on export and the XML round-trips.

    Bones are different: they get their head set BEFORE axis correction
    using the hybrid src.matrix_world.translation, which is NOT the
    proper XML world position. After axis correction bakes R_x(+90) into
    bone data, the bone ends up at R_x(+90) of the hybrid value, not at
    R_x(+90) of the true XML world position. This helper recovers the
    true XML world translation by walking the chain with R_xml extracted
    from each ancestor's R_blender via R_xml = R_x(-90) @ R_blender @ R_x(+90).
    """
    import mathutils
    M_inv = mathutils.Matrix.Rotation(math.radians(-90), 3, 'X')
    M = mathutils.Matrix.Rotation(math.radians(90), 3, 'X')

    chain = []
    o = obj
    while o is not None:
        chain.append(o)
        o = o.parent
    chain.reverse()

    pos = mathutils.Vector((0.0, 0.0, 0.0))
    rot = mathutils.Matrix.Identity(3)
    for o in chain:
        T_local = mathutils.Vector(o.location)
        R_blender = o.matrix_basis.to_3x3()
        R_xml = M_inv @ R_blender @ M
        pos = pos + rot @ T_local
        rot = rot @ R_xml
    return pos


def _process_skin_weights(import_collection, shape_map, shape_id_to_obj, report):
    """For each Skin-Weights-Shape (4 weighted bones per vertex):
      - Build an Armature with one Bone per source-TG (referenced via
        skinBindNodeIds), positioned at the source-TG's world location.
      - Add an Armature-Modifier on the mesh pointing to this armature.
      - Create one vertex group per bone (matching bone name = TG name).
      - Distribute the per-vertex bw/bi values into those vertex groups.

    The source-TG Empties are intentionally NOT removed — they keep the
    original i3d hierarchy intact so the rest of the scene (lights/cameras
    parented under them, userAttribute liw=true, etc.) still works.
    """
    import bpy as _bpy
    from mathutils import Vector as _Vec, Matrix as _Mat

    # Flush pending transforms so source_obj.matrix_world reflects the
    # location/rotation set by _apply_transform.
    _bpy.context.view_layer.update()

    node_id_to_obj = {}
    for obj in import_collection.objects:
        nid = obj.get('_i3d_nodeId')
        if nid is not None:
            try:
                node_id_to_obj[int(nid)] = obj
            except (TypeError, ValueError):
                pass

    skin_meshes = []
    for shape_id, shape in shape_map.items():
        if not shape.is_armature_skin:
            continue
        mesh_obj = shape_id_to_obj.get(shape_id)
        if mesh_obj is None:
            continue
        skin_meshes.append((shape_id, shape, mesh_obj))

    if not skin_meshes:
        return

    # Remember the original active/mode so we can restore them after the
    # Edit-Mode toggles needed for bone creation.
    orig_active = _bpy.context.view_layer.objects.active
    try:
        orig_mode = _bpy.context.mode
    except AttributeError:
        orig_mode = 'OBJECT'

    for shape_id, shape, mesh_obj in skin_meshes:
        raw = mesh_obj.get('_i3d_skinBindNodeIds_raw', '')
        try:
            bind_ids = [int(x) for x in str(raw).split() if x.strip()]
        except ValueError:
            report('WARNING',
                   f"{mesh_obj.name}: skinBindNodeIds {raw!r} not parsable — "
                   f"skin-weights skipped")
            continue
        if not bind_ids:
            continue

        # Resolve every bind ID to its source TG empty.
        source_tgs = [node_id_to_obj.get(nid) for nid in bind_ids]
        missing = [nid for nid, src in zip(bind_ids, source_tgs) if src is None]
        if missing:
            report('WARNING',
                   f"{mesh_obj.name}: skinBindNodes not found: {missing} — "
                   f"corresponding bones will be placed at origin")

        # ---- Build the Armature datablock + object ----
        arm_name = f"{mesh_obj.name}_armature"
        arm_data = _bpy.data.armatures.new(arm_name)
        arm_obj = _bpy.data.objects.new(arm_name, arm_data)
        import_collection.objects.link(arm_obj)
        arm_obj.parent = mesh_obj.parent
        # Place the armature at the mesh's world location so bone offsets
        # remain simple deltas.
        arm_obj.matrix_world = mesh_obj.matrix_world.copy()
        arm_obj['_i3d_kind'] = 'Armature'
        # Markers for a future GE LUA collapse script: identifies this
        # armature as a wrapper synthesized at import-time, and records the
        # mesh it skins so the script can rewrite skinBindNodeIds.
        # Stored as userAttribute_* so the Giants exporter writes them into
        # the .i3d XML as <UserAttribute> entries (dccBlender.py:2214 only
        # picks up that naming pattern). Bone-level Custom Properties are
        # NOT exported by the Giants exporter — so we pack per-bone info
        # into a single string on the armature object instead.
        arm_obj['userAttribute_boolean_skinWrapper'] = True
        arm_obj['userAttribute_string_skinWrapperOwner'] = mesh_obj.name

        # Edit-Mode to add bones
        _bpy.context.view_layer.objects.active = arm_obj
        _bpy.ops.object.mode_set(mode='EDIT')
        try:
            bone_names = []
            used_names = set()
            for slot_idx, (nid, src) in enumerate(zip(bind_ids, source_tgs)):
                if src is None:
                    base_name = f"__skin_missing_{nid}"
                else:
                    base_name = src.name
                # Ensure unique bone names within this armature.
                name = base_name
                suffix = 1
                while name in used_names:
                    name = f"{base_name}.{suffix:03d}"
                    suffix += 1
                used_names.add(name)
                bone_names.append(name)

                bone = arm_data.edit_bones.new(name)
                if src is not None:
                    inv_arm = arm_obj.matrix_world.inverted()
                    # Use proper XML-chain world translation to avoid the
                    # hybrid T1 + R_blender @ T2 bug (see
                    # _compute_xml_world_translation docstring). Assumes
                    # arm_obj has no rotation in its world chain (true for
                    # standard skin-mesh hierarchies where the armature is
                    # parented to a rotation-less container).
                    head_local = (_compute_xml_world_translation(src)
                                  - _compute_xml_world_translation(arm_obj))

                    # pre-axis bone.matrix = R_x(-90) @ src.matrix_world @ R_x(+90)
                    # (= Y-up XML representation, since the exporter's bakeTransformMatrix
                    # + the "rotation.x -= 90" trick together un-do Blender's Z-up conversion
                    # and the axis-correction bake)
                    R_xm90 = _Mat.Rotation(math.radians(-90), 3, 'X')
                    R_xp90 = _Mat.Rotation(math.radians(90), 3, 'X')
                    src_rot = (inv_arm @ src.matrix_world).to_3x3()
                    R_target = R_xm90 @ src_rot @ R_xp90
                    
                    y_axis = R_target.col[1]
                    z_axis = R_target.col[2]
                    if y_axis.length > 1e-6:
                        y_axis.normalize()
                    else:
                        y_axis = _Vec((0.0, 1.0, 0.0))
                    if z_axis.length > 1e-6:
                        z_axis.normalize()
                    else:
                        z_axis = _Vec((0.0, 0.0, 1.0))
                    bone.head = head_local
                    bone.tail = bone.head + y_axis * 0.1
                    bone.align_roll(z_axis)
        finally:
            _bpy.ops.object.mode_set(mode='OBJECT')

        # ---- Per-bone markers packed as one string on the armature ----
        # The Giants exporter only writes per-object UserAttributes, so we
        # cannot expose per-bone Custom Properties through it. Instead, pack
        # the bone-name -> original-NodeID mapping into a single semicolon-
        # separated string on the armature object:
        #     "boneName0:nid0;boneName1:nid1;..."
        bone_map_parts = []
        for slot_idx, (b_name, b_nid) in enumerate(zip(bone_names, bind_ids)):
            bone_map_parts.append(f"{b_name}:{int(b_nid)}")
        arm_obj['userAttribute_string_skinBoneMap'] = ';'.join(bone_map_parts)

        # Also keep the original NodeName list, for diagnostics / sanity.
        arm_obj['userAttribute_string_skinBoneOriginalNames'] = ','.join(
            (s.name if s is not None else f"__missing_{nid}")
            for s, nid in zip(source_tgs, bind_ids)
        )

        # ---- Mesh-side markers: preserve the original skinBindNodeIds list ----
        # The Skin-Bindings pre-pass set _i3d_skinBindNodeIds_raw from the XML.
        # We now also expose it through userAttribute_string_skinBindNodeIds_raw
        # so it survives Giants-exporter re-export and is readable by a future
        # GE LUA collapse script.
        mesh_obj['userAttribute_string_skinBindOriginalNodeIds'] = ' '.join(
            str(n) for n in bind_ids
        )
        mesh_obj['userAttribute_string_skinBindOriginalNodeNames'] = ','.join(
            (s.name if s is not None else f"__missing_{nid}")
            for s, nid in zip(source_tgs, bind_ids)
        )
        mesh_obj['userAttribute_string_skinArmatureName'] = arm_obj.name

        # ---- Armature modifier on the mesh ----
        # Remove any pre-existing armature modifier first (idempotency).
        for m in list(mesh_obj.modifiers):
            if m.type == 'ARMATURE':
                mesh_obj.modifiers.remove(m)
        mod = mesh_obj.modifiers.new(name="Armature", type='ARMATURE')
        mod.object = arm_obj

        # ---- Vertex groups ----
        vg_map = {}
        for bone_name in bone_names:
            # If a vertex group with this name already exists (shouldn't),
            # reuse it rather than creating duplicates.
            existing = mesh_obj.vertex_groups.get(bone_name)
            vg_map[bone_name] = existing if existing else mesh_obj.vertex_groups.new(name=bone_name)

        # ---- Apply weights ----
        # shape.blend_weights[i] = (w0, w1, w2, w3) floats summing to 1
        # shape.blend_indices[i] = (bi0, bi1, bi2, bi3) bone-slot ints
        num_slots = len(bone_names)
        weight_count = 0
        for v_idx, (bw_tuple, bi_tuple) in enumerate(zip(shape.blend_weights,
                                                         shape.blend_indices)):
            for w, bi in zip(bw_tuple, bi_tuple):
                if w == 0.0:
                    # Zero-weight slot — typical for vertices that only use
                    # 1-3 bones; Giants pads bi=0, w=0.0 in the unused slots.
                    continue
                if bi >= num_slots:
                    continue
                bone_name = bone_names[bi]
                vg_map[bone_name].add([v_idx], float(w), 'REPLACE')
                weight_count += 1

        # Keep _i3d_skinBindNodeIds_raw on the mesh — a later GE LUA script
        # may consume it to rewrite skinBindNodeIds back onto the original TGs
        # (see _i3d_skinWrapper marker on the armature).

        report('INFO',
               f"{mesh_obj.name}: Armature {arm_obj.name!r} with "
               f"{len(bone_names)} bone(s), {weight_count} vertex weight(s)")

    # Restore previous active object + mode
    if orig_active is not None:
        try:
            _bpy.context.view_layer.objects.active = orig_active
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Skin Bind Node IDs (maybe later version, very complex) 

# ---------------------------------------------------------------------------
# Merge-Group geo-splitting (Phase 3b)
# ---------------------------------------------------------------------------


def _fix_rootspace_mergegroup_local_space(import_collection, report):
    """Map root-space merge-group slot vertices into each bound node's local
    space. Triggered for shapes whose noBindPose flag is absent (Shape options
    high bit 0x80000000 cleared) -> verts are in the merge-group root's space.
    Covers FS22 v7 AND files produced by the Giants Blender exporter (which
    never writes the flag, regardless of file version) -- see GitHub #2 / #8.

    Runs AFTER axis correction, when every object's matrix_world is the correct
    Z-up world transform (transform_apply has finalized positions; see the
    bone-translation note in Phase E). For each tagged slot mesh, map verts from
    root space into the slot's local space via T = M_bone_inv @ M_root, so it
    renders at the assembled position. mesh.transform also rotates the custom
    split normals (verified empirically).
    """
    import bpy as _bpy
    count = 0
    for obj in list(import_collection.objects):
        root_name = obj.get('_i3d_rootspace_mg_root')
        if root_name is None:
            continue
        try:
            if obj.type == 'MESH':
                root_obj = _bpy.data.objects.get(root_name)
                if root_obj is not None:
                    T = obj.matrix_world.inverted() @ root_obj.matrix_world
                    obj.data.transform(T)
                    count += 1
        finally:
            del obj['_i3d_rootspace_mg_root']
    if count:
        report('INFO', f"Root-space merge-group local-space fix: {count} slot mesh(es)")


def _process_merge_groups(import_collection, shape_map, shape_id_to_obj, report):
    """For each MergeGroup-Shape (1 bone-index per vertex):
      - Split the merged geometry per source-TG via blend_indices[i][0]
      - Replace the source-TG Empty with a Mesh object carrying its sub-geometry
      - Set i3D_mergeGroup + i3D_mergeGroupRoot on root + members

    Without this pass, re-export through the Giants exporter crashes when it
    tries to read mesh data from a TransformGroup member (Empties have None
    vertex arrays — dccBlender.py:446).
    """
    import bpy as _bpy

    # Force matrix_local / matrix_world to reflect the location/rotation/scale
    # set by _apply_transform on every node. Without this, reading
    # source_obj.matrix_local on a freshly created+positioned Empty can still
    # return Identity, silently stripping its translation when we re-build
    # the object as a Mesh below.
    _bpy.context.view_layer.update()

    # nodeId -> object lookup for resolving skinBindNodeIds
    node_id_to_obj = {}
    for obj in import_collection.objects:
        nid = obj.get('_i3d_nodeId')
        if nid is not None:
            try:
                node_id_to_obj[int(nid)] = obj
            except (TypeError, ValueError):
                pass

    mg_counter = 0
    for shape_id, shape in shape_map.items():
        if not shape.is_merge_group:
            continue

        root_obj = shape_id_to_obj.get(shape_id)
        if root_obj is None:
            report('WARNING',
                   f"MergeGroup shape id={shape_id} ({shape.name!r}): no Blender object "
                   f"registered — skipped")
            continue

        raw = root_obj.get('_i3d_skinBindNodeIds_raw', '')
        try:
            bind_ids = [int(x) for x in str(raw).split() if x.strip()]
        except ValueError:
            report('WARNING',
                   f"{root_obj.name}: skinBindNodeIds {raw!r} not parsable — "
                   f"MergeGroup split skipped")
            continue
        if not bind_ids:
            report('WARNING',
                   f"{root_obj.name}: skinBindNodeIds empty — MergeGroup split skipped")
            continue

        # Sanity: the first bind id should equal the mesh's own nodeId
        own_nid = int(root_obj.get('_i3d_nodeId', -1))
        if bind_ids[0] != own_nid:
            report('WARNING',
                   f"{root_obj.name}: first skinBindNodeId {bind_ids[0]} != own "
                   f"nodeId {own_nid} — split anyway, results may be wrong")

        mg_counter += 1
        if mg_counter > 9:
            report('ERROR',
                   f"{root_obj.name}: more than 9 MergeGroups in this scene — "
                   f"Giants exporter limits to 9. Re-export will lose excess groups.")
            # Keep going so the user at least sees the geometry; the property won't
            # be valid for re-export but the rest of the import succeeds.

        mg_num = min(mg_counter, 9)

        # -------- Compute per-slot vertex / triangle index lists --------
        num_slots = len(bind_ids)
        per_slot_verts = [[] for _ in range(num_slots)]
        # vertex global -> local index per slot
        global_to_local = [{} for _ in range(num_slots)]
        for v_idx, bi_tuple in enumerate(shape.blend_indices):
            slot = bi_tuple[0]
            if slot >= num_slots:
                slot = 0
            local = len(per_slot_verts[slot])
            per_slot_verts[slot].append(v_idx)
            global_to_local[slot][v_idx] = local

        # Pre-compute the (global) per-triangle subset index from shape.subsets.
        # Subset[i] covers triangles [first_index/3, (first_index+num_indices)/3).
        global_face_subset = [0] * len(shape.triangles)
        for subset_idx, sub in enumerate(shape.subsets):
            first_tri = sub.first_index // 3
            last_tri = (sub.first_index + sub.num_indices) // 3
            for t in range(max(0, first_tri), min(len(shape.triangles), last_tri)):
                global_face_subset[t] = subset_idx

        per_slot_tris = [[] for _ in range(num_slots)]
        per_slot_face_subsets = [[] for _ in range(num_slots)]
        cross_tri_count = 0
        for tri_idx, tri in enumerate(shape.triangles):
            g1, g2, g3 = tri.p1 - 1, tri.p2 - 1, tri.p3 - 1
            s1 = shape.blend_indices[g1][0]
            s2 = shape.blend_indices[g2][0]
            s3 = shape.blend_indices[g3][0]
            if s1 == s2 == s3:
                local = (global_to_local[s1][g1],
                         global_to_local[s1][g2],
                         global_to_local[s1][g3])
                per_slot_tris[s1].append(local)
                per_slot_face_subsets[s1].append(global_face_subset[tri_idx])
            else:
                cross_tri_count += 1
                local = (global_to_local[s1][g1],
                         global_to_local[s1].setdefault(g2, len(per_slot_verts[s1])),
                         global_to_local[s1].setdefault(g3, len(per_slot_verts[s1])))
                per_slot_tris[s1].append(local)
                per_slot_face_subsets[s1].append(global_face_subset[tri_idx])

        if cross_tri_count:
            report('INFO',
                   f"{root_obj.name}: {cross_tri_count} cross-slot triangle(s) "
                   f"assigned by first-vertex heuristic")

        # Capture the root mesh's existing material list BEFORE we replace it.
        root_materials = list(root_obj.data.materials)

        def _build_sub_mesh(name, slot_vert_indices, slot_tri_locals, slot_face_subsets):
            """Build a Mesh datablock for one MergeGroup slot.

            Carries over per-vertex attributes the shape provides:
            - all up-to-4 UV channels (set per loop, same index as the vertex)
            - vertex colors (CORNER domain, like in _build_mesh_datablock)
            Materials: only those actually used by this slot's faces.
            """
            verts = [(shape.positions[i].x, shape.positions[i].y, shape.positions[i].z)
                     for i in slot_vert_indices]
            mdb = _bpy.data.meshes.new(name=name)
            mdb.from_pydata(verts, [], slot_tri_locals)
            mdb.update(calc_edges=True)

            # ---- Materials (only used subsets) ----
            used = sorted(set(slot_face_subsets))
            remap = {s: i for i, s in enumerate(used)}
            for subset_idx in used:
                if 0 <= subset_idx < len(root_materials):
                    mdb.materials.append(root_materials[subset_idx])
                else:
                    mdb.materials.append(None)
            for poly_idx, subset_idx in enumerate(slot_face_subsets):
                if poly_idx < len(mdb.polygons):
                    mdb.polygons[poly_idx].material_index = remap[subset_idx]

            # ---- UV channels (0..3) ----
            uv_layer_names = ("UVMap", "UV2", "UV3", "UV4")
            for uv_ch, uv_set in enumerate(shape.uv_sets):
                if uv_set is None:
                    continue
                uv_layer = mdb.uv_layers.new(name=uv_layer_names[uv_ch])
                for poly in mdb.polygons:
                    for loop_idx in poly.loop_indices:
                        loop = mdb.loops[loop_idx]
                        local_v = loop.vertex_index
                        global_v = slot_vert_indices[local_v]
                        uv = uv_set[global_v]
                        uv_layer.data[loop_idx].uv = (uv.u, uv.v)

            # ---- Vertex colors (CORNER domain, per loop, like _build_mesh_datablock) ----
            if shape.vertex_colors is not None:
                color_layer = mdb.color_attributes.new(
                    name="Color", type='FLOAT_COLOR', domain='CORNER'
                )
                for poly in mdb.polygons:
                    for loop_idx in poly.loop_indices:
                        loop = mdb.loops[loop_idx]
                        local_v = loop.vertex_index
                        global_v = slot_vert_indices[local_v]
                        c = shape.vertex_colors[global_v]
                        color_layer.data[loop_idx].color = (c.x, c.y, c.z, c.w)

            # Custom split normals (GitHub #1/#7), remapped to local verts.
            if shape.normals is not None:
                _apply_custom_split_normals(
                    mdb,
                    [(shape.normals[g].x, shape.normals[g].y, shape.normals[g].z)
                     for g in slot_vert_indices],
                )
            return mdb

        # -------- Slot 0: replace root_obj's mesh with the slot-0 sub-mesh --------
        old_mesh = root_obj.data
        new_mesh = _build_sub_mesh(
            old_mesh.name + "_mg0",
            per_slot_verts[0], per_slot_tris[0], per_slot_face_subsets[0],
        )
        root_obj.data = new_mesh
        if old_mesh.users == 0:
            _bpy.data.meshes.remove(old_mesh, do_unlink=True)

        root_obj['i3D_mergeGroup'] = mg_num
        root_obj['i3D_mergeGroupRoot'] = True

        # Render-relevant properties to inherit from root onto each sub-member.
        # The original XML had them only on the root <Shape>; the source TGs
        # were plain <TransformGroup> with no render attributes. After the
        # split the sub-members carry real geometry, so they should render
        # with the same shadow/clip-distance/etc. as the root.
        # We do NOT inherit:
        #   - i3D_mergeGroup* (set explicitly per member)
        #   - physics props (dynamic/compound/static/kinematic/collisionFilter*)
        #     because re-export should treat each member as a TG without its
        #     own physics body — the Giants exporter only reads them off the
        #     parent shape.
        _INHERIT_RENDER_PROPS = (
            'i3D_castsShadows', 'i3D_receiveShadows', 'i3D_nonRenderable',
            'i3D_clipDistance', 'i3D_objectMask', 'i3D_navMeshMask',
            'i3D_decalLayer', 'i3D_doubleSided', 'i3D_distanceBlending',
            'i3D_lodDistance', 'i3D_renderedInViewports', 'i3D_terrainDecal',
            'i3D_oc', 'i3D_cpuMesh',
            'i3D_lod1', 'i3D_lod2', 'i3D_lod3',
        )
        inherited = {}
        for prop_name in _INHERIT_RENDER_PROPS:
            if prop_name in root_obj.keys():
                inherited[prop_name] = root_obj[prop_name]

        # -------- Slots 1..N-1: replace source-TG Empties with Mesh objects --------
        for slot_idx in range(1, num_slots):
            source_nid = bind_ids[slot_idx]
            source_obj = node_id_to_obj.get(source_nid)
            if source_obj is None:
                report('WARNING',
                       f"{root_obj.name}: skinBindNode {source_nid} (slot {slot_idx}) "
                       f"not found in scene — slot skipped")
                continue

            slot_mesh = _build_sub_mesh(
                source_obj.name + f"_mg{slot_idx}",
                per_slot_verts[slot_idx], per_slot_tris[slot_idx],
                per_slot_face_subsets[slot_idx],
            )

            # Replace Empty -> Mesh object in place, preserving name / hierarchy.
            if source_obj.type == 'EMPTY':
                # Capture EVERYTHING in world-space terms before the empty
                # is destroyed. matrix_world is the only transform field that
                # is invariant under Blender's various reparent quirks.
                old_name = source_obj.name
                old_world = source_obj.matrix_world.copy()
                old_parent = source_obj.parent
                old_children = list(source_obj.children)
                old_child_worlds = {c.name: c.matrix_world.copy() for c in old_children}
                old_props = {k: source_obj[k] for k in source_obj.keys()}

                # Unlink from collections so the name is free for the new object
                for coll in list(source_obj.users_collection):
                    coll.objects.unlink(source_obj)
                source_obj.name = old_name + "__to_remove"
                _bpy.data.objects.remove(source_obj, do_unlink=True)

                mesh_obj = _bpy.data.objects.new(name=old_name, object_data=slot_mesh)
                for k, v in old_props.items():
                    if k == '_RNA_UI':
                        continue
                    try:
                        mesh_obj[k] = v
                    except Exception:
                        pass
                # Order: parent FIRST, then link to collection, THEN set
                # matrix_world. Setting matrix_world makes Blender compute
                # the correct matrix_local + matrix_parent_inverse so the
                # mesh ends up at exactly the old empty's world position,
                # regardless of any inverse-matrix quirks from the previous
                # parent-set.
                if old_parent is not None:
                    mesh_obj.parent = old_parent
                import_collection.objects.link(mesh_obj)
                mesh_obj.matrix_world = old_world

                # Re-parent former Empty children. Use the same world-space
                # approach: cache world, reparent, restore world. Blender
                # adjusts matrix_parent_inverse internally to satisfy the
                # requested world matrix.
                for child in old_children:
                    cw = old_child_worlds.get(child.name)
                    child.parent = mesh_obj
                    if cw is not None:
                        child.matrix_world = cw

                source_obj = mesh_obj
                # Update node_id_to_obj because the underlying obj reference changed
                source_nid_int = int(source_obj.get('_i3d_nodeId', source_nid))
                node_id_to_obj[source_nid_int] = source_obj
            else:
                # Already a mesh somehow — just swap its data block.
                source_obj.data = slot_mesh

            source_obj['i3D_mergeGroup'] = mg_num
            source_obj['i3D_mergeGroupRoot'] = False
            # Root-space merge groups (noBindPose flag absent on the shape) need
            # the post-axis local-space fix. Bone-local shapes (FS25-Giants) skip.
            # Covers FS22 v7 AND Giants-Blender-exporter v10 outputs (#2 / #8).
            if not getattr(shape, 'no_bind_pose', False):
                source_obj['_i3d_rootspace_mg_root'] = root_obj.name
            # Inherit render-relevant properties from root (only if not already
            # set on the member — e.g. _i3d_raw_* and userAttributes survive).
            for _ip_name, _ip_val in inherited.items():
                if _ip_name not in source_obj.keys():
                    try:
                        source_obj[_ip_name] = _ip_val
                    except Exception:
                        pass

        # Drop the informational raw attr now that we've consumed it.
        if '_i3d_skinBindNodeIds_raw' in root_obj.keys():
            del root_obj['_i3d_skinBindNodeIds_raw']

        report('INFO',
               f"{root_obj.name}: MergeGroup #{mg_num} split into {num_slots} slot(s) "
               f"({sum(len(s) for s in per_slot_verts)} verts, "
               f"{sum(len(s) for s in per_slot_tris)} tris)")


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
    """Bake the Y-up -> Z-up X+90 rotation into mesh vertices and curve points
    WITHOUT clobbering object-level rotations that came from the .i3d XML.

    Naive approach (selected wrapper + transform_apply(rotation=True)) bakes
    every Object's rotation_euler into its mesh data, including pre-existing
    XML rotations like <Shape rotation="0 -90 0">. The re-export then loses
    those XML rotations and the vertex data is silently permuted - vent flaps
    rotate around the wrong axis, AttacherJoints sit askew, etc.

    Strategy (Nadine's idea, verified non-destructive):
      1. Snapshot rotation_euler of every object in the import (top-level +
         recursive children) and zero them out. World rotation is now only
         the wrapper's X+90.
      2. Wrapper-Trick + transform_apply(rotation=True): with all object
         rotations at zero, transform_apply bakes ONLY the X+90 from the
         wrapper into the geometry.
      3. Restore each object's original rotation_euler from the snapshot.

    Result: mesh vertices in Z-up coordinates, object rotation_euler values
    preserved 1:1 from the source .i3d. Re-export through the Giants exporter
    writes the original XML rotation properties back unchanged, and undoes
    the X+90 vertex bake as part of its own Z-up -> Y-up conversion.
    """
    import mathutils

    # 0. Filter out top-level objects that opted out of axis correction
    #    (e.g. the TerrainTransformGroup plane built directly in Z-up).
    #    Their descendants are also excluded — they don't get re-parented
    #    to the wrapper, don't get their rotation zeroed, and don't get
    #    transform_apply called on them. They simply pass through.
    SKIP_KEY = '_i3d_skip_axis_correction'
    filtered_top = [o for o in top_level_objs if not o.get(SKIP_KEY)]
    skipped_count = len(top_level_objs) - len(filtered_top)
    top_level_objs = filtered_top

    # 1. Snapshot + zero out all object rotations in the import.
    all_objs = list(top_level_objs)
    for obj in list(top_level_objs):
        all_objs.extend(obj.children_recursive)

    saved_rotations = {}
    for obj in all_objs:
        # Always snapshot rotation_euler regardless of rotation_mode - the
        # Giants exporter / our importer write to rotation_euler by default
        # and Blender keeps rotation_euler in sync with rotation_quaternion
        # for round-tripping. Edge case: if a user changed rotation_mode
        # between import and re-import this would lose data, but the import
        # never sets a non-EULER mode.
        saved_rotations[obj.name] = obj.rotation_euler.copy()
        obj.rotation_euler = (0.0, 0.0, 0.0)

    bpy.context.view_layer.update()

    # 1b. Single-user-fy multi-user meshes so transform_apply doesn't bail
    #     ("Cannot apply to a multi user"). Snapshot the sharing topology
    #     so we can re-share the canonical Mesh datablock after the apply.
    #     Safe because every object's rotation_euler is now 0 (step 1) and
    #     the wrapper applies the SAME X+90 to every selected child, so the
    #     post-apply vertex data on each former follower is bit-identical
    #     to the canonical's — re-sharing loses no information.
    sharing_groups = {}  # id(mesh-datablock) -> list of objs sharing it
    for obj in all_objs:
        if obj.type == 'MESH' and obj.data is not None:
            sharing_groups.setdefault(id(obj.data), []).append(obj)

    restore_sharing = []  # [(canonical_obj, [follower_objs, ...]), ...]
    for objs in sharing_groups.values():
        if len(objs) <= 1:
            continue
        canonical_obj = objs[0]
        for follower in objs[1:]:
            follower.data = follower.data.copy()
        restore_sharing.append((canonical_obj, objs[1:]))

    bpy.context.view_layer.update()

    # 2. Wrapper + transform_apply, same as before but now only the X+90
    #    from the wrapper exists in world space, so that is all that gets
    #    baked into the mesh data.
    wrapper = bpy.data.objects.new("__fs25_axis_correction_temp__", None)
    bpy.context.scene.collection.objects.link(wrapper)
    wrapper.rotation_euler = (math.radians(90), 0, 0)

    for obj in top_level_objs:
        obj.parent = wrapper
        obj.matrix_parent_inverse = mathutils.Matrix.Identity(4)

    bpy.context.view_layer.update()

    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.view_layer.objects.active = wrapper
    wrapper.select_set(True)
    for child in wrapper.children_recursive:
        child.select_set(True)

    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)

    # Unparent children - because transform_apply was called, the wrapper
    # rotation is already baked into the children's geometry; their
    # rotation_euler at this point is (0,0,0) on all of them.
    direct_children = list(wrapper.children)
    for child in direct_children:
        world = child.matrix_world.copy()
        child.parent = None
        child.matrix_world = world

    bpy.data.objects.remove(wrapper, do_unlink=True)

    # 2b. Re-share Mesh datablocks: every follower goes back to the
    #     canonical mesh. The per-follower temporary copies become orphans
    #     (users == 0) and get removed.
    for canonical_obj, followers in restore_sharing:
        canonical_data = canonical_obj.data
        for follower in followers:
            old = follower.data
            follower.data = canonical_data
            if old is not None and old.users == 0:
                bpy.data.meshes.remove(old, do_unlink=True)

    # 3. Restore original rotation_euler on every snapshotted object.
    #    Order doesn't matter because Blender resolves parent chains lazily.
    for obj in all_objs:
        rot = saved_rotations.get(obj.name)
        if rot is not None:
            obj.rotation_euler = rot

    bpy.context.view_layer.update()

    report('INFO',
           f"Axis correction Y-up -> Z-up applied to {len(top_level_objs)} "
           f"top-level object(s); restored XML rotations on "
           f"{len(saved_rotations)} object(s)"
           + (f"; skipped {skipped_count} marked object(s)."
              if skipped_count else "."))


def _post_import_view_setup(import_collection, report):
    """After import: switch all 3D Viewports to Material Preview shading,
    frame the imported objects (Numpad-. equivalent), and bump the viewport
    clip-end to 10000 when any imported object exceeds 500 units in either
    world-location or dimensions.

    No-op when no View3D area exists (e.g. headless / background invocation).

    Selection side effect: temporarily selects all imported objects to drive
    view_selected. The caller is expected to deselect afterwards if needed
    (import_i3d does so in its 'clear selection' block).
    """
    # 1. Decide whether to bump clip_end. Threshold: 500 units on any of:
    #    - obj.location component (Euclidean axis-aligned)
    #    - obj.dimensions component (axis-aligned bounding box extent)
    threshold = 500.0
    need_clip_bump = False
    for obj in import_collection.all_objects:
        try:
            loc = obj.matrix_world.translation
            if (abs(loc.x) > threshold
                    or abs(loc.y) > threshold
                    or abs(loc.z) > threshold):
                need_clip_bump = True
                break
            if obj.type in ('MESH', 'CURVE', 'SURFACE'):
                d = obj.dimensions
                if d.x > threshold or d.y > threshold or d.z > threshold:
                    need_clip_bump = True
                    break
        except (ReferenceError, AttributeError):
            # Stale wrapper (deleted by merge-groups pass etc.) - skip.
            continue

    # 2. Select all imported objects (visible ones). select_set on hidden
    #    objects raises RuntimeError; that is fine, we swallow it.
    try:
        bpy.ops.object.select_all(action='DESELECT')
    except RuntimeError:
        pass
    any_selected = False
    for obj in import_collection.all_objects:
        try:
            obj.select_set(True)
            any_selected = True
        except (ReferenceError, RuntimeError):
            continue

    # 3. Per View3D area: shading + clip_end. Then per area frame the
    #    selection via view_selected (one region override per area).
    framed = 0
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for space in area.spaces:
                if space.type != 'VIEW_3D':
                    continue
                try:
                    space.shading.type = 'MATERIAL'
                except (AttributeError, TypeError):
                    pass
                if need_clip_bump:
                    try:
                        if space.clip_end < 10000.0:
                            space.clip_end = 10000.0
                    except (AttributeError, TypeError):
                        pass
            if any_selected:
                for region in area.regions:
                    if region.type != 'WINDOW':
                        continue
                    try:
                        with bpy.context.temp_override(area=area, region=region):
                            bpy.ops.view3d.view_selected()
                        framed += 1
                    except Exception as e:
                        report('INFO',
                               f"view_selected skipped for one viewport: {e}")
                    break

    if need_clip_bump:
        report('INFO',
               "Large imported object detected (>500 units) - bumped "
               "3D Viewport clip-end to 10000 in all View3D areas.")
    if framed:
        report('INFO', f"Framed imported objects in {framed} viewport(s).")


def _force_empty_visibility_on_all_views(report):
    """Force show + select for empties in every SpaceView3D so import
    post-processing (axis correction's wrapper-empty trick, view_selected
    etc.) works even when the user has empties hidden via the viewport's
    'Selectability & Visibility' filter.

    Returns a snapshot list of (space_ref, prev_viewport, prev_select)
    tuples for later restore via _restore_empty_visibility. Caller is
    expected to call restore in a finally block.
    """
    saved = []
    n_forced = 0
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for space in area.spaces:
                if space.type != 'VIEW_3D':
                    continue
                try:
                    prev_viewport = space.show_object_viewport_empty
                    prev_select = space.show_object_select_empty
                except AttributeError:
                    # Very old Blender without these flags - skip silently.
                    continue
                saved.append((space, prev_viewport, prev_select))
                if not prev_viewport or not prev_select:
                    n_forced += 1
                try:
                    space.show_object_viewport_empty = True
                    space.show_object_select_empty = True
                except (AttributeError, TypeError):
                    pass
    if n_forced:
        report('INFO',
               f"Forced empty visibility on for {n_forced} viewport(s) "
               f"during import (will be restored at end).")
    return saved


def _restore_empty_visibility(saved):
    """Restore the per-viewport flags previously snapshotted by
    _force_empty_visibility_on_all_views. Stale spaces (closed by user
    during the import) are silently ignored."""
    for space, prev_viewport, prev_select in saved:
        try:
            space.show_object_viewport_empty = prev_viewport
            space.show_object_select_empty = prev_select
        except (ReferenceError, AttributeError, TypeError):
            pass
