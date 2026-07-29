"""Load and place FS25 crawler tracks (Raupenfahrwerke) onto a vehicle.

A vehicle's ``<wheels><wheelConfiguration>/<crawlers>/<crawler>`` entry
references an external crawler i3d and shows one node subtree of it. Mount forms:

  * ``linkNode``       - a single mount empty the crawler snaps onto (Lexion).
  * ``linkWheelNodes`` - no mount empty; the crawler's drive wheels are aligned
                         to the vehicle's wheel nodes by translation (Jaguar).
An optional ``offset`` shifts the placement; a shared i3d may hold several
assemblies as separate scene roots (e.g. mach4R front/back) or one symmetric
node used for both sides (leftNode == rightNode, e.g. forestry tracks).

On the vehicle the flat 10 m track band is bent around the wheels at render time
by the ``motionPathRubber`` shader, so a plain import leaves it lying flat. We
reproduce the wrap geometrically: build the rubber-band outline around the
crawler's rotating wheels (:mod:`i3d_crawler_path`) and bend the strip's
vertices onto it (all in the crawler-root-local frame).

Reuses the wheel loader's helpers (``_drive_object_map``, ``_link``,
``_dup_subtree``, ``_tag_tree``) and ``i3d_reference_loader``. No ``bpy.ops`` /
menu calls, so this is independent of Blender's UI language.
"""

import itertools
import math

import bpy
from mathutils import Matrix, Vector

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


def _join_path(base, rel):
    """Join a crawler show-path (our format, e.g. "0>0" or a bare root "0>")
    with an FS-relative node path (e.g. "1" or "0|0|1")."""
    return (base + rel) if base.endswith(">") else (base + "|" + rel)


def _hide_subtree(root):
    for o in _descendants(root):
        o.hide_viewport = True
        o.hide_render = True


