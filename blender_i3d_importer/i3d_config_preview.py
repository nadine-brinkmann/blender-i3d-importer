"""Live preview of vehicle/placeable store configurations in Blender.

Resolves ``objectChange`` node ids to the imported objects and applies a selected
configuration option, reproducing the game's apply order: every non-selected
option gets its ``Inactive`` values, then the selected option its ``Active``
values last (so it overrides). Visual only - it toggles Blender viewport
visibility and local transforms; nothing is persisted for export.

Mirrors dataS/scripts/utils/ObjectChangeUtil.lua (updateObjectChanges /
setObjectChanges).
"""

import json
import math
import bpy

from . import i3d_material_templates


def resolve_node_objects(import_id):
    """Build ``node_id -> object`` for one import.

    The node ids in objectChange are i3dMapping ids; "Load Config XML" stored
    each id as the object's ``I3D_XMLconfigID``, so we read that back.
    """
    res = {}
    for o in bpy.data.objects:
        if o.get("_i3d_import_id") != import_id:
            continue
        nid = o.get("I3D_XMLconfigID")
        if nid is not None and nid not in res:
            res[nid] = o
    return res


def _vec3(s):
    vals = [float(x) for x in s.replace(",", " ").split()[:3]]
    while len(vals) < 3:
        vals.append(0.0)
    return vals


def _set_shader_param(obj, param_name, value_str):
    """Best-effort: set the fs25_param node(s) for *param_name* on the object's
    debug materials from a vec4 string. Skips silently when the param has no node
    (re-export material, or a shader param that is not a CustomParameter).
    """
    vals = [float(x) for x in value_str.replace(",", " ").split()]
    while len(vals) < 4:
        vals.append(0.0)
    slot_idx = {"x": 0, "y": 1, "z": 2, "w": 3, "alpha": 3}
    for ms in obj.material_slots:
        mat = ms.material
        if mat is None or not mat.use_nodes:
            continue
        for n in mat.node_tree.nodes:
            if not n.name.startswith("fs25_param:"):
                continue
            if n.get("fs25_xml_param") != param_name:
                continue
            slot = n.get("fs25_xml_slot", "all")
            if n.bl_idname == "ShaderNodeRGB":
                dv = n.outputs[0].default_value
                if slot == "rgb":
                    dv[0], dv[1], dv[2] = vals[0], vals[1], vals[2]
                else:
                    dv[0], dv[1], dv[2], dv[3] = vals[0], vals[1], vals[2], vals[3]
            else:  # ShaderNodeValue (scalar slice)
                n.outputs[0].default_value = vals[slot_idx.get(slot, 0)]


def _apply(node, attrs, id_to_obj, active):
    obj = id_to_obj.get(node)
    if obj is None:
        return
    suf = "Active" if active else "Inactive"

    vis = attrs.get("visibility" + suf)
    if vis is not None:
        hidden = (vis != "true")
        obj.hide_viewport = hidden
        obj.hide_render = hidden

    # translation/rotation/scale are absolute local values (verified to match the
    # imported objects' i3d-local frame; no axis conversion needed).
    tr = attrs.get("translation" + suf)
    if tr is not None:
        obj.location = _vec3(tr)
    ro = attrs.get("rotation" + suf)
    if ro is not None:
        obj.rotation_euler = [math.radians(v) for v in _vec3(ro)]
    sc = attrs.get("scale" + suf)
    if sc is not None:
        obj.scale = _vec3(sc)
    # shaderParameter: best-effort. Only the debug material carries fs25_param
    # nodes; shared materials make this per-material, not per-shape.
    sp = attrs.get("shaderParameter")
    if sp is not None:
        val = attrs.get("shaderParameter" + suf)
        if val is not None:
            _set_shader_param(obj, sp, val)


def apply_option(options, selected_index, id_to_obj):
    """Apply one configuration type, reproducing the game's order.

    ``options`` is a list of dicts ``{"label": str, "changes": [{"node", "attrs"}]}``.
    Non-selected options are applied with their Inactive values first, then the
    selected option with its Active values last so it overrides.
    """
    for i, opt in enumerate(options):
        if i == selected_index:
            continue
        for ch in opt["changes"]:
            _apply(ch["node"], ch["attrs"], id_to_obj, active=False)
    if 0 <= selected_index < len(options):
        for ch in options[selected_index]["changes"]:
            _apply(ch["node"], ch["attrs"], id_to_obj, active=True)


