"""Resolve a vehicle's wheel configuration to the external tire/rim i3d files
and their attach nodes, for the wheel-loading feature.

Chain: vehicle XML ``<wheels><wheelConfigurations>`` -> a wheelConfiguration's
``<wheel dimensions=...>`` + ``<physics driveNode=.../>``, combined with a
``<tireCombination brand=.. names=..>`` -> ``tires/<brand>/<model>/<dim>.xml``
-> the tire / outerRim i3d paths. Pure data; no Blender.
"""

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class WheelSpec:
    dimensions: str
    drive_node: Optional[str]        # i3dMapping id where the wheel attaches
    is_left: bool
    rim_offset: float
    tire_i3d: Optional[str]          # resolved absolute path (or None)
    outer_rim_i3d: Optional[str]
    rim_width_diam: Optional[str]    # e.g. "23 38"
    tire_xml: Optional[str]          # the resolved tire config xml
    outer_rim_node: Optional[str]    # rim variant node, OUR path format (e.g. "1>0")
    inner_rim_node_left: Optional[str]
    inner_rim_node_right: Optional[str]
    # configId extras (tire xml <configurations>): a weight/additional part and
    # whether the inner rim is dropped (e.g. the "dual" config sets innerRim "-").
    add_i3d: Optional[str] = None
    add_node_left: Optional[str] = None
    add_node_right: Optional[str] = None
    add_offset: float = 0.0
    add_scale: Optional[str] = None      # e.g. "0.6 1 1" (direct scale, not w/diam)
    drop_inner_rim: bool = False
    phys_width: float = 0.0              # tire physics width (m) - for twin spacing
    base_x: float = 0.0                  # extra local-X shift (twin additional wheel)
    connector_i3d: Optional[str] = None  # twin connector i3d (dual001.i3d)
    connector_node_left: Optional[str] = None
    connector_node_right: Optional[str] = None
    # <connector> runtime parameters for the RIM_DUAL shader bake (see
    # i3d_wheel_loader._bake_connector_mesh). connector_gap is the game's
    # setConnectedWheel offset = mainWheel rimOffset + additionalWheel#offset
    # (Wheel.lua l.210), in metres; diameter/hookOffset/startPosOffset/
    # endPosOffset are the optional <connector> attributes in INCHES;
    # connector_simple is False for the useWidthAndDiam/usePosAndScale shader
    # variants (different game code path, not baked - approximation fallback).
    connector_gap: float = 0.0
    connector_scale: Optional[str] = None
    connector_diameter: Optional[float] = None
    connector_hook_offset: Optional[float] = None
    connector_start_pos_offset: float = 0.0
    connector_end_pos_offset: float = 0.0
    connector_simple: bool = True
    # usePosAndScale variant (HUB_DUAL shader, e.g. MT655 hubs/dual004.i3d):
    # startPos/endPos in inches + a uniform scale baked by the shader.
    connector_mode: str = "simple"       # "simple" | "posscale" | "widthdiam"
    connector_start_pos: Optional[float] = None
    connector_end_pos: Optional[float] = None
    connector_uniform_scale: Optional[float] = None
    # <wheel><innerRim node=.. offset=../> vehicle-XML override (e.g. Puma
    # NARROW_1500): picks a different, pre-modelled rim001.i3d node (e.g. the
    # "concave" variants) instead of the tire xml's flat default, plus its own
    # offset. innerRim is excluded from the general rimOffset addition, so this
    # is the only offset it gets.
    inner_rim_offset: float = 0.0
    # Full <wheel><innerRim>/<outerRim>/<tire> override support (corpus-
    # verified: 408 innerRim / 64 outerRim / 210 tire wheel-level overrides
    # across the base game). Each part can independently override its i3d
    # file, widthAndDiam, a raw direct scale (takes precedence - real game
    # mechanic, not our preview approximation, so it must survive export),
    # and isInverted (a baked 180 degree Z rotation).
    inner_rim_i3d: Optional[str] = None       # falls back to outer_rim_i3d
    inner_rim_width_diam: Optional[str] = None  # falls back to rim_width_diam
    inner_rim_scale: Optional[str] = None     # e.g. "0.6 1 1" (direct, not w/diam)
    inner_rim_is_inverted: bool = False
    outer_rim_scale: Optional[str] = None
    outer_rim_is_inverted: bool = False
    tire_is_inverted: bool = False


@dataclass
class HubSpec:
    """A wheel hub/centre cap from the vehicle's <wheels><hubs> section."""
    link_node: str                   # i3dMapping id of the drive node it mounts to
    hub_i3d: Optional[str]           # resolved hubs.i3d path
    node: Optional[str]              # chosen left/right node, OUR path format
    is_left: bool
    scale: Optional[str]             # e.g. "0.45 0.45 0.45" (direct scale)
    offset: float = 0.0              # X offset (signed by isLeft)


def _abs_existing(data_dir, rel):
    p = os.path.join(data_dir, rel)
    return p if os.path.isfile(p) else None


def _fs_node_to_ours(p):
    """FS index path (e.g. "1|0") -> our _i3d_node_path ("1>0"): first | -> >."""
    if not p:
        return None
    return p.replace("|", ">", 1)


