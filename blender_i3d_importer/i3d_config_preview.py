"""Live preview of vehicle/placeable store configurations in Blender.

Resolves ``objectChange`` node ids to the imported objects and applies a selected
configuration option, reproducing the game's apply order: every non-selected
option gets its ``Inactive`` values, then the selected option its ``Active``
values last (so it overrides). Visual only - it toggles Blender viewport
visibility and local transforms; nothing is persisted for export.

Mirrors dataS/scripts/utils/ObjectChangeUtil.lua (updateObjectChanges /
setObjectChanges).

Colour configurations (baseColor / designColor%d / rimColor / wrappingColor,
placeable color) are applied by ``apply_color_config``: the selected option's
colour/template is painted onto the type-level target slots and the option's
own material entries, mirroring VehicleConfigurationItemColor.onPostLoad +
VehicleConfigurationDataMaterial.onLoadFinished (incl. the use_base_color /
use_design_color_index / use_rim_color references against the CURRENT
selection of the referenced configuration).
"""

import json
import math
import os
import bpy
import mathutils

from . import i3d_config_parser
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


def _material_pair(mat, kind):
    """The material of *mat*'s import pair with the given ``kind``
    ('debug' / 'export'), matched on (_i3d_material_id, _i3d_import_uuid).
    Returns *mat* itself when it already has that kind, None when the
    counterpart does not exist.
    """
    if mat is None:
        return None
    if mat.get("_i3d_material_kind") == kind:
        return mat
    mid = mat.get("_i3d_material_id")
    uuid = mat.get("_i3d_import_uuid")
    if mid is None:
        return None
    for m in bpy.data.materials:
        if (m.get("_i3d_material_kind") == kind
                and m.get("_i3d_material_id") == mid
                and m.get("_i3d_import_uuid") == uuid):
            return m
    return None


def debug_pair(mat):
    """The DEBUG material of *mat*'s pair.

    ALL preview state (config colours, material templates, shader params) lives
    on the debug material - it is the only one carrying fs25_param:* nodes. The
    export material is never touched by the preview; changes reach it solely via
    the Sync button, which keeps a re-export byte-identical to the source until
    the user deliberately pushes something across.

    Which of the two sits in a mesh slot depends on the
    ATTACH_DEBUG_MATERIALS_TO_MESH preference, so every preview pass resolves
    the slot material to its debug counterpart instead of assuming the debug
    material is the attached one (that assumption silently disabled the whole
    colour picker when the preference was off).
    """
    return _material_pair(mat, "debug")


def _set_shader_param(obj, param_name, value_str):
    """Best-effort: set the fs25_param node(s) for *param_name* on the object's
    debug materials from a vec4 string. Skips silently when the param has no node
    (a shader param that is not a CustomParameter).
    """
    vals = [float(x) for x in value_str.replace(",", " ").split()]
    while len(vals) < 4:
        vals.append(0.0)
    slot_idx = {"x": 0, "y": 1, "z": 2, "w": 3, "alpha": 3}
    for ms in obj.material_slots:
        mat = debug_pair(ms.material)
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


_M4 = mathutils.Matrix.Rotation(math.radians(90), 4, 'X')
_M4I = _M4.inverted()


def _set_local_transform(obj, tr, ro, sc, restore_tr=False, restore_ro=False,
                         restore_sc=False):
    """Apply objectChange translation/rotation/scale as the node's i3d-LOCAL
    transform components (each may be None = keep current).

    Imported objects can carry a non-identity ``matrix_parent_inverse`` (the
    importer parents keep-world; ``location`` then holds world-ish values).
    Setting ``obj.location``/``rotation_euler`` directly is only correct for
    identity parent-inverse - on MT655 that collapsed wheelBackLeftTwin and
    the fender parts to the world origin ('Strays unter dem Traktor').

    Method: recover the current i3d-local matrix from the Blender-local one
    (``parent_inverse @ matrix_basis``) via the importer's M-conjugation
    (M = +90deg X, importer._set_transform), replace only the given
    components, and write back through ``matrix_basis``. Pure object-local
    math - no parent lookups, no depsgraph refresh needed, correct for both
    parenting conventions."""
    mpi = obj.matrix_parent_inverse
    if "_i3d_orig_basis" not in obj:
        # First config touch: snapshot the original basis so an objectChange
        # that defines only ONE side can fall back to the import state for
        # the other (game semantics, see easyShed01/visibility).
        obj["_i3d_orig_basis"] = [c for row in obj.matrix_basis for c in row]
    L_i3d = _M4I @ (mpi @ obj.matrix_basis) @ _M4
    t, r, s = L_i3d.decompose()
    if restore_tr or restore_ro or restore_sc:
        ob = list(obj["_i3d_orig_basis"])
        OB = mathutils.Matrix((ob[0:4], ob[4:8], ob[8:12], ob[12:16]))
        t0, r0, s0 = (_M4I @ (mpi @ OB) @ _M4).decompose()
    if tr is not None:
        t = mathutils.Vector(_vec3(tr))
    elif restore_tr:
        t = t0
    if ro is not None:
        r = mathutils.Euler([math.radians(v) for v in _vec3(ro)],
                            'XYZ').to_quaternion()
    elif restore_ro:
        r = r0
    if sc is not None:
        s = mathutils.Vector(_vec3(sc))
    elif restore_sc:
        s = s0
    L_new = (mathutils.Matrix.Translation(t) @ r.to_matrix().to_4x4()
             @ mathutils.Matrix.Diagonal(s.to_4d()))
    obj.matrix_basis = mpi.inverted() @ (_M4 @ L_new @ _M4I)