# ---------------------------------------------------------------------------
# Material-swap configurations (design / Black Beauty): set the slot material's
# fs25_param nodes from a resolved material template. Visual preview only;
# debug-material parameters, shared per material.
# ---------------------------------------------------------------------------

_TEMPLATE_CACHE = {}


def _load_templates_cached(data_base):
    if not data_base:
        return {}
    if data_base not in _TEMPLATE_CACHE:
        _TEMPLATE_CACHE[data_base] = i3d_material_templates.load_templates(data_base)
    return _TEMPLATE_CACHE[data_base]


def _import_materials_by_slot(import_id):
    by_slot = {}
    seen = set()
    for o in bpy.data.objects:
        if o.get("_i3d_import_id") != import_id:
            continue
        for ms in o.material_slots:
            mat = ms.material
            if mat is None or mat.name in seen:
                continue
            seen.add(mat.name)
            slot = mat.get("materialSlotName")
            if slot:
                by_slot.setdefault(slot, []).append(mat)
    return by_slot


def _set_mat_param(mat, param, value_str):
    """Set the fs25_param node(s) for *param* on a single material."""
    if not mat.use_nodes:
        return
    vals = [float(x) for x in value_str.replace(",", " ").split()]
    if not vals:
        return
    slot_idx = {"x": 0, "y": 1, "z": 2, "w": 3, "alpha": 3}
    for n in mat.node_tree.nodes:
        if not n.name.startswith("fs25_param:"):
            continue
        if n.get("fs25_xml_param") != param:
            continue
        if n.bl_idname == "ShaderNodeRGB":
            dv = n.outputs[0].default_value
            for i in range(min(3, len(vals))):
                dv[i] = vals[i]
        else:
            idx = slot_idx.get(n.get("fs25_xml_slot", "all"), 0)
            n.outputs[0].default_value = vals[idx] if idx < len(vals) else vals[0]


def _capture_orig(mat):
    """Snapshot a material's current fs25_param values once (for restore)."""
    if mat.get("_i3d_cfg_orig") is not None or not mat.use_nodes:
        return
    orig = {}
    for n in mat.node_tree.nodes:
        if not n.name.startswith("fs25_param:"):
            continue
        p = n.get("fs25_xml_param")
        if p not in i3d_material_templates.PARAM_ATTRS or p in orig:
            continue
        if n.bl_idname == "ShaderNodeRGB":
            dv = n.outputs[0].default_value
            orig[p] = "%g %g %g" % (dv[0], dv[1], dv[2])
        else:
            orig[p] = "%g" % n.outputs[0].default_value
    mat["_i3d_cfg_orig"] = json.dumps(orig)


def _restore_mat(mat):
    raw = mat.get("_i3d_cfg_orig")
    if not raw:
        return
    try:
        orig = json.loads(raw)
    except (ValueError, TypeError):
        return
    for p, v in orig.items():
        _set_mat_param(mat, p, v)


def _apply_template_to_mat(mat, resolved, color_only):
    cs = resolved.get("colorScale")
    if cs is not None:
        _set_mat_param(mat, "colorScale", cs)
    if not color_only:
        for p in i3d_material_templates.PARAM_ATTRS:
            if p != "colorScale" and p in resolved:
                _set_mat_param(mat, p, resolved[p])


def capture_material_originals(import_id, types):
    """Snapshot originals for every material referenced by a material config."""
    by_slot = _import_materials_by_slot(import_id)
    for t in types:
        for o in t.get("options", []):
            for m in o.get("materials", []):
                for mat in by_slot.get(m["slot"], []):
                    _capture_orig(mat)


def _apply_materials(import_id, options, selected_index, data_base):
    by_slot = _import_materials_by_slot(import_id)
    # restore every slot this config type touches, then apply the selected option
    touched = {m["slot"] for o in options for m in o.get("materials", [])}
    for slot in touched:
        for mat in by_slot.get(slot, []):
            _restore_mat(mat)
    if 0 <= selected_index < len(options):
        templates = _load_templates_cached(data_base)
        for m in options[selected_index].get("materials", []):
            resolved = i3d_material_templates.resolve_template(m["template"], templates)
            if not resolved:
                continue
            for mat in by_slot.get(m["slot"], []):
                _apply_template_to_mat(mat, resolved, m.get("color_only", False))


def apply_config(import_id, ct, selected_index, data_base=None):
    """Apply one configuration type: objectChanges and material swaps."""
    options = ct["options"]
    id2obj = resolve_node_objects(import_id)
    apply_option(options, selected_index, id2obj)
    if any(o.get("materials") for o in options):
        _apply_materials(import_id, options, selected_index, data_base)