def _abs_data(data_dir, dollar_path):
    if not dollar_path or not dollar_path.startswith("$data/"):
        return None
    p = os.path.join(data_dir, dollar_path[len("$data/"):])
    return p if os.path.isfile(p) else None


_TIRE_REGISTRY_CACHE = {}


def _tire_registry(data_dir):
    """Parse shared/wheels/wheels.xml into (brand, model, dim, category, prio).
    Cached per data_dir. Used when a vehicle has no <tireCombination> but a
    tireCategories list (tires are then drawn from this shared registry)."""
    if data_dir in _TIRE_REGISTRY_CACHE:
        return _TIRE_REGISTRY_CACHE[data_dir]
    reg = []
    try:
        root = ET.parse(os.path.join(data_dir, "shared", "wheels",
                                     "wheels.xml")).getroot()
    except (ET.ParseError, OSError):
        _TIRE_REGISTRY_CACHE[data_dir] = reg
        return reg
    for w in root.iter("wheel"):
        fn = w.get("filename") or ""
        if "/tires/" not in fn:
            continue
        parts = fn.split("/tires/")[1].split("/")
        if len(parts) < 3:
            continue
        brand, model, dimfile = parts[0], parts[1], parts[2]
        dim = dimfile[:-4] if dimfile.lower().endswith(".xml") else dimfile
        try:
            prio = float(w.get("priority") or 1)
        except ValueError:
            prio = 1.0
        reg.append((brand, model, dim, w.get("category") or "", prio))
    _TIRE_REGISTRY_CACHE[data_dir] = reg
    return reg


def _category_combos(data_dir, categories, dims):
    """Brand-level tire options for ONE config: brands whose registry tires cover
    ALL of the config's *dims* (intersection) in one of *categories*. Ordered by
    the brand's best priority (the shop's default first). The model is resolved
    per dim later (a brand often uses different models front/back), so each
    option is ``(brand, None)``. This mirrors the in-game brand selector, which
    only offers manufacturers that supply every wheel size of the chosen config.
    """
    dims = set(dims)
    if not dims:
        return []
    cover = {}                       # brand -> {dim: best_prio}
    for brand, model, dim, cat, prio in _tire_registry(data_dir):
        # empty *categories* = no category filter (vehicles with explicit
        # dimensions but no tireCombination/tireCategories, e.g. skid-steers):
        # resolve by dimension alone.
        if (not categories or cat in categories) and dim in dims:
            cover.setdefault(brand, {})
            cover[brand][dim] = max(cover[brand].get(dim, 0.0), prio)
    opts = [(brand, max(dmap.values()))
            for brand, dmap in cover.items() if dims <= set(dmap)]
    opts.sort(key=lambda t: (-t[1], t[0]))
    return [(b, None) for b, _ in opts]


def _real_tire_combinations(el):
    """<tireCombination> children of *el* that name a real model. Placeholder
    entries with names ""/"-" (e.g. the Puma's per-config NOKIAN "-") are not a
    brand restriction - they mean "use the tireCategories registry"."""
    out = []
    if el is None:
        return out
    for tc in el.findall("tireCombination"):
        names = (tc.get("names") or "").strip()
        if names in ("", "-"):
            continue
        out.append((tc.get("brand", ""), names))
    return out


def _config_combos(cfg_el, cfgs_parent, data_dir, dims):
    """Tire (brand, model) options for ONE wheel configuration: explicit
    <tireCombination> (per-config, else parent-level), otherwise the shared
    registry scoped to this config's tireCategories + *dims*. Registry options are
    brand-level (model None); explicit options carry their model name."""
    combos = _real_tire_combinations(cfg_el) or _real_tire_combinations(cfgs_parent)
    if combos:
        return combos
    cats = cfg_el.get("tireCategories")
    if not cats and cfgs_parent is not None:
        cats = cfgs_parent.get("tireCategories")
    # Empty cats -> _category_combos matches by dimension alone (no category
    # filter), covering vehicles with explicit dims but no tireCombination/
    # tireCategories.
    return _category_combos(data_dir, set((cats or "").split()), dims)


def _registry_tire_file(data_dir, brand, dim):
    """Best-priority registry tire xml for (brand, dim), or None."""
    best = None
    for b, model, d, cat, prio in _tire_registry(data_dir):
        if b == brand and d == dim and (best is None or prio > best[0]):
            best = (prio, model)
    if best is None:
        return None
    return _abs_existing(data_dir, os.path.join(
        "shared", "wheels", "tires", brand.lower(), best[1], dim + ".xml"))


def _resolve_tire_xml(data_dir, combos, dim, brand_index=0):
    """tires xml for the chosen brand at *dim*, falling back to the other brands
    (in order) when that brand lacks the dimension. Registry brands (model None)
    resolve their best model for *dim*; explicit combos use their model name."""
    order = list(range(len(combos)))
    if 0 <= brand_index < len(combos):
        order = [brand_index] + [i for i in order if i != brand_index]
    for i in order:
        brand, model = combos[i]
        if model is None:
            cand = _registry_tire_file(data_dir, brand, dim)
        else:
            # <tireCombination names> may list SEVERAL models (e.g. "TM600
            # TM1000"): a brand uses different models per dimension (TM600 for
            # narrow, TM1000 for wide). Pick whichever model has the file.
            cand = None
            for m in model.split():
                cand = _abs_existing(data_dir, os.path.join(
                    "shared", "wheels", "tires", brand.lower(), m, dim + ".xml"))
                if cand:
                    break
        if cand:
            return cand
    return None


