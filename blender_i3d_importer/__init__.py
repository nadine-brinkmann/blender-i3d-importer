"""
FS25 i3d Importer for Blender

Imports meshes from FS22/FS25 *.i3d.shapes files into Blender, including materials, all UVs, vertex colors, custom properties etc.
Optionally creates additional materials which give the same look and feel as the materials in the Giants Editor. These additional 
materials cannot be re-exported, but the standard imported materials can (therefore 2 separate sets of materials are created
when you use this option)
Decodes the *.i3d.shapes binary directly in Python — no external tool needed.

"""

bl_info = {
    "name": "i3d Importer",
    "author": "Nadine Brinkmann",
    "version": (0, 5, 0),
    "blender": (5, 1, 0),
    "location": "File > Import > Farming Simulator i3d (.i3d)",
    "description": (
        "Imports Farming Simulator 22/25 .i3d files into Blender. "
        "Full scene hierarchy (meshes, splines, lights, cameras, "
        "references, notes, terrain), two material flavors "
        "(re-export-clean and PBR-debug), N-panel workflow tools, "
        "round-trip with the Giants i3d Exporter. Native Python "
        ".i3d.shapes decoder (v7/v9/v10) - no external tool needed."
    ),
    "category": "Import-Export",
}

import bpy
import os
import json
import re
import shutil
from pathlib import Path
from bpy.props import (
    BoolProperty, StringProperty, EnumProperty, FloatVectorProperty, IntProperty,
)
from bpy.types import AddonPreferences, Operator
from bpy_extras.io_utils import ImportHelper

# Module reload support (handy during development).
# Order matters: reload submodules first, then the importer, so the importer
# picks up the fresh submodules on reload.
if "importer" in locals():
    import importlib
    importlib.reload(i3d_attr_mapping)
    importlib.reload(i3d_shader_parser)
    importlib.reload(i3d_xml_parser)
    importlib.reload(i3d_material_templates)
    importlib.reload(i3d_config_parser)
    importlib.reload(i3d_config_preview)
    importlib.reload(i3d_reference_loader)
    importlib.reload(i3d_wheel_resolver)
    importlib.reload(i3d_wheel_loader)
    importlib.reload(i3d_crawler_path)
    importlib.reload(i3d_crawler_loader)
    importlib.reload(i3d_shapes_reader)
    importlib.reload(i3d_shapes_models)
    importlib.reload(i3d_shapes_to_meshdata)
    importlib.reload(material_inventory)
    importlib.reload(recipe_loader)
    importlib.reload(importer)
else:
    from . import i3d_attr_mapping
    from . import i3d_shader_parser
    from . import i3d_xml_parser
    from . import i3d_material_templates
    from . import i3d_config_parser
    from . import i3d_config_preview
    from . import i3d_reference_loader
    from . import i3d_wheel_resolver
    from . import i3d_wheel_loader
    from . import i3d_crawler_path
    from . import i3d_crawler_loader
    from . import i3d_shapes_reader
    from . import i3d_shapes_models
    from . import i3d_shapes_to_meshdata
    from . import material_inventory
    from . import recipe_loader
    from . import importer


# Defaults — passed to importer.import_i3d() by the operator.
DEFAULT_FS25_DATA_BASE = ""
DEFAULT_EXPORT_DIR     = ""

# Master blend with node-group snippets for PBR debug materials.
DEFAULT_SNIPPETS_BLEND_PATH = os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    "lib",
    "fs25_node_snippets.blend",
)

# Default terrain base color: hex #343A1D (sRGB) converted to linear RGB.
# Blender stores Color properties as linear; the Color Picker displays
# sRGB. With this default the Color Picker shows back #343A1D.
DEFAULT_TERRAIN_BASE_COLOR = (0.03434, 0.042311, 0.012286, 1.0)

# Default PoC <CombinedLayer> names loaded from the map terrain.
# 5 layers + detail/weight channels only fit Eevee's 32-sampler limit.
DEFAULT_TERRAIN_POC_LAYER_NAMES = "ASPHALT,GRASS,MUD,FOREST_LEAVES,FOREST_GRASS"

# Hard cap: more than 5 layers exceed Eevee's per-material sampler limit.
MAX_TERRAIN_POC_LAYERS = 5


class FS25_OT_terrain_base_color_reset(Operator):
    """Reset the 'Terrain base color' preference back to the default
    (linear conversion of sRGB #343A1D)."""
    bl_idname = "fs25.terrain_base_color_reset"
    bl_label = "Reset terrain base color"
    bl_description = ("Reset the 'Terrain base color' preference back to "
                      "the default (linear conversion of sRGB #343A1D)")
    bl_options = {'INTERNAL'}

    def execute(self, context):
        prefs = context.preferences.addons[__package__].preferences
        prefs.terrain_base_color = DEFAULT_TERRAIN_BASE_COLOR
        return {'FINISHED'}


class FS25I3DImporterPreferences(AddonPreferences):
    """Paths to external tools/folders + global defaults.

    Shown in Edit > Preferences > Add-ons > FS25 i3d Importer.
    Replace the module constants previously hardcoded in importer.py.
    """
    bl_idname = __package__

    fs25_data_base: StringProperty(
        name="FS25 game data folder (required)",
        description="Folder containing 'data/' — used for $data/-path resolution "
                    "(textures, shader XMLs). Also written automatically into "
                    "the Game Location Setting of the Giants i3d Exporter "
                    "on every import for convenience.",
        subtype='DIR_PATH',
        default=DEFAULT_FS25_DATA_BASE,
    )
    export_dir: StringProperty(
        name="Re-export output folder (optional)",
        description="Target folder for the Giants i3d exporter on re-export. "
                    "Written automatically into the Output File Location Setting "
                    "of the Giants i3d Exporter on every import (convenience).",
        subtype='DIR_PATH',
        default=DEFAULT_EXPORT_DIR,
    )
    apply_axis_correction_default: BoolProperty(
        name="Apply axis correction by default",
        description="Default for the operator checkbox 'Apply axis correction "
                    "(Y-up -> Z-up)'. Can still be overridden per import.",
        default=True,
    )
    auto_hide_invisible_shapes_default: BoolProperty(
        name="Auto-hide invisible shapes by default",
        description="Default for the operator checkbox 'Auto-hide invisible "
                    "shapes'. On import, hides shapes with visibility=false or "
                    "nonRenderable=true (except terrainDecal=true) via "
                    "hide_set(True). Can still be overridden per import.",
        default=True,
    )
    build_pbr_debug_materials_default: BoolProperty(
        name="Build PBR debug materials by default",
        description="Default for the operator checkbox 'Build PBR debug "
                    "materials'. If active, an additional <name>_pbr_debug "
                    "material is created for each i3d material. This material "
                    "emulates the look and feel of the material in the "
                    "Giants Editor. Can still be overridden per import.",
        default=True,
    )
    attach_debug_materials_to_mesh_default: BoolProperty(
        name="Attach debug materials to mesh by default",
        description="Default for linking the debug material to the meshes "
                    "INSTEAD of the re-export material. The re-export "
                    "material still stays in the blender file and can be "
                    "swapped later manually. Note: re-export only works "
                    "correctly with the re-export material — swap back manually "
                    "before re-export when this flag is on.",
        default=False,
    )
    auto_load_config_xml: BoolProperty(
        name="Load XML with the same name automatically if present",
        description="After importing an .i3d, if a config XML with the same name "
                    "exists in the same folder (e.g. vario1000.xml next to "
                    "vario1000.i3d), load it automatically: assign its i3dMappings "
                    "and load the store-config preview. Saves picking the file by "
                    "hand.",
        default=True,
    )
    add_sort_order_prefix_default: BoolProperty(
        name="Add GE sort-order prefix by default",
        description="Default for the operator checkbox 'Add GE sort-order "
                    "prefix'. Prepends a 4-digit sort key (0010, 0020, ...) to "
                    "every imported node name so the Giants exporter reproduces "
                    "the original Giants Editor scenegraph order on re-export. "
                    "The exporter strips the prefix, so it never reaches the "
                    ".i3d. Can still be overridden per import.",
        default=True,
    )
    terrain_lod_default: EnumProperty(
        name="Terrain LOD by default",
        description="Vertex density for the terrain mesh on import. Lower "
                    "LODs are faster and lighter. The terrain is one-way "
                    "(no re-export); for map-edge editing in Blender (e.g. "
                    "snapping a backgroundMesh to the terrain border).",
        items=[
            ('OFF',     "Off",     "Don't import TerrainTransformGroup"),
            ('QUARTER', "Quarter", "~256K verts for 2k map (513x513)"),
            ('HALF',    "Half",    "~1M verts for 2k map (1025x1025)"),
            ('FULL',    "Full",    "1 vertex per heightmap pixel (~4M verts for 2k map)"),
        ],
        default='HALF',
    )
    terrain_base_color: FloatVectorProperty(
        name="Terrain base color (uncovered)",
        description="Base color shown on the terrain where no PoC "
                    "<CombinedLayer> has any weight. Default is a "
                    "muted dark green (#343A1D as sRGB). Color Picker "
                    "shows the sRGB hex; stored internally as linear RGB.",
        subtype='COLOR',
        size=4,
        min=0.0, max=1.0,
        default=DEFAULT_TERRAIN_BASE_COLOR,
    )
    terrain_poc_layer_names: StringProperty(
        name="Terrain PoC layer names",
        description="Comma-separated list of <CombinedLayer> names from "
                    "the map's terrain to load (case-sensitive). Up to "
                    "5 entries; the maximum is fixed by Eevee's 32-sampler "
                    "per-material limit. Invalid names are replaced with "
                    "defaults; extra entries beyond 5 are dropped with a "
                    "warning.",
        default=DEFAULT_TERRAIN_POC_LAYER_NAMES,
    )

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Paths", icon='FILE_FOLDER')
        box.prop(self, "fs25_data_base")
        box.prop(self, "export_dir")

        box = layout.box()
        box.label(text="Import Defaults", icon='IMPORT')
        box.prop(self, "apply_axis_correction_default")
        box.prop(self, "auto_hide_invisible_shapes_default")
        box.prop(self, "build_pbr_debug_materials_default")
        box.prop(self, "attach_debug_materials_to_mesh_default")
        box.prop(self, "add_sort_order_prefix_default")
        box.prop(self, "auto_load_config_xml")

        box = layout.box()
        box.label(text="Terrain", icon='WORLD')
        box.prop(self, "terrain_lod_default")
        row = box.row(align=True)
        row.prop(self, "terrain_base_color")
        row.operator("fs25.terrain_base_color_reset", text="",
                     icon='LOOP_BACK')
        box.prop(self, "terrain_poc_layer_names")


