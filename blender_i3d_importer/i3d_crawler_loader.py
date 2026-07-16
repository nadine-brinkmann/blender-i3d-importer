"""Load and place FS25 crawler tracks (Raupenfahrwerke) onto a vehicle.

A vehicle's ``<wheels><wheelConfiguration>/<crawlers>/<crawler>`` entry
references an external crawler i3d (e.g. ``terraTracLexion.i3d``) that carries
both a "left" and a "right" mesh as its two top-level nodes. Each ``<crawler>``
mounts one copy of that i3d on an ``i3dMapping`` link node and shows only its
own side (the other side's subtree is hidden).

On the vehicle the flat 10 m track band is bent around the wheels at render time
by the ``motionPathRubber`` shader, so a plain import leaves it lying flat. We
reproduce the wrap geometrically: build the rubber-band outline around the
crawler's rotating wheels (:mod:`i3d_crawler_path`) and bend the strip's
vertices onto it.

MVP scope: the ``linkNode`` form (Lexion). The ``linkWheelNodes`` form (Jaguar)
is a follow-up.

Reuses the wheel loader's helpers so there is one source of truth for how a
part is seated on the vehicle:
  * ``i3d_wheel_loader._drive_object_map`` - i3dMapping id -> imported object
  * ``i3d_wheel_loader._link``             - snap a root onto a mount node
  * ``i3d_reference_loader.import_referenced_i3d`` - import an external i3d

No ``bpy.ops`` / menu calls are used, so this is independent of Blender's UI
language.
"""

import bpy
from mathutils import Vector

from . import (i3d_crawler_path, i3d_reference_loader, i3d_wheel_loader,
               i3d_wheel_resolver)

# Own cleanup tag (separate from the wheels' ``_i3d_wheel_import``) so switching
# between a crawler config and a wheel-only config clears the right objects.
_CRAWLER_TAG = "_i3d_crawler_import"

# A track band strip is the long flat 10 m mesh; anything shorter (wheels,
# decals, mounts) is not a strip. Metres.
_STRIP_MIN_Y = 5.0


def _descendants(root):
    """*root* plus every object below it (self-included)."""
    out = []
    stack = [root]
    while stack:
        o = stack.pop()
        out.append(o)
        stack.extend(o.children)
    return out


def _hide_subtree(root):
    for o in _descendants(root):
        o.hide_viewport = True
        o.hide_render = True


def remove_crawlers(import_id):
    """Delete all crawler objects previously loaded for *import_id*. Returns the
    number removed. Safe to call when none exist."""
    victims = [o for o in bpy.data.objects
               if o.get(_CRAWLER_TAG) == import_id]
    for o in victims:
        try:
            bpy.data.objects.remove(o, do_unlink=True)
        except Exception:
            pass
    return len(victims)


def _hide_root_for_path(roots, node_path):
    """Find the object with ``_i3d_node_path == node_path`` among *roots* and
    their descendants; return it or None."""
    if not node_path:
        return None
    for r in roots:
        for o in _descendants(r):
            if o.get("_i3d_node_path") == node_path:
                return o
    return None


def _mesh_y_extent(obj):
    ys = [v.co.y for v in obj.data.vertices]
    return (max(ys) - min(ys)) if ys else 0.0


def _owns_variant(strip, stem):
    """True if *strip* is this crawler's own track variant (not a foreign one
    sharing the i3d, e.g. the Trion band inside terraTracLexion.i3d). The own
    band's material name carries the crawler i3d stem
    (``terraTracLexionTrack...``); the foreign one carries another
    (``terraTracJaguarTrack...``)."""
    if not stem:
        return True
    s = stem.lower()
    return any(m and s in m.name.lower() for m in strip.data.materials)


def _deform_strip(strip, root, pts, cum, length):
    """Bend the flat track strip *strip* onto the belt outline. The strip's
    long axis (local Y) maps around the loop; its inner running surface is
    seated on the wheel radius and the tread grows outward."""
    strip.data = strip.data.copy()          # single-user before editing
    me = strip.data
    coords = [(v.co.x, v.co.y, v.co.z) for v in me.vertices]
    xs = [c[0] for c in coords]
    width = (max(xs) - min(xs)) if xs else 0.0
    z_ref = i3d_crawler_path.inner_surface_z(coords, width)
    ys = [c[1] for c in coords]
    y_min = min(ys)
    y_range = (max(ys) - y_min) or 1.0
    m_root = root.matrix_world
    m_strip_inv = strip.matrix_world.inverted()
    for v in me.vertices:
        s = (v.co.y - y_min) / y_range
        (a, b), (ta, tb) = i3d_crawler_path.sample(pts, cum, length, s * length)
        na, nb = (tb, -ta)                  # CCW outward normal in the wrap plane
        off = z_ref - v.co.z                # running surface -> path, tread out
        des = Vector((v.co.x, a + na * off, b + nb * off))
        v.co = m_strip_inv @ (m_root @ des)
    me.update()
    strip["_i3d_crawler_wrapped"] = True