def _save_id_map(configs):
    return {c.get("saveId"): c for c in configs if c.get("saveId")}


def _effective_wheels(config, sid_map, _seen=None):
    """Merged wheel dicts for *config*, applying ``<wheels baseConfig=..>``
    inheritance per wheel index. A child ``<wheel/>`` inherits everything from the
    base config's wheel at the same index and overrides what it sets (dimensions,
    isLeft, rimOffset, physics/driveNode). Returns a list of attr dicts.
    """
    _seen = _seen if _seen is not None else set()
    wn = config.find("wheels")
    if wn is None:
        return []
    base = []
    base_id = wn.get("baseConfig")
    if base_id and base_id in sid_map and base_id not in _seen:
        _seen.add(base_id)
        base = _effective_wheels(sid_map[base_id], sid_map, _seen)
    out = []
    for i, w in enumerate(wn.findall("wheel")):
        merged = dict(base[i]) if i < len(base) else {}
        # "filename" is the direct-tire-xml form (<wheel filename="..."> e.g.
        # berthoud winAir) - the runtime equivalent of what dimension configs
        # are expanded into (VehicleConfigurationItemWheel inserts #filename).
        for k in ("dimensions", "filename", "isLeft", "rimOffset", "configId"):
            v = w.get(k)
            if v is not None:
                merged[k] = v
        phys = w.find("physics")
        if phys is not None:
            dn = phys.get("driveNode") or phys.get("repr")
            if dn:
                merged["driveNode"] = dn
        # `dimensions` may list several sizes ("27x8_5R15 27x10_5__15"): kept full
        # here; the caller selects a size column via _wheel_dim(.., dim_col).
        merged["_el"] = w  # source element of the SELECTED config
        # Element chain (nearest config first) for per-attribute inheritance
        # of <wheel> child elements (innerRim/outerRim/tire/additional/
        # additionalWheel) through baseConfig: the game resolves EVERY
        # attribute independently through this chain (WheelXMLObject:getValue),
        # so e.g. Farmall120C BROAD's <innerRim offset="0.02"/> (no filename)
        # inherits DEFAULT's filename=rim006.i3d + nodeLeft/nodeRight (#10).
        merged["_els"] = [w] + (base[i].get("_els", []) if i < len(base) else [])
        out.append(merged)
    return out


def _wheel_dim(dims_str, dim_col):
    """The dimension for size column *dim_col* of a wheel whose `dimensions` may
    list several sizes. Falls back to the first size; "-" (a gap in a size column)
    yields None."""
    if not dims_str:
        return None
    parts = dims_str.split()
    d = parts[dim_col] if 0 <= dim_col < len(parts) else parts[0]
    return None if d == "-" else d


def _config_dims(wheels, dim_col):
    """Set of dimensions for size column *dim_col* across *wheels*."""
    return {d for d in (_wheel_dim(w.get("dimensions"), dim_col) for w in wheels)
            if d}


def parse_brands(vehicle_xml_path, data_dir=None, config_index=0, dim_col=0):
    """List the tire brands as [(index, brand, names)] for the SELECTED wheel
    configuration + size column: explicit <tireCombination> or the registry brands
    that cover this config's dimensions. ``names`` is None for a registry brand
    (model resolved per dim). Scoping to config_index/dim_col keeps the list at the
    in-game length (e.g. 6 for the Puma default) instead of spanning everything."""
    try:
        root = ET.parse(vehicle_xml_path).getroot()
    except (ET.ParseError, OSError):
        return []
    cfgs = root.find("wheels/wheelConfigurations")
    if cfgs is None:
        return []
    configs = cfgs.findall("wheelConfiguration")
    if not configs:
        return []
    idx = config_index if 0 <= config_index < len(configs) else 0
    sid_map = _save_id_map(configs)
    wheels = _effective_wheels(configs[idx], sid_map)
    dims = _config_dims(wheels, dim_col)
    combos = _config_combos(configs[idx], cfgs, data_dir or "", dims)
    return [(i, b, m) for i, (b, m) in enumerate(combos)]


def parse_wheel_options(vehicle_xml_path):
    """Wheel selector options as ``[(config_index, dim_col, label)]``, expanding a
    multi-size `dimensions` attribute into one option per size column. Single-size
    configs yield one option per configuration (label = config name); multi-size
    configs yield one option per size (label = the size string). Returns [] when
    the vehicle has no wheel configurations."""
    try:
        root = ET.parse(vehicle_xml_path).getroot()
    except (ET.ParseError, OSError):
        return []
    cfgs = root.find("wheels/wheelConfigurations")
    if cfgs is None:
        return []
    configs = cfgs.findall("wheelConfiguration")
    if not configs:
        return []
    sid_map = _save_id_map(configs)
    out = []
    for i, c in enumerate(configs):
        wheels = _effective_wheels(c, sid_map)
        ncols = max((len(w.get("dimensions", "").split())
                     for w in wheels if w.get("dimensions")), default=1)
        # numDynamicConfigurations caps how many shop configs the game
        # generates from the size columns (default: unlimited). farmall120C
        # sets 1 on every wheelConfiguration - in the in-game store each shows
        # up exactly ONCE; the extra columns are only alternatives for tire-
        # brand coverage (#11: we wrongly offered every column as an option,
        # three configs became nine buttons). Which column the game's single
        # config ends up using depends on brand priority
        # (g_wheelManager:getTiresForDimensionCombinations, engine-side, not
        # in the script dump) - we take the columns in document order.
        try:
            cap = int(c.get("numDynamicConfigurations") or 0)
        except ValueError:
            cap = 0
        if cap > 0:
            ncols = min(ncols, cap)
        base = c.get("name") or c.get("saveId") or ("Config %d" % i)
        for col in range(ncols):
            # Remaining multi-column configs: the game names them all
            # identically and disambiguates as "Name (2)", "Name (3)"
            # (VehicleConfigurationItemWheel.generateConfigurations). Size
            # labels looked leaner but repeat (two columns often share the
            # front size) and hid WHICH config a button belongs to.
            label = base if col == 0 else "%s (%d)" % (base, col + 1)
            out.append((i, col, label))
    return out