def _remove_subtree(root):
    for o in reversed(_descendants(root)):
        try:
            bpy.data.objects.remove(o, do_unlink=True)
        except Exception:
            pass


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
    sharing the i3d, e.g. the Jaguar band inside terraTracLexion.i3d). The own
    band's material name carries the crawler i3d stem
    (``terraTracLexionTrack...``); the foreign one carries another
    (``terraTracJaguarTrack...``)."""
    if not stem:
        return True
    s = stem.lower()
    return any(m and s in m.name.lower() for m in strip.data.materials)


def _apply_offset(root, spec):
    """Apply the crawler's ``offset`` (FS Y-up "x y z") as a world translation,
    converted to Blender Z-up (x, -z, y). No-op when absent."""
    off = spec.get("offset")
    if not off or len(off) < 3:
        return
    ox, oy, oz = off[0], off[1], off[2]
    root.matrix_world = Matrix.Translation(Vector((ox, -oz, oy))) @ root.matrix_world


def _place_by_wheels(root, l_objs, rotating, show):
    """2-point alignment for the ``linkWheelNodes`` form: pick the crawler
    rotating-wheel pair whose spacing matches the two vehicle link wheels, then
    yaw + translate so that pair lands on the link wheels. This is what actually
    positions the track (the biggest wheel is the elevated drive sprocket, not a
    ground reference). Returns True on success."""
    l0 = l_objs[0].matrix_world.translation.copy()
    l1 = l_objs[1].matrix_world.translation.copy()
    link_d = (l1 - l0).length
    by_path = {o.get("_i3d_node_path"): o for o in _descendants(root)}
    cw = {}
    for node, _r in rotating:
        o = by_path.get(_join_path(show, node))
        if o is not None:
            cw[node] = o.matrix_world.translation.copy()
    if len(cw) < 2:
        return False
    # crawler pair whose separation best matches the link-wheel spacing
    best = None
    for a, b in itertools.combinations(cw, 2):
        score = abs((cw[b] - cw[a]).length - link_d)
        if best is None or score < best[0]:
            best = (score, a, b)
    _, a, b = best
    dl = l1 - l0
    # of the two possible pairings, take the one needing the smaller yaw
    opt = []
    for i, j in ((a, b), (b, a)):
        dc = cw[j] - cw[i]
        ang = math.atan2(dl.y, dl.x) - math.atan2(dc.y, dc.x)
        ang = (ang + math.pi) % (2 * math.pi) - math.pi
        opt.append((abs(ang), i, ang))
    opt.sort()
    _, i, ang = opt[0]
    place = (Matrix.Translation(l0) @ Matrix.Rotation(ang, 4, 'Z')
             @ Matrix.Translation(-cw[i]))
    root.matrix_world = place @ root.matrix_world
    return True


def _place_crawler(root, spec, links, report=None):
    """Seat the imported crawler *root* on the vehicle. Returns True on success.

    Prefers ``linkWheelNodes`` (aligns the crawler's matching wheel pair to the
    vehicle wheel nodes) when present - some crawlers (xerion) also carry a
    ``linkNode`` that is only the movement parent, not the visual anchor. Falls
    back to snapping the root onto the ``linkNode`` mount empty (Lexion, mach4R).
    """
    show = spec.get("show_path")
    link_wheels = spec.get("link_wheels") or []
    rotating = spec.get("rotating") or []

    if len(link_wheels) >= 2 and rotating and show:
        l_objs = [links.get(x) for x in link_wheels[:2]]
        if all(l_objs) and _place_by_wheels(root, l_objs, rotating, show):
            _apply_offset(root, spec)
            return True

    link_node = spec.get("link_node")
    if link_node:
        mount = links.get(link_node)
        if mount is None:
            if report is not None:
                report("WARNING", "Crawler link node %r not found" % link_node)
            return False
        i3d_wheel_loader._link(root, mount)
        _apply_offset(root, spec)
        return True

    # Single link wheel and no mount empty: align the first drive wheel by
    # translation (rare fallback).
    if link_wheels and rotating and show:
        link_obj = links.get(link_wheels[0])
        if link_obj is None:
            if report is not None:
                report("WARNING", "Crawler link wheel %r not found"
                       % link_wheels[0])
            return False
        r_max = max(r for _, r in rotating)
        big = [n for n, r in rotating if abs(r - r_max) < 1e-6]
        by_path = {o.get("_i3d_node_path"): o for o in _descendants(root)}
        craw = by_path.get(_join_path(show, big[0])) if big else None
        if craw is None:
            return False
        t = link_obj.matrix_world.translation - craw.matrix_world.translation
        root.matrix_world = Matrix.Translation(t) @ root.matrix_world
        _apply_offset(root, spec)
        return True
    return False

def _deform_strip(strip, root, pts, cum, length, track_width=1.0):
    """Bend the flat track strip *strip* onto the belt outline, working in the
    crawler-root-local frame: fore/aft = Y (maps around the loop), width = X
    (kept), up = Z (inner running surface seated on the wheel radius, tread
    outward). Root-local coords - not the strip's mesh-local coords - keep the
    band centred on the wheels even when the strip sits off-axis (Jaguar band).

    *track_width* scales the band width about its centre (FS applies trackWidth
    as scale X on the track parts; crawler.xsd: 'Track width is set as scale X')
    so the same band renders narrower/wider per config (e.g. Crawler vs Broad)."""
    strip.data = strip.data.copy()          # single-user before editing
    me = strip.data
    to_root = root.matrix_world.inverted() @ strip.matrix_world
    from_root = to_root.inverted()
    rlp = [to_root @ v.co for v in me.vertices]   # root-local vertex positions
    xs = [p.x for p in rlp]
    x_min = min(xs) if xs else 0.0
    x_max = max(xs) if xs else 0.0
    width = x_max - x_min
    x_c = (x_min + x_max) / 2.0
    z_ref = i3d_crawler_path.inner_surface_z(
        [(p.x, p.y, p.z) for p in rlp], width)
    ys = [p.y for p in rlp]
    y_min = min(ys)
    y_range = (max(ys) - y_min) or 1.0
    for v, p in zip(me.vertices, rlp):
        s = (p.y - y_min) / y_range
        (a, b), (ta, tb) = i3d_crawler_path.sample(pts, cum, length, s * length)
        na, nb = (tb, -ta)                  # CCW outward normal in the wrap plane
        off = z_ref - p.z                   # running surface -> path, tread out
        px = x_c + (p.x - x_c) * track_width
        v.co = from_root @ Vector((px, a + na * off, b + nb * off))
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
        o = by_path.get(_join_path(show, node))
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

    prefix = show if show.endswith(">") else show + "|"
    strips = [o for o in desc
              if (o.get("_i3d_node_path") or "").startswith(prefix)
              and o.type == 'MESH' and o.data and not o.hide_viewport
              and _mesh_y_extent(o) >= _STRIP_MIN_Y]
    if not strips:
        return
    # The stem split only matters when one i3d ships several band variants (the
    # CLAAS TerraTrac i3d carries both a Lexion and a Jaguar band). With a single
    # band - or if none matches the stem - wrap everything rather than risk
    # hiding the only track.
    own = [o for o in strips if _owns_variant(o, stem)]
    if len(strips) == 1 or not own:
        own = strips
    track_width = spec.get("track_width") or 1.0
    for o in strips:
        if o in own:
            _deform_strip(o, root, pts, cum, length, track_width)
        else:
            _hide_subtree(o)
            o["_i3d_invisible_in_ge"] = True


def _spec_sig(specs):
    """Identity of a crawler set - lets us skip a costly re-import when only the
    tire brand/size changed (the crawler is unaffected). Keyed on the actual
    crawler-relevant fields, so two configs with an identical crawler set share
    a signature and don't reload, while a real change (e.g. trackWidth) does."""
    return repr([(sp.get("i3d"), sp.get("link_node"),
                  tuple(sp.get("link_wheels") or []), sp.get("show_path"),
                  sp.get("offset"), sp.get("track_width"), sp.get("stem"))
                 for sp in specs])


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

    specs = i3d_wheel_resolver.parse_crawlers(vehicle_xml_path, data_dir,
                                              config_index)
    if not specs:
        remove_crawlers(import_id)          # e.g. switched to a wheel-only config
        i3d_wheel_loader._restore_selection(prev_sel, prev_act)
        return 0

    # Crawlers don't change when only the tire brand/size is swapped, and the
    # external i3d import is expensive - skip the reload when the exact same
    # crawler set is already placed for this import.
    sig = _spec_sig(specs)
    existing = [o for o in bpy.data.objects if o.get(_CRAWLER_TAG) == import_id]
    if existing and all(o.get("_i3d_crawler_sig") == sig for o in existing):
        i3d_wheel_loader._restore_selection(prev_sel, prev_act)
        return sum(1 for o in existing if (o.get("_i3d_node_path") or "").endswith(">"))

    remove_crawlers(import_id)

    # i3dMapping id -> vehicle object (same resolution wheels use for driveNode).
    links = i3d_wheel_loader._drive_object_map(vehicle_xml_path, import_id)

    # Import each distinct crawler i3d only ONCE (the shapes decrypt is the
    # expensive part). Extra sides/assemblies are cheap linked-data duplicates
    # of the pristine template roots. A shared i3d may expose several scene
    # roots (mach4R front/back), so template roots are keyed by node path.
    templates = {}
    for spec in specs:
        i3d = spec["i3d"]
        if i3d not in templates:
            imp = i3d_reference_loader.import_referenced_i3d(
                i3d, parent=None,
                tags={_CRAWLER_TAG: import_id, "_i3d_crawler_sig": sig},
                report=report)
            templates[i3d] = {r.get("_i3d_node_path"): r for r in imp}

    placed = 0
    to_wrap = []                            # (root, spec) wrapped in a 2nd pass
    for spec in specs:
        roots_by_path = templates.get(spec["i3d"]) or {}
        show = spec.get("show_path") or ""
        root_key = show[:show.index(">") + 1] if ">" in show else None
        tmpl = roots_by_path.get(root_key)
        if tmpl is None:
            continue
        root = i3d_wheel_loader._dup_subtree(tmpl)
        i3d_wheel_loader._tag_tree(
            root, {_CRAWLER_TAG: import_id, "_i3d_crawler_sig": sig})

        if not _place_crawler(root, spec, links, report=report):
            _remove_subtree(root)           # could not seat it (missing link)
            continue
        # Hide the mud/dirt overlay + far-LOD meshes, exactly as tires do.
        i3d_wheel_loader._hide_lod_and_mud(root)

        # Hide the opposite side - unless it is the same node (symmetric track,
        # leftNode == rightNode), where the whole subtree is shown for both.
        hp = spec.get("hide_path")
        if hp and hp != show:
            hide_root = _hide_root_for_path([root], hp)
            if hide_root is not None:
                _hide_subtree(hide_root)
            elif report is not None:
                report("WARNING", "Crawler opposite side %r not found in %s"
                       % (hp, spec["i3d"]))

        to_wrap.append((root, spec))
        placed += 1

    # The pristine templates were only import sources - drop them (their mesh
    # data stays alive, shared by the duplicated instances).
    for roots_by_path in templates.values():
        for tmpl in roots_by_path.values():
            if tmpl is not None:
                _remove_subtree(tmpl)

    if wrap and to_wrap:
        # Placement must be reflected in child world matrices before wrapping.
        bpy.context.view_layer.update()
        for root, spec in to_wrap:
            try:
                _wrap_crawler(root, spec, report=report)
            except Exception as exc:          # never let wrap break the load
                if report is not None:
                    report("WARNING", "Crawler wrap failed: %r" % exc)

    i3d_wheel_loader._restore_selection(prev_sel, prev_act)
    return placed
