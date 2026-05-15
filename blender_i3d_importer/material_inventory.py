"""Inventory of FS25 custom-parameter UI metadata.

The PBR debug material exposes each custom parameter as a Value/RGB node
named "fs25_param:<param>". This inventory maps each param name to its
display group and order so the N-Panel can render related parameters
together (e.g. all Vehicle Mask amounts in one section).

UI control type (slider vs color picker) is auto-derived from the node
type (ShaderNodeValue -> slider, ShaderNodeRGB -> color picker). The
display label is read from node.label. This module is purely metadata -
no Blender API calls.
"""

import re


# Display order of groups in the N-Panel. Groups not listed appear last
# in insertion order.
FS25_PARAM_GROUP_ORDER = [
    "Vehicle Brand Color",
    "Vehicle Detail Specular",
    "Vehicle Masks",
    "Clear Coat",
    "Dirt & Moss",
    "Snow Overlay",
    "Emissive",
    "Placeable Tint",
    "Multitint",
    "Multitint Mask",
    "Other",
]


# Static param_name -> (group, order_in_group).
# Lower order = displayed first within the group.
FS25_PARAM_INVENTORY = {
    # Vehicle Brand Color
    "colorScale":                    ("Vehicle Brand Color", 10),

    # Vehicle Detail Specular
    "smoothnessScale":               ("Vehicle Detail Specular", 10),
    "metalnessScale":                ("Vehicle Detail Specular", 20),

    # Vehicle Masks
    "scratches_dirt_snow_wetness_x": ("Vehicle Masks", 10),  # Scratches
    "scratches_dirt_snow_wetness_y": ("Vehicle Masks", 20),  # Dirt
    "scratches_dirt_snow_wetness_z": ("Vehicle Masks", 30),  # Snow
    "dirtColor":                     ("Vehicle Masks", 40),

    # Clear Coat
    "clearCoatIntensity":            ("Clear Coat", 10),
    "clearCoatSmoothness":           ("Clear Coat", 20),

    # Dirt & Moss
    "dirtMossMix_x":                 ("Dirt & Moss", 10),  # Moss Intensity
    "dirtMossTint_y":                ("Dirt & Moss", 20),  # Moss Tint
    "dirtMossMix_y":                 ("Dirt & Moss", 30),  # Dirt Intensity
    "dirtMossTint_x":                ("Dirt & Moss", 40),  # Dirt Tint

    # Snow Overlay
    "snowIntensity":                 ("Snow Overlay", 10),

    # Emissive
    "lightControl":                  ("Emissive", 10),

    # Placeable Tint
    "placeableColorScale_rgb":       ("Placeable Tint", 10),
    "placeableColorScale_w":         ("Placeable Tint", 20),

    # Multitint Mask
    "contrastLuminiosity_x":         ("Multitint Mask", 10),
    "contrastLuminiosity_y":         ("Multitint Mask", 20),
}


# Regex for dynamic multitint slot params: colorScale0..7_rgb / _w.
_MULTITINT_PATTERN = re.compile(r"^colorScale(\d+)_(rgb|w)$")


def lookup_param(param_name):
    """Return (group, order) for a param name.

    Handles static entries from FS25_PARAM_INVENTORY plus dynamic multitint
    slot params like colorScale3_rgb. Unknown params land in 'Other' group
    with order 999, so they still render in the N-Panel (defensive).
    """
    if param_name in FS25_PARAM_INVENTORY:
        return FS25_PARAM_INVENTORY[param_name]

    m = _MULTITINT_PATTERN.match(param_name)
    if m:
        slot = int(m.group(1))
        # _rgb before _w within a slot. Slot 0 occupies orders 10..19,
        # slot 1 orders 20..29, etc.
        suffix_offset = 0 if m.group(2) == "rgb" else 1
        return ("Multitint", (slot + 1) * 10 + suffix_offset)

    return ("Other", 999)