def parse_wheel_configs(vehicle_xml_path):
    """List the vehicle's wheel configurations as ``[(index, label)]`` for the UI.
    ``label`` is the raw ``name`` ($l10n key or text); the caller prettifies it.
    Returns [] when the vehicle has no wheel configurations.
    """
    try:
        root = ET.parse(vehicle_xml_path).getroot()
    except (ET.ParseError, OSError):
        return []
    cfgs = root.find("wheels/wheelConfigurations")
    if cfgs is None:
        return []
    out = []
    for i, c in enumerate(cfgs.findall("wheelConfiguration")):
        out.append((i, c.get("name") or c.get("saveId") or ("Config %d" % i)))
    return out


def _tire_parts(troot, data_dir, config_id):
    """Resolve tire/rim/additional parts from a tire xml root for *config_id*.
    Returns a dict; configId pulls in <configurations> overrides (weight3 adds an
    <additional>, dual sets innerRim "-" -> drop_inner).
    """
    r = dict(tire_i3d=None, tire_inverted=False,
             outer_rim=None, rim_wd=None, outer_node=None,
             outer_scale=None, outer_inverted=False,
             inner_l=None, inner_r=None, inner_i3d=None, inner_wd=None,
             inner_scale=None, inner_inverted=False, inner_offset=0.0,
             add_i3d=None, add_nl=None, add_nr=None,
             add_offset=0.0, add_scale=None, drop_inner=False, phys_width=0.0)
    if troot is None:
        return r
    default = troot.find("default")
    if default is not None:
        ph = default.find("physics")
        if ph is not None:
            try:
                r["phys_width"] = float(ph.get("width") or 0.0)
            except ValueError:
                pass
        te = default.find("tire")
        if te is not None:
            r["tire_i3d"] = _abs_data(data_dir, te.get("filename"))
            r["tire_inverted"] = (te.get("isInverted") == "true")
        ore = default.find("outerRim")
        if ore is not None:
            r["outer_rim"] = _abs_data(data_dir, ore.get("filename"))
            r["rim_wd"] = ore.get("widthAndDiam")
            r["outer_node"] = _fs_node_to_ours(ore.get("node"))
            r["outer_scale"] = ore.get("scale")
            r["outer_inverted"] = (ore.get("isInverted") == "true")
        ire = default.find("innerRim")
        if ire is not None:
            r["inner_l"] = _fs_node_to_ours(ire.get("nodeLeft"))
            r["inner_r"] = _fs_node_to_ours(ire.get("nodeRight"))
            # innerRim usually shares the outerRim's i3d/widthAndDiam unless it
            # states its own (TM100 example: both explicitly "7 44", same file).
            r["inner_i3d"] = _abs_data(data_dir, ire.get("filename")) or r["outer_rim"]
            r["inner_wd"] = ire.get("widthAndDiam") or r["rim_wd"]
            r["inner_scale"] = ire.get("scale")
            r["inner_inverted"] = (ire.get("isInverted") == "true")
    if config_id and config_id != "default":
        for c in troot.findall("configurations/configuration"):
            if c.get("id") != config_id:
                continue
            ir = c.find("innerRim")
            if ir is not None and not (ir.get("filename") or "").strip("-"):
                r["drop_inner"] = True
            ad = c.find("additional")
            if ad is not None:
                r["add_i3d"] = _abs_data(data_dir, ad.get("filename"))
                r["add_nl"] = _fs_node_to_ours(ad.get("nodeLeft"))
                r["add_nr"] = _fs_node_to_ours(ad.get("nodeRight"))
                r["add_scale"] = ad.get("scale")
                try:
                    r["add_offset"] = float(ad.get("offset") or 0.0)
                except ValueError:
                    pass
    return r


def _chain_child_attr(els, tag, attr, alt_attr=None):
    """Resolve ``<wheel>/<tag>@attr`` through the baseConfig element chain
    (*els*, nearest config first). Mirrors WheelXMLObject:getValue: every
    attribute inherits independently through the chain and the nearest level
    that defines it wins. A literal "-" is the game's explicit-clear marker
    and is returned as-is so callers can distinguish "cleared" from "absent".
    *alt_attr* mirrors getValueAlternative (checked per level, e.g. nodeLeft
    falling back to node).

    NOTE: getXMLFileAndKey's body is missing from the decompiled game dump,
    so "vehicle chain before tire-xml default" precedence is inferred (the
    "-"-clear convention exists exactly for chain inheritance) and matches
    the in-game reference for Farmall120C: BROAD/WEIGHTS/NARROW show
    DEFAULT's dished rim006, not the tire xml's flat default rim (#10)."""
    for el in els:
        if el is None:
            continue
        child = el.find(tag)
        if child is None:
            continue
        v = child.get(attr)
        if v is None and alt_attr is not None:
            v = child.get(alt_attr)
        if v is not None:
            return v
    return None