def _i3d_rot_to_blender(obj, deg3):
    """Convert an objectChange rotation (i3d Y-up, XYZ degrees) into the
    imported object's Blender ``rotation_euler``, mirroring the importer's
    transform convention (importer._set_transform): R_b = M @ R_xml @ M^-1
    with M = +90deg X. LIGHT/CAMERA objects carry the exporter-compensating
    extra post-mult R_x(+90) (nested lights: raw XML values) - same special
    cases as on import, so an objectChange targeting a light node lands in
    the same frame the importer produced."""
    e = mathutils.Euler([math.radians(v) for v in deg3], 'XYZ')
    R = e.to_matrix().to_4x4()
    M = mathutils.Matrix.Rotation(math.radians(90), 4, 'X')
    if obj.type in ('LIGHT', 'CAMERA'):
        if obj.parent is not None and obj.parent.type in ('LIGHT', 'CAMERA'):
            return e
        return (M @ R @ M.inverted() @ M).to_euler('XYZ')
    return (M @ R @ M.inverted()).to_euler('XYZ')


def _set_cfg_hidden(o, hidden):
    """Hide/show an object for the config preview and tag it so 'Prepare for
    Export' can find and re-show every config-hidden part (the Giants exporter
    writes each node's current viewport visibility, so a preview-hidden part would
    otherwise be dropped/exported as visibility=false)."""
    if "_i3d_orig_hidden" not in o:
        # First config touch: remember the ORIGINAL (import) state - the own
        # flag WITHOUT any config influence: neither an inherited ancestor
        # hide (_i3d_cfg_inh) nor an earlier config hide (_i3d_cfg_hidden -
        # scenes touched before this feature existed have no snapshot yet).
        o["_i3d_orig_hidden"] = bool(o.hide_viewport
                                     and not o.get("_i3d_cfg_inh")
                                     and not o.get("_i3d_cfg_hidden"))
    o.hide_viewport = hidden
    o.hide_render = hidden
    if hidden:
        o["_i3d_cfg_hidden"] = True
    elif "_i3d_cfg_hidden" in o:
        del o["_i3d_cfg_hidden"]


def _restore_orig_visibility(o):
    """Implicit objectChange side: reset the node's OWN visibility to its
    original import state (easyShed01: the 'No' option defines only
    visibilityActive="false"; selecting 'Yes' must restore the i3d default =
    visible - the game captures the original value for the missing side)."""
    orig = bool(o.get("_i3d_orig_hidden",
                      o.hide_viewport and not o.get("_i3d_cfg_inh")
                      and not o.get("_i3d_cfg_hidden")))
    o.hide_viewport = orig
    o.hide_render = orig
    # No config marker either way: the state IS the original again (an
    # originally hidden node counts as import-hidden, not config-hidden).
    if "_i3d_cfg_hidden" in o:
        del o["_i3d_cfg_hidden"]


