"""
Parser for FS25 shader XMLs (e.g. data/shaders/vehicleShader.xml).

for a material with `customShaderId` and optional
`customShaderVariation`, returns the effective UV mapping per custom-map name:

    custommap_name -> ShaderUvUsage(uv_type, uv_scale)

Shader XML structure (verified in
data/shaders/{vehicleShader, buildingShader, vertexPaintShader, placeableShader}.xml):

  <CustomShader version="5">
      <UvUsages>                                 <- base defaults
          <UvUsage textureName="..." uvType="uv0|uv1|uv2|uv3|worldspace|custom"
                                     uvScale="1.0"/>
          ...
      </UvUsages>
      <Variations>
          <Variation name="vmaskUV2" groups="...">
              <UvUsages>                          <- override per textureName (optional)
                  <UvUsage textureName="glossMap" uvType="uv1" uvScale="1.0"/>
              </UvUsages>
              <![CDATA[ ... shader code ... ]]>
          </Variation>
          <Variation name="uvTransform" .../>    <- variation WITHOUT UvUsages -> no-op
          ...
      </Variations>
  </CustomShader>

In the material:
  <Material customShaderId="10" customShaderVariation="vmaskUV2_normalUV3" .../>

`customShaderVariation` matches exactly ONE <Variation name="..."/> entry.
Combinations are pre-defined as standalone variations (e.g.
"vmaskUV2_normalUV3", "mergeChildren_hideByIndex_customParallax") - no merging
of multiple variations needed.

Override logic:
  effective[textureName] = variation_override[textureName]    if present
                         else base[textureName]

Domain-agnostic - returns Giants values 1:1. The mapping Giants -> Blender
(uv0 -> "UVMap", worldspace -> Generated, custom -> UVMap fallback) lives in
the importer.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ShaderUvUsage:
    """A <UvUsage> entry from the shader XML."""
    texture_name: str
    uv_type: str               # "uv0" | "uv1" | "uv2" | "uv3" | "worldspace" | "custom"
    uv_scale: Optional[float] = None


@dataclass
class ShaderInfo:
    """Parsed shader XML - only the parts relevant for UV mapping."""
    path: Path
    base_uv_usages: Dict[str, ShaderUvUsage] = field(default_factory=dict)
    # variations[variation_name] = {textureName: ShaderUvUsage}
    # Contains ONLY the entries overridden in the respective <UvUsages> block.
    # Variations without <UvUsages> are present here with an empty dict - so
    # resolve_uv_mapping can distinguish "variation exists (without overrides)"
    # from "variation does not exist" (warning case).
    variations: Dict[str, Dict[str, ShaderUvUsage]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_shader(shader_xml_path: Path) -> ShaderInfo:
    """Parse a shader XML file.

    Raises:
        FileNotFoundError  - if shader_xml_path does not exist.
        ET.ParseError      - on malformed XML.

    Tolerant of missing sections (<UvUsages>, <Variations>): the corresponding
    dicts simply stay empty.
    """
    path = Path(shader_xml_path)
    tree = ET.parse(str(path))
    root = tree.getroot()  # usually <CustomShader version="5">

    info = ShaderInfo(path=path)

    base_uv_elem = root.find('UvUsages')
    if base_uv_elem is not None:
        info.base_uv_usages = _parse_uvusages_block(base_uv_elem)

    variations_elem = root.find('Variations')
    if variations_elem is not None:
        for var_elem in variations_elem.findall('Variation'):
            name = var_elem.get('name')
            if not name:
                continue
            inner_uv = var_elem.find('UvUsages')
            if inner_uv is not None:
                info.variations[name] = _parse_uvusages_block(inner_uv)
            else:
                # Variation without <UvUsages> block -> no overrides,
                # effective == base.
                info.variations[name] = {}

    return info


def _parse_uvusages_block(uvusages_elem: ET.Element) -> Dict[str, ShaderUvUsage]:
    """Parse a single <UvUsages> block (root or inside a variation)."""
    result: Dict[str, ShaderUvUsage] = {}
    for u in uvusages_elem.findall('UvUsage'):
        tex_name = u.get('textureName')
        if not tex_name:
            continue
        # uvType passed through 1:1 - the importer decides what to do with
        # unknown/custom values during the Giants -> Blender conversion.
        # Defensive: missing uvType -> "uv0" (FS25 schemas always include it).
        uv_type = u.get('uvType', 'uv0')

        uv_scale: Optional[float] = None
        uv_scale_str = u.get('uvScale')
        if uv_scale_str is not None:
            try:
                uv_scale = float(uv_scale_str)
            except ValueError:
                uv_scale = None

        result[tex_name] = ShaderUvUsage(
            texture_name=tex_name,
            uv_type=uv_type,
            uv_scale=uv_scale,
        )
    return result


# ---------------------------------------------------------------------------
# Resolve (apply variation override)
# ---------------------------------------------------------------------------

def resolve_uv_mapping(info: ShaderInfo,
                       variation_name: Optional[str],
                       report: Optional[Callable] = None
                       ) -> Dict[str, ShaderUvUsage]:
    """Effective UV mapping per textureName for a given variation.

    Procedure:
      1. Copy info.base_uv_usages.
      2. If variation_name is in info.variations:
           apply per-textureName overrides from that variation.
      3. If variation_name is given but NOT in info.variations:
           warn (when `report` is provided) and return base only.

    `variation_name` = None / empty string -> base only.

    Returns:
        Dict {texture_name: ShaderUvUsage} (incl. uv_scale).
    """
    # Shallow copy is sufficient - ShaderUvUsage instances are immutable in
    # practice and are not modified.
    effective: Dict[str, ShaderUvUsage] = dict(info.base_uv_usages)

    if variation_name:
        overrides = info.variations.get(variation_name)
        if overrides is None:
            if report is not None:
                report('WARNING',
                       f"Shader '{info.path.name}': customShaderVariation "
                       f"'{variation_name}' not defined - only base UvUsages "
                       f"applied")
        else:
            for tex_name, override in overrides.items():
                effective[tex_name] = override

    return effective