def _wrap_crawler(root, spec, report=None):
    """Wrap the shown track band of *root* around its wheels; hide any foreign
    variant band. No-op when the spec lacks rotating-wheel data."""
    show = spec.get("show_path")
    rotating = spec.get("rotating") or []
    stem = spec.get("stem") or ""
    if not (show and rotating):
        return

    desc = _descendants(root)
    by_path = {o.get("_i3d_node_path"): o for o in desc}

    # Rotating wheels in the crawler-root-local wrap plane (Y=fore/aft, Z=up).
    inv = root.matrix_world.inverted()
    wheels = []
    for node, radius in rotating:
        o = by_path.get("%s|%s" % (show, node))
        if o is None:
            continue
        p = inv @ o.matrix_world.translation
        wheels.append((p.y, p.z, radius))
    if len(wheels) < 2:
        if report is not None:
            report("WARNING", "Crawler %s: too few rotating wheels resolved"
                   % show)
        return

    pts, cum, length = i3d_crawler_path.build_belt_path(wheels)
    if length <= 0.0:
        return

    prefix = show + "|"
    for o in desc:
        np_ = o.get("_i3d_node_path") or ""
        if not np_.startswith(prefix):
            continue
        if o.type != 'MESH' or not o.data or o.hide_viewport:
            continue                        # skip wheels/decals and hidden mud/LOD
        if _mesh_y_extent(o) < _STRIP_MIN_Y:
            continue                        # not the long track band
        if _owns_variant(o, stem):
            _deform_strip(o, root, pts, cum, length)
        else:
            # Foreign variant sharing the i3d - hide it (flagged so 'Show All'
            # can bring it back before re-export).
            _hide_subtree(o)
            o["_i3d_invisible_in_ge"] = True


def load_crawlers(vehicle_xml_path, data_dir, import_id, config_index=0,
                  report=None, wrap=True):
    """Load every crawler of the selected wheelConfiguration onto the vehicle.

    Old crawler placements for *import_id* are always cleared first, so calling
    this for a wheel-only config removes any crawlers left over from a previous
    config. When *wrap* is true the track bands are bent around their wheels;
    pass ``wrap=False`` to keep them flat. Returns the number of crawlers
    placed (0 = config has none).
    """
    # Snapshot the user's selection/active so the referenced imports below do
    # not steal it (and shift the viewport). Restored before every return.
    prev_sel = list(bpy.context.selected_objects)
    prev_act = bpy.context.view_layer.objects.active

    remove_crawlers(import_id)

    specs = i3d_wheel_resolver.parse_crawlers(vehicle_xml_path, data_dir,
                                              config_index)
    if not specs:
        i3d_wheel_loader._restore_selection(prev_sel, prev_act)
        return 0

    # i3dMapping id -> vehicle object (same resolution wheels use for driveNode).
    links = i3d_wheel_loader._drive_object_map(vehicle_xml_path, import_id)

    placed = 0
    for spec in specs:
        mount = links.get(spec["link_node"])
        if mount is None:
            if report is not None:
                report("WARNING",
                       "Crawler link node %r not found on vehicle"
                       % spec["link_node"])
            continue

        roots = i3d_reference_loader.import_referenced_i3d(
            spec["i3d"], parent=None, tags={_CRAWLER_TAG: import_id},
            report=report)
        if not roots:
            continue

        # Seat each imported root at the mount node's origin (no inherited
        # offset), exactly like a wheel on its drive node.
        for root in roots:
            i3d_wheel_loader._link(root, mount)
            # Hide the mud/dirt overlay + far-LOD meshes, exactly as tires do
            # (they render as a pale/white overlay in the debug view). Flagged
            # _i3d_invisible_in_ge so 'Show All' restores them before re-export.
            i3d_wheel_loader._hide_lod_and_mud(root)

        # Keep this crawler's own side, hide the opposite side's whole subtree.
        hide_root = _hide_root_for_path(roots, spec["hide_path"])
        if hide_root is not None:
            _hide_subtree(hide_root)
        elif report is not None and spec["hide_path"]:
            report("WARNING",
                   "Crawler opposite side %r not found in %s"
                   % (spec["hide_path"], spec["i3d"]))

        # Bend the flat band around the wheels (motionPathRubber look).
        if wrap:
            for root in roots:
                try:
                    _wrap_crawler(root, spec, report=report)
                except Exception as exc:      # never let wrap break the load
                    if report is not None:
                        report("WARNING", "Crawler wrap failed: %r" % exc)

        placed += 1

    # Restore the user's selection / active object (referenced imports select
    # the freshly imported objects). Mirrors the wheel loader.
    i3d_wheel_loader._restore_selection(prev_sel, prev_act)
    return placed