class IMPORT_OT_fs25_i3d(Operator, ImportHelper):
    """Import a Farming Simulator 25 i3d file"""
    bl_idname = "import_scene.fs25_i3d"
    bl_label = "Import FS25 i3d"
    bl_options = {'UNDO'}

    filename_ext = ".i3d"
    filter_glob: StringProperty(default="*.i3d", options={'HIDDEN'})

    apply_axis_correction: BoolProperty(
        name="Apply axis correction (Y-up -> Z-up)",
        description=(
            "Bake X+90 deg rotation into all imported geometry (mesh vertices, "
            "curve points, object transforms). The Giants i3d exporter applies "
            "the inverse conversion optionally on re-export — turn this off "
            "if the exporter's axis-conversion option is disabled. Default "
            "comes from add-on preferences."
        ),
        default=True,  # overridden in invoke() from prefs
    )

    auto_hide_invisible_shapes: BoolProperty(
        name="Auto-hide invisible shapes",
        description=(
            "Hides shapes with visibility='false' on import or "
            "nonRenderable='true' without terrainDecal='true' (convenience). "
            "Uses hide_set(True), like the H shortcut. Default comes from "
            "add-on preferences."
        ),
        default=True,  # overridden in invoke() from prefs
    )

    build_pbr_debug_materials: BoolProperty(
        name="Build PBR debug materials",
        description=(
            "Creates an additional <name>_pbr_debug material per i3d material. "
            "This material emulates the look and feel of the material "
            "in the Giants Editor. Default comes from add-on "
            "preferences."
        ),
        default=True,  # overridden in invoke() from prefs
    )

    attach_debug_materials_to_mesh: BoolProperty(
        name="Attach debug materials to mesh",
        description=(
            "Link the debug material to the meshes "
            "INSTEAD of the re-export material. The re-export "
            "material still stays in the blender file and can be "
            "swapped later manually. Note: Re-export fidelity is not "
            "directly given while this flag is on — swap back manually."
        ),
        default=False,  # overridden in invoke() from prefs
    )

    add_sort_order_prefix: BoolProperty(
        name="Add GE sort-order prefix",
        description=(
            "Prepend a 4-digit sort key (0010, 0020, ...) to every imported "
            "node name so the Giants exporter reproduces the original Giants "
            "Editor scenegraph order on re-export. The exporter strips the "
            "prefix, so it never reaches the .i3d. Default comes from add-on "
            "preferences."
        ),
        default=True,  # overridden in invoke() from prefs
    )

    terrain_lod: EnumProperty(
        name="Terrain LOD",
        description=(
            "Vertex density for the terrain mesh built from "
            "TerrainTransformGroup's heightmap. Lower LODs are faster and "
            "lighter. The terrain is imported one-way only (the Giants "
            "Blender Exporter cannot re-emit a TerrainTransformGroup), "
            "intended for map-edge editing (snap a backgroundMesh to the "
            "terrain border). Default comes from add-on preferences."
        ),
        items=[
            ('OFF',     "Off",     "Don't import TerrainTransformGroup"),
            ('QUARTER', "Quarter", "~256K verts for 2k map (513x513)"),
            ('HALF',    "Half",    "~1M verts for 2k map (1025x1025)"),
            ('FULL',    "Full",    "1 vertex per heightmap pixel (~4M verts for 2k map)"),
        ],
        default='HALF',  # overridden in invoke() from prefs
    )

    terrain_base_color: FloatVectorProperty(
        name="Terrain base color (uncovered)",
        description=(
            "Base color shown on the terrain where no PoC "
            "<CombinedLayer> has any weight. Default comes from add-on "
            "preferences (linear RGB; Color Picker displays sRGB hex)."
        ),
        subtype='COLOR',
        size=4,
        min=0.0, max=1.0,
        default=DEFAULT_TERRAIN_BASE_COLOR,
    )

    terrain_poc_layer_names: StringProperty(
        name="Terrain PoC layer names",
        description=(
            "Comma-separated list of <CombinedLayer> names from the "
            "map's terrain to load (case-sensitive). Up to 5 entries. "
            "Invalid names are replaced with defaults; extra entries "
            "beyond 5 are dropped with a warning. Default comes from "
            "add-on preferences."
        ),
        default=DEFAULT_TERRAIN_POC_LAYER_NAMES,
    )

    def invoke(self, context, event):
        prefs = context.preferences.addons[__package__].preferences
        self.apply_axis_correction = prefs.apply_axis_correction_default
        self.auto_hide_invisible_shapes = prefs.auto_hide_invisible_shapes_default
        self.build_pbr_debug_materials = prefs.build_pbr_debug_materials_default
        self.attach_debug_materials_to_mesh = prefs.attach_debug_materials_to_mesh_default
        self.add_sort_order_prefix = prefs.add_sort_order_prefix_default
        self.terrain_lod = prefs.terrain_lod_default
        self.terrain_base_color = prefs.terrain_base_color
        self.terrain_poc_layer_names = prefs.terrain_poc_layer_names
        return super().invoke(context, event)

    def execute(self, context):
        prefs = context.preferences.addons[__package__].preferences

        # Mandatory: FS25 game data folder must be set + exist.
        # Without it $data/-path resolution fails and the Giants exporter
        # is misconfigured (gameLocation = "\").
        data_base = prefs.fs25_data_base
        if not data_base or not os.path.isdir(data_base):
            def _draw_popup(self_, ctx_):
                if not data_base:
                    self_.layout.label(text="FS25 game data folder is not set.")
                else:
                    self_.layout.label(text="FS25 game data folder does not exist:")
                    self_.layout.label(text=f"  {data_base}")
                self_.layout.separator()
                self_.layout.label(text="Please set it in the add-on preferences.")
            context.window_manager.popup_menu(
                _draw_popup, title="Configuration required", icon='ERROR')
            # Open preferences focused on this add-on (best-effort).
            try:
                bpy.ops.screen.userpref_show('INVOKE_DEFAULT')
                context.preferences.active_section = 'ADDONS'
                bpy.ops.preferences.addon_show(module=__package__)
            except Exception:
                pass
            self.report({'ERROR'},
                        "FS25 game data folder not configured - import cancelled")
            return {'CANCELLED'}

        try:
            _before = set(bpy.data.objects)
            # Seed the scene's sticky material mode from THIS import's dialog
            # options (which invoke() pre-fills from the preferences, but the
            # user can change them per import). Set BEFORE the import so anything
            # the import itself pulls in already follows this choice. Parts loaded
            # later (wheels, referenced i3ds) read it, and every scene-wide switch
            # in the N-Panel overwrites it.
            i3d_reference_loader.set_material_mode(
                context.scene,
                'debug' if (self.build_pbr_debug_materials
                            and self.attach_debug_materials_to_mesh)
                else 'export')
            count, warnings = importer.import_i3d(
                self.filepath, report=self.report,
                apply_axis_correction=self.apply_axis_correction,
                auto_hide_invisible_shapes=self.auto_hide_invisible_shapes,
                build_pbr_debug_materials=self.build_pbr_debug_materials,
                attach_debug_materials_to_mesh=self.attach_debug_materials_to_mesh,
                add_sort_order_prefix=self.add_sort_order_prefix,
                terrain_lod=self.terrain_lod,
                terrain_base_color=tuple(self.terrain_base_color),
                terrain_poc_layer_names=self.terrain_poc_layer_names,
                fs25_data_base=prefs.fs25_data_base,
                export_dir=prefs.export_dir,
                snippets_blend_path=DEFAULT_SNIPPETS_BLEND_PATH,
            )
            if warnings > 0:
                self.report({'INFO'}, f"FS25 i3d Import: {count} object(s) imported ({warnings} warning(s) - see log)")
            else:
                self.report({'INFO'}, f"FS25 i3d Import: {count} object(s) imported")
            # Optional: auto-load a same-named config XML next to the .i3d.
            if prefs.auto_load_config_xml:
                _xml = os.path.splitext(self.filepath)[0] + ".xml"
                if os.path.isfile(_xml):
                    _new_id = next(
                        (o.get('_i3d_import_id') for o in bpy.data.objects
                         if o not in _before and o.get('_i3d_import_id')), None)
                    if _new_id:
                        try:
                            _apply_config_xml(context, _new_id, _xml, self.report)
                        except Exception as _e:
                            self.report({'INFO'},
                                        "Auto config XML load skipped: %r" % _e)
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Import failed: {e}")
            return {'CANCELLED'}


class FS25_OT_switch_materials(Operator):
    """Switch materials on selected objects between re-export and debug
    variants. Matching uses the _i3d_material_id + _i3d_material_kind custom
    properties (robust against renaming)."""
    bl_idname = "fs25.switch_materials"
    bl_label = "Switch i3d Materials"
    bl_options = {'UNDO'}

    target_kind: bpy.props.EnumProperty(
        name="Target Kind",
        items=[
            ('debug',  "Debug",   "Switch to PBR debug materials"),
            ('export', "Export",  "Switch to re-export-true materials"),
            ('toggle', "Toggle",  "Toggle between debug and export"),
        ],
        default='toggle',
    )

    scope: bpy.props.EnumProperty(
        name="Scope",
        items=[
            ('selection',    "Selection",    "Only selected mesh objects"),
            ('all_imported', "All imported", "All mesh objects carrying i3d "
                                             "materials, regardless of selection "
                                             "or visibility"),
        ],
        default='selection',
    )

    @classmethod
    def poll(cls, context):
        # Active whenever the scene contains any i3d mesh material. The
        # 'Export (all)' button works scene-wide (scope='all_imported') and
        # must not depend on selection; the Debug/Toggle buttons keep their
        # selection requirement via layout.enabled in the N-Panel draw().
        for obj in context.scene.objects:
            if obj.type != 'MESH' or not obj.data:
                continue
            for slot in obj.material_slots:
                m = slot.material
                if m and m.get('_i3d_material_kind') in ('debug', 'export'):
                    return True
        return False

    def execute(self, context):
        # Lookup: (material_id, import_uuid, kind) -> material.
        # import_uuid disambiguates material pairs across multiple imports
        # that share material_id 0/1/2/... (per-import IDs).
        # Materials imported before the UUID-based pairing was introduced
        # have import_uuid=None; those still pair with each other inside
        # the same scene best-effort (but cannot disambiguate across two
        # pre-fix imports - workaround: re-import).
        lookup = {}
        for m in bpy.data.materials:
            mid = m.get('_i3d_material_id')
            kind = m.get('_i3d_material_kind')
            imp_uuid = m.get('_i3d_import_uuid')
            if mid is not None and kind in ('debug', 'export'):
                lookup[(int(mid), imp_uuid, kind)] = m

        # Object source depends on scope. 'all_imported' covers every mesh
        # carrying an i3d material, regardless of selection or visibility -
        # this is what the N-Panel 'Export (all)' button uses so no debug
        # material can ever leak into a re-export via hidden helper objects
        # (collision parts, shadowFocusBox, fill-root, component roots).
        # slot.material assignment works on hidden objects without unhiding.
        if self.scope == 'all_imported':
            objects = [o for o in bpy.data.objects if o.type == 'MESH' and o.data]
        else:
            objects = [o for o in context.selected_objects
                       if o.type == 'MESH' and o.data]

        swapped = 0
        skipped = 0
        for obj in objects:
            for slot in obj.material_slots:
                cur = slot.material
                if cur is None:
                    continue
                mid = cur.get('_i3d_material_id')
                cur_kind = cur.get('_i3d_material_kind')
                cur_uuid = cur.get('_i3d_import_uuid')
                if mid is None or cur_kind not in ('debug', 'export'):
                    # Non-i3d material: skip silently (no 'skipped' noise,
                    # relevant in all_imported scope across the whole scene).
                    continue
                # Determine target kind
                if self.target_kind == 'toggle':
                    want = 'debug' if cur_kind == 'export' else 'export'
                else:
                    want = self.target_kind
                if want == cur_kind:
                    # Already in target state
                    continue
                pair = lookup.get((int(mid), cur_uuid, want))
                if pair is None:
                    self.report({'WARNING'},
                                f"No {want} counterpart for material '{cur.name}' "
                                f"(id={mid}, import_uuid={cur_uuid}) found")
                    skipped += 1
                    continue
                slot.material = pair
                swapped += 1

        # A scene-wide switch is the user's choice for the WHOLE scene, so it
        # becomes the mode that later imports (wheels, referenced i3ds) follow.
        # A selection-limited switch says nothing about the scene and is not
        # recorded. 'toggle' has no single resulting kind - the N-Panel buttons
        # always pass an explicit kind.
        if self.scope == 'all_imported' and self.target_kind in ('debug', 'export'):
            i3d_reference_loader.set_material_mode(context.scene, self.target_kind)

        self.report({'INFO'}, f"Material switch: {swapped} swapped, {skipped} skipped")
        return {'FINISHED'}


def _serialize_param_group(slots):
    """Combine a sync param group into a single customParameter_*
    string value. slots is a dict slot_name -> (node, mode).

    Recognized slot names (canonical order):
      'all'    - whole value in a single slider (default for unsplit)
      'rgb'    - 3 float components (R, G, B of an RGB(A) param)
      'w'      - 1 float (the 4th component, blend / alpha)
      'alpha'  - alias for 'w'
      'x','y','z' - vector components, in that order

    The combined value is written as space-separated floats.
    Returns None if the slot combination is not recognized.
    """
    # Single 'all' slot: standard non-split case.
    if 'all' in slots and len(slots) == 1:
        node, mode = slots['all']
        value = node.outputs[0].default_value
        if mode == 'float':
            return f"{float(value):.6f}"
        if mode == 'inverted_float':
            return f"{1.0 - float(value):.6f}"
        if mode == 'rgba':
            return ' '.join(f"{float(c):.6f}" for c in value)
        return None

    # Split case: combine slots in canonical order.
    components = []
    if 'rgb' in slots:
        node, _mode = slots['rgb']
        v = node.outputs[0].default_value
        components.extend([float(v[0]), float(v[1]), float(v[2])])
    # 'w' and 'alpha' are aliases - one of them at most expected.
    for sname in ('w', 'alpha'):
        if sname in slots:
            node, _mode = slots[sname]
            components.append(float(node.outputs[0].default_value))
            break
    for sname in ('x', 'y', 'z'):
        if sname in slots:
            node, _mode = slots[sname]
            components.append(float(node.outputs[0].default_value))
    if not components:
        return None
    return ' '.join(f"{v:.6f}" for v in components)


def _sync_debug_material(mat):
    """Push one debug material's fs25_param:* node values to its paired export
    material's customParameter_* IDProperties. Returns (export_name, n_synced,
    n_skipped) or None when there is no usable debug material / export pair."""
    if mat is None or mat.get('_i3d_material_kind') != 'debug':
        return None
    mid = mat.get('_i3d_material_id')
    uuid = mat.get('_i3d_import_uuid')
    if mid is None or not mat.use_nodes or mat.node_tree is None:
        return None
    pair = None
    for m in bpy.data.materials:
        if (m.get('_i3d_material_id') == mid
                and m.get('_i3d_import_uuid') == uuid
                and m.get('_i3d_material_kind') == 'export'):
            pair = m
            break
    if pair is None:
        return None
    prefix = 'fs25_param:'
    groups = {}
    for node in mat.node_tree.nodes:
        if not node.name.startswith(prefix):
            continue
        slider_name = node.name[len(prefix):]
        xml_param = node.get('fs25_xml_param')
        if xml_param is None:
            xml_param = slider_name
        xml_slot = node.get('fs25_xml_slot') or 'all'
        mode = node.get('fs25_serialize')
        if mode is None:
            if node.type == 'RGB':
                mode = 'rgba'
            elif node.type == 'VALUE':
                mode = 'float'
            else:
                continue
        groups.setdefault(xml_param, {})[xml_slot] = (node, mode)
    n_synced = n_skipped = 0
    for xml_param, slots in groups.items():
        try:
            serialized = _serialize_param_group(slots)
        except Exception:
            n_skipped += 1
            continue
        if serialized is None:
            n_skipped += 1
            continue
        pair['customParameter_' + xml_param] = serialized
        n_synced += 1
    # Detail textures swapped by a material config (e.g. Design Line chrome): the
    # fs25_tex:<role> node carries the canonical $data path -> write the export
    # material's customTexture_<role>. Unswapped nodes have no path, so the export
    # keeps its original texture.
    for node in mat.node_tree.nodes:
        if not node.name.startswith('fs25_tex:'):
            continue
        dp = node.get('fs25_data_path')
        if dp:
            role = node.name[len('fs25_tex:'):]
            pair['customTexture_' + role] = dp
            n_synced += 1
    return (pair.name, n_synced, n_skipped)


