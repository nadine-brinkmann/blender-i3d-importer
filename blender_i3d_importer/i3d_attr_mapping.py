"""
Mapping i3d-XML scene attributes -> Blender custom properties

Goal: re-export fidelity with the Giants i3d Exporter v10.0.x.
Source of truth: `io_export_i3d_10_0_2/dcc/__init__.py`  (SETTINGS_ATTRIBUTES)
              + `io_export_i3d_10_0_2/i3d_export.py`     (verified mapping
                                                          e.g. clipDistance/lodDistance)

Rule: i3d XML attribute "<name>" -> Blender custom property "i3D_<name>"
  + type conversion via TYPE_* from SETTINGS_ATTRIBUTES.

Special cases:
  - lodDistance     -> split into i3D_lod1/lod2/lod3 + i3D_lod (flag).
  - skinBindNodeIds -> stored as _i3d_skinBindNodeIds_raw 
  - referenceId     -> resolved by the importer from the Files map into
                       i3D_referenceFilename (no generic mapping).
  - Light/Camera attributes (type, color, range, coneAngle, ...) are NOT in
    the map: they go into real Blender Light/Camera datablocks

Unknown attributes (not in SCENE_ATTR_MAP, not in SKIP_ATTRS, not in
SPECIAL_ATTRS) are stored as `_i3d_raw_<name>` strings so nothing gets lost.
An INFO message points this out.
"""

from typing import Callable, Dict, Tuple


# ---------------------------------------------------------------------------
# Type converters
# ---------------------------------------------------------------------------

def _to_bool(s) -> bool:
    """'true'/'false'/'1'/'0' -> bool. Anything else -> False."""
    if isinstance(s, bool):
        return s
    return str(s).strip().lower() in ('true', '1', 'yes')


def _to_int(s, default: int = 0) -> int:
    """Safe int parsing, autodetect hex (0x...) and decimal."""
    try:
        return int(str(s).strip(), 0)
    except (ValueError, TypeError):
        return default


def _to_float(s, default: float = 0.0) -> float:
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return default


def _hex_or_dec_to_decstr(s) -> str:
    """'0x10004' -> '65540'. '255' -> '255'. The exporter expects a decimal string."""
    try:
        return str(int(str(s).strip(), 0))
    except (ValueError, TypeError):
        return str(s)


def _to_str(s) -> str:
    return str(s)


# ---------------------------------------------------------------------------
# Attributes that are NOT stored as custom properties
# ---------------------------------------------------------------------------

# Handled elsewhere (transform, identity, material slots).
SKIP_ATTRS = {
    'name', 'nodeId', 'shapeId',
    'translation', 'rotation', 'scale',
    'materialIds',
}

# Need their own logic (see apply_attrs_to_object or importer.py).
SPECIAL_ATTRS = {
    'lodDistance',          # -> i3D_lod1/lod2/lod3 + i3D_lod
    'skinBindNodeIds',      # -> _i3d_skinBindNodeIds_raw
    'referenceId',          # -> i3D_referenceFilename via Files map (in the importer)
    'referenceChildPath',   # -> i3D_referenceChildPath directly in the importer
                            #    (also when missing - exporter needs the key)
}


# ---------------------------------------------------------------------------
# Main mapping: XML attribute -> (Blender property name, type converter)
# ---------------------------------------------------------------------------