def _apply(node, attrs, id_to_obj, active):
    obj = id_to_obj.get(node)
    if obj is None:
        return
    suf = "Active" if active else "Inactive"

    other_suf = "Inactive" if active else "Active"
    vis = attrs.get("visibility" + suf)
    if vis is not None:
        # Set ONLY this node's own flag, exactly like the game: visibility is
        # hierarchical at RENDER time (a node draws only if it and all its
        # ancestors are visible) but every node keeps its own independent
        # flag. The previous recursive set stomped sibling config state -
        # farmall120C's front-weight options all actively show
        # attacherFrameFrontBase, whose children_recursive include
        # frontWeight01-03, so showing the frame re-showed every weight disc
        # regardless of the selected option (#12). The render hierarchy is
        # emulated afterwards by _enforce_visibility_hierarchy.
        _set_cfg_hidden(obj, (vis != "true"))
    elif attrs.get("visibility" + other_suf) is not None:
        # Only the OTHER side is defined: implicit value = the node's
        # ORIGINAL import state (easyShed01 'No' has only
        # visibilityActive="false"; selecting 'Yes' must restore visible).
        _restore_orig_visibility(obj)

    # translation/rotation/scale are absolute LOCAL values in the i3d frame.
    # Set them through the matrix chain (_set_local_transform): it handles
    # the importer's keep-world parenting (matrix_parent_inverse) AND the
    # M-conjugated rotation storage (#13: warningSign fold rotated about the
    # wrong axis; MT655 strays: direct obj.location writes collapsed
    # parent-inverse nodes to the world origin). LIGHT/CAMERA rotations keep
    # the dedicated path (importer stores them with an extra R_x(+90)
    # post-mult that the generic conjugation does not model).
    tr = attrs.get("translation" + suf)
    ro = attrs.get("rotation" + suf)
    sc = attrs.get("scale" + suf)
    # One-sided objectChanges: the missing side falls back to the node's
    # ORIGINAL transform (game captures it at load - e.g. MT655's
    # wheelBackLeftTwin defines only translationActive).
    rtr = tr is None and attrs.get("translation" + other_suf) is not None
    rro = ro is None and attrs.get("rotation" + other_suf) is not None
    rsc = sc is None and attrs.get("scale" + other_suf) is not None
    if obj.type in ('LIGHT', 'CAMERA'):
        if tr is not None:
            obj.location = _vec3(tr)
        if ro is not None:
            obj.rotation_euler = _i3d_rot_to_blender(obj, _vec3(ro))
        if sc is not None:
            obj.scale = _vec3(sc)
    elif (tr is not None or ro is not None or sc is not None
          or rtr or rro or rsc):
        _set_local_transform(obj, tr, ro, sc, rtr, rro, rsc)
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
# Material-swap configurations (design / Black Beauty) and colour
# configurations (baseColor / designColor%d / ...): set the slot materials'
# fs25_param nodes from a resolved material template and/or the option colour.
# Visual preview only; debug-material parameters, shared per material.
# ---------------------------------------------------------------------------

_TEMPLATE_CACHE = {}


def _load_templates_cached(data_base):
    if not data_base:
        return {}
    if data_base not in _TEMPLATE_CACHE:
        _TEMPLATE_CACHE[data_base] = i3d_material_templates.load_templates(data_base)
    return _TEMPLATE_CACHE[data_base]


def _import_materials_by_slot(import_id):
    """Materials by shapes-slot-name for one import - INCLUDING its loaded
    wheel parts: design configs can target wheel slots (Enyaq 'Rim Color'
    swaps rim_color_mat on the visionAero rims), and the wheels live in
    their own reference imports (_i3d_wheel_import ties them back)."""
    by_slot = {}
    seen = set()
    for o in bpy.data.objects:
        if (o.get("_i3d_import_id") != import_id
                and o.get("_i3d_wheel_import") != import_id):
            continue
        for ms in o.material_slots:
            # the slot may hold the export material (ATTACH_DEBUG_MATERIALS_TO_
            # MESH off) - the preview always targets the pair's debug material
            mat = debug_pair(ms.material)
            if mat is None or mat.name in seen:
                continue
            seen.add(mat.name)
            slot = mat.get("materialSlotName")
            if slot:
                by_slot.setdefault(slot, []).append(mat)
    return by_slot


def _resolve_data_path(data_base, p):
    """Resolve a ``$data/...`` template path to an existing file.

    FS ships textures as ``.dds`` while the XML references ``.png``; try the
    given extension first, then ``.dds``/``.png``. Returns None if not found.
    """
    if not p or not p.startswith("$data/"):
        return None
    base = os.path.join(i3d_material_templates._data_dir(data_base),
                        p[len("$data/"):])
    if os.path.isfile(base):
        return base
    root, _ext = os.path.splitext(base)
    for alt in (root + ".dds", root + ".png"):
        if os.path.isfile(alt):
            return alt
    return None