def _apply_wheel_additional(els, data_dir, mp):
    """A ``<wheel>`` in the vehicle XML can carry its own ``<additional>``
    (e.g. the WEIGHTS configuration's wheel weights:
    ``<additional filename=".../weights/cnh/weight001.i3d" nodeLeft nodeRight
    offset/>``). Resolved per attribute through the baseConfig chain *els*
    (nearest first); values found there override the tire xml's configId
    ``<additional>`` already in *mp*, absent attributes keep the tire xml's
    value (per-attribute merge, like the game). filename "-" drops the part.
    Mutates *mp* in place."""
    if not any(el is not None and el.find("additional") is not None
               for el in els):
        return
    fn = _chain_child_attr(els, "additional", "filename")
    if fn == "-":
        mp["add_i3d"] = None
        return
    if fn:
        path = _abs_data(data_dir, fn)
        if path:
            mp["add_i3d"] = path
    if mp.get("add_i3d") is None:
        return
    nl = _chain_child_attr(els, "additional", "nodeLeft", alt_attr="node")
    if nl and nl != "-":
        mp["add_nl"] = _fs_node_to_ours(nl)
    nr = _chain_child_attr(els, "additional", "nodeRight", alt_attr="node")
    if nr and nr != "-":
        mp["add_nr"] = _fs_node_to_ours(nr)
    sc = _chain_child_attr(els, "additional", "scale")
    if sc and sc != "-":
        mp["add_scale"] = sc
    off = _chain_child_attr(els, "additional", "offset")
    if off is not None and off != "-":
        try:
            mp["add_offset"] = float(off)
        except ValueError:
            pass


def _wheel_visualpart_override(els, tag, data_dir, is_left):
    """Read a ``<wheel>``'s ``<innerRim>``/``<outerRim>``/``<tire>`` override
    (vehicle XML), resolved per attribute through the baseConfig element
    chain *els* (nearest config first) - e.g. Farmall120C BROAD only sets
    ``<innerRim offset="0.02"/>`` and inherits filename/nodeLeft/nodeRight
    from DEFAULT's ``<innerRim filename=".../rim006.i3d" .../>`` (#10), while
    Puma NARROW_1500 fully overrides:
    ``<innerRim filename=".../rim001.i3d" node="6|1" offset="-0.01"/>``.

    Corpus-verified (758 base-game vehicle xmls): 408 innerRim / 64 outerRim /
    210 tire wheel-level overrides exist. innerRim commonly also carries its
    own ``filename`` (354), ``widthAndDiam`` (196) or a raw ``scale`` (52);
    node is given either as a single ``node`` (166, this ``<wheel>`` is
    already side-specific) or as ``nodeLeft``/``nodeRight`` (160, same
    fallback as the tire xml's own default - mirrored here). outerRim never
    used nodeLeft/nodeRight or scale in the corpus, tire only ever used
    ``isInverted`` (36) - implemented anyway for schema completeness since the
    game accepts all these on any of the three.

    Returns a dict with only the keys ACTUALLY present somewhere in the
    chain, so the caller updates its resolved defaults selectively (an absent
    key means "keep whatever the tire xml/default already gave"). filename
    "-" (the game's explicit-clear) returns ``{"drop": True}``.
    """
    out = {}
    if not any(el is not None and el.find(tag) is not None for el in els):
        return out
    fn = _chain_child_attr(els, tag, "filename")
    if fn == "-":
        return {"drop": True}
    if fn:
        path = _abs_data(data_dir, fn)
        if path:
            out["i3d"] = path
    raw_node = _chain_child_attr(
        els, tag, "nodeLeft" if is_left else "nodeRight", alt_attr="node")
    if raw_node and raw_node != "-":
        node = _fs_node_to_ours(raw_node)
        if node:
            out["node"] = node
    wd = _chain_child_attr(els, tag, "widthAndDiam")
    if wd and wd != "-":
        out["wd"] = wd
    sc = _chain_child_attr(els, tag, "scale")
    if sc and sc != "-":
        out["scale"] = sc
    off = _chain_child_attr(els, tag, "offset")
    if off is not None and off != "-":
        try:
            out["offset"] = float(off)
        except ValueError:
            pass
    out["is_inverted"] = (_chain_child_attr(els, tag, "isInverted") == "true")
    return out


