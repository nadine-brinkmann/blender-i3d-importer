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
    "version": (0, 2, 0),
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
from bpy.props import (
    BoolProperty, StringProperty, EnumProperty, FloatVectorProperty,
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
            count, warnings = importer.import_i3d(
                self.filepath, report=self.report,
                apply_axis_correction=self.apply_axis_correction,
                auto_hide_invisible_shapes=self.auto_hide_invisible_shapes,
                build_pbr_debug_materials=self.build_pbr_debug_materials,
                attach_debug_materials_to_mesh=self.attach_debug_materials_to_mesh,
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

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

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

        swapped = 0
        skipped = 0
        for obj in context.selected_objects:
            if obj.type != 'MESH' or not obj.data:
                continue
            for slot in obj.material_slots:
                cur = slot.material
                if cur is None:
                    continue
                mid = cur.get('_i3d_material_id')
                cur_kind = cur.get('_i3d_material_kind')
                cur_uuid = cur.get('_i3d_import_uuid')
                if mid is None or cur_kind not in ('debug', 'export'):
                    skipped += 1
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

        self.report({'INFO'}, f"Material switch: {swapped} swapped, {skipped} skipped")
        return {'FINISHED'}


class FS25_OT_prepare_for_community_exporter(Operator):
    """Mirror the Giants-style re-export custom properties (customShader,
    customShaderVariation, customParameter_*, customTexture_*) into the
    community 'GIANTS I3D Community Exporter' addon's material.i3d_attributes,
    so imported models also round-trip through that exporter.

    The official Giants exporter reads textures/shader data from material
    custom IDProperties (which the importer already writes). The community
    exporter instead reads its own material.i3d_attributes PropertyGroup and
    the Principled BSDF sockets. This operator bridges the former into the
    latter for the selected meshes' materials (or all i3d materials when
    nothing is selected). Run it after switching slots to the Export
    materials (the debug materials carry no re-export properties).

    Safe to re-run; it overwrites only the bridged fields.
    """
    bl_idname = "fs25.prepare_for_community_exporter"
    bl_label = "Prepare for Community Exporter"
    bl_options = {'UNDO'}

    @staticmethod
    def _gather_materials(context):
        mats = []
        seen = set()
        sel_meshes = [o for o in context.selected_objects
                      if o.type == 'MESH' and o.data]
        if sel_meshes:
            for obj in sel_meshes:
                for slot in obj.material_slots:
                    m = slot.material
                    if m is not None and m.name not in seen:
                        seen.add(m.name)
                        mats.append(m)
        else:
            for m in bpy.data.materials:
                if (m.get('_i3d_material_id') is not None
                        and m.get('_i3d_material_kind') == 'export'
                        and m.name not in seen):
                    seen.add(m.name)
                    mats.append(m)
        return mats

    @staticmethod
    def _rename_glossmap_node(mat):
        """Label the image node feeding Principled 'Roughness' as 'glossmap'
        so the community exporter (which reads gloss from the Specular socket
        or a node named/labelled 'glossmap') picks it up. Giants ignores node
        names, so this is harmless for that path. Returns True if relabelled."""
        nt = mat.node_tree
        if nt is None:
            return False
        bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is None or 'Roughness' not in bsdf.inputs:
            return False
        sock = bsdf.inputs['Roughness']
        if not sock.is_linked:
            return False
        src = sock.links[0].from_node
        if src.type != 'TEX_IMAGE':
            return False
        src.label = 'glossmap'
        if src.name.lower() != 'glossmap':
            src.name = 'glossmap'
        return True

    def _bridge_material(self, mat):
        """Returns (status, detail). status in {'ok','no_shader','partial','skip'}."""
        attrs = mat.i3d_attributes  # caller guarantees the attribute exists

        # Standard gloss discoverability (independent of custom shader).
        self._rename_glossmap_node(mat)

        shader_path = mat.get('customShader')
        if not shader_path:
            # Plain material: BSDF Base Color / Normal already export via the
            # community exporter's PrincipledBSDFWrapper; gloss handled above.
            return ('skip', 'no customShader')

        shader_stem = os.path.splitext(os.path.basename(str(shader_path)))[0]

        # Game shader mode (importer materials reference $data/shaders/*).
        if attrs.use_custom_shaders:
            attrs.use_custom_shaders = False

        # Setting shader_name runs the community addon's ShaderManager, which
        # populates variations/params/textures. If the shader isn't in the
        # addon's known set (FS data path not configured, or unknown shader),
        # the setter resets the name back to '' — detect that.
        attrs.shader_name = shader_stem
        if attrs.shader_name != shader_stem:
            return ('no_shader',
                    f"shader '{shader_stem}' not found by the community addon "
                    f"(check its FS data path / installed shaders)")

        # Variation (must be set AFTER shader_name so the variation list exists).
        variation = mat.get('customShaderVariation')
        if variation:
            attrs.shader_variation_name = str(variation)

        # Custom parameters -> shader_material_params (only where the param
        # exists for this shader/variation and the value count matches).
        params = attrs.shader_material_params
        param_keys = set(params.keys())
        applied_params = 0
        for key in list(mat.keys()):
            if not key.startswith('customParameter_'):
                continue
            pname = key[len('customParameter_'):]
            if pname not in param_keys:
                continue
            try:
                vals = [float(x) for x in str(mat[key]).split()]
            except ValueError:
                continue
            try:
                cur_len = len(params[pname])
            except TypeError:
                cur_len = 1
            if vals and len(vals) == cur_len:
                params[pname] = vals
                applied_params += 1

        # Custom textures -> shader_material_textures[*].source.
        tex_by_name = {t.name: t for t in attrs.shader_material_textures}
        applied_tex = 0
        for key in list(mat.keys()):
            if not key.startswith('customTexture_'):
                continue
            tname = key[len('customTexture_'):]
            tex = tex_by_name.get(tname)
            src = str(mat[key])
            if tex is not None and src:
                tex.source = src
                applied_tex += 1

        return ('ok',
                f"shader='{shader_stem}'"
                + (f" var='{variation}'" if variation else "")
                + f", {applied_params} param(s), {applied_tex} texture(s)")

    def execute(self, context):
        mats = self._gather_materials(context)
        if not mats:
            self.report({'WARNING'}, "No i3d materials found "
                                     "(select imported meshes, or import first)")
            return {'CANCELLED'}

        # Community addon registers i3d_attributes on bpy.types.Material.
        if not hasattr(mats[0], 'i3d_attributes'):
            self.report({'ERROR'},
                        "Community 'GIANTS I3D Community Exporter' addon not "
                        "installed/enabled — material.i3d_attributes missing")
            return {'CANCELLED'}

        n_ok = n_noshader = n_skip = 0
        for mat in mats:
            try:
                status, detail = self._bridge_material(mat)
            except Exception as e:  # never let one material abort the batch
                self.report({'WARNING'}, f"'{mat.name}': {e}")
                continue
            if status == 'ok':
                n_ok += 1
            elif status == 'no_shader':
                n_noshader += 1
                self.report({'WARNING'}, f"'{mat.name}': {detail}")
            else:
                n_skip += 1

        self.report({'INFO'},
                    f"Community export prep: {n_ok} bridged, "
                    f"{n_noshader} shader-not-found, {n_skip} skipped "
                    f"(of {len(mats)} material(s))")
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


class FS25_OT_sync_debug_to_export_material(bpy.types.Operator):
    """Sync the values of every fs25_param:* slider node in the active
    debug material back to the customParameter_* IDProperties of its
    paired export material. Required before re-export to persist any
    changes made via the FS25 Material Settings panel - the re-export
    reads from the export material, not from the debug node tree."""
    bl_idname = "fs25.sync_debug_to_export_material"
    bl_label = "Sync to Export Material"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.active_material is None:
            return False
        return obj.active_material.get('_i3d_material_kind') == 'debug'

    def execute(self, context):
        mat = context.active_object.active_material
        mid = mat.get('_i3d_material_id')
        imp_uuid = mat.get('_i3d_import_uuid')
        if mid is None:
            self.report({'WARNING'},
                        "Active material is not an FS25 import")
            return {'CANCELLED'}

        # Find the paired export material via (material_id, uuid, kind).
        pair = None
        for m in bpy.data.materials:
            if (m.get('_i3d_material_id') == mid
                    and m.get('_i3d_import_uuid') == imp_uuid
                    and m.get('_i3d_material_kind') == 'export'):
                pair = m
                break
        if pair is None:
            self.report({'WARNING'},
                        f"No export counterpart for '{mat.name}'")
            return {'CANCELLED'}

        if not mat.use_nodes or mat.node_tree is None:
            self.report({'WARNING'},
                        "Active debug material has no node tree")
            return {'CANCELLED'}

        prefix = 'fs25_param:'

        # Gather all fs25_param:* nodes, group them by fs25_xml_param
        # (the XML <CustomParameter name>). Each group has one or more
        # slots: 'all' for unsplit, or any combination of 'rgb' / 'w' /
        # 'alpha' / 'x' / 'y' / 'z' for split parameters. The groups
        # are then combined per group into one customParameter_<name>
        # string and written to the paired export material.
        groups = {}
        for node in mat.node_tree.nodes:
            if not node.name.startswith(prefix):
                continue
            slider_name = node.name[len(prefix):]
            xml_param = node.get('fs25_xml_param')
            if xml_param is None:
                xml_param = slider_name  # backward-compat fallback
            xml_slot = node.get('fs25_xml_slot') or 'all'
            mode = node.get('fs25_serialize')
            if mode is None:
                # Backward-compat: infer from node type.
                if node.type == 'RGB':
                    mode = 'rgba'
                elif node.type == 'VALUE':
                    mode = 'float'
                else:
                    continue
            groups.setdefault(xml_param, {})[xml_slot] = (node, mode)

        n_synced = 0
        n_skipped = 0
        for xml_param, slots in groups.items():
            try:
                serialized = _serialize_param_group(slots)
            except Exception as e:
                self.report({'WARNING'},
                            f"Failed to combine '{xml_param}': {e}")
                n_skipped += 1
                continue
            if serialized is None:
                n_skipped += 1
                continue
            pair[f'customParameter_{xml_param}'] = serialized
            n_synced += 1

        msg = f"Synced {n_synced} parameter(s) to '{pair.name}'"
        if n_skipped:
            msg += f" ({n_skipped} skipped)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class FS25_PT_i3d_importer_panel(bpy.types.Panel):
    """N-Panel entry in the 3D Viewport sidebar with material-switch buttons."""
    bl_idname = "FS25_PT_i3d_importer_panel"
    bl_label = "i3d Importer"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "i3d Importer"

    def draw(self, context):
        layout = self.layout

        # Material switch section
        box = layout.box()
        box.label(text="Material Switch", icon='MATERIAL')
        box.label(text="Affects selected meshes:")

        row = box.row(align=True)
        op_dbg = row.operator("fs25.switch_materials", text="Debug")
        op_dbg.target_kind = 'debug'
        op_exp = row.operator("fs25.switch_materials", text="Export")
        op_exp.target_kind = 'export'

        row = box.row()
        op_tog = row.operator("fs25.switch_materials", text="Toggle", icon='ARROW_LEFTRIGHT')
        op_tog.target_kind = 'toggle'

        # Community exporter round-trip section
        box = layout.box()
        box.label(text="Community Exporter", icon='EXPORT')
        box.label(text="Bridge i3d props -> i3d_attributes:")
        box.operator("fs25.prepare_for_community_exporter",
                     text="Prepare for Community Exporter")


class FS25_PT_material_settings(bpy.types.Panel):
    """Sub-panel showing FS25 custom-parameter sliders for the active material.

    The PBR debug material exposes each FS25 custom parameter as a labeled
    Value/RGB node with name prefix 'fs25_param:'. This panel scans the
    active material's node tree for those, groups them via
    material_inventory.lookup_param(), and renders sliders/color pickers
    grouped by topic (Vehicle Brand Color, Clear Coat, Multitint, ...).
    """
    bl_idname = "FS25_PT_material_settings"
    bl_label = "FS25 Material Settings"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "i3d Importer"
    bl_parent_id = "FS25_PT_i3d_importer_panel"

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
        layout.operator("fs25.sync_debug_to_export_material",
                        icon='FILE_REFRESH')


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
    bl_label = "FS25 Debug View"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "i3d Importer"
    bl_parent_id = "FS25_PT_i3d_importer_panel"

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
        self.report({'INFO'}, f"Shown {n} snow/ice mesh(es)")
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
        self.report({'INFO'}, f"Hidden {n} snow/ice mesh(es)")
        return {'FINISHED'}


class FS25_PT_snow_heaps(bpy.types.Panel):
    """Sub-panel: show/hide all snow + ice meshes in the scene.

    snowHeapShader.xml covers both snow heaps and icicles (via its
    `icicle` variation), so this panel acts on both."""
    bl_idname = "FS25_PT_snow_heaps"
    bl_label = "FS25 Snow + Ice"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "i3d Importer"
    bl_parent_id = "FS25_PT_i3d_importer_panel"

    def draw(self, context):
        layout = self.layout
        layout.label(text="Unhide the snow features before export",
                     icon='INFO')
        count = sum(1 for o in context.scene.objects
                    if o.get('_i3d_is_snow_heap'))
        if count == 0:
            layout.label(text="No snow / ice meshes in scene", icon='INFO')
            return
        layout.label(text=f"{count} snow / ice mesh(es) found", icon='FREEZE')
        row = layout.row(align=True)
        row.operator("fs25.snow_heaps_show", text="Show All",
                     icon='HIDE_OFF')
        row.operator("fs25.snow_heaps_hide", text="Hide All",
                     icon='HIDE_ON')


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
        self.report({'INFO'}, f"Shown {n} GE-invisible object(s)")
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
        self.report({'INFO'}, f"Hidden {n} GE-invisible object(s)")
        return {'FINISHED'}


class FS25_PT_invisible_ge_objects(bpy.types.Panel):
    """Sub-panel: show/hide all objects marked invisible in the
    Giants Editor (visibility=false or nonRenderable=true without
    terrainDecal=true). Flagged via the _i3d_invisible_in_ge custom
    property on import."""
    bl_idname = "FS25_PT_invisible_ge_objects"
    bl_label = "FS25 Invisible GE-objects"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "i3d Importer"
    bl_parent_id = "FS25_PT_i3d_importer_panel"

    def draw(self, context):
        layout = self.layout
        layout.label(text="Unhide the hidden GE objects before export",
                     icon='INFO')
        count = sum(1 for o in context.scene.objects
                    if o.get('_i3d_invisible_in_ge'))
        if count == 0:
            layout.label(text="No GE-invisible objects in scene",
                         icon='INFO')
            return
        layout.label(text=f"{count} GE-invisible object(s) found",
                     icon='GHOST_ENABLED')
        row = layout.row(align=True)
        row.operator("fs25.invisible_ge_show", text="Show All",
                     icon='HIDE_OFF')
        row.operator("fs25.invisible_ge_hide", text="Hide All",
                     icon='HIDE_ON')


def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_fs25_i3d.bl_idname, text="Farming Simulator i3d (.i3d)")


def register():
    bpy.utils.register_class(FS25_OT_terrain_base_color_reset)
    bpy.utils.register_class(FS25I3DImporterPreferences)
    bpy.utils.register_class(IMPORT_OT_fs25_i3d)
    bpy.utils.register_class(FS25_OT_switch_materials)
    bpy.utils.register_class(FS25_OT_prepare_for_community_exporter)
    bpy.utils.register_class(FS25_OT_snow_heaps_show)
    bpy.utils.register_class(FS25_OT_snow_heaps_hide)
    bpy.utils.register_class(FS25_OT_invisible_ge_show)
    bpy.utils.register_class(FS25_OT_invisible_ge_hide)
    bpy.utils.register_class(FS25_OT_sync_debug_to_export_material)
    bpy.utils.register_class(FS25_PT_i3d_importer_panel)
    # Sub-panel order (top -> bottom in the N-Panel):
    #   1. FS25 Snow + Ice
    #   2. FS25 Invisible GE-objects
    #   3. FS25 Material Settings
    #   4. FS25 Debug View
    bpy.utils.register_class(FS25_PT_snow_heaps)
    bpy.utils.register_class(FS25_PT_invisible_ge_objects)
    bpy.utils.register_class(FS25_PT_material_settings)
    bpy.utils.register_class(FS25_PT_debug_view)
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
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    del bpy.types.Scene.fs25_debug_only_active
    del bpy.types.Scene.fs25_debug_mode
    bpy.utils.unregister_class(FS25_PT_debug_view)
    bpy.utils.unregister_class(FS25_PT_material_settings)
    bpy.utils.unregister_class(FS25_PT_invisible_ge_objects)
    bpy.utils.unregister_class(FS25_PT_snow_heaps)
    bpy.utils.unregister_class(FS25_PT_i3d_importer_panel)
    bpy.utils.unregister_class(FS25_OT_sync_debug_to_export_material)
    bpy.utils.unregister_class(FS25_OT_invisible_ge_hide)
    bpy.utils.unregister_class(FS25_OT_invisible_ge_show)
    bpy.utils.unregister_class(FS25_OT_snow_heaps_hide)
    bpy.utils.unregister_class(FS25_OT_snow_heaps_show)
    bpy.utils.unregister_class(FS25_OT_prepare_for_community_exporter)
    bpy.utils.unregister_class(FS25_OT_switch_materials)
    bpy.utils.unregister_class(IMPORT_OT_fs25_i3d)
    bpy.utils.unregister_class(FS25I3DImporterPreferences)
    bpy.utils.unregister_class(FS25_OT_terrain_base_color_reset)


if __name__ == "__main__":
    register()