def _set_mat_texture(mat, role, filepath, data_path=None):
    """Swap the image of the marked detail-texture node (fs25_tex:<role>).

    The glossmap (detailSpecular: R=smoothness, G=AO, B=metalness) and the normal
    map are DATA, not colour - they must be Non-Color or the channels are
    gamma-distorted (a freshly loaded image defaults to sRGB), which breaks the
    smoothness/metalness look (e.g. silver chrome stays dull).

    data_path: the canonical ``$data/...`` path the texture came from; stored on
    the node as ``fs25_data_path`` so the Sync button can write it into the export
    material's ``customTexture_<role>`` (re-export keeps the swapped texture).
    """
    node = mat.node_tree.nodes.get("fs25_tex:" + role)
    if node is None or node.bl_idname != "ShaderNodeTexImage":
        return
    try:
        img = bpy.data.images.load(filepath, check_existing=True)
    except (RuntimeError, OSError):
        return
    if role != "detailDiffuse":
        try:
            img.colorspace_settings.name = "Non-Color"
        except Exception:
            pass
    node.image = img
    if data_path:
        node["fs25_data_path"] = data_path


def _set_mat_param(mat, param, value_str):
    """Set the fs25_param node(s) for *param* on a single material.

    Array-parameter fallback: the building shader declares colorScale with
    arraySize=8, so placeable materials (and our debug nodes) carry
    colorScale0..7 instead of a plain colorScale. When no node matches the
    plain name, element 0 is set - the only element base-game colour configs
    observably write (every rudolfHormann mainColor material carries exactly
    colorScale0 holding the default colour). Higher elements colour OTHER
    regions of the material and are left alone; the engine-side behaviour of
    setShaderParameter on them is not verifiable from the decompile."""
    if not mat.use_nodes:
        return
    vals = [float(x) for x in value_str.replace(",", " ").split()]
    if not vals:
        return
    slot_idx = {"x": 0, "y": 1, "z": 2, "w": 3, "alpha": 3}
    names = (param,) if param[-1:].isdigit() else (param, param + "0")
    for name in names:
        hit = False
        for n in mat.node_tree.nodes:
            if not n.name.startswith("fs25_param:"):
                continue
            if n.get("fs25_xml_param") != name:
                continue
            hit = True
            if n.bl_idname == "ShaderNodeRGB":
                dv = n.outputs[0].default_value
                for i in range(min(3, len(vals))):
                    dv[i] = vals[i]
            else:
                idx = slot_idx.get(n.get("fs25_xml_slot", "all"), 0)
                # Only set components that were actually provided: a 3-value
                # colour must leave w untouched, exactly like the game's
                # setShaderParameter(node, "colorScale", r, g, b, nil). The
                # debug wiring feeds colorScale0.w into BlendFactor0, so the
                # previous vals[0] fallback painted the blend factor with the
                # RED component (garageSmall: dark colours stopped tinting,
                # bright ones washed out).
                if idx < len(vals):
                    n.outputs[0].default_value = vals[idx]
        if hit:
            break


def _capture_orig(mat):
    """Snapshot a material's current fs25_param values once (for restore).

    Materials without any fs25_param node (i.e. export materials, which the
    preview never touches) are skipped entirely - snapshotting them wrote three
    empty ``{}`` IDProperties onto every export material and, worse, the
    once-only semantics below then blocked a later real capture.
    """
    if mat.get("_i3d_cfg_orig") is not None or not mat.use_nodes:
        return
    if not any(n.name.startswith("fs25_param:") for n in mat.node_tree.nodes):
        return
    orig = {}
    # Two passes, RGB nodes first: an RGB and a scalar node can share one
    # param name (colorScale0 -> _rgb + _w); which one got snapshotted used
    # to depend on node order, and a scalar-first snapshot would restore
    # only the red channel. The colour snapshot must win - the w scalar is
    # never touched by 3-component colour applies (see _set_mat_param).
    for rgb_pass in (True, False):
        for n in mat.node_tree.nodes:
            if not n.name.startswith("fs25_param:"):
                continue
            if (n.bl_idname == "ShaderNodeRGB") != rgb_pass:
                continue
            p = n.get("fs25_xml_param")
            if p is None or p in orig:
                continue
            # accept array elements too (colorScale0..7, building shader);
            # stored under the REAL node name so restore matches exactly.
            if p.rstrip("0123456789") not in i3d_material_templates.PARAM_ATTRS:
                continue
            if rgb_pass:
                dv = n.outputs[0].default_value
                orig[p] = "%g %g %g" % (dv[0], dv[1], dv[2])
            else:
                orig[p] = "%g" % n.outputs[0].default_value
    mat["_i3d_cfg_orig"] = json.dumps(orig)
    tex = {}
    for role in i3d_material_templates.TEXTURE_ATTRS:
        node = mat.node_tree.nodes.get("fs25_tex:" + role)
        if node is not None and node.bl_idname == "ShaderNodeTexImage":
            tex[role] = node.image.name if node.image else ""
    mat["_i3d_cfg_orig_tex"] = json.dumps(tex)
    # Original $data texture paths from the paired export material, so a later
    # restore (e.g. Design Line "No") can revert the export texture too.
    exp = _export_pair(mat)
    orig_data = {}
    if exp is not None:
        for role in i3d_material_templates.TEXTURE_ATTRS:
            v = exp.get("customTexture_" + role)
            if v:
                orig_data[role] = v
    mat["_i3d_cfg_orig_tex_data"] = json.dumps(orig_data)