class FS25_OT_sync_debug_to_export_material(bpy.types.Operator):
    """Sync fs25_param:* slider values from debug material(s) to the
    customParameter_* IDProperties of their paired export material(s). Re-export
    reads from the export material, so this persists changes made via the FS25
    Material Settings / config preview. scope='selected' = the active object's
    material; scope='all' = every debug material in the file."""
    bl_idname = "fs25.sync_debug_to_export_material"
    bl_label = "Sync to Export Material"
    bl_options = {'REGISTER', 'UNDO'}

    scope: StringProperty(default='selected')

    def execute(self, context):
        if self.scope == 'all':
            done = params = 0
            for m in bpy.data.materials:
                if m.get('_i3d_material_kind') != 'debug':
                    continue
                r = _sync_debug_material(m)
                if r is not None:
                    done += 1
                    params += r[1]
            self.report({'INFO'}, "Synced %d material(s), %d parameter(s) to "
                        "export materials." % (done, params))
            return {'FINISHED'}
        obj = context.active_object
        mat = obj.active_material if obj is not None else None
        if mat is None or mat.get('_i3d_material_kind') != 'debug':
            self.report({'WARNING'},
                        "Active material is not an FS25 debug material.")
            return {'CANCELLED'}
        r = _sync_debug_material(mat)
        if r is None:
            self.report({'WARNING'}, "No export counterpart for '%s'" % mat.name)
            return {'CANCELLED'}
        name, n, sk = r
        msg = "Synced %d parameter(s) to '%s'" % (n, name)
        if sk:
            msg += " (%d skipped)" % sk
        self.report({'INFO'}, msg)
        return {'FINISHED'}


def _apply_config_xml(context, import_id, filepath, report):
    """Assign a vehicle/placeable config XML's i3dMappings to *import_id*, register
    it in the Giants exporter, and load its store-config preview. Shared by the
    'Load Config XML' operator and the optional auto-load on i3d import.
    Returns True on success, False when the XML has no <i3dMappings>.
    """
    _cfgmap = json.loads(context.scene.get('_i3d_configxml', '{}'))
    _cfgmap[import_id] = bpy.path.abspath(filepath)
    context.scene['_i3d_configxml'] = json.dumps(_cfgmap)
    mappings = i3d_xml_parser.parse_i3d_mappings(filepath)
    if not mappings:
        report({'WARNING'}, "No <i3dMappings> found in this XML.")
        return False
    by_path = {}
    for mid, npath in mappings:
        by_path.setdefault(npath, mid)

    def _path_segments(p):
        # "0>0|8|4" -> ('0','0','8','4'); "0>" -> ('0',). Segment-wise, so a
        # ReferenceNode path is a clean prefix test against a mapping path.
        head, _sep, tail = p.partition('>')
        return (head,) + tuple(tail.split('|')) if tail else (head,)

    path_index = {}
    ref_nodes = []  # (segments, referenced_filename) for imported ReferenceNodes
    for obj in context.scene.objects:
        if obj.get('_i3d_import_id') != import_id:
            continue
        pth = obj.get('_i3d_node_path')
        if pth:
            path_index.setdefault(pth, []).append(obj)
            _rn = obj.get('i3D_referenceFilename')
            if _rn:
                ref_nodes.append((_path_segments(pth), _rn))
    applied = 0
    unmatched = []
    for npath, mid in by_path.items():
        objs = path_index.get(npath)
        if not objs:
            unmatched.append((mid, npath))
            continue
        for obj in objs:
            obj['I3D_XMLconfigID'] = mid
            obj['I3D_XMLconfigBool'] = True
            applied += 1
    exporter_note = ""
    settings = getattr(context.scene, "I3D_UIexportSettings", None)
    if settings is not None:
        attr = ("i3D_updateXMLFilePath" if hasattr(settings, "i3D_updateXMLFilePath")
                else "I3D_updateXMLFilePath" if hasattr(settings, "I3D_updateXMLFilePath")
                else None)
        if attr is not None:
            # Register a COPY in our own export folder with the exporter, not the
            # original game XML: registering the game path directly (i) risks the
            # Giants exporter overwriting a base-game file on re-export, and (ii)
            # is the suspected cause of "exportObjectDataTexture: [Errno 22]
            # Invalid argument" on re-export (game paths can be read-only/
            # protected). Mirrors the existing i3D_exportFileLocation convention
            # for the i3d output itself (see importer.py, EXPORT_DIR / i3d.name).
            # Copy only if the destination is missing, so re-loading the same
            # vehicle never clobbers an already-exported XML.
            export_xml_path = filepath
            _edir = _export_dir()
            if _edir:
                try:
                    _dst = Path(bpy.path.abspath(_edir)) / Path(filepath).name
                    if not _dst.exists():
                        _dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(filepath, _dst)
                    export_xml_path = str(_dst)
                except OSError as exc:
                    report({'WARNING'},
                           "Could not copy config XML to export folder (%s) - "
                           "registering the original game path instead, "
                           "re-export may fail or touch the game file." % exc)
            abspath = bpy.path.abspath(export_xml_path)
            current = getattr(settings, attr)
            if abspath not in current:
                setattr(settings, attr, current + ";{};;".format(abspath))
                exporter_note = "; added to Giants exporter XML Config Files"
    # Store-config preview. wheelConfigurations start UNSELECTED (sel = -1): the
    # default object-changes are still applied (index 0 below), but no wheel
    # button is highlighted and the tires are not loaded until the user clicks.
    try:
        _cfg_types = i3d_config_parser.parse_configurations(filepath)
        _rim_colors = i3d_wheel_resolver.parse_rim_colors(filepath)
        _brands = i3d_wheel_resolver.parse_brands(
            filepath, i3d_material_templates._data_dir(_fs25_data_base() or ""))
        _sets = i3d_config_parser.parse_configuration_sets(filepath)
        _wheelopts = i3d_wheel_resolver.parse_wheel_options(filepath)
        if _cfg_types or _rim_colors or _brands or _sets or _wheelopts:
            _store = json.loads(context.scene.get('_i3d_storecfg', '{}'))
            _types_d = i3d_config_parser.to_dict(_cfg_types) if _cfg_types else []
            _db = _fs25_data_base()
            # useDefaultColors: append the game's generated palette so the
            # picker matches the in-game colour list (q4m: 1 manual + 35).
            if _types_d:
                i3d_config_preview.expand_default_colors(_types_d, _db)
            # Wheel selector, resolver-driven (multi-size configs expand into one
            # option per size). Tires load on click (sel=-1), not at import.
            _wheelopts_entry = ({"options": [
                {"label": _l, "config_index": _ci, "dim_col": _dc}
                for _ci, _dc, _l in _wheelopts], "sel": -1}
                if _wheelopts else None)
            # Initial selection = the game's default option per type
            # (getDefaultConfigIdFromItems: isDefault/first selectable, not stured 0).
            # wheelConfigurations stays -1 (tires load on click, not at import).
            _entry = {
                "types": _types_d,
                "sel": {t["tag"]: (-1 if t["tag"] == "wheelConfigurations"
                                   else t.get("default", 0))
                        for t in _types_d}}
            # Configuration sets (presets): pin the default set's sub-config
            # indices. The set chooser replaces those sub-configs in the UI.
            if _sets:
                _dset = next((i for i, s in enumerate(_sets["sets"])
                              if s.get("is_default")), 0)
                _entry["sets"] = {"title": _sets["title"],
                                  "controlled": _sets["controlled"],
                                  "options": _sets["sets"], "sel": _dset}
                for _cn, _ix in _sets["sets"][_dset]["configs"].items():
                    _tag = _cn + "Configurations"
                    if _tag in _entry["sel"] and _tag != "wheelConfigurations":
                        _entry["sel"][_tag] = _ix
            if _wheelopts_entry:
                _entry["wheelopts"] = _wheelopts_entry
            if _rim_colors:
                # Rim colour applies to wheels/weights once they are loaded
                # (default index 0 = the stock colour); applied after a wheel
                # config is loaded, not here.
                _entry["rimcolor"] = {
                    "options": [{"label": i3d_config_preview.rim_color_label(_t, _db),
                                 "template": _t, "selectable": _s}
                                for _i, _t, _s in _rim_colors],
                    "sel": 0}
            if _brands and len(_brands) > 1:
                _entry["brand"] = {
                    "options": [{"label": (_b.title() + " " + (_n or "")).strip(),
                                 "index": _bi, "brand": _b}
                                for _bi, _b, _n in _brands],
                    "sel": 0}
            _store[import_id] = _entry
            context.scene['_i3d_storecfg'] = json.dumps(_store)
            if _types_d:
                i3d_config_preview.capture_material_originals(import_id, _types_d)
                for _t in _types_d:
                    # Apply the finalized selection (incl. default-set overrides);
                    # wheelConfigurations shows its default object-changes but its
                    # button stays unselected (sel=-1) until the user loads tires.
                    _ix = (_t.get("default", 0)
                           if _t["tag"] == "wheelConfigurations"
                           else _entry["sel"].get(_t["tag"], _t.get("default", 0)))
                    i3d_config_preview.apply_config(import_id, _t, _ix, _db,
                                                    entry=_entry)
    except Exception as _e:
        report({'INFO'}, "Store-config preview not loaded: %r" % _e)
    if unmatched:
        # Split unmatched mappings: those whose node path runs INTO an imported
        # ReferenceNode (the target lives in the referenced i3d, which we do not
        # import - the mapping is lost) vs. the rest (e.g. skinned bones/joints).
        _ref_lost = {}   # referenced filename -> [mid, ...]
        _other = []
        for _mid, _np in unmatched:
            _segs = _path_segments(_np)
            _best = None  # (prefix_len, filename)
            for _rsegs, _rname in ref_nodes:
                if len(_rsegs) < len(_segs) and _segs[:len(_rsegs)] == _rsegs:
                    if _best is None or len(_rsegs) > _best[0]:
                        _best = (len(_rsegs), _rname)
            if _best is not None:
                _ref_lost.setdefault(_best[1], []).append(_mid)
            else:
                _other.append(_mid)
        for _fname, _mids in _ref_lost.items():
            report({'WARNING'},
                   "%d i3dMapping(s) not assigned because they point into the "
                   "referenced i3d file (%s): %s. We do not import the referenced "
                   "i3d, therefore the mapping is lost."
                   % (len(_mids), _fname, ", ".join(_mids)))
        if _other:
            report({'WARNING'},
                   "%d i3dMapping(s) not assigned (no matching object - maybe because it's a "
                   "skinned bone/joint): %s" % (len(_other), ", ".join(_other)))
    msg = "Applied %d i3dMapping(s) to import '%s' from %d entries" % (
        applied, import_id, len(by_path))
    if unmatched:
        msg += "; %d not assigned (see warning)" % len(unmatched)
    report({'INFO'}, msg + exporter_note + ".")
    return True