def _apply_wheel_part_overrides(els, data_dir, mp, is_left):
    """Apply all three ``<wheel>``-level part overrides (innerRim/outerRim/
    tire) onto *mp* in place, resolved through the baseConfig chain *els*.
    Absent attributes keep the tire xml's default (already in *mp* from
    ``_tire_parts``); filename "-" drops the part."""
    ov = _wheel_visualpart_override(els, "innerRim", data_dir, is_left)
    if ov.get("drop"):
        mp["drop_inner"] = True
    else:
        if "i3d" in ov:
            mp["inner_i3d"] = ov["i3d"]
            # An explicit vehicle-XML innerRim FILE overrides an external
            # configId drop: the vehicle level is nearer in the game's
            # attribute chain than the tire xml's <configuration id="dual">
            # <innerRim filename="-"> (MT655 twin discs, rim010/011/012).
            mp["drop_inner"] = False
        if "node" in ov:
            mp["inner_l"] = mp["inner_r"] = ov["node"]
        if "wd" in ov:
            mp["inner_wd"] = ov["wd"]
        if "scale" in ov:
            mp["inner_scale"] = ov["scale"]
        if "offset" in ov:
            mp["inner_offset"] = ov["offset"]
        if ov.get("is_inverted"):
            mp["inner_inverted"] = True

    ov = _wheel_visualpart_override(els, "outerRim", data_dir, is_left)
    if ov.get("drop"):
        mp["outer_rim"] = None
    else:
        if "i3d" in ov:
            mp["outer_rim"] = ov["i3d"]
        if "node" in ov:
            mp["outer_node"] = ov["node"]
        if "wd" in ov:
            mp["rim_wd"] = ov["wd"]
        if "scale" in ov:
            mp["outer_scale"] = ov["scale"]
        if ov.get("is_inverted"):
            mp["outer_inverted"] = True

    ov = _wheel_visualpart_override(els, "tire", data_dir, is_left)
    if ov.get("drop"):
        mp["tire_i3d"] = None
    else:
        if "i3d" in ov:
            mp["tire_i3d"] = ov["i3d"]
        if ov.get("is_inverted"):
            mp["tire_inverted"] = True


def _spec_from_parts(dim, drive, is_left, rim_offset, tire_xml, mp, base_x=0.0):
    return WheelSpec(dim, drive, is_left, rim_offset,
                     mp["tire_i3d"], mp["outer_rim"], mp["rim_wd"], tire_xml,
                     mp["outer_node"], mp["inner_l"], mp["inner_r"],
                     mp["add_i3d"], mp["add_nl"], mp["add_nr"], mp["add_offset"],
                     mp["add_scale"], mp["drop_inner"], mp["phys_width"], base_x,
                     inner_rim_offset=mp.get("inner_offset", 0.0),
                     inner_rim_i3d=mp.get("inner_i3d"),
                     inner_rim_width_diam=mp.get("inner_wd"),
                     inner_rim_scale=mp.get("inner_scale"),
                     inner_rim_is_inverted=mp.get("inner_inverted", False),
                     outer_rim_scale=mp.get("outer_scale"),
                     outer_rim_is_inverted=mp.get("outer_inverted", False),
                     tire_is_inverted=mp.get("tire_inverted", False))


def parse_rim_colors(vehicle_xml_path):
    """List <rimColorConfigurations> as [(index, materialTemplateName, selectable)].
    selectable is False for isSelectable="false" entries (e.g. the NH/Steyr white
    rims that are auto-applied with their design brand, not user-choosable)."""
    try:
        root = ET.parse(vehicle_xml_path).getroot()
    except (ET.ParseError, OSError):
        return []
    el = root.find("rimColorConfigurations")
    if el is None:
        return []
    out = []
    for i, c in enumerate(el.findall("rimColorConfiguration")):
        t = c.get("materialTemplateName")
        if t:
            out.append((i, t, c.get("isSelectable") != "false"))
    return out


def resolve_hubs(vehicle_xml_path, data_dir):
    """Resolve the vehicle's <wheels><hubs> to placeable HubSpecs.

    Each <hub linkNode=.. filename=hub_xml isLeft scale offset/> points to a hub
    config xml that references a shared hubs.i3d + a left/right node. Mirrors
    Wheels.loadHubFromXML / onWheelHubI3DLoaded.
    """
    try:
        root = ET.parse(vehicle_xml_path).getroot()
    except (ET.ParseError, OSError):
        return []
    hubs_el = root.find("wheels/hubs")
    if hubs_el is None:
        return []
    out = []
    for h in hubs_el.findall("hub"):
        link = h.get("linkNode")
        if not link:
            continue
        is_left = (h.get("isLeft") == "true")
        scale = h.get("scale")
        try:
            offset = float(h.get("offset") or 0.0)
        except ValueError:
            offset = 0.0
        hub_xml = _abs_data(data_dir, h.get("filename"))
        hub_i3d = node = None
        if hub_xml:
            try:
                hroot = ET.parse(hub_xml).getroot()
            except (ET.ParseError, OSError):
                hroot = None
            if hroot is not None:
                hub_i3d = _abs_data(data_dir, (hroot.findtext("filename") or "").strip())
                nodes_el = hroot.find("nodes")
                if nodes_el is not None:
                    node = _fs_node_to_ours(
                        nodes_el.get("left" if is_left else "right"))
        out.append(HubSpec(link, hub_i3d, node, is_left, scale, offset))
    return out


