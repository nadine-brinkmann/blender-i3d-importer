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

Forestry boggie tracks (Olofsfors, e.g. the John Deere harvester1270G) are a
third form: NO ``<rotatingParts>`` and no wheels of their own - the belt runs
around the two vehicle wheels named by ``linkWheelNodes``, and the band is not
one strip but a chain of small origin-stacked segments the ``motionPath``
shader distributes along a baked path (trackArray texture). We place the root
the way ``Crawlers.lua`` does (back link wheel plus LOCAL ``offset`` toward the
front wheel) and distribute the segments along the belt outline, seating the
tread plates' inner face on the measured tire radius.

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

# A segmented band (forestry tracks) shows up as one parent with many small
# origin-stacked mesh children (ecoTrack: 66). Fewer siblings than this is not
# a chain.
_SEGMENT_MIN_COUNT = 8


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


def _mesh_z_extent(obj):
    zs = [v.co.z for v in obj.data.vertices]
    return (max(zs) - min(zs)) if zs else 0.0


def _tire_ref(link_obj):
    """(world centre, radius) of the visual tire below a wheel link node, or
    ``(None, 0.0)``. The tire is the biggest visible wheel mesh below the node
    (rims are smaller; mud/far LODs are flagged GE-invisible and skipped).

    The CENTRE matters, not just the radius: the game moves the crawler
    linkNode onto the wheel's first visual tire node on the first update
    ('we might have a rimOffset', Crawlers.lua) - on the harvester1270G the
    tire sits rimOffset ~0.22 outboard of the repr node, and the band is
    centred on the tire, not on the link node (verified visually)."""
    best, best_d = None, 0.0
    for o in _descendants(link_obj):
        if (o.type == 'MESH' and o.data and not o.hide_viewport
                and not o.get("_i3d_invisible_in_ge")
                and o.get("_i3d_wheel_import")):
            d = max(o.dimensions.y, o.dimensions.z)
            if d > best_d:
                best, best_d = o, d
    if best is None:
        return None, 0.0
    return best.matrix_world.translation.copy(), best_d / 2.0


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


def _place_at_link_wheels(root, l_objs, spec):
    """Game placement for crawlers WITHOUT ``<rotatingParts>`` (forestry boggie
    tracks). Crawlers.lua parents the crawler to a ``crawlerLinkNode`` created
    at the BACK link wheel (``linkWheelNodes[1]``), oriented toward the front
    one, then applies ``<crawler offset>`` via ``setTranslation`` - a LOCAL
    translation: FS x = lateral, y = up, z = toward the front wheel (the
    harvester1270G's ``0 0 0.707`` is half its 1.414 m boggie wheelbase, i.e.
    the root lands midway between the wheels). Translation only - the vehicle
    is imported axis-aligned and the wrap measures the wheels root-locally."""
    back = _tire_ref(l_objs[0])[0] or l_objs[0].matrix_world.translation.copy()
    front = _tire_ref(l_objs[1])[0] or l_objs[1].matrix_world.translation.copy()
    d = front - back
    fwd = d.normalized() if d.length > 1e-9 else Vector((0.0, -1.0, 0.0))
    origin = back
    off = spec.get("offset")
    if off and len(off) >= 3:
        up = Vector((0.0, 0.0, 1.0))
        # FS X = up x forward maps to Blender up.cross(fwd) (both axes are
        # converted by the same Y-up -> Z-up mapping). Only z is exercised by
        # vanilla data; x/y kept for completeness.
        origin = back + up.cross(fwd) * off[0] + up * off[1] + fwd * off[2]
    root.matrix_world = (Matrix.Translation(origin - root.matrix_world.translation)
                         @ root.matrix_world)


def _place_crawler(root, spec, links, report=None):
    """Seat the imported crawler *root* on the vehicle. Returns True on success.

    Prefers ``linkWheelNodes`` (aligns the crawler's matching wheel pair to the
    vehicle wheel nodes) when present - some crawlers (xerion) also carry a
    ``linkNode`` that is only the movement parent, not the visual anchor. Falls
    back to snapping the root onto the ``linkNode`` mount empty (Lexion, mach4R).
    Without ``<rotatingParts>`` (forestry tracks) the root is seated at the back
    link wheel plus the local XML offset, exactly like the game does.
    """
    show = spec.get("show_path")
    link_wheels = spec.get("link_wheels") or []
    rotating = spec.get("rotating") or []

    if len(link_wheels) >= 2 and rotating and show:
        l_objs = [links.get(x) for x in link_wheels[:2]]
        if all(l_objs) and _place_by_wheels(root, l_objs, rotating, show):
            _apply_offset(root, spec)
            return True

    if len(link_wheels) >= 2 and not rotating:
        l_objs = [links.get(x) for x in link_wheels[:2]]
        if all(l_objs):
            _place_at_link_wheels(root, l_objs, spec)
            return True
        if report is not None:
            report("WARNING", "Crawler link wheels %r not found"
                   % (link_wheels[:2],))
        return False

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