class FS25_OT_load_config_xml(Operator, ImportHelper):
    """Load i3dMappings from a vehicle/placeable config XML and assign them to
    the imported objects (sets I3D_XMLconfigID + I3D_XMLconfigBool so the Giants
    exporter can re-write the <i3dMappings> block on export). Scoped to the
    import of the active object."""
    bl_idname = "fs25.load_config_xml"
    bl_label = "Load Config XML (i3dMappings)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".xml"
    filter_glob: StringProperty(default="*.xml", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.get('_i3d_import_id') is not None

    def execute(self, context):
        import_id = context.active_object.get('_i3d_import_id')
        if _apply_config_xml(context, import_id, self.filepath, self.report):
            return {'FINISHED'}
        return {'CANCELLED'}


class FS25_PT_i3d_importer_panel(bpy.types.Panel):
    """N-Panel entry in the 3D Viewport sidebar with material-switch buttons."""
    bl_idname = "FS25_PT_i3d_importer_panel"
    bl_label = "i3d Importer"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "i3d Importer"

    def draw(self, context):
        layout = self.layout

        # One-click pre-export prep (snow + GE-invisible + config reset +
        # export materials, in order). Prominent at the top.
        pbox = layout.box()
        pbox.label(text="Before Export", icon='EXPORT')
        pbox.operator("fs25.prepare_for_export", text="Prepare for Export",
                      icon='CHECKMARK')



        # Tree season - shown only when the file actually has a
        # tree-branch debug material (treeBranchShader SEASONAL).
        if any(m.get('_i3d_tree_branch_debug') for m in bpy.data.materials):
            box = layout.box()
            box.label(text="Tree Season")
            box.prop(context.scene, "fs25_tree_season", text="")


class FS25_PT_material_switch(bpy.types.Panel):
    """Sub-panel: switch imported meshes between debug and re-export materials.

    'Only selected' scopes the buttons to the mesh selection; otherwise they
    act on every imported mesh in the scene."""
    bl_idname = "FS25_PT_material_switch"
    bl_label = "Material Switch"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "i3d Importer"
    bl_parent_id = "FS25_PT_i3d_importer_panel"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        only_sel = scene.fs25_matswitch_only_selected
        layout.prop(scene, "fs25_matswitch_only_selected", text="Only selected")

        # Only selected -> act on the current mesh selection; otherwise scene-
        # wide (every imported mesh, incl. hidden helpers - safest for export).
        scope = 'selection' if only_sel else 'all_imported'
        has_sel = any(o.type == 'MESH' for o in context.selected_objects)
        row = layout.row(align=True)
        row.enabled = (has_sel or not only_sel)
        op_dbg = row.operator("fs25.switch_materials", text="Debug material")
        op_dbg.target_kind = 'debug'
        op_dbg.scope = scope
        op_exp = row.operator("fs25.switch_materials", text="Export material")
        op_exp.target_kind = 'export'
        op_exp.scope = scope


class FS25_PT_material_settings(bpy.types.Panel):
    """Sub-panel showing FS25 custom-parameter sliders for the active material.

    The PBR debug material exposes each FS25 custom parameter as a labeled
    Value/RGB node with name prefix 'fs25_param:'. This panel scans the
    active material's node tree for those, groups them via
    material_inventory.lookup_param(), and renders sliders/color pickers
    grouped by topic (Vehicle Brand Color, Clear Coat, Multitint, ...).
    """
    bl_idname = "FS25_PT_material_settings"
    bl_label = "Material Settings"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "i3d Importer"
    bl_parent_id = "FS25_PT_i3d_importer_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout

        # Always-on hint: changes here live in the debug material's
        # node tree; re-export reads from the paired export material's
        # customParameter_* IDProperties. The Sync button below copies
        # the slider values across.
        layout.label(
            text="Debug only - click 'Sync to Export Material' before re-export",
            icon='INFO')

        obj = context.active_object
        if obj is None or obj.active_material is None:
            layout.label(text="No active material", icon='INFO')
            return

        mat = obj.active_material
        if not mat.use_nodes or mat.node_tree is None:
            layout.label(text="Material has no node tree", icon='INFO')
            return

        # Scan for fs25_param: nodes, bucket by inventory group.
        prefix = "fs25_param:"
        groups = {}  # group_name -> list of (order, node)
        for node in mat.node_tree.nodes:
            if not node.name.startswith(prefix):
                continue
            param_name = node.name[len(prefix):]
            group, order = material_inventory.lookup_param(param_name)
            groups.setdefault(group, []).append((order, node))

        if not groups:
            layout.label(text="No FS25 parameters in this material",
                         icon='INFO')
            return

        # Render in the defined group order; unknown groups appended in
        # insertion order (defensive against new params added later).
        ordered = list(material_inventory.FS25_PARAM_GROUP_ORDER)
        for g in groups:
            if g not in ordered:
                ordered.append(g)

        for group_name in ordered:
            entries = groups.get(group_name)
            if not entries:
                continue
            box = layout.box()
            box.label(text=group_name, icon='NODE')
            for _order, node in sorted(entries, key=lambda x: x[0]):
                row = box.row()
                # Display label takes precedence; fallback to param name.
                text = node.label or node.name[len(prefix):]
                row.prop(node.outputs[0], "default_value", text=text)

        # Sync slider values back to the paired export material's
        # customParameter_* IDProperties so re-export sees them.
        layout.separator()
        layout.label(text="Sync to Export Material")
        row = layout.row(align=True)
        sub = row.row(align=True)
        _am = context.active_object.active_material if context.active_object else None
        sub.enabled = (_am is not None
                       and _am.get('_i3d_material_kind') == 'debug')
        op = sub.operator("fs25.sync_debug_to_export_material", text="Selected",
                          icon='FILE_REFRESH')
        op.scope = 'selected'
        op_all = row.operator("fs25.sync_debug_to_export_material", text="All",
                              icon='FILE_REFRESH')
        op_all.scope = 'all'


# ---------------------------------------------------------------------------
# Debug View
# Scene-level EnumProperty + Panel that drive the per-material
# fs25_debug:* nodes via recipe_loader.apply_debug_mode_to_material().
# ---------------------------------------------------------------------------

# Module-level keepalive for the dynamic enum items - Blender requires the
# Python strings to outlive the callback invocation.
_DEBUG_MODE_ITEMS_CACHE = []


def _debug_mode_items(self, context):
    """Build the dropdown options dynamically from the active material's masks.

    Items:
        NORMAL          -> normal PBR view
        MASK:<name>     -> one entry per mask in the active material
        VERTEX_COLORS   -> vertex colors
    """
    global _DEBUG_MODE_ITEMS_CACHE
    items = [
        ('NORMAL', "Default", "Show the standard PBR material (no debug overlay)"),
    ]
    obj = context.active_object if context else None
    if obj is not None and obj.active_material is not None:
        for name in list(obj.active_material.get('_fs25_debug_masks', [])):
            items.append((f'MASK:{name}', name, f"Show {name}"))
    items.append(('VERTEX_COLORS', "Vertex Colors",
                  "Show the vertex color attribute"))
    _DEBUG_MODE_ITEMS_CACHE = items
    return items


def _on_debug_mode_change(self, context):
    """Apply the selected debug mode to one or all materials.

    If `fs25_debug_only_active` is set, only the active object's active
    material is changed. Otherwise all materials in bpy.data.materials
    that carry the FS25 debug switch are updated.
    """
    mode_str = self.fs25_debug_mode
    only_active = self.fs25_debug_only_active

    if only_active:
        obj = context.active_object if context else None
        mat = obj.active_material if (obj is not None) else None
        if mat is not None:
            recipe_loader.apply_debug_mode_to_material(mat, mode_str)
    else:
        for mat in bpy.data.materials:
            recipe_loader.apply_debug_mode_to_material(mat, mode_str)


class FS25_PT_debug_view(bpy.types.Panel):
    """Sub-panel: switch the active or all FS25 materials into debug view modes.

    Modes: Normal / one of the available masks / Vertex Colors.
    """
    bl_idname = "FS25_PT_debug_view"
    bl_label = "Debug View"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "i3d Importer"
    bl_parent_id = "FS25_PT_i3d_importer_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        obj = context.active_object
        if obj is None or obj.active_material is None:
            layout.label(text="No active material", icon='INFO')
            return

        mat = obj.active_material
        switch = (mat.node_tree.nodes.get("fs25_debug:switch")
                  if mat.use_nodes and mat.node_tree else None)
        if switch is None:
            layout.label(text="No FS25 debug switch in this material",
                         icon='INFO')
            return

        layout.prop(scene, "fs25_debug_mode", text="Mode")
        layout.prop(scene, "fs25_debug_only_active",
                    text="Only active material")

        # Vertex Color attribute name - searchable dropdown sourced from
        # the active mesh's color_attributes collection. 
        vc_attr = mat.node_tree.nodes.get("fs25_debug:vertex_color")
        mesh = obj.data if obj.type == 'MESH' else None
        if vc_attr is not None:
            box = layout.box()
            box.label(text="Vertex Color layer:", icon='COLOR')

            if mesh is not None and hasattr(mesh, 'color_attributes'):
                box.prop_search(vc_attr, "attribute_name",
                                mesh, "color_attributes", text="")
            else:
                box.prop(vc_attr, "attribute_name", text="")


# ---------------------------------------------------------------------------
# Snow heaps
# Show/hide objects whose material points to snowHeapShader.xml. The
# importer flags them via obj['_i3d_is_snow_heap'].
# ---------------------------------------------------------------------------

class FS25_OT_snow_heaps_show(bpy.types.Operator):
    """Make all snow/ice objects visible in the current view layer
    (clears the Outliner eye via obj.hide_set(False))."""
    bl_idname = "fs25.snow_heaps_show"
    bl_label = "Show All Snow + Ice"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = 0
        for obj in context.scene.objects:
            if obj.get('_i3d_is_snow_heap'):
                obj.hide_set(False)
                n += 1
        self.report({'INFO'}, f"{n} object(s) unhidden")
        return {'FINISHED'}


class FS25_OT_snow_heaps_hide(bpy.types.Operator):
    """Hide all snow/ice objects in the current view layer
    (closes the Outliner eye via obj.hide_set(True))."""
    bl_idname = "fs25.snow_heaps_hide"
    bl_label = "Hide All Snow + Ice"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = 0
        for obj in context.scene.objects:
            if obj.get('_i3d_is_snow_heap'):
                obj.hide_set(True)
                n += 1
        self.report({'INFO'}, f"{n} object(s) hidden")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Level of Detail (LOD)
# A TransformGroup carrying lodDistance is a LOD group (obj['i3D_lod']=True);
# its children are the LOD levels in scene-graph order (child 0 = LOD0, 1 =
# LOD1, ...). A child's level is the trailing index of its _i3d_node_path
# (its sibling index under the group), independent of the optional sort prefix.
# ---------------------------------------------------------------------------

def _lod_children_levels(parent):
    """[(child, level), ...] for a LOD group's direct children.

    level = trailing index of _i3d_node_path (0-based sibling index == LOD
    level). Falls back to the 4-digit sort-order name prefix, else Blender's
    child order, assigning 0..n."""
    kids = list(parent.children)
    lvl_map = []
    for c in kids:
        p = c.get('_i3d_node_path')
        seg = re.split(r'[>|]', p)[-1] if p else ""
        if seg.isdigit():
            lvl_map.append((c, int(seg)))
        else:
            lvl_map = None
            break
    if lvl_map is not None and kids:
        return lvl_map
    def _key(c):
        m = re.match(r'^(\d{4})', c.name or "")
        return (0, int(m.group(1))) if m else (1, c.name or "")
    return [(c, i) for i, c in enumerate(sorted(kids, key=_key))]


class FS25_OT_set_lod_level(bpy.types.Operator):
    """Show one LOD level and hide the others across every LOD group in the
    scene. For each group (obj['i3D_lod']=True) the child subtree at the chosen
    level is shown via the Outliner eye and the other level subtrees hidden;
    objects outside any LOD group are left untouched."""
    bl_idname = "fs25.set_lod_level"
    bl_label = "Set LOD level"
    bl_options = {'REGISTER', 'UNDO'}

    level: bpy.props.IntProperty(name="LOD level", default=0, min=0)

    def execute(self, context):
        shown = hidden = groups = 0
        for parent in context.scene.objects:
            if not parent.get('i3D_lod'):
                continue
            groups += 1
            for child, lvl in _lod_children_levels(parent):
                show = (lvl == self.level)
                subtree = [child] + list(child.children_recursive)
                for o in subtree:
                    o.hide_set(not show)
                if show:
                    shown += len(subtree)
                else:
                    hidden += len(subtree)
        if groups == 0:
            self.report({'WARNING'}, "No LOD groups in scene")
            return {'CANCELLED'}
        self.report({'INFO'}, f"LOD{self.level}: {shown} shown, {hidden} hidden")
        return {'FINISHED'}


class FS25_PT_visibility(bpy.types.Panel):
    """Show/hide helper: LOD levels, snow+ice and GE-invisible meshes. Each
    block only appears when relevant objects exist; the whole panel hides when
    none do."""
    bl_idname = "FS25_PT_visibility"
    bl_label = "Visibility"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "i3d Importer"
    bl_parent_id = "FS25_PT_i3d_importer_panel"

    @classmethod
    def poll(cls, context):
        objs = context.scene.objects
        return any(o.get('_i3d_is_snow_heap') or o.get('_i3d_invisible_in_ge')
                   or o.get('i3D_lod') for o in objs)

    def draw(self, context):
        layout = self.layout
        objs = context.scene.objects
        snow = sum(1 for o in objs if o.get('_i3d_is_snow_heap'))
        ge = sum(1 for o in objs if o.get('_i3d_invisible_in_ge'))
        has_lod = any(o.get('i3D_lod') for o in objs)

        if has_lod:
            box = layout.box()
            box.label(text="Level of Detail (LOD)", icon='MOD_DECIM')
            row = box.row(align=True)
            row.operator("fs25.set_lod_level", text="LOD0").level = 0
            row.operator("fs25.set_lod_level", text="LOD1").level = 1

        if snow:
            box = layout.box()
            box.label(text="Snow & Ice", icon='FREEZE')
            row = box.row(align=True)
            row.operator("fs25.snow_heaps_show", text=f"Show {snow}", icon='HIDE_OFF')
            row.operator("fs25.snow_heaps_hide", text=f"Hide {snow}", icon='HIDE_ON')
        if ge:
            box = layout.box()
            box.label(text="Invisible in GE", icon='GHOST_ENABLED')
            row = box.row(align=True)
            row.operator("fs25.invisible_ge_show", text=f"Show {ge}", icon='HIDE_OFF')
            row.operator("fs25.invisible_ge_hide", text=f"Hide {ge}", icon='HIDE_ON')


# ---------------------------------------------------------------------------
# Invisible GE-objects
# Show/hide objects flagged by _should_hide_for_visibility(): GE
# visibility="false" or nonRenderable="true" (without terrainDecal="true").
# The importer flags them via obj['_i3d_invisible_in_ge'] unconditionally
# (even when auto_hide_invisible_shapes is off on import).
# ---------------------------------------------------------------------------

class FS25_OT_invisible_ge_show(bpy.types.Operator):
    """Make all GE-invisible objects visible in the current view layer
    (clears the Outliner eye via obj.hide_set(False))."""
    bl_idname = "fs25.invisible_ge_show"
    bl_label = "Show All Invisible GE-objects"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = 0
        for obj in context.scene.objects:
            if obj.get('_i3d_invisible_in_ge'):
                obj.hide_set(False)
                n += 1
        self.report({'INFO'}, f"{n} object(s) unhidden")
        return {'FINISHED'}


class FS25_OT_invisible_ge_hide(bpy.types.Operator):
    """Hide all GE-invisible objects in the current view layer
    (closes the Outliner eye via obj.hide_set(True))."""
    bl_idname = "fs25.invisible_ge_hide"
    bl_label = "Hide All Invisible GE-objects"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = 0
        for obj in context.scene.objects:
            if obj.get('_i3d_invisible_in_ge'):
                obj.hide_set(True)
                n += 1
        self.report({'INFO'}, f"{n} object(s) hidden")
        return {'FINISHED'}


def _reset_config_preview():
    """Re-show every part hidden by the store-config preview: clear the preview
    hide flags and the _i3d_cfg_hidden/_i3d_cfg_inh tags. Returns the number of
    parts shown."""
    n = 0
    for obj in bpy.data.objects:
        if obj.get('_i3d_cfg_hidden') or obj.get('_i3d_cfg_inh'):
            obj.hide_viewport = False
            obj.hide_render = False
            for k in ('_i3d_cfg_hidden', '_i3d_cfg_inh'):
                if k in obj:
                    del obj[k]
            n += 1
    return n


def _restore_prebake_wheel_meshes():
    """Swap every baked wheel part (rims + twin connectors,
    i3d_wheel_loader._bake_*_mesh) back to its PRISTINE pre-bake mesh
    datablock: the export must write the UN-deformed geometry - GE/game
    re-run the shader at runtime, a baked mesh would deform twice.

    MUST run BEFORE the export material switch: materials live on the mesh
    datablock, so switching first and restoring afterwards left the rims on
    their debug materials and wrote the widthAndDiam parameter onto the
    debug (not export) materials - rims came out standard-sized in GE
    (Vario1000, #14 follow-up). Returns the number of meshes restored."""
    n = 0
    for o in bpy.data.objects:
        if o.get("_i3d_wheel_import") is None or o.type != 'MESH' or not o.data:
            continue
        pre = o.data.get("_i3d_prebake_mesh")
        if pre:
            nd = bpy.data.meshes.get(pre)
            if nd is not None and nd is not o.data:
                o.data = nd
                n += 1
    return n


def _prepare_rims_for_export():
    """Make re-exported rims deform correctly in the Giants Editor / game. The FS
    rim shader (vehicleShader 'rim', getRimPos) dishes the rim procedurally from a
    widthAndDiam parameter applied to the NORMALISED mesh; our preview instead uses
    an object scale, so a re-export would double-transform (scale + shader) and the
    shader would run with the default 40x40. This sets each rim's real width/diam
    as a material customParameter (splitting the mesh data + material when several
    sizes share one physical rim mesh) and resets the preview scale to 1, so the
    shader alone sizes and dishes the rim. Returns the number of rim parts prepared.

    Note: this is an export step - it un-scales the rims in the viewport (Blender
    cannot run getRimPos), so the preview rims look normalised afterwards.

    Splitting must duplicate the MESH DATA, not just re-link a material on the
    object: the Giants exporter reads materials straight off
    ``bpy.data.meshes[...].materials`` (dccBlender.getShapeMaterials), not via
    ``object.material_slots`` - so a per-object material-slot override (link=
    'OBJECT') is invisible to it and was silently dropped on export."""
    from collections import defaultdict
    rims = [o for o in bpy.data.objects
            if o.get("_i3d_wheel_role") in ("rim_outer", "rim_inner")
            and o.get("_i3d_rim_wd") and o.type == 'MESH']
    if not rims:
        return 0
    by_data = defaultdict(list)
    for o in rims:
        by_data[o.data.name].append(o)
    data_copies = {}   # (data_name, wd) -> duplicated mesh datablock
    for data_name, objs in by_data.items():
        sizes = sorted({o.get("_i3d_rim_wd") for o in objs})
        first_wd = sizes[0]          # this size keeps the original mesh/material
        for o in objs:
            wd = o.get("_i3d_rim_wd")
            param = "%s 1" % wd                      # "7 44" -> "7 44 1"
            if wd != first_wd:
                dk = (data_name, wd)
                nd = data_copies.get(dk)
                if nd is None:
                    nd = o.data.copy()
                    # give this mesh copy its own material datablocks too, so
                    # the widthAndDiam we set below doesn't leak back onto the
                    # original (still shared) material.
                    for i, m in enumerate(nd.materials):
                        if m is not None:
                            nd.materials[i] = m.copy()
                    data_copies[dk] = nd
                o.data = nd
            for m in o.data.materials:
                if m is not None:
                    m["customParameter_widthAndDiam"] = param
            # Rims with a raw XML scale keep it: the game applies scale AND
            # the widthAndDiam shader sequentially (WheelVisualPart:setNode),
            # so the exported node needs the scale on top of the parameter.
            if not o.get("_i3d_rim_keep_scale"):
                o.scale = (1.0, 1.0, 1.0)

    # Twin connectors: same principle as the rims. The export writes the
    # PRISTINE mesh, and GE runs the rimDual/hubDual shader with the MATERIAL
    # parameters - the file defaults (connectorPos "0 80 40 40") blow the
    # cage up to a ~2 m drum (Vestrum re-export). Write the runtime values
    # (stored by the loader at bake time) as customParameter_* into the
    # export materials, splitting mesh data + materials when connectors with
    # different parameters share one datablock (front vs rear twins).
    conns = [o for o in bpy.data.objects
             if o.get("_i3d_wheel_role") == "connector"
             and o.get("_i3d_conn_shader") and o.type == 'MESH']
    by_data_c = defaultdict(list)
    for o in conns:
        by_data_c[o.data.name].append(o)
    for data_name, objs in by_data_c.items():
        def _pkey(o):
            return (o.get("_i3d_conn_connectorPos") or "",
                    o.get("_i3d_conn_widthAndDiam") or "",
                    o.get("_i3d_conn_posAndScale") or "")
        psets = sorted({_pkey(o) for o in objs})
        first = psets[0]
        copies = {}
        for o in objs:
            pk = _pkey(o)
            if pk != first:
                nd = copies.get(pk)
                if nd is None:
                    nd = o.data.copy()
                    for i, m in enumerate(nd.materials):
                        if m is not None:
                            nd.materials[i] = m.copy()
                    copies[pk] = nd
                o.data = nd
            for m in o.data.materials:
                if m is None:
                    continue
                if o.get("_i3d_conn_connectorPos"):
                    m["customParameter_connectorPos"] = \
                        o["_i3d_conn_connectorPos"]
                if o.get("_i3d_conn_widthAndDiam"):
                    m["customParameter_widthAndDiam"] = \
                        o["_i3d_conn_widthAndDiam"]
                if o.get("_i3d_conn_posAndScale"):
                    m["customParameter_connectorPosAndScale"] = \
                        o["_i3d_conn_posAndScale"]
    return len(rims) + len(conns)


class FS25_OT_config_show_all(bpy.types.Operator):
    """Re-show every part hidden by the store-config preview (undo all config
    hiding). Mirrors the Show-All buttons of the Snow and GE-invisible panels."""
    bl_idname = "fs25.config_show_all"
    bl_label = "Show All Config Parts"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = _reset_config_preview()
        self.report({'INFO'}, "Shown %d config-hidden part(s)" % n)
        return {'FINISHED'}


class FS25_OT_config_reset_default(bpy.types.Operator):
    """Reset the store-config preview to the vehicle's default configuration: set
    every config type back to its default option and re-apply it, so the non-
    default parts (incl. isSelectable="false" ones) are hidden again. Counterpart
    to 'Show All Config Parts', which cannot be undone by picking options."""
    bl_idname = "fs25.config_reset_default"
    bl_label = "Default Config"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        import_id = obj.get('_i3d_import_id') if obj else None
        if import_id is None:
            self.report({'WARNING'}, "Select an imported object first.")
            return {'CANCELLED'}
        store = json.loads(context.scene.get('_i3d_storecfg', '{}'))
        entry = store.get(import_id)
        if not entry:
            self.report({'WARNING'}, "No store config loaded for this import.")
            return {'CANCELLED'}
        db = _fs25_data_base()
        sel = entry.setdefault("sel", {})
        # Re-apply every visual config type at its default option. Wheels keep
        # their state (tires are separately loaded geometry, not config-hidden).
        for ct in entry.get("types", []):
            if ct["tag"] == "wheelConfigurations":
                continue
            di = ct.get("default", 0)
            sel[ct["tag"]] = di
            i3d_config_preview.apply_config(import_id, ct, di, db, entry=entry)
        context.scene['_i3d_storecfg'] = json.dumps(store)
        self.report({'INFO'}, "Reset to default configuration")
        return {'FINISHED'}


class FS25_OT_prepare_for_export(bpy.types.Operator):
    """One click to get the scene ready for the Giants i3d exporter, in order:
    (1) show all snow/ice, (2) show all GE-invisible objects, (3) reset the store-
    config preview (re-show every config-hidden part), (4) switch every imported
    mesh to its re-export material. The Giants exporter writes each node's current
    viewport visibility and has no auto-unhide, so anything left hidden would be
    dropped or exported as visibility=false - this makes the re-export complete."""
    bl_idname = "fs25.prepare_for_export"
    bl_label = "Prepare for Export"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n_snow = n_ge = n_cfg = 0
        # 1. snow/ice, 2. GE-invisible: clear the Outliner eye (hide_set) as the
        # dedicated buttons do.
        for obj in context.scene.objects:
            if obj.get('_i3d_is_snow_heap'):
                obj.hide_set(False)
                n_snow += 1
            if obj.get('_i3d_invisible_in_ge'):
                obj.hide_set(False)
                n_ge += 1
        # 3. reset the config preview: re-show every part hidden by a store config.
        n_cfg = _reset_config_preview()
        # 4. swap baked wheel meshes back to their pristine datablocks BEFORE
        # the material switch - materials live on the mesh datablock, so the
        # switch and the widthAndDiam parameters below must hit the mesh that
        # is actually exported (see _restore_prebake_wheel_meshes).
        n_pre = _restore_prebake_wheel_meshes()
        # 5. switch every imported mesh to its re-export material (scene-wide, so
        # hidden helper meshes cannot leak a debug material into the export).
        try:
            bpy.ops.fs25.switch_materials(target_kind='export',
                                          scope='all_imported')
            mat_msg = "export materials applied"
        except Exception as exc:
            mat_msg = "material switch skipped (%r)" % exc
        # 6. rims: write the real widthAndDiam onto the (now active) export material
        # and un-scale, so the Giants rim shader dishes them correctly on re-export.
        n_rim = _prepare_rims_for_export()
        self.report({'INFO'},
                    "Prepared for export: snow %d, GE-invisible %d, config-reset "
                    "%d, wheel meshes %d, rims %d; %s"
                    % (n_snow, n_ge, n_cfg, n_pre, n_rim, mat_msg))
        return {'FINISHED'}




def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_fs25_i3d.bl_idname, text="Farming Simulator i3d (.i3d)")