SCENE_ATTR_MAP: Dict[str, Tuple[str, Callable]] = {
    # Rendering / shadow
    # visibility is NOT read by the Giants exporter from this custom property
    # but from visible_in_viewport_get() - re-export fidelity therefore comes
    # via the hide logic. Entry here is informational.
    'visibility':                   ('i3D_visibility',                   _to_bool),
    'castsShadows':                 ('i3D_castsShadows',                 _to_bool),
    'castsShadowsPerInstance':      ('i3D_castsShadowsPerInstance',      _to_bool),
    'receiveShadows':               ('i3D_receiveShadows',               _to_bool),
    'receiveShadowsPerInstance':    ('i3D_receiveShadowsPerInstance',    _to_bool),
    'renderedInViewports':          ('i3D_renderedInViewports',          _to_bool),
    'nonRenderable':                ('i3D_nonRenderable',                _to_bool),
    'renderInvisible':              ('i3D_renderInvisible',              _to_bool),
    'visibleShaderParam':           ('i3D_visibleShaderParam',           _to_float),
    'clipDistance':                 ('i3D_clipDistance',                 _to_float),
    'objectMask':                   ('i3D_objectMask',                   _to_int),
    'navMeshMask':                  ('i3D_navMeshMask',                  _to_int),
    'doubleSided':                  ('i3D_doubleSided',                  _to_bool),
    'decalLayer':                   ('i3D_decalLayer',                   _to_int),
    'terrainDecal':                 ('i3D_terrainDecal',                 _to_bool),
    'cpuMesh':                      ('i3D_cpuMesh',                      _to_bool),
    'mergeGroup':                   ('i3D_mergeGroup',                   _to_int),
    'mergeGroupRoot':               ('i3D_mergeGroupRoot',               _to_bool),
    'boundingVolume':               ('i3D_boundingVolume',               _to_str),
    'alphaBlending':                ('i3D_alphaBlending',                _to_bool),

    # Physics - body/material
    'collision':                    ('i3D_collision',                    _to_bool),
    'collisionFilterMask':          ('i3D_collisionFilterMask',          _hex_or_dec_to_decstr),
    'collisionFilterGroup':         ('i3D_collisionFilterGroup',         _hex_or_dec_to_decstr),
    'static':                       ('i3D_static',                       _to_bool),
    'dynamic':                      ('i3D_dynamic',                      _to_bool),
    'kinematic':                    ('i3D_kinematic',                    _to_bool),
    'compound':                     ('i3D_compound',                     _to_bool),
    'compoundChild':                ('i3D_compoundChild',                _to_bool),
    'trigger':                      ('i3D_trigger',                      _to_bool),
    'density':                      ('i3D_density',                      _to_float),
    'staticFriction':               ('i3D_staticFriction',               _to_float),
    'dynamicFriction':              ('i3D_dynamicFriction',              _to_float),
    'restitution':                  ('i3D_restitution',                  _to_float),
    'linearDamping':                ('i3D_linearDamping',                _to_float),
    'angularDamping':               ('i3D_angularDamping',               _to_float),
    'solverIterationCount':         ('i3D_solverIterationCount',         _to_int),
    'ccd':                          ('i3D_ccd',                          _to_bool),

    # Physics - joints / drive
    'joint':                        ('i3D_joint',                        _to_bool),
    'projection':                   ('i3D_projection',                   _to_bool),
    'projDistance':                 ('i3D_projDistance',                 _to_float),
    'projAngle':                    ('i3D_projAngle',                    _to_float),
    'xAxisDrive':                   ('i3D_xAxisDrive',                   _to_bool),
    'yAxisDrive':                   ('i3D_yAxisDrive',                   _to_bool),
    'zAxisDrive':                   ('i3D_zAxisDrive',                   _to_bool),
    'drivePos':                     ('i3D_drivePos',                     _to_bool),
    'driveForceLimit':              ('i3D_driveForceLimit',              _to_float),
    'driveSpring':                  ('i3D_driveSpring',                  _to_float),
    'driveDamping':                 ('i3D_driveDamping',                 _to_float),
    'breakableJoint':               ('i3D_breakableJoint',               _to_bool),
    'jointBreakForce':              ('i3D_jointBreakForce',              _to_float),
    'jointBreakTorque':             ('i3D_jointBreakTorque',             _to_float),

    # Visibility / weather / time-of-day
    'minuteOfDayStart':             ('i3D_minuteOfDayStart',             _to_int),
    'minuteOfDayEnd':               ('i3D_minuteOfDayEnd',               _to_int),
    'dayOfYearStart':               ('i3D_dayOfYearStart',               _to_int),
    'dayOfYearEnd':                 ('i3D_dayOfYearEnd',                 _to_int),
    'weatherMask':                  ('i3D_weatherMask',                  _hex_or_dec_to_decstr),
    'viewerSpacialityMask':         ('i3D_viewerSpacialityMask',         _hex_or_dec_to_decstr),
    'weatherPreventMask':           ('i3D_weatherPreventMask',           _hex_or_dec_to_decstr),
    'viewerSpacialityPreventMask':  ('i3D_viewerSpacialityPreventMask',  _hex_or_dec_to_decstr),

    # Split (tiled meshes)
    'splitType':                    ('i3D_splitType',                    _to_int),
    'splitMinU':                    ('i3D_splitMinU',                    _to_float),
    'splitMinV':                    ('i3D_splitMinV',                    _to_float),
    'splitMaxU':                    ('i3D_splitMaxU',                    _to_float),
    'splitMaxV':                    ('i3D_splitMaxV',                    _to_float),
    'splitUvWorldScale':            ('i3D_splitUvWorldScale',            _to_float),

    # Light-specific attributes that the exporter knows as object properties.
    # (type/color/range/coneAngle/dropOff/emitDiffuse/emitSpecular deliberately
    # NOT here - they go to real Blender Light datablocks)
    'softShadowsLightSize':         ('i3D_softShadowsLightSize',         _to_float),
    'softShadowsLightDistance':     ('i3D_softShadowsLightDistance',     _to_float),
    'softShadowsDepthBiasFactor':   ('i3D_softShadowsDepthBiasFactor',   _to_float),
    'softShadowsMaxPenumbraSize':   ('i3D_softShadowsMaxPenumbraSize',   _to_float),
    'isLightScattering':            ('i3D_isLightScattering',            _to_bool),
    'lightScatteringIntensity':     ('i3D_lightScatteringIntensity',     _to_float),
    'lightScatteringConeAngle':     ('i3D_lightScatteringConeAngle',     _to_float),
    'iesProfileFile':               ('i3D_iesProfileFile',               _to_str),

    # Reference
    'referenceRuntimeLoaded':       ('i3D_referenceRuntimeLoaded',       _to_bool),
    # referenceId -> own code path (Files-map lookup -> i3D_referenceFilename)
}