def resolve_wheels(vehicle_xml_path, data_dir, config_index=0, dim_col=0,
                   brand_index=0) -> List[WheelSpec]:
    """Return the WheelSpecs of one wheel configuration + size column.

    baseConfig inheritance is resolved, so configs that inherit their wheels
    from another config (e.g. weight/twin variants) still resolve their tires.
    dim_col picks a size when a wheel's `dimensions` lists several (multi-size).
    """
    try:
        root = ET.parse(vehicle_xml_path).getroot()
    except (ET.ParseError, OSError):
        return []
    cfgs = root.find("wheels/wheelConfigurations")
    if cfgs is None:
        return []
    configs = cfgs.findall("wheelConfiguration")
    if not configs:
        return []
    idx = config_index if 0 <= config_index < len(configs) else 0
    sid_map = _save_id_map(configs)
    wheels = _effective_wheels(configs[idx], sid_map)
    if not wheels:
        return []
    # Tire brands are scoped to THIS config's dimensions (the in-game brand list
    # only offers manufacturers that supply every wheel size of the config).
    dims = _config_dims(wheels, dim_col)
    combos = _config_combos(configs[idx], cfgs, data_dir, dims)

    specs = []
    for w in wheels:
        dim = _wheel_dim(w.get("dimensions"), dim_col)
        fname = w.get("filename")
        if not dim and not fname:
            continue
        drive = w.get("driveNode")
        is_left = (w.get("isLeft") == "true")
        try:
            rim_offset = float(w.get("rimOffset") or 0.0)
        except ValueError:
            rim_offset = 0.0
        config_id = w.get("configId")
        if dim:
            tire_xml = _resolve_tire_xml(data_dir, combos, dim, brand_index)
        else:
            # Direct-tire-xml wheel (<wheel filename="...">, e.g. berthoud
            # winAir, #15): no dimension/brand lookup, the tire config XML is
            # given outright - same file format the dimension path resolves
            # to. Use the file stem as the pseudo-dimension for labels.
            tire_xml = _abs_data(data_dir, fname)
            dim = os.path.splitext(os.path.basename(tire_xml or fname))[0]
        troot = None
        if tire_xml:
            try:
                troot = ET.parse(tire_xml).getroot()
            except (ET.ParseError, OSError):
                troot = None
        mp = _tire_parts(troot, data_dir, config_id)
        els = w.get("_els") or [w.get("_el")]
        _apply_wheel_additional(els, data_dir, mp)
        _apply_wheel_part_overrides(els, data_dir, mp, is_left)
        specs.append(_spec_from_parts(dim, drive, is_left, rim_offset, tire_xml, mp))

        # Twin wheels: each <additionalWheel> is a second wheel mounted outboard.
        # Its X = main_X + sign*(rimOffset + addOffset + 0.5*mainWidth + 0.5*addWidth)
        # (WheelVisual:setConnectedWheel). configId="dual" drops its inner rim.
        # The shader-procedural connector hub is NOT reproduced here.
        # <additionalWheel> from the nearest chain element that defines one
        # (a config inheriting a twin-wheel base keeps its additional wheels).
        aw_host = next((e for e in els
                        if e is not None
                        and e.find("additionalWheel") is not None), None)
        if aw_host is not None:
            for aw in aw_host.findall("additionalWheel"):
                try:
                    aw_off = float(aw.get("offset") or 0.0)
                except ValueError:
                    aw_off = 0.0
                aw_left = (aw.get("isLeft") == "true"
                           if aw.get("isLeft") is not None else is_left)
                ap = _tire_parts(troot, data_dir, aw.get("configId"))
                # The <additionalWheel> element can carry its OWN part
                # overrides - MT655's twins define the outer twin DISC here
                # (<innerRim filename="rim010/011/012.i3d" node="1|x"
                # widthAndDiam/offset>), while the tire xml's dual config
                # drops the inner rim; the vehicle-XML override is nearer in
                # the chain and wins (drop is cleared in
                # _apply_wheel_part_overrides). Also covers <additional> and
                # tire/outerRim overrides on additional wheels.
                _apply_wheel_additional([aw], data_dir, ap)
                _apply_wheel_part_overrides([aw], data_dir, ap, aw_left)
                mw = mp["phys_width"]
                aw_w = ap["phys_width"] or mw
                raw = rim_offset + aw_off
                val = raw + 0.5 * mw + 0.5 * aw_w
                if not aw_left:
                    val = -val
                if raw < 0:
                    val = -val
                # connector (twin spacer cage) referenced by the additionalWheel
                conn_i3d = conn_nl = conn_nr = None
                ce = aw.find("connector")
                if ce is not None:
                    fn = ce.get("filename") or ""
                    if fn.lower().endswith(".i3d"):
                        # Direct i3d + node form (MT655:
                        # <connector filename="...hubs/dual004.i3d" node="0|0"
                        # usePosAndScale="true">) - one side-specific node,
                        # no wrapper XML. WheelVisualPart:loadFromXML reads
                        # #node as the shared fallback for both sides.
                        conn_i3d = _abs_data(data_dir, fn)
                        conn_nl = _fs_node_to_ours(ce.get("nodeLeft")
                                                   or ce.get("node"))
                        conn_nr = _fs_node_to_ours(ce.get("nodeRight")
                                                   or ce.get("node"))
                    else:
                        conn_xml = _abs_data(data_dir, fn)
                        if conn_xml:
                            try:
                                croot = ET.parse(conn_xml).getroot()
                            except (ET.ParseError, OSError):
                                croot = None
                            fe = (croot.find("file") if croot is not None
                                  else None)
                            if fe is not None:
                                conn_i3d = _abs_data(data_dir, fe.get("name"))
                                conn_nl = _fs_node_to_ours(fe.get("leftNode"))
                                conn_nr = _fs_node_to_ours(fe.get("rightNode"))
                # additional wheel uses rimOffset 0 (game passes 0); shift = base_x
                _asp = _spec_from_parts(dim, drive, aw_left, 0.0,
                                        tire_xml, ap, base_x=val)
                _asp.connector_i3d = conn_i3d
                _asp.connector_node_left = conn_nl
                _asp.connector_node_right = conn_nr
                if ce is not None:
                    def _cf(attr):
                        v = ce.get(attr)
                        try:
                            return float(v) if v is not None else None
                        except ValueError:
                            return None
                    # game: setConnectedWheel(main.rimOffset + aw#offset)
                    _asp.connector_gap = raw
                    _asp.connector_scale = ce.get("scale")
                    _asp.connector_diameter = _cf("diameter")
                    _asp.connector_hook_offset = _cf("hookOffset")
                    _asp.connector_start_pos_offset = _cf("startPosOffset") or 0.0
                    _asp.connector_end_pos_offset = _cf("endPosOffset") or 0.0
                    _asp.connector_start_pos = _cf("startPos")
                    _asp.connector_end_pos = _cf("endPos")
                    _asp.connector_uniform_scale = _cf("uniformScale")
                    if ce.get("usePosAndScale") == "true":
                        _asp.connector_mode = "posscale"
                    elif ce.get("useWidthAndDiam") == "true":
                        _asp.connector_mode = "widthdiam"
                    _asp.connector_simple = (_asp.connector_mode == "simple")
                specs.append(_asp)
    return specs