# --- Tree season control (treeBranchShader SEASONAL debug materials) -------
# Season -> (leaf-diffuse quadrant offset, leaves-enabled). The leaf-quadrant
# Mapping and the 'leaf enable' Value node are tagged by label in the debug
# material (recipe_loader treeBranchShader block).
_TREE_SEASON_PRESETS = {
    'SUMMER': ((0.5, 0.5), 1.0),
    'AUTUMN': ((0.0, 0.0), 1.0),
    'WINTER': ((0.5, 0.0), 0.0),   # leaves off -> branches only; offset irrelevant
    'SPRING': ((0.0, 0.5), 1.0),
}


def _update_tree_season(self, context):
    """Switch all FS25 tree-branch debug materials to the chosen season."""
    season = getattr(context.scene, 'fs25_tree_season', 'SUMMER')
    offset, leaf_enable = _TREE_SEASON_PRESETS.get(season, ((0.5, 0.5), 1.0))
    for mat in bpy.data.materials:
        if not mat.get('_i3d_tree_branch_debug'):
            continue
        nt = mat.node_tree
        if nt is None:
            continue
        for n in nt.nodes:
            if n.type == 'MAPPING' and n.label == 'i3d_tree_leaf_quadrant':
                n.inputs['Location'].default_value = (offset[0], offset[1], 0.0)
            elif n.type == 'VALUE' and n.label == 'i3d_tree_leaf_enable':
                n.outputs[0].default_value = leaf_enable


# --- Editable i3dMapping id proxy -------------------------------------------
# The Giants exporter keeps the mapping id in TWO separate storages on Blender
# 5.1+: the RNA property obj.I3D_XMLconfigID (shown in the exporter UI) and the
# IDProperty obj["I3D_XMLconfigID"] (what the export actually reads). This proxy
# writes BOTH, so our N-panel field stays in sync with the exporter field and
# exports correctly with or without the exporter-side patch.
def _i3d_mapping_id_get(self):
    return self.get("I3D_XMLconfigID", "")


def _i3d_mapping_id_set(self, value):
    # The Giants exporter keeps this in two separate storages on Blender 5.1+,
    # and its checkbox callback resets the id to the node name when toggled on.
    # To get a custom id into the export reliably:
    #   1) enable + set the RNA side first, so the exporter UI shows it (box on,
    #      custom id) - setting the id AFTER the bool overrides the node-name reset;
    #   2) always write the IDProperties the export actually reads, so it works
    #      with or without the exporter patch and regardless of RNA/IDProp split.
    try:
        self.I3D_XMLconfigBool = True
        self.I3D_XMLconfigID = value
    except (AttributeError, TypeError):
        pass                                  # exporter not installed - id-prop suffices
    self["I3D_XMLconfigBool"] = 1
    self["I3D_XMLconfigID"] = value