def _restore_mat(mat):
    raw = mat.get("_i3d_cfg_orig")
    if raw:
        try:
            orig = json.loads(raw)
        except (ValueError, TypeError):
            orig = {}
        for p, v in orig.items():
            _set_mat_param(mat, p, v)
    rawt = mat.get("_i3d_cfg_orig_tex")
    if rawt:
        try:
            origt = json.loads(rawt)
        except (ValueError, TypeError):
            origt = {}
        for role, imgname in origt.items():
            node = mat.node_tree.nodes.get("fs25_tex:" + role)
            if node is not None:
                node.image = bpy.data.images.get(imgname) if imgname else None
    try:
        origd = json.loads(mat.get("_i3d_cfg_orig_tex_data") or "{}")
    except (ValueError, TypeError):
        origd = {}
    for role in i3d_material_templates.TEXTURE_ATTRS:
        node = mat.node_tree.nodes.get("fs25_tex:" + role)
        if node is None:
            continue
        dp = origd.get(role)
        if dp:
            node["fs25_data_path"] = dp
        elif node.get("fs25_data_path") is not None:
            del node["fs25_data_path"]


def _apply_template_to_mat(mat, resolved, color_only, data_base=None):
    cs = resolved.get("colorScale")
    if cs is not None:
        _set_mat_param(mat, "colorScale", cs)
    if not color_only:
        for p in i3d_material_templates.PARAM_ATTRS:
            if p != "colorScale" and p in resolved:
                _set_mat_param(mat, p, resolved[p])
        for role in i3d_material_templates.TEXTURE_ATTRS:
            tpath = resolved.get(role)
            if tpath and data_base:
                fp = _resolve_data_path(data_base, tpath)
                if fp:
                    _set_mat_texture(mat, role, fp, data_path=tpath)


# ---------------------------------------------------------------------------
# Colour resolution (VehicleConfigurationItemColor semantics)
# ---------------------------------------------------------------------------


def _rgb_str(s):
    """Normalise an ``r g b [a]`` attribute string to ``"r g b"``, or None."""
    try:
        vals = [float(x) for x in s.replace(",", " ").split()]
    except (ValueError, AttributeError):
        return None
    if len(vals) < 3:
        return None
    return "%g %g %g" % (vals[0], vals[1], vals[2])


def _template_color(name, templates):
    """colorScale of a material template (with parentTemplate inheritance)."""
    resolved = i3d_material_templates.resolve_template(name, templates)
    return _rgb_str(resolved.get("colorScale") or "") if resolved else None


def _resolve_option_color(opt, ct, templates):
    """Effective ``(colorscale_str, template_name)`` of a colour option -
    either may be None (VehicleConfigurationItemColor.loadFromXML + postLoad):

    - ``#color`` is an RGB string OR a material-template name (colour lookup).
    - ``#materialTemplateName`` defines the LOOK, and the colour when
      ``#color`` is absent; an explicit ``#color`` wins (current-patch
      semantics: ``self.color = self.color or color``).
    - Options without a template get the type's
      defaultColorMaterialTemplateName as look ONLY when useDefaultColors is
      set (postLoad assigns the fallback inside that branch; the enyaq sets
      the attribute without the flag - it is inert there, colour-only apply).
    """
    col = None
    cattr = opt.get("color") or ""
    if cattr:
        if cattr in templates:
            col = _template_color(cattr, templates)
        else:
            col = _rgb_str(cattr)
    tpl = opt.get("template") or ""
    if not tpl and ct.get("use_default_colors"):
        tpl = ct.get("default_color_template") or ""
    if col is None and tpl:
        col = _template_color(tpl, templates)
    return col, (tpl or None)


