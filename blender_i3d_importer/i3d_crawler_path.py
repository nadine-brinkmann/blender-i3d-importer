"""Pure geometry for wrapping a flat crawler track strip around its wheels.

No ``bpy`` here - everything works on plain tuples so it can be unit-tested
outside Blender. The loader (``i3d_crawler_loader``) measures the wheel
positions/radii from the imported scene, calls :func:`build_belt_path` to get
the rubber-band outline, then samples it per vertex to bend the flat 10 m track
strip around the wheels.

Wrap plane convention: the crawler lies in a 2D plane (for FS25 vehicles the
local Y-Z plane, X = track width). ``a`` is the fore/aft axis, ``b`` the up
axis; ``r`` a wheel radius.
"""

import math


def build_belt_path(wheels, step_deg=2.0):
    """Convex-hull belt (rubber-band outline) around coplanar disks, CCW.

    wheels: ``[(a, b, r), ...]``. Returns ``(points, cum, length)`` where
    ``points`` is a closed CCW polyline ``[(a, b), ...]`` tracing the outer
    belt (external tangents + arcs), ``cum`` the cumulative arc length
    (``len(cum) == len(points) + 1``, ``cum[-1] == length``). Disks that fall
    inside the hull (e.g. small road wheels tucked between big drive wheels)
    are naturally skipped.
    """
    n = len(wheels)
    if n < 2:
        if n == 1:
            a, b, r = wheels[0]
            steps = max(8, int(360 / step_deg))
            pts = [(a + r * math.cos(2 * math.pi * i / steps),
                    b + r * math.sin(2 * math.pi * i / steps))
                   for i in range(steps)]
            return pts, _cum(pts), _perimeter(pts)
        return [], [0.0], 0.0

    def rot(v, ang):
        c, s = math.cos(ang), math.sin(ang)
        return (v[0] * c - v[1] * s, v[0] * s + v[1] * c)

    def dep_normal(i, j):
        ax, ay, ri = wheels[i]
        bx, by, rj = wheels[j]
        dx, dy = bx - ax, by - ay
        d = math.hypot(dx, dy)
        if d < 1e-9:
            return None
        u = (dx / d, dy / d)
        c = max(-1.0, min(1.0, (ri - rj) / d))
        return rot(u, -math.acos(c))          # CCW outer external tangent

    start = min(range(n), key=lambda i: wheels[i][1] - wheels[i][2])
    hull = []
    cur = start
    n_in = (0.0, -1.0)
    for _ in range(n + 1):
        best = best_ang = best_nd = None
        base = math.atan2(n_in[1], n_in[0])
        for j in range(n):
            if j == cur:
                continue
            nd = dep_normal(cur, j)
            if nd is None:
                continue
            ang = (math.atan2(nd[1], nd[0]) - base) % (2 * math.pi)
            if best_ang is None or ang < best_ang:
                best_ang, best, best_nd = ang, j, nd
        hull.append((cur, best_nd))
        cur, n_in = best, best_nd
        if cur == start:
            break

    pts = []
    m = len(hull)
    for k in range(m):
        idx, dep = hull[k]
        arr = hull[(k - 1) % m][1]
        a, b, r = wheels[idx]
        a0 = math.atan2(arr[1], arr[0])
        a1 = math.atan2(dep[1], dep[0])
        sweep = (a1 - a0) % (2 * math.pi)
        steps = max(1, int(sweep / math.radians(step_deg)))
        for s in range(steps):
            t = a0 + sweep * s / steps
            pts.append((a + r * math.cos(t), b + r * math.sin(t)))
    return pts, _cum(pts), _perimeter(pts)


def _cum(pts):
    cum = [0.0]
    for i in range(1, len(pts) + 1):
        a = pts[i - 1]
        b = pts[i % len(pts)]
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return cum


def _perimeter(pts):
    return _cum(pts)[-1]


def sample(points, cum, length, dist):
    """Point ``(a, b)`` and unit tangent ``(ta, tb)`` at arc-length *dist*
    (wrapped modulo *length*, so the strip closes into a loop)."""
    if not points:
        return (0.0, 0.0), (1.0, 0.0)
    d = dist % length if length > 1e-9 else 0.0
    lo = 0
    for i in range(len(points)):
        if cum[i + 1] >= d:
            lo = i
            break
    a = points[lo]
    b = points[(lo + 1) % len(points)]
    seg = cum[lo + 1] - cum[lo]
    f = (d - cum[lo]) / seg if seg > 1e-9 else 0.0
    px = a[0] + (b[0] - a[0]) * f
    py = a[1] + (b[1] - a[1]) * f
    tx = b[0] - a[0]
    ty = b[1] - a[1]
    tl = math.hypot(tx, ty) or 1.0
    return (px, py), (tx / tl, ty / tl)


def inner_surface_z(coords, width, full_frac=0.7, min_frac=0.05, bins=24):
    """Z of the innermost full-width sheet of a flat track strip.

    A crawler track strip has, across its thickness: tread lugs (outer, ground
    side), two full-width structural sheets, and narrow central guide lugs
    (inner). The wheels ride on the *inner* full-width sheet - that z must be
    placed on the wheel radius so the band hugs the wheels (aligning the guide
    lug tips instead leaves a gap).

    coords: iterable of ``(x, y, z)``. Returns that z, or max z as a fallback.
    """
    zs = [c[2] for c in coords]
    if not zs:
        return 0.0
    zmin, zmax = min(zs), max(zs)
    span = (zmax - zmin) or 1.0
    total = len(coords)
    buckets = {}
    for x, _y, z in coords:
        bi = min(bins - 1, int((z - zmin) / span * bins))
        buckets.setdefault(bi, []).append((x, z))
    candidates = []
    for members in buckets.values():
        xspan = max(m[0] for m in members) - min(m[0] for m in members)
        if len(members) > total * min_frac and xspan >= full_frac * width:
            candidates.append(sum(m[1] for m in members) / len(members))
    return max(candidates) if candidates else zmax