def _fs25_data_base():
    try:
        return bpy.context.preferences.addons[__package__].preferences.fs25_data_base
    except Exception:
        return ""


def _export_dir():
    try:
        return bpy.context.preferences.addons[__package__].preferences.export_dir
    except Exception:
        return ""


def _pretty_cfg_label(s):
    """Make an l10n key or raw config label readable for the UI.

    Base-game l10n keys (e.g. $l10n_configuration_valueUniversalShares) cannot be
    resolved to their translation (the strings are not in the data files), so we
    strip the key and split CamelCase: -> "Universal Shares".
    """
    if not s:
        return "Option"
    if s.startswith("$l10n_"):
        s = s.split("value", 1)[1] if "value" in s else s.rsplit("_", 1)[-1]
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s).strip()
    s = (s[:1].upper() + s[1:]) if s else s
    return s or "Option"


def _option_label(opt):
    """Button label for one config option: prettified name plus the option's
    ``params`` value(s). Giants uses ONE l10n key with %s placeholders for a
    whole option group (farmall120C: four $l10n_configuration_frontWeightX
    entries, params 60/220/300/380) - the translated text lives in dataS.gar
    and is not resolvable here, so without the params every button read
    identically (#12). The placeholder shows up as a trailing 'X' in the
    prettified key name and is replaced by the params; a literal %s (mod
    XMLs with plain-text names) is substituted directly."""
    raw = opt.get("label") or ""
    params = (opt.get("params") or "").strip()
    if params and "%s" in raw:
        # Plain-text name with %s placeholders (timberFrameFH16 Extension:
        # name="1 x 5%s" params="$l10n_unit_mShort" - the game resolves the
        # l10n PARAMS before substituting). We cannot translate, so strip the
        # token down to its unit like the configurationSet labels do
        # (i3d_config_parser._set_label): "$l10n_unit_mShort" -> "m".
        label = raw
        for p in params.split("|"):
            # clean each param BEFORE substituting: "1 x 5%s" glues the unit
            # to a digit ("5mShort"), where a \b-based cleanup cannot bite.
            p = re.sub(r"^\$l10n_(?:unit_)?", "", p)
            p = re.sub(r"^([a-zA-Z]{1,6})Short$", r"\1", p)
            label = label.replace("%s", p, 1)
        label = re.sub(r"\$l10n_(?:unit_|configuration_value|configuration_)?",
                       "", label)
        label = re.sub(r"\b([a-zA-Z]{1,6})Short\b", r"\1", label)
        return label.strip() or "Option"
    label = _pretty_cfg_label(raw)
    if not params:
        return label
    if label.endswith(" X"):
        label = label[:-2]
    return ("%s %s" % (label, " ".join(params.split("|")))).strip()


# ---------------------------------------------------------------------------
# Colour swatches (store-config colour picker)
# ---------------------------------------------------------------------------

_color_icons = None      # bpy.utils.previews collection (runtime icons)
_ICON_N = 32             # icon edge length in px


def _color_icon_collection():
    global _color_icons
    if _color_icons is None:
        import bpy.utils.previews
        _color_icons = bpy.utils.previews.new()
    return _color_icons