def _distribute_segments(parts, root, pts, cum, length, track_width=1.0):
    """Place the origin-stacked chain segments of a forestry track along the
    belt outline (crawler-root-local frame, X = width, wrap plane = Y-Z).

    The chain alternates full-width tread plates (one interleaved half of the
    slots) and connecting links sitting half a pitch further along. Each part
    keeps its own geometry and is moved rigidly: local +Y follows the belt
    tangent, local +Z the outward normal; the plates' inner face (their min z,
    the sheet that rides on the tire) is seated on the wheel radius. In-game
    the motionPath shader does exactly this from the baked trackArray texture.

    *track_width* scales the part width (local X), matching the strip case."""
    parts = sorted(parts, key=lambda o: o.name)
    n = len(parts)
    paired = n >= 4 and n % 2 == 0
    plates = parts
    if paired:
        even, odd = parts[0::2], parts[1::2]

        def _med_z(group):
            es = sorted(_mesh_z_extent(o) for o in group)
            return es[len(es) // 2]

        # the plates are the flat group (small z extent); the links carry the
        # tall guide lugs
        plates = even if _med_z(even) <= _med_z(odd) else odd
    zb = -min(min((v.co.z for v in o.data.vertices), default=0.0)
              for o in plates)
    n_pitch = n // 2 if paired else n
    for i, o in enumerate(parts):
        k = i // 2 if paired else i
        half = 0.5 if (paired and i % 2 == 1) else 0.0
        s = (k + half) / n_pitch
        (a, b), (ta, tb) = i3d_crawler_path.sample(pts, cum, length, s * length)
        na, nb = tb, -ta                    # CCW outward normal in the wrap plane
        # Segment orientation settled EMPIRICALLY on the harvester1270G
        # (Olofsfors tracks, all four boggies confirmed visually): relative to
        # the naive (width, tangent, outward) frame - which MIRRORS the
        # segments (det -1, their outside facing inside) - the width axis and
        # the tangent and the radial axis are all negated, keeping the seat
        # position. det +1, plates ride on the tire, the tall tread bars
        # point outward.
        m = Matrix(((-track_width, 0.0, 0.0, 0.0),
                    (0.0, -ta, -na, a + na * zb),
                    (0.0, -tb, -nb, b + nb * zb),
                    (0.0, 0.0, 0.0, 1.0)))
        o.matrix_world = root.matrix_world @ m
        o["_i3d_crawler_wrapped"] = True


def _wrap_from_link_wheels(root, spec, links, report=None):
    """Wrap for crawlers WITHOUT ``<rotatingParts>`` (forestry boggie tracks):
    the belt circles the two vehicle link wheels. Wheel centres come from the
    link objects, the radius from the loaded tire meshes below them (crawler
    configs override the physics radius with band thickness included, so the
    visual mesh - not the config value - is the right source)."""
    show = spec.get("show_path")
    link_wheels = spec.get("link_wheels") or []
    if not show or len(link_wheels) < 2:
        return
    l_objs = [links.get(x) for x in link_wheels[:2]]
    if not all(l_objs):
        return
    refs = [_tire_ref(o) for o in l_objs]
    radius = max((r for _c, r in refs), default=0.0)
    if radius <= 0.0:
        if report is not None:
            report("WARNING", "Crawler %s: no tire mesh found to measure the "
                   "wheel radius, band left flat" % show)
        return
    inv = root.matrix_world.inverted()
    centres = [c if c is not None else o.matrix_world.translation.copy()
               for (c, _r), o in zip(refs, l_objs)]
    wheels = [((inv @ c).y, (inv @ c).z, radius) for c in centres]
    pts, cum, length = i3d_crawler_path.build_belt_path(wheels)
    if length <= 0.0:
        return

    prefix = show if show.endswith(">") else show + "|"

    def _in_show(o):
        # merged-children parts carry no node path of their own - walk up to
        # the nearest ancestor that does
        while o is not None:
            p = o.get("_i3d_node_path")
            if p is not None:
                return p == show or p.startswith(prefix)
            o = o.parent
        return False

    groups = {}
    for o in _descendants(root):
        if (o.type == 'MESH' and o.data and not o.hide_viewport
                and not o.get("_i3d_invisible_in_ge")   # hide_set() far LODs
                and _in_show(o) and _mesh_y_extent(o) < _STRIP_MIN_Y
                and o.parent is not None):
            groups.setdefault(o.parent, []).append(o)
    seg_sets = [v for v in groups.values() if len(v) >= _SEGMENT_MIN_COUNT]
    if not seg_sets:
        if report is not None:
            report("WARNING", "Crawler %s: no band segments found to wrap"
                   % show)
        return
    track_width = spec.get("track_width") or 1.0
    for parts in seg_sets:
        _distribute_segments(parts, root, pts, cum, length, track_width)


def _wrap_crawler(root, spec, links=None, report=None):
    """Wrap the shown track band of *root* around its wheels; hide any foreign
    variant band. Crawlers with ``<rotatingParts>`` bend their strip around the
    crawler's own wheels; without them (forestry tracks) the segments are
    distributed around the vehicle's link wheels."""
    show = spec.get("show_path")
    rotating = spec.get("rotating") or []
    if not show:
        return
    if not rotating:
        _wrap_from_link_wheels(root, spec, links or {}, report=report)
        return

    desc = _descendants(root)
    by_path = {o.get("_i3d_node_path"): o for o in desc}
    stem = spec.get("stem") or ""

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
        # leftNode == rightNode) or a DIFFERENT scene root (left/right as
        # separate roots, e.g. Olofsfors EX/KovaX: the other root was never
        # instantiated, so there is nothing to hide).
        hp = spec.get("hide_path")
        hide_key = hp[:hp.index(">") + 1] if hp and ">" in hp else None
        if hp and hp != show and hide_key == root_key:
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
                _wrap_crawler(root, spec, links=links, report=report)
            except Exception as exc:          # never let wrap break the load
                if report is not None:
                    report("WARNING", "Crawler wrap failed: %r" % exc)

    i3d_wheel_loader._restore_selection(prev_sel, prev_act)
    return placed