def _resolve_material_reference(entry, m, templates):
    """``(colorscale, template)`` of the configuration referenced by one of
    the use_* flags, following the CURRENT selection in ``entry["sel"]``
    (VehicleConfigurationDataMaterial.onLoadFinished). None when *m* carries
    no reference or it cannot be resolved.

    use_rim_color prefers the dedicated Rim-Color section
    (``entry["rimcolor"]``, wheel-loader driven - its selection is the live
    UI state), then the parsed rimColorConfigurations type."""
    if not entry:
        return None
    cname = None
    if m.get("use_base_color"):
        cname = "baseColor"
    else:
        n = m.get("use_design_color_index") or 0
        if n:
            cname = "designColor" if n <= 1 else "designColor%d" % n
        elif m.get("use_rim_color"):
            cname = "rimColor"
    if cname is None:
        return None
    if cname == "rimColor":
        # The dedicated Rim Color section is the live UI for the rim colour
        # (its sel updates on click; the parsed type's sel does not), so it
        # takes precedence over the rimColorConfigurations type.
        rc = entry.get("rimcolor")
        if rc and rc.get("options"):
            sel = rc.get("sel", 0)
            if 0 <= sel < len(rc["options"]):
                tpl = rc["options"][sel].get("template")
                if tpl:
                    return _template_color(tpl, templates), tpl
    tag = cname + "Configurations"
    for t in entry.get("types", []):
        if t["tag"] == tag and t.get("options"):
            sel = entry.get("sel", {}).get(tag, t.get("default", 0))
            if not isinstance(sel, int) or not (0 <= sel < len(t["options"])):
                sel = t.get("default", 0)
            return _resolve_option_color(t["options"][sel], t, templates)
    return None


def _apply_color_to_mat(mat, col, tpl, color_only, templates, data_base):
    """Apply an effective colour + optional look template to one material.
    Template parameters first (colorScale only when *color_only*), then the
    explicit colour LAST so an option colour overrides the template's."""
    _capture_orig(mat)
    if tpl:
        resolved = i3d_material_templates.resolve_template(tpl, templates)
        if resolved:
            _apply_template_to_mat(mat, resolved, color_only, data_base)
    if col:
        _set_mat_param(mat, "colorScale", col)


def capture_material_originals(import_id, types):
    """Snapshot originals for every material referenced by a material config
    (option-level entries AND the colour configs' type-level target slots)."""
    by_slot = _import_materials_by_slot(import_id)
    for t in types:
        for s in t.get("color_slots", []):
            for mat in by_slot.get(s["slot"], []):
                _capture_orig(mat)
        for o in t.get("options", []):
            for m in o.get("materials", []):
                for mat in by_slot.get(m["slot"], []):
                    _capture_orig(mat)


def expand_default_colors(types, data_base):
    """Append the game's generated default-colour palette to every colour
    config type with useDefaultColors (VehicleConfigurationItemColor /
    PlaceableConfigurationItemColor .postLoad): one option per palette
    template that resolves, coloured by the brand template with the type's
    default_color_template as look. defaultColorIndex (1-based, into the
    palette) marks the generated default and re-points the type default.
    The trailing in-game "Custom Color" entry is isSelectable=false and is
    omitted. Works on the to_dict() representation and must run BEFORE the
    entry's initial selections are derived."""
    templates = _load_templates_cached(data_base)
    if not templates:
        return
    for t in types:
        if not (t.get("is_color") and t.get("use_default_colors")):
            continue
        palette = (i3d_config_parser.DEFAULT_COLORS_PLACEABLE
                   if t["tag"] == "colorConfigurations"
                   else i3d_config_parser.DEFAULT_COLORS_VEHICLE)
        dci = t.get("default_color_index") or 0
        price = t.get("color_price") or ""
        base_tpl = t.get("default_color_template") or "calibratedPaint"
        for i, name in enumerate(palette, 1):
            if name not in templates:
                continue
            t["options"].append({
                "label": name, "changes": [], "materials": [],
                "selectable": True, "is_default": (i == dci), "params": "",
                "color": name, "ui_color": "", "template": base_tpl,
                "is_metallic": False, "is_mat": False,
                "price": ("0" if i == dci else price)})
        if dci:
            for j, o in enumerate(t["options"]):
                if o.get("is_default") and o.get("selectable", True):
                    t["default"] = j
                    break


def _apply_material_entry(m, col, tpl, by_slot, templates, data_base):
    """Paint one material entry onto its slot materials."""
    for mat in by_slot.get(m["slot"], []):
        _apply_color_to_mat(mat, col, tpl, m.get("color_only", False),
                            templates, data_base)


def _entry_color(m, entry, templates, opt_col=None, opt_tpl=None):
    """Effective (colour, template) for one material entry: a use_* reference
    beats the entry's own template, which beats the option colour/template."""
    ref = _resolve_material_reference(entry, m, templates)
    if ref is not None:
        return ref
    if m.get("template"):
        return None, m["template"]
    return opt_col, opt_tpl