def _srgb(c):
    """linear -> sRGB (fs colours are linear; icon bytes are shown as-is)."""
    c = max(0.0, min(1.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055


# Finish-marker pixel patterns (local coords in a 12x12 corner field),
# mirroring the three in-game symbols: shiny = 4-ray star, metallic =
# 3x3 dot grid, mat = diagonal stripes.
_FINISH_PIXELS = {
    "s": [(5, 1), (5, 2), (5, 3), (5, 4), (5, 5), (5, 6), (5, 7), (5, 8),
          (5, 9), (1, 5), (2, 5), (3, 5), (4, 5), (6, 5), (7, 5), (8, 5),
          (9, 5), (4, 4), (6, 4), (4, 6), (6, 6)],
    "m": [(x, y) for x in (2, 5, 8) for y in (2, 5, 8)],
    "f": ([(x, x) for x in range(1, 10)]
          + [(x, x + 4) for x in range(1, 6)]
          + [(x, x - 4) for x in range(5, 10)]),
}


def _swatch_icon_id(rgb, finish):
    """icon_id of a 32x32 colour swatch with a finish marker, generated once
    into the runtime preview collection (ImagePreview.icon_pixels_float is
    writable - no temp PNG files needed)."""
    col = _color_icon_collection()
    r, g, b = (_srgb(v) for v in rgb)
    key = "sw_%02x%02x%02x_%s" % (int(r * 255), int(g * 255), int(b * 255),
                                  finish)
    p = col.get(key)
    if p is None:
        p = col.new(key)
        p.icon_size = (_ICON_N, _ICON_N)
        px = [r, g, b, 1.0] * (_ICON_N * _ICON_N)
        # subtle darker border for separation on similar panel backgrounds
        for k in range(_ICON_N):
            for i in (k, (_ICON_N - 1) * _ICON_N + k,
                      k * _ICON_N, k * _ICON_N + _ICON_N - 1):
                px[i * 4:i * 4 + 3] = [r * 0.55, g * 0.55, b * 0.55]
        # finish marker bottom right: white on dark, black on bright colours
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        mc = 0.0 if lum > 0.5 else 1.0
        for (x, y) in _FINISH_PIXELS.get(finish, ()):
            gx, gy = _ICON_N - 14 + x, 2 + y      # icon origin = bottom left
            if 0 <= gx < _ICON_N and 0 <= gy < _ICON_N:
                i = (gy * _ICON_N + gx) * 4
                px[i:i + 3] = [mc, mc, mc]
        p.icon_pixels_float = px
    return p.icon_id


def _color_swatch_data(opt, ct, templates):
    """(rgb, finish) of one colour option for its swatch icon - game UI rules
    (VehicleConfigurationItemColor.loadFromXML): a chrome template forces
    metallic + grey 0.3, silver grey 0.4 (both OVERRIDE uiColor), then the
    explicit uiColor, then the resolved option colour."""
    tpl = (opt.get("template") or "").lower()
    is_metallic = bool(opt.get("is_metallic"))
    rgb = None
    if "chrome" in tpl:
        rgb, is_metallic = (0.3, 0.3, 0.3), True
    elif "silver" in tpl:
        rgb = (0.4, 0.4, 0.4)
    if rgb is None and opt.get("ui_color"):
        v = i3d_config_preview._rgb_str(opt["ui_color"])
        if v:
            rgb = tuple(float(x) for x in v.split())
    if rgb is None:
        c, _t = i3d_config_preview._resolve_option_color(opt, ct, templates)
        if c:
            rgb = tuple(float(x) for x in c.split())
    if rgb is None:
        rgb = (1.0, 1.0, 1.0)
    finish = "m" if is_metallic else ("f" if opt.get("is_mat") else "s")
    return rgb, finish


def _color_option_label(opt, templates):
    """Readable colour name: the colour/look template's ``title`` from
    brandMaterialTemplates when available (l10n-resolved, e.g. "Mamba Green"),
    else the prettified raw label (SKODA_MAMBA_GREEN -> Skoda Mamba Green)."""
    for key in ("color", "template"):
        nm = opt.get(key) or ""
        t = templates.get(nm)
        if t is not None and t.get("title"):
            ttl = t["title"]
            # titles can themselves be l10n keys ($l10n_ui_colorSkodaEnergyBlue)
            if ttl.startswith("$l10n_"):
                ttl = _pretty_cfg_label(ttl)
                if ttl.startswith("Color "):
                    ttl = ttl[len("Color "):]
            return ttl
    lbl = opt.get("label") or "Option"
    if "_" in lbl or lbl.isupper():
        return lbl.replace("_", " ").title()
    return _pretty_cfg_label(lbl)


def _update_brand_options(context, import_id, entry, config_index, dim_col=0):
    """Recompute the tire-brand options for the selected wheel configuration + size
    and reset the brand selection. Each config offers only the manufacturers that
    supply all of its wheel sizes, so the list (and default) differs per config -
    e.g. the Puma default has 6 brands, its narrow-row configs far fewer."""
    cfgmap = json.loads(context.scene.get('_i3d_configxml', '{}'))
    xml = cfgmap.get(import_id)
    data_dir = i3d_material_templates._data_dir(_fs25_data_base() or "")
    if not (xml and data_dir and os.path.isfile(xml)):
        return
    # Remember the currently-selected brand so a compatible choice survives a
    # config/size switch; only fall back to the default when that brand is not
    # offered by the new configuration.
    prev = entry.get("brand")
    prev_brand = None
    if prev and prev.get("options"):
        psel = prev.get("sel", 0)
        if 0 <= psel < len(prev["options"]):
            prev_brand = (prev["options"][psel].get("brand") or "").lower()
    brands = i3d_wheel_resolver.parse_brands(xml, data_dir, config_index, dim_col)
    if brands and len(brands) > 1:
        options = [{"label": (b.title() + " " + (n or "")).strip(),
                    "index": bi, "brand": b} for bi, b, n in brands]
        sel = 0
        if prev_brand:
            sel = next((o["index"] for o in options
                        if (o["brand"] or "").lower() == prev_brand), 0)
        entry["brand"] = {"options": options, "sel": sel}
    else:
        entry.pop("brand", None)


def _reload_wheels(context, import_id, entry, config_index, dim_col=0):
    """Load the wheels for *config_index* + size column *dim_col* using the entry's
    selected tire brand, then re-apply the selected rim colour. Returns False if the
    config XML / FS25 data base are unavailable. Shared by the wheel/brand ops."""
    cfgmap = json.loads(context.scene.get('_i3d_configxml', '{}'))
    xml = cfgmap.get(import_id)
    data_dir = i3d_material_templates._data_dir(_fs25_data_base() or "")
    if not (xml and os.path.isfile(xml) and data_dir and os.path.isdir(data_dir)):
        return False
    brand = entry.get("brand", {}).get("sel", 0)
    i3d_wheel_loader.load_all_wheels(xml, data_dir, import_id, config_index,
                                     brand_index=brand, dim_col=dim_col)
    # Crawler tracks (Raupenfahrwerke) live in a parallel <crawlers> section
    # of the same wheelConfiguration; no-op when the config has none.
    i3d_crawler_loader.load_crawlers(xml, data_dir, import_id, config_index)
    rc = entry.get("rimcolor")
    if rc and rc.get("options"):
        sel = rc.get("sel", 0)
        if 0 <= sel < len(rc["options"]):
            # Bake into the export material only for the stock colour (index 0);
            # user picks stay preview-only and go to export via the Sync button.
            i3d_config_preview.apply_rim_color(
                import_id, rc["options"][sel]["template"], _fs25_data_base(),
                to_export=(sel == 0))
    return True


def _config_ref_matches(m, config_name):
    """True if material entry *m* colour-references *config_name*."""
    if m.get("use_base_color") and config_name == "baseColor":
        return True
    n = m.get("use_design_color_index") or 0
    if n and config_name == ("designColor" if n <= 1 else "designColor%d" % n):
        return True
    return bool(m.get("use_rim_color")) and config_name == "rimColor"


def _reapply_color_dependents(import_id, entry, changed_tag, db):
    """Re-apply every config type with a material entry that references the
    changed colour configuration (VehicleConfigurationDataMaterial's use_*
    flags resolve against the CURRENT selection, so dependants must follow a
    colour change - e.g. the enyaq rim design option "Base Color")."""
    cname = changed_tag[:-len("Configurations")]
    for t in entry.get("types", []):
        if t["tag"] == changed_tag:
            continue
        mats = list(t.get("color_slots") or [])
        for o in t.get("options", []):
            mats.extend(o.get("materials", []))
        if not any(_config_ref_matches(m, cname) for m in mats):
            continue
        sel = entry.get("sel", {}).get(t["tag"], t.get("default", 0))
        if not isinstance(sel, int) or sel < 0:
            continue
        i3d_config_preview.apply_config(import_id, t, sel, db, entry=entry)


class FS25_OT_apply_store_config(Operator):
    """Apply a store-configuration option to this import (visual preview only)."""
    bl_idname = "fs25.apply_store_config"
    bl_label = "Apply store config option"
    bl_options = {'REGISTER', 'UNDO'}

    config_tag: StringProperty()
    option_index: IntProperty()

    @classmethod
    def description(cls, context, properties):
        """Colour options get their colour name + price as tooltip."""
        try:
            obj = context.active_object
            import_id = obj.get('_i3d_import_id') if obj else None
            store = json.loads(context.scene.get('_i3d_storecfg', '{}'))
            entry = store.get(import_id) or {}
            ct = next((t for t in entry.get("types", [])
                       if t["tag"] == properties.config_tag), None)
            if ct and ct.get("is_color"):
                opt = ct["options"][properties.option_index]
                templates = i3d_config_preview._load_templates_cached(
                    _fs25_data_base())
                nm = _color_option_label(opt, templates)
                pr = (opt.get("price") or "").strip()
                return nm + ((" - %s $" % pr) if pr else "")
        except Exception:
            pass
        return "Apply this store-configuration option (visual preview only)"

    def execute(self, context):
        obj = context.active_object
        import_id = obj.get('_i3d_import_id') if obj else None
        if import_id is None:
            self.report({'WARNING'}, "Select an imported object first.")
            return {'CANCELLED'}
        store = json.loads(context.scene.get('_i3d_storecfg', '{}'))
        entry = store.get(import_id)
        if not entry:
            self.report({'WARNING'}, "No store config loaded for this import.")
            return {'CANCELLED'}
        ct = next((t for t in entry["types"] if t["tag"] == self.config_tag), None)
        if ct is None:
            return {'CANCELLED'}
        entry["sel"][self.config_tag] = self.option_index
        # A new wheel configuration changes which tire brands are available, so
        # rebuild the brand list (and reset it to that config's default brand)
        # before loading the tires below.
        if self.config_tag == "wheelConfigurations":
            _update_brand_options(context, import_id, entry, self.option_index)
        context.scene['_i3d_storecfg'] = json.dumps(store)
        db = _fs25_data_base()
        i3d_config_preview.apply_config(import_id, ct, self.option_index, db,
                                        entry=entry)
        # Changing a colour re-colours every type that references it via
        # use_base_color / use_design_color_index / use_rim_color (enyaq rims
        # follow the base colour while their "Base Color" option is active).
        if ct.get("is_color"):
            _reapply_color_dependents(import_id, entry, self.config_tag, db)
        # A wheel configuration also swaps the actual tires/rims (external i3ds).
        # Load + place them for the chosen option (replaces previously loaded
        # wheels). Only happens on click, not on the default apply at XML load.
        if self.config_tag == "wheelConfigurations":
            try:
                ok = _reload_wheels(context, import_id, entry, self.option_index)
            except Exception as exc:
                self.report({'WARNING'}, "Tires not loaded: %r" % exc)
            else:
                if not ok:
                    self.report({'INFO'}, "Object changes applied; load the "
                                "vehicle Config XML and set the FS25 data base "
                                "to also load the tires.")
        return {'FINISHED'}


class FS25_OT_apply_rim_color(Operator):
    """Apply a rim colour (and its gloss) to this import's wheels + weights."""
    bl_idname = "fs25.apply_rim_color"
    bl_label = "Apply rim colour"
    bl_options = {'REGISTER', 'UNDO'}

    option_index: IntProperty()

    @classmethod
    def description(cls, context, properties):
        """Swatch buttons carry no text - the colour name is the tooltip."""
        try:
            obj = context.active_object
            import_id = obj.get('_i3d_import_id') if obj else None
            store = json.loads(context.scene.get('_i3d_storecfg', '{}'))
            rc = (store.get(import_id) or {}).get("rimcolor") or {}
            return _pretty_cfg_label(
                rc["options"][properties.option_index]["label"])
        except Exception:
            return "Apply this rim colour (visual preview only)"

    def execute(self, context):
        obj = context.active_object
        import_id = obj.get('_i3d_import_id') if obj else None
        if import_id is None:
            self.report({'WARNING'}, "Select an imported object first.")
            return {'CANCELLED'}
        store = json.loads(context.scene.get('_i3d_storecfg', '{}'))
        entry = store.get(import_id)
        rc = entry.get("rimcolor") if entry else None
        if not rc or not (0 <= self.option_index < len(rc.get("options", []))):
            return {'CANCELLED'}
        rc["sel"] = self.option_index
        context.scene['_i3d_storecfg'] = json.dumps(store)
        db = _fs25_data_base()
        i3d_config_preview.apply_rim_color(
            import_id, rc["options"][self.option_index]["template"], db)
        # use_rim_color references resolve against this section's selection.
        _reapply_color_dependents(import_id, entry, "rimColorConfigurations",
                                  db)
        return {'FINISHED'}


class FS25_OT_apply_tire_brand(Operator):
    """Switch the tire brand and reload the wheels (keeps the current size
    configuration and rim colour)."""
    bl_idname = "fs25.apply_tire_brand"
    bl_label = "Apply tire brand"
    bl_options = {'REGISTER', 'UNDO'}

    option_index: IntProperty()

    def execute(self, context):
        obj = context.active_object
        import_id = obj.get('_i3d_import_id') if obj else None
        if import_id is None:
            self.report({'WARNING'}, "Select an imported object first.")
            return {'CANCELLED'}
        store = json.loads(context.scene.get('_i3d_storecfg', '{}'))
        entry = store.get(import_id)
        brand = entry.get("brand") if entry else None
        if not brand or not (0 <= self.option_index < len(brand.get("options", []))):
            return {'CANCELLED'}
        brand["sel"] = self.option_index
        context.scene['_i3d_storecfg'] = json.dumps(store)
        wo = entry.get("wheelopts")
        if wo and wo.get("sel", -1) >= 0:
            opt = wo["options"][wo["sel"]]
            try:
                _reload_wheels(context, import_id, entry,
                               opt["config_index"], opt["dim_col"])
            except Exception as exc:
                self.report({'WARNING'}, "Tires not reloaded: %r" % exc)
        else:
            self.report({'INFO'}, "Brand set - click a wheel option to load tires.")
        return {'FINISHED'}


class FS25_OT_load_wheel_option(Operator):
    """Load a wheel option: one wheel configuration, expanded per size. Applies the
    config's object changes (fenders etc.), rebuilds the brand list for that size,
    and loads the tires/rims/hubs. Replaces the old wheel entry in the type menu so
    that size-only vehicles (skid-steers, sprayers) get a wheel menu too."""
    bl_idname = "fs25.load_wheel_option"
    bl_label = "Load wheel option"
    bl_options = {'REGISTER', 'UNDO'}

    option_index: IntProperty()

    def execute(self, context):
        obj = context.active_object
        import_id = obj.get('_i3d_import_id') if obj else None
        if import_id is None:
            self.report({'WARNING'}, "Select an imported object first.")
            return {'CANCELLED'}
        store = json.loads(context.scene.get('_i3d_storecfg', '{}'))
        entry = store.get(import_id)
        wo = entry.get("wheelopts") if entry else None
        if not wo or not (0 <= self.option_index < len(wo.get("options", []))):
            return {'CANCELLED'}
        opt = wo["options"][self.option_index]
        cfg_i, col = opt["config_index"], opt["dim_col"]
        wo["sel"] = self.option_index
        entry.setdefault("sel", {})["wheelConfigurations"] = cfg_i
        db = _fs25_data_base()
        # Object changes of the wheel config (e.g. fenders), if the config has any.
        ct = next((t for t in entry.get("types", [])
                   if t["tag"] == "wheelConfigurations"), None)
        if ct is not None:
            i3d_config_preview.apply_config(import_id, ct, cfg_i, db)
        # Brands depend on the selected config + size.
        _update_brand_options(context, import_id, entry, cfg_i, col)
        context.scene['_i3d_storecfg'] = json.dumps(store)
        try:
            ok = _reload_wheels(context, import_id, entry, cfg_i, col)
        except Exception as exc:
            self.report({'WARNING'}, "Tires not loaded: %r" % exc)
            return {'FINISHED'}
        if not ok:
            self.report({'INFO'}, "Load the vehicle Config XML and set the FS25 "
                        "data base to load the tires.")
        return {'FINISHED'}


class FS25_OT_unload_wheels(Operator):
    """Remove all loaded wheel parts of this import (tires, rims, weights,
    twin connectors, hubs) - back to the bare vehicle, the state right after
    import. The game loads wheels dynamically at runtime, so a re-export is
    cleanest WITHOUT them: no duplicate static wheels in the i3d and no
    shifted i3dMapping index paths"""
    bl_idname = "fs25.unload_wheels"
    bl_label = "Unload wheels"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        import_id = obj.get('_i3d_import_id') if obj else None
        if import_id is None:
            self.report({'WARNING'}, "Select an imported object first.")
            return {'CANCELLED'}
        store = json.loads(context.scene.get('_i3d_storecfg', '{}'))
        entry = store.get(import_id)
        if entry:
            wo = entry.get("wheelopts")
            if wo:
                wo["sel"] = -1
            # The brand list belongs to a loaded wheel config.
            entry.pop("brand", None)
            context.scene['_i3d_storecfg'] = json.dumps(store)
        n = i3d_wheel_loader.remove_wheels(import_id)
        # Crawler tracks are loaded dynamically by the game too, so 'None' must
        # strip them as well - otherwise a re-export would bake in the (wrapped)
        # track band.
        n_crawl = i3d_crawler_loader.remove_crawlers(import_id)
        i3d_wheel_loader._purge_empty_ref_collections()
        # Leave no residue: baked wheel meshes are now user-less, and their
        # pristine pre-bake datablocks are pinned only by our fake user
        # (marked _i3d_prebake_kept at bake time) - purge both.
        n_mesh = 0
        for me in list(bpy.data.meshes):
            try:
                if me.users == 0:
                    bpy.data.meshes.remove(me)
                    n_mesh += 1
            except Exception:
                pass
        for me in list(bpy.data.meshes):
            try:
                if (me.get("_i3d_prebake_kept") and me.use_fake_user
                        and me.users == 1):
                    bpy.data.meshes.remove(me)
                    n_mesh += 1
            except Exception:
                pass
        self.report({'INFO'},
                    "Removed %d wheel part(s), %d crawler object(s), purged "
                    "%d mesh(es)." % (n, n_crawl, n_mesh))
        return {'FINISHED'}


class FS25_OT_apply_config_set(Operator):
    """Apply a configuration set (preset): pin every controlled config type to the
    set's index and re-apply it. Replaces picking those sub-configs individually
    (e.g. one working-width preset sets folding/animation/width at once)."""
    bl_idname = "fs25.apply_config_set"
    bl_label = "Apply configuration set"
    bl_options = {'REGISTER', 'UNDO'}

    set_index: IntProperty()

    def execute(self, context):
        obj = context.active_object
        import_id = obj.get('_i3d_import_id') if obj else None
        if import_id is None:
            self.report({'WARNING'}, "Select an imported object first.")
            return {'CANCELLED'}
        store = json.loads(context.scene.get('_i3d_storecfg', '{}'))
        entry = store.get(import_id)
        sets = entry.get("sets") if entry else None
        if not sets or not (0 <= self.set_index < len(sets.get("options", []))):
            return {'CANCELLED'}
        sets["sel"] = self.set_index
        db = _fs25_data_base()
        by_tag = {t["tag"]: t for t in entry.get("types", [])}
        sel = entry.setdefault("sel", {})
        for cname, idx0 in sets["options"][self.set_index]["configs"].items():
            tag = cname + "Configurations"
            ct = by_tag.get(tag)
            if ct is not None:
                sel[tag] = idx0
                i3d_config_preview.apply_config(import_id, ct, idx0, db)
            if tag == "wheelConfigurations":
                # a preset can pin the wheel config - load its tires too
                sel[tag] = idx0
                try:
                    _reload_wheels(context, import_id, entry, idx0)
                except Exception as exc:
                    self.report({'WARNING'}, "Tires not loaded: %r" % exc)
        context.scene['_i3d_storecfg'] = json.dumps(store)
        self.report({'INFO'},
                    "Applied set: %s" % sets["options"][self.set_index]["label"])
        return {'FINISHED'}


class FS25_OT_toggle_cfg_section(Operator):
    """Collapse / expand a Store-Config section."""
    bl_idname = "fs25.toggle_cfg_section"
    bl_label = "Toggle section"
    bl_options = {'REGISTER', 'UNDO'}

    section: StringProperty()

    def execute(self, context):
        obj = context.active_object
        import_id = obj.get('_i3d_import_id') if obj else None
        store = json.loads(context.scene.get('_i3d_storecfg', '{}'))
        entry = store.get(import_id) if import_id else None
        if entry is None:
            return {'CANCELLED'}
        ex = entry.setdefault("expand", {})
        ex[self.section] = not ex.get(self.section, True)
        context.scene['_i3d_storecfg'] = json.dumps(store)
        return {'FINISHED'}


class FS25_PT_store_config(bpy.types.Panel):
    """Store-configuration preview - switch design / work-area / etc. live."""
    bl_idname = "FS25_PT_store_config"
    bl_label = "Store Config & i3dMappings"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "i3d Importer"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        import_id = obj.get('_i3d_import_id') if obj else None

        # i3dMappings: load a config XML for the active import (moved here from
        # the main panel - both are about loading extra XMLs).
        mbox = layout.box()
        mbox.label(text="i3dMappings", icon='FILE_TEXT')
        if import_id is not None:
            mbox.operator("fs25.load_config_xml", text="Load Config XML", icon='IMPORT')
        else:
            mbox.label(text="Select an imported object first", icon='INFO')
        if obj is not None and obj.get('I3D_XMLconfigID') is not None:
            mbox.prop(obj, "i3d_importer_mapping_id", text="Mapping ID")

        if import_id is None:
            return
        store = json.loads(context.scene.get('_i3d_storecfg', '{}'))
        entry = store.get(import_id)
        if not entry or not (entry.get("types") or entry.get("rimcolor")
                             or entry.get("wheelopts") or entry.get("sets")):
            layout.label(text="No store configurations loaded", icon='INFO')
            layout.label(text="Use Load Config XML above")
            return
        expand = entry.get("expand", {})

        # Show every config-hidden part again (mirrors the Snow / GE-invisible
        # Show-All buttons), and reset back to the default configuration (re-hides
        # non-default parts, incl. isSelectable="false" which options can't).
        _row = layout.row(align=True)
        _row.operator("fs25.config_show_all", text="Show All", icon='HIDE_OFF')
        _row.operator("fs25.config_reset_default", text="Defaults",
                      icon='LOOP_BACK')

        def _header(box, key, label):
            ex = expand.get(key, True)
            row = box.row(align=True)
            op = row.operator("fs25.toggle_cfg_section", text="", emboss=False,
                              icon='TRIA_DOWN' if ex else 'TRIA_RIGHT')
            op.section = key
            row.label(text=label)
            return ex

        # Nicer headers for the untitled base-game config types.
        _LABELS = {"wheelConfigurations": "Wheels",
                   "motorConfigurations": "Motor"}

        def _draw_color_type(t):
            # Colour configuration: swatch grid like the in-game colour
            # picker. Even a single colour is shown (the vehicle's colour is
            # a visible property; useDefaultColors palettes grow it later).
            vis = [(i, o) for i, o in enumerate(t["options"])
                   if o.get("selectable", True)]
            if not vis:
                return
            templates = i3d_config_preview._load_templates_cached(
                _fs25_data_base())
            box = layout.box()
            if not _header(box, t["tag"], _pretty_cfg_label(t["name"])):
                return
            sel = entry["sel"].get(t["tag"], t.get("default", 0))
            grid = box.grid_flow(row_major=True, columns=6, even_columns=True,
                                 even_rows=True, align=True)
            # Icon size follows the widget HEIGHT (verified 5.1: a scaled
            # grid draws proportionally larger icons), so scale_y doubles
            # the swatch size like the in-game picker.
            grid.scale_y = 2.0
            for i, opt in vis:
                rgb, finish = _color_swatch_data(opt, t, templates)
                op = grid.operator("fs25.apply_store_config", text="",
                                   icon_value=_swatch_icon_id(rgb, finish),
                                   depress=(i == sel))
                op.config_tag = t["tag"]
                op.option_index = i
            if isinstance(sel, int) and 0 <= sel < len(t["options"]):
                cur = t["options"][sel]
                nm = _color_option_label(cur, templates)
                pr = (cur.get("price") or "").strip()
                box.label(text=nm + ((" (%s $)" % pr) if pr else ""))

        def _draw_type(t):
            # Wheels are drawn by _draw_wheels (resolver-driven, size-expanded).
            if t["tag"] == "wheelConfigurations":
                return
            # rimColor is applied by the dedicated Rim Color section (wheel
            # loader) - drawing the parsed type too would duplicate it. The
            # type still serves as the use_rim_color reference source.
            if t["tag"] == "rimColorConfigurations":
                return
            # Sub-configs pinned by a configuration set are chosen via the set
            # chooser (_draw_sets), not individually.
            _s = entry.get("sets")
            if _s and t["tag"][:-len("Configurations")] in _s.get("controlled", []):
                return
            # Colour configurations render as a swatch grid, not text buttons.
            if t.get("is_color"):
                _draw_color_type(t)
                return
            # isSelectable="false" options exist in-game only as a dependency of
            # another config (e.g. NH/Steyr rim) - keep their index but do not
            # offer them as buttons. A type left with <=1 choosable option is not
            # a real choice, so the whole section is hidden.
            vis = [(i, o) for i, o in enumerate(t["options"])
                   if o.get("selectable", True)]
            if len(vis) <= 1:
                return
            box = layout.box()
            label = _LABELS.get(t["tag"]) or _pretty_cfg_label(t["name"])
            if _header(box, t["tag"], label):
                sel = entry["sel"].get(t["tag"], 0)
                # Yes/No (2-option) types render as a left/right group like the
                # in-game store; longer lists stay as a vertical column.
                container = (box.row(align=True) if len(vis) == 2
                             else box.column(align=True))
                for i, opt in vis:
                    op = container.operator("fs25.apply_store_config",
                                            text=_option_label(opt),
                                            depress=(i == sel))
                    op.config_tag = t["tag"]
                    op.option_index = i

        def _draw_brand():
            br = entry.get("brand")
            if br and br.get("options"):
                box = layout.box()
                if _header(box, "tireBrand", "Tire Brand"):
                    bsel = br.get("sel", 0)
                    col = box.column(align=True)
                    for opt in br["options"]:
                        op = col.operator("fs25.apply_tire_brand",
                                          text=opt["label"],
                                          depress=(opt["index"] == bsel))
                        op.option_index = opt["index"]

        def _draw_rimcolor():
            rc = entry.get("rimcolor")
            if not rc:
                return
            selectable = [i for i, o in enumerate(rc.get("options", []))
                          if o.get("selectable", True)]
            if len(selectable) <= 1:
                return  # no real colour choice (e.g. only the stock rim)
            box = layout.box()
            if _header(box, "rimColor", "Rim Color"):
                templates = i3d_config_preview._load_templates_cached(
                    _fs25_data_base())
                rsel = rc.get("sel", 0)
                grid = box.grid_flow(row_major=True, columns=6,
                                     even_columns=True, even_rows=True,
                                     align=True)
                grid.scale_y = 2.0
                for i in selectable:
                    opt = rc["options"][i]
                    pseudo = {"template": opt.get("template") or ""}
                    rgb, finish = _color_swatch_data(pseudo, {}, templates)
                    op = grid.operator("fs25.apply_rim_color", text="",
                                       icon_value=_swatch_icon_id(rgb, finish),
                                       depress=(i == rsel))
                    op.option_index = i
                if 0 <= rsel < len(rc["options"]):
                    box.label(text=_pretty_cfg_label(
                        rc["options"][rsel].get("label") or "Option"))

        def _draw_sets():
            s = entry.get("sets")
            if not s or len(s.get("options", [])) <= 1:
                return
            box = layout.box()
            # RAW title into the prettifier: pre-stripping "$l10n_" here left
            # the key's namespace prefix standing ("ui_platform" ->
            # "Ui_platform", Tigrecar); _pretty_cfg_label handles the full
            # key correctly ("$l10n_ui_platform" -> "Platform").
            title = (s.get("title") or "Configuration Set")
            if _header(box, "configSets", _pretty_cfg_label(title)):
                ssel = s.get("sel", 0)
                col = box.column(align=True)
                for i, opt in enumerate(s["options"]):
                    op = col.operator("fs25.apply_config_set", text=opt["label"],
                                      depress=(i == ssel))
                    op.set_index = i

        def _draw_wheels():
            wo = entry.get("wheelopts")
            if not wo or not wo.get("options"):
                return
            box = layout.box()
            if _header(box, "wheels", "Wheels"):
                wsel = wo.get("sel", -1)
                col = box.column(align=True)
                # "None" = the default state (no wheels loaded; the game loads
                # them dynamically at runtime). Clicking it removes every
                # loaded wheel part again - cleanest state for a re-export.
                col.operator("fs25.unload_wheels", text="None",
                             depress=(wsel < 0))
                for i, opt in enumerate(wo["options"]):
                    op = col.operator("fs25.load_wheel_option",
                                      text=_pretty_cfg_label(opt["label"]),
                                      depress=(i == wsel))
                    op.option_index = i

        # Order: Set chooser, Motor, Wheels, Tire Brand, remaining types, Rim Color.
        types = entry.get("types", [])
        by_tag = {t["tag"]: t for t in types}
        drawn = {"wheelConfigurations"}   # drawn via _draw_wheels
        _draw_sets()
        _tm = by_tag.get("motorConfigurations")
        if _tm:
            _draw_type(_tm)
            drawn.add("motorConfigurations")
        _draw_wheels()
        _draw_brand()
        for _t in types:
            if _t["tag"] not in drawn:
                _draw_type(_t)
        _draw_rimcolor()


def register():
    bpy.utils.register_class(FS25_OT_terrain_base_color_reset)
    bpy.utils.register_class(FS25I3DImporterPreferences)
    bpy.utils.register_class(IMPORT_OT_fs25_i3d)
    bpy.utils.register_class(FS25_OT_switch_materials)
    bpy.utils.register_class(FS25_OT_snow_heaps_show)
    bpy.utils.register_class(FS25_OT_snow_heaps_hide)
    bpy.utils.register_class(FS25_OT_invisible_ge_show)
    bpy.utils.register_class(FS25_OT_invisible_ge_hide)
    bpy.utils.register_class(FS25_OT_set_lod_level)
    bpy.utils.register_class(FS25_OT_config_show_all)
    bpy.utils.register_class(FS25_OT_config_reset_default)
    bpy.utils.register_class(FS25_OT_prepare_for_export)
    bpy.utils.register_class(FS25_OT_sync_debug_to_export_material)
    bpy.utils.register_class(FS25_OT_load_config_xml)
    bpy.utils.register_class(FS25_OT_apply_store_config)
    bpy.utils.register_class(FS25_OT_apply_rim_color)
    bpy.utils.register_class(FS25_OT_apply_tire_brand)
    bpy.utils.register_class(FS25_OT_load_wheel_option)
    bpy.utils.register_class(FS25_OT_unload_wheels)
    bpy.utils.register_class(FS25_OT_apply_config_set)
    bpy.utils.register_class(FS25_OT_toggle_cfg_section)
    bpy.types.Object.i3d_importer_mapping_id = StringProperty(
        name="Mapping ID", get=_i3d_mapping_id_get, set=_i3d_mapping_id_set)
    bpy.utils.register_class(FS25_PT_i3d_importer_panel)
    # Sub-panel order (top -> bottom in the N-Panel):
    #   1. Material Switch
    #   2. Material Settings
    #   3. Visibility
    #   4. Debug View
    bpy.utils.register_class(FS25_PT_material_switch)
    bpy.utils.register_class(FS25_PT_material_settings)
    bpy.utils.register_class(FS25_PT_visibility)
    bpy.utils.register_class(FS25_PT_debug_view)
    bpy.utils.register_class(FS25_PT_store_config)
    bpy.types.Scene.fs25_debug_mode = EnumProperty(
        name="FS25 Debug Mode",
        description="Show the standard material, a mask, or vertex colors",
        items=_debug_mode_items,
        update=_on_debug_mode_change,
    )
    bpy.types.Scene.fs25_debug_only_active = BoolProperty(
        name="Only active material",
        description="When set, debug mode changes apply only to the "
                    "active object's active material. Otherwise they "
                    "apply to every FS25 material in the file.",
        default=False,
        update=_on_debug_mode_change,
    )
    bpy.types.Scene.fs25_matswitch_only_selected = BoolProperty(
        name="Only selected",
        description="When set, the Material Switch buttons act only on the "
                    "selected meshes; otherwise on every imported mesh.",
        default=False,
    )
    bpy.types.Scene.fs25_tree_season = EnumProperty(
        name="Tree Season",
        description="Season shown by FS25 tree-branch debug materials "
                    "(treeBranchShader SEASONAL): switches the leaf diffuse "
                    "quadrant and toggles leaves (Winter = branches only). "
                    "Debug visualization only - the re-export material is "
                    "unaffected.",
        items=[
            ('SUMMER', "Summer", "Full green leaves"),
            ('AUTUMN', "Autumn", "Autumn-coloured leaves"),
            ('WINTER', "Winter", "No leaves (branches only)"),
            ('SPRING', "Spring", "Spring leaves"),
        ],
        default='SUMMER',
        update=_update_tree_season,
    )
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    global _color_icons
    if _color_icons is not None:
        import bpy.utils.previews
        bpy.utils.previews.remove(_color_icons)
        _color_icons = None
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    del bpy.types.Scene.fs25_tree_season
    del bpy.types.Scene.fs25_matswitch_only_selected
    del bpy.types.Scene.fs25_debug_only_active
    del bpy.types.Scene.fs25_debug_mode
    bpy.utils.unregister_class(FS25_PT_store_config)
    bpy.utils.unregister_class(FS25_PT_debug_view)
    bpy.utils.unregister_class(FS25_PT_visibility)
    bpy.utils.unregister_class(FS25_PT_material_settings)
    bpy.utils.unregister_class(FS25_PT_material_switch)
    bpy.utils.unregister_class(FS25_PT_i3d_importer_panel)
    del bpy.types.Object.i3d_importer_mapping_id
    bpy.utils.unregister_class(FS25_OT_load_config_xml)
    bpy.utils.unregister_class(FS25_OT_toggle_cfg_section)
    bpy.utils.unregister_class(FS25_OT_apply_config_set)
    bpy.utils.unregister_class(FS25_OT_load_wheel_option)
    bpy.utils.unregister_class(FS25_OT_unload_wheels)
    bpy.utils.unregister_class(FS25_OT_apply_tire_brand)
    bpy.utils.unregister_class(FS25_OT_apply_rim_color)
    bpy.utils.unregister_class(FS25_OT_apply_store_config)
    bpy.utils.unregister_class(FS25_OT_sync_debug_to_export_material)
    bpy.utils.unregister_class(FS25_OT_prepare_for_export)
    bpy.utils.unregister_class(FS25_OT_config_reset_default)
    bpy.utils.unregister_class(FS25_OT_config_show_all)
    bpy.utils.unregister_class(FS25_OT_invisible_ge_hide)
    bpy.utils.unregister_class(FS25_OT_invisible_ge_show)
    bpy.utils.unregister_class(FS25_OT_set_lod_level)
    bpy.utils.unregister_class(FS25_OT_snow_heaps_hide)
    bpy.utils.unregister_class(FS25_OT_snow_heaps_show)
    bpy.utils.unregister_class(FS25_OT_switch_materials)
    bpy.utils.unregister_class(IMPORT_OT_fs25_i3d)
    bpy.utils.unregister_class(FS25I3DImporterPreferences)
    bpy.utils.unregister_class(FS25_OT_terrain_base_color_reset)


if __name__ == "__main__":
    register()
    