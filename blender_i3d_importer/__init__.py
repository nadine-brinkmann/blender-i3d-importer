"""
FS25 i3d Importer for Blender

Imports meshes from FS22/FS25 *.i3d.shapes files into Blender, including materials, all UVs, vertex colors, custom properties etc.
Optionally creates additional materials which give the same look and feel as the materials in the Giants Editor. These additional 
materials cannot be re-exported, but the standard imported materials can (therefore 2 separate sets of materials are created
when you use this option)
Uses i3d-to-objx as a subprocess for extracting the information from the *.i3d.shapes files.

"""

bl_info = {
    "name": "i3d Importer",
    "author": "Nadine Brinkmann",
    "version": (0, 1, 0),
    "blender": (5, 1, 0),
    "location": "File > Import > Farming Simulator i3d (.i3d)",
    "description": "Imports meshes from FS22/FS25 *.i3d.shapes files, incl materials, UVs, vertex colors, custom properties...",
    "category": "Import-Export",
}

import bpy
import os
from bpy.props import BoolProperty, StringProperty, EnumProperty
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
    importlib.reload(objx_parser)
    importlib.reload(material_inventory)
    importlib.reload(recipe_loader)
    importlib.reload(importer)
else:
    from . import i3d_attr_mapping
    from . import i3d_shader_parser
    from . import i3d_xml_parser
    from . import objx_parser
    from . import material_inventory
    from . import recipe_loader
    from . import importer


# Defaults — passed to importer.import_i3d() by the operator. 
# The subprocess exe ships with the add-on (self-contained exe).
DEFAULT_TOOL_PATH      = os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    "bin",
    "i3dToObjx.exe",
)
DEFAULT_FS25_DATA_BASE = ""
DEFAULT_EXPORT_DIR     = ""

# Master blend with node-group snippets for PBR debug materials.
DEFAULT_SNIPPETS_BLEND_PATH = os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    "lib",
    "fs25_node_snippets.blend",
)


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

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "fs25_data_base")
        layout.prop(self, "export_dir")
        layout.prop(self, "apply_axis_correction_default")
        layout.prop(self, "auto_hide_invisible_shapes_default")
        layout.prop(self, "build_pbr_debug_materials_default")
        layout.prop(self, "attach_debug_materials_to_mesh_default")


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

    def invoke(self, context, event):
        prefs = context.preferences.addons[__package__].preferences
        self.apply_axis_correction = prefs.apply_axis_correction_default
        self.auto_hide_invisible_shapes = prefs.auto_hide_invisible_shapes_default
        self.build_pbr_debug_materials = prefs.build_pbr_debug_materials_default
        self.attach_debug_materials_to_mesh = prefs.attach_debug_materials_to_mesh_default
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
                tool_path=DEFAULT_TOOL_PATH,
                fs25_data_base=prefs.fs25_data_base,
                export_dir=prefs.export_dir,
                snippets_blend_path=DEFAULT_SNIPPETS_BLEND_PATH,
            )
            if warnings > 0:
                self.report({'INFO'}, f"FS25 i3d Import: {count} shape(s) imported ({warnings} warning(s) - see log)")
            else:
                self.report({'INFO'}, f"FS25 i3d Import: {count} shape(s) imported")
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
        # Lookup: (material_id, kind) -> material
        lookup = {}
        for m in bpy.data.materials:
            mid = m.get('_i3d_material_id')
            kind = m.get('_i3d_material_kind')
            if mid is not None and kind in ('debug', 'export'):
                lookup[(int(mid), kind)] = m

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
                pair = lookup.get((int(mid), want))
                if pair is None:
                    self.report({'WARNING'},
                                f"No {want} counterpart for material '{cur.name}' "
                                f"(id={mid}) found")
                    skipped += 1
                    continue
                slot.material = pair
                swapped += 1

        self.report({'INFO'}, f"Material switch: {swapped} swapped, {skipped} skipped")
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
    """Make all snow/ice objects visible (clears hide_viewport + hide_render)."""
    bl_idname = "fs25.snow_heaps_show"
    bl_label = "Show All Snow + Ice"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = 0
        for obj in context.scene.objects:
            if obj.get('_i3d_is_snow_heap'):
                obj.hide_viewport = False
                obj.hide_render = False
                n += 1
        self.report({'INFO'}, f"Shown {n} snow/ice mesh(es)")
        return {'FINISHED'}


class FS25_OT_snow_heaps_hide(bpy.types.Operator):
    """Hide all snow/ice objects (sets hide_viewport + hide_render)."""
    bl_idname = "fs25.snow_heaps_hide"
    bl_label = "Hide All Snow + Ice"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = 0
        for obj in context.scene.objects:
            if obj.get('_i3d_is_snow_heap'):
                obj.hide_viewport = True
                obj.hide_render = True
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


def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_fs25_i3d.bl_idname, text="Farming Simulator i3d (.i3d)")


def register():
    bpy.utils.register_class(FS25I3DImporterPreferences)
    bpy.utils.register_class(IMPORT_OT_fs25_i3d)
    bpy.utils.register_class(FS25_OT_switch_materials)
    bpy.utils.register_class(FS25_OT_snow_heaps_show)
    bpy.utils.register_class(FS25_OT_snow_heaps_hide)
    bpy.utils.register_class(FS25_PT_i3d_importer_panel)
    # Sub-panel order (top -> bottom in the N-Panel):
    #   1. FS25 Snow + Ice
    #   2. FS25 Material Settings
    #   3. FS25 Debug View
    bpy.utils.register_class(FS25_PT_snow_heaps)
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
    bpy.utils.unregister_class(FS25_PT_snow_heaps)
    bpy.utils.unregister_class(FS25_PT_i3d_importer_panel)
    bpy.utils.unregister_class(FS25_OT_snow_heaps_hide)
    bpy.utils.unregister_class(FS25_OT_snow_heaps_show)
    bpy.utils.unregister_class(FS25_OT_switch_materials)
    bpy.utils.unregister_class(IMPORT_OT_fs25_i3d)
    bpy.utils.unregister_class(FS25I3DImporterPreferences)


if __name__ == "__main__":
    register()