def _apply_materials(import_id, options, selected_index, data_base,
                     entry=None, ct=None):
    by_slot = _import_materials_by_slot(import_id)
    templates = _load_templates_cached(data_base)
    # restore every slot this config type touches, then apply the selected option
    touched = {m["slot"] for o in options for m in o.get("materials", [])}
    for slot in touched:
        for mat in by_slot.get(slot, []):
            _restore_mat(mat)
    if 0 <= selected_index < len(options):
        opt = options[selected_index]
        for m in opt.get("materials", []):
            col, tpl = _entry_color(m, entry, templates)
            _apply_material_entry(m, col, tpl, by_slot, templates, data_base)


def apply_color_config(import_id, entry, ct, selected_index, data_base=None):
    """Apply a colour-configuration option: paint the type-level target slots
    (enyaq: skodaEnyaq_baseColor_mat; placeables: mainColor_mat) and the
    option's own material entries (enyaq interior colorOnly slots).

    Mirrors VehicleConfigurationItemColor.onPostLoad: a type-level entry with
    its own materialTemplateName is a fixed material (option-independent);
    otherwise the selected option's colour/template is applied, honouring
    materialTemplateUseColorOnly and the use_* references."""
    options = ct["options"]
    by_slot = _import_materials_by_slot(import_id)
    templates = _load_templates_cached(data_base)
    touched = {s["slot"] for s in ct.get("color_slots", [])}
    touched |= {m["slot"] for o in options for m in o.get("materials", [])}
    for slot in touched:
        for mat in by_slot.get(slot, []):
            _restore_mat(mat)
    if not (0 <= selected_index < len(options)):
        return
    opt = options[selected_index]
    ocol, otpl = _resolve_option_color(opt, ct, templates)
    for s in ct.get("color_slots", []):
        if s.get("template"):
            col, tpl = None, s["template"]       # fixed slot material
        else:
            col, tpl = _entry_color(s, entry, templates, ocol, otpl)
        _apply_material_entry(s, col, tpl, by_slot, templates, data_base)
    for m in opt.get("materials", []):
        col, tpl = _entry_color(m, entry, templates, ocol, otpl)
        _apply_material_entry(m, col, tpl, by_slot, templates, data_base)


def _enforce_visibility_hierarchy(import_id):
    """Emulate the game's hierarchical visibility: a node renders only if it
    AND all its ancestors are visible, while every node keeps its OWN flag
    (a config can hide a child whose parent another option shows, e.g. the
    farmall120C weight discs under the always-shown attacher frame). _apply
    writes only the own flags; this pass computes the effective state for the
    whole import. Nodes hidden purely because of an ancestor get marked
    (``_i3d_cfg_inh``) so the next pass can tell their own flag apart from
    the inherited one - Blender has no separate 'effective' flag."""
    imported = [o for o in bpy.data.objects
                if o.get("_i3d_import_id") == import_id]
    idset = set(imported)
    own = {}
    for o in imported:
        if o.get("_i3d_cfg_hidden"):
            own[o] = True               # config-hidden (own flag)
        elif o.get("_i3d_cfg_inh"):
            own[o] = False              # hidden only via an ancestor last pass
        else:
            own[o] = o.hide_viewport    # original/import state
    for o in imported:
        eff = False
        cur = o
        while cur is not None and cur in idset:
            if own[cur]:
                eff = True
                break
            cur = cur.parent
        o.hide_viewport = eff
        o.hide_render = eff
        if eff and not own[o]:
            o["_i3d_cfg_inh"] = True
        elif "_i3d_cfg_inh" in o:
            del o["_i3d_cfg_inh"]


def apply_config(import_id, ct, selected_index, data_base=None, entry=None):
    """Apply one configuration type: objectChanges, material swaps, colours.

    *entry* (the whole store-config dict of this import) enables the use_*
    colour references; without it those entries fall back to their own
    template (or do nothing)."""
    options = ct["options"]
    id2obj = resolve_node_objects(import_id)
    apply_option(options, selected_index, id2obj)
    if ct.get("is_color"):
        apply_color_config(import_id, entry, ct, selected_index, data_base)
    elif any(o.get("materials") for o in options):
        _apply_materials(import_id, options, selected_index, data_base,
                         entry=entry, ct=ct)
    _enforce_visibility_hierarchy(import_id)


_RIM_COLOR_SLOTS = ("rim_inner_mat", "rim_outer_mat", "rim_additional_mat")