# ---------------------------------------------------------------------------
# Crawler tracks (Raupenfahrwerke)
# ---------------------------------------------------------------------------
# A wheelConfiguration can carry a <crawlers> section instead of / next to
# <wheels>. Each <crawler filename="....xml" linkNode=".." isLeft=".."/> points
# at a crawler XML (NOT the i3d directly); that XML's <file name=".i3d"
# leftNode="0|0" rightNode="0|1"/> names the actual track i3d plus which of its
# two top-level nodes is the left resp. right side. MVP: linkNode form (Lexion);
# the Jaguar's linkWheelNodes form is a follow-up.


def _read_crawler_xml(data_dir, cr_xml_abs):
    """Return (i3d_abs, left_path, right_path, rotating) from a crawler XML.

    left/right paths use our ``_i3d_node_path`` form (e.g. "0|0" -> "0>0").
    rotating is ``[(node_str, radius_float), ...]`` from ``<rotatingParts>``
    (node kept raw, resolved against the shown side at load time).
    """
    try:
        root = ET.parse(cr_xml_abs).getroot()
    except (ET.ParseError, OSError):
        return (None, None, None, [])
    f = root.find("file")
    if f is None:
        return (None, None, None, [])
    rotating = []
    rp = root.find("rotatingParts")
    if rp is not None:
        for r in rp.findall("rotatingPart"):
            node = r.get("node")
            try:
                radius = float(r.get("radius"))
            except (TypeError, ValueError):
                continue
            if node:
                rotating.append((node, radius))
    return (_abs_data(data_dir, f.get("name")),
            _fs_node_to_ours(f.get("leftNode")),
            _fs_node_to_ours(f.get("rightNode")),
            rotating)


def parse_crawlers(vehicle_xml_path, data_dir, config_index=0):
    """Crawler specs for the wheelConfiguration at *config_index*.

    Returns [] when that config has no <crawlers> (wheel-only configs) so the
    caller can no-op. Each spec::

        {
            "i3d":        absolute path to the crawler .i3d,
            "link_node":  i3dMapping id the crawler mounts on (linkNode),
            "is_left":    bool,
            "show_path":  _i3d_node_path of the side to keep visible,
            "hide_path":  _i3d_node_path of the side to hide,
        }

    MVP: only <crawler> entries that use the single ``linkNode`` attribute are
    returned; ``linkWheelNodes`` entries (Jaguar) are skipped for now.
    """
    try:
        root = ET.parse(vehicle_xml_path).getroot()
    except (ET.ParseError, OSError):
        return []
    cfgs = root.find("wheels/wheelConfigurations")
    if cfgs is None:
        return []
    configs = cfgs.findall("wheelConfiguration")
    if not (0 <= config_index < len(configs)):
        return []
    crawlers_el = configs[config_index].find("crawlers")
    if crawlers_el is None:
        return []

    specs = []
    for cr in crawlers_el.findall("crawler"):
        link = cr.get("linkNode")
        cr_xml = _abs_data(data_dir, cr.get("filename"))
        if not (link and cr_xml):
            # No single linkNode (e.g. Jaguar linkWheelNodes) or missing XML.
            continue
        i3d, left_p, right_p, rotating = _read_crawler_xml(data_dir, cr_xml)
        if not i3d:
            continue
        is_left = (cr.get("isLeft") == "true")
        specs.append({
            "i3d": i3d,
            "stem": os.path.splitext(os.path.basename(i3d))[0],
            "link_node": link,
            "is_left": is_left,
            "show_path": left_p if is_left else right_p,
            "hide_path": right_p if is_left else left_p,
            "rotating": rotating,
        })
    return specs