# ---------------------------------------------------------------------------
# Application function
# ---------------------------------------------------------------------------

def apply_attrs_to_object(obj, raw_attrs: Dict[str, str], report: Callable) -> None:
    """
    Apply raw_attrs (parsed XML attributes) to `obj` as custom properties.

    obj       - bpy.types.Object (or mock with dict interface + .name)
    raw_attrs - dict from i3d_xml_parser (all attributes except name/nodeId/
                shapeId/translation/rotation/scale/materialIds - those already
                live separately on the I3DSceneNode object)
    report    - callable(level, msg), e.g. the _report wrapper in the importer
    """
    # ---- Special case: lodDistance ---------------------------------------
    if 'lodDistance' in raw_attrs:
        # Format per i3d_export.py: "0 lod1 lod2 lod3" (leading 0 is required).
        parts = str(raw_attrs['lodDistance']).split()
        floats = []
        for p in parts[1:]:  # discard the leading 0
            try:
                floats.append(float(p))
            except ValueError:
                pass
        if floats:
            obj['i3D_lod'] = True
            for i, v in enumerate(floats[:3], start=1):
                obj[f'i3D_lod{i}'] = v

    # ---- Special case: skinBindNodeIds -----------------------------------
    # Intermediate property stored on obj during
    # the tree walk, no re-export fidelity 
    if 'skinBindNodeIds' in raw_attrs:
        obj['_i3d_skinBindNodeIds_raw'] = str(raw_attrs['skinBindNodeIds'])

    # ---- Standard mapping ------------------------------------------------
    for xml_attr, value in raw_attrs.items():
        if xml_attr in SKIP_ATTRS or xml_attr in SPECIAL_ATTRS:
            continue

        if xml_attr in SCENE_ATTR_MAP:
            blender_prop, converter = SCENE_ATTR_MAP[xml_attr]
            try:
                obj[blender_prop] = converter(value)
            except Exception as e:
                report('WARNING',
                       f"{obj.name}: conversion '{xml_attr}={value}' failed: {e}")
                obj[f'_i3d_raw_{xml_attr}'] = str(value)
        else:
            # Unknown attribute: park it so nothing gets lost.
            obj[f'_i3d_raw_{xml_attr}'] = str(value)