def _rim_materials(import_id):
    """Materials of this import's wheel parts that the game's rim-colour pass
    would touch, matched by the material slot NAME from the .i3d.shapes binary
    (``mat["materialSlotName"]``, set per subset by the importer).

    This mirrors Wheels.lua:onLoadFinished exactly: spec.rimMaterial ->
    "rim_inner_mat"/"rim_outer_mat", spec.additionalMaterial (falls back to
    the selected rimColor configuration when the vehicle XML defines no
    additionalMaterial, Wheels.lua ~l.357) -> "rim_additional_mat", applied
    via VehicleMaterial:applyToVehicle -> getMaterialSlotName(node, i). The
    engine matches the SHAPES-file subset slot name - NOT the material name
    in the i3d XML.

    History (kept because this looked wrong for one vehicle after another):
    originally matched by material NAME ('rim_' prefix) - wrongly painted
    Puma's wheel weight; then by wheel-part ROLE (rim_outer/rim_inner only) -
    missed Vario1000's weight, which verifiably follows the rim colour
    in-game. Resolution (2026-07-02): the two structurally identical weight
    i3d XMLs differ ONLY in their shapes binaries - weight001 (Puma) has
    EMPTY subset slot names (engine never matches -> weight stays black),
    weight003 (Vario1000) has 'rim_additional_mat' (follows the rim colour).
    Slot-name matching reproduces both without per-vehicle special cases and
    also stops painting rim BOLT subsets ('rim_bolt_mat', e.g. rim006), which
    the role filter wrongly included.

    In-game verified (Nadine, 2026-07-02): (1) connector shapes
    (rims/dual001.i3d) carry a 'rim_inner_mat' subset whose inner band DOES
    follow the rim colour in-game - the silver-looking ring is the unpainted
    'rim_bolt_mat' subset, so painting by slot name is correct here too.
    (2) The original Vario1000's rims DO turn black with
    RIM_CONFIGURATION_BLACK although the vehicle XML pins
    <rimMaterial materialTemplateName="FENDT_RED1"> - i.e. in practice the
    rimColor configuration beats the XML rimMaterial (the decompiled
    loadMaterial in Wheels.lua suggests the opposite precedence and is
    misleading here). The picker overriding rim_inner/rim_outer is therefore
    game-correct for such vehicles.
    Deduplicated."""
    seen = set()
    out = []
    for o in bpy.data.objects:
        # Wheel parts AND crawler parts: the crawler's drive-wheel rims carry a
        # 'rim_inner_mat' shapes slot, which the game's rim-colour pass follows.
        if (o.get("_i3d_wheel_import") != import_id
                and o.get("_i3d_crawler_import") != import_id):
            continue
        for ms in o.material_slots:
            m = debug_pair(ms.material)      # slot may hold the export material
            if m is None or m.name in seen:
                continue
            seen.add(m.name)
            if m.get("materialSlotName") in _RIM_COLOR_SLOTS:
                out.append(m)
    return out


def rim_color_label(template_name, data_base):
    """Human label for a rim-colour material template (its title, else name)."""
    t = _load_templates_cached(data_base).get(template_name) or {}
    return t.get("title") or template_name


def _export_pair(mat):
    """The re-export material paired with *mat* (same _i3d_material_id +
    _i3d_import_uuid, kind 'export'), or None.

    Handed an export material it returns that material - callers pass the debug
    material, but the identity case keeps the old accidental self-match honest.
    """
    return _material_pair(mat, "export")


def _set_export_colorscale(exp_mat, colorscale_str):
    """Bake the rim colour into the export material as the colorScale custom
    parameter the Giants exporter writes (<CustomParameter name="colorScale">).
    The diffuse/Base Color is left as the base texture - the shader tints it by
    colorScale, so setting both would double-apply."""
    parts = colorscale_str.split()
    while len(parts) < 4:
        parts.append("1")
    exp_mat["customParameter_colorScale"] = " ".join(parts[:4])


def apply_rim_color(import_id, template_name, data_base, to_export=False):
    """Apply a rim-colour material template (colorScale + smoothness/clearCoat +
    textures) to all rim/weight materials of this import. Restores each material's
    original first so switching colours is clean.

    to_export: only when True is the colour also baked into the paired EXPORT
    material (customParameter_colorScale). The caller passes True only for the
    automatic default/stock colour on wheel load; user colour picks stay
    preview-only and are pushed to the export material via the Sync button.
    """
    templates = _load_templates_cached(data_base)
    resolved = i3d_material_templates.resolve_template(template_name, templates)
    cs = resolved.get("colorScale") if resolved else None
    for mat in _rim_materials(import_id):
        _capture_orig(mat)
        _restore_mat(mat)
        if resolved:
            _apply_template_to_mat(mat, resolved, False, data_base)
        if to_export and cs:
            exp = _export_pair(mat)
            if exp is not None:
                _set_export_colorscale(exp, cs)
