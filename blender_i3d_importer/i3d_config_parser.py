"""Parser for the ``<...Configurations>`` / ``<objectChange>`` blocks of a
Farming Simulator vehicle/placeable config XML.

It extracts the store-configuration structure as a plain data model for the
in-Blender config preview. Only the visually relevant ``objectChange``
attributes are kept (visibility, translation, rotation, scale, shaderParameter);
physics/collision attributes (mass, compoundChild, rigidBodyType, centerOfMass)
have no visual effect in Blender and are ignored.

The ``node`` references inside ``objectChange`` are i3dMapping ids; this module
keeps them as raw ids - resolving them to Blender objects is the apply engine's
job (see the importer). Attribute semantics follow the game's own parser
(dataS/scripts/utils/ObjectChangeUtil.lua).
"""

from dataclasses import dataclass, field
from typing import List
import xml.etree.ElementTree as ET


# objectChange attributes with a visual effect in Blender. Kept as raw strings;
# the apply engine parses booleans ("true"/"false") and vectors ("x y z").
_VIS_ATTRS = (
    "visibilityActive", "visibilityInactive",
    "translationActive", "translationInactive",
    "rotationActive", "rotationInactive",
    "scaleActive", "scaleInactive",
    "shaderParameter", "sharedShaderParameter",
    "shaderParameterActive", "shaderParameterInactive",
)


@dataclass
class ObjectChange:
    """One ``<objectChange>`` entry, reduced to its visual attributes."""
    node: str                                    # i3dMapping id
    attrs: dict = field(default_factory=dict)    # subset of _VIS_ATTRS present


@dataclass
class MaterialChange:
    """One ``<material>`` swap inside a configuration option."""
    slot: str                                    # materialSlotName
    template: str                                # materialTemplateName
    color_only: bool = False                     # materialTemplateUseColorOnly


@dataclass
class ConfigOption:
    """One option of a configuration type (e.g. one ``<designConfiguration>``)."""
    label: str
    changes: List[ObjectChange] = field(default_factory=list)
    materials: List["MaterialChange"] = field(default_factory=list)


@dataclass
class ConfigType:
    """A configuration type that carries visual object changes."""
    tag: str                                     # e.g. "designConfigurations"
    name: str                                    # e.g. "design"
    options: List[ConfigOption] = field(default_factory=list)


def parse_configurations(filepath) -> List[ConfigType]:
    """Return the configuration types that carry visual ``objectChange`` entries.

    Options are returned in document order (index 0 = first option, which is the
    in-game default). Types and options without any visual object change are
    omitted. Returns an empty list on a parse error or when the file has no such
    configurations.
    """
    try:
        root = ET.parse(str(filepath)).getroot()
    except (ET.ParseError, OSError):
        return []

    types: List[ConfigType] = []
    for cfgs in root.iter():
        if not cfgs.tag.endswith("Configurations"):
            continue
        options: List[ConfigOption] = []
        for opt in list(cfgs):
            if not opt.tag.endswith("Configuration"):
                continue
            changes: List[ObjectChange] = []
            for oc in opt.iter("objectChange"):
                node = oc.get("node")
                if not node:
                    continue
                attrs = {a: oc.get(a) for a in _VIS_ATTRS if oc.get(a) is not None}
                if attrs:
                    changes.append(ObjectChange(node=node, attrs=attrs))
            materials = []
            for mc in opt:
                if mc.tag != "material" or not mc.get("materialTemplateName"):
                    continue
                slot = mc.get("materialSlotName")
                if not slot:
                    continue
                materials.append(MaterialChange(
                    slot=slot,
                    template=mc.get("materialTemplateName"),
                    color_only=(mc.get("materialTemplateUseColorOnly") == "true"),
                ))
            if changes or materials:
                label = (opt.get("name") or opt.get("vehicleName")
                         or opt.get("title") or opt.get("saveId") or "Option")
                options.append(ConfigOption(label=label, changes=changes,
                                            materials=materials))
        if options:
            name = cfgs.tag[:-len("Configurations")]
            types.append(ConfigType(tag=cfgs.tag, name=name, options=options))
    return types


def to_dict(types):
    """Serialise the parsed config types to plain JSON-able lists/dicts."""
    return [
        {
            "tag": t.tag,
            "name": t.name,
            "options": [
                {
                    "label": o.label,
                    "changes": [{"node": c.node, "attrs": c.attrs} for c in o.changes],
                    "materials": [{"slot": m.slot, "template": m.template,
                                   "color_only": m.color_only} for m in o.materials],
                }
                for o in t.options
            ],
        }
        for t in types
    ]
