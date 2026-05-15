# Patches for the Giants i3d Exporter

These patches are **optional**. You only need them if you want to re-export
an imported `.i3d` through the official Giants i3d Exporter
(`io_export_i3d_10_0_x`) and have the round-trip work cleanly. If you only
**import** files into Blender (no re-export), skip this entirely.

## Overview

| Patch | Symptom | When needed |
|---|---|---|
| `01-giants-exporter-referenceChildPath-keyerror.patch` | `KeyError: 'i3D_referenceChildPath'` during re-export | When the i3d contains ReferenceNodes (most vehicles, many buildings) |
| `02-giants-exporter-emissive-color-default.patch` | Every material gets `emissiveColor="1 1 1 1"` in the re-exported i3d | Recommended for all round-trip workflows |

## Where to find the Giants Exporter

The exporter is installed as a Blender add-on. The folder is named
`io_export_i3d_10_0_x` (where `x` is the minor version) and located under
your Blender user-add-on folder:

- **Windows:** `%APPDATA%\Blender Foundation\Blender\<version>\scripts\addons\io_export_i3d_10_0_x\`

Replace `<version>` with your Blender version (e.g. `5.1`).

You can also find the exact path inside Blender:
`Edit` → `Preferences` → `Add-ons` → search for `i3d` → expand the Giants
exporter entry → check the **File** path shown there.

---

## Patch 01 — `referenceChildPath` KeyError

### Symptom

Re-exporting an i3d that contains `<ReferenceNode>` entries (e.g. most FS25
vehicle files) aborts the Giants exporter with:

```
KeyError: 'i3D_referenceChildPath'
```

### Cause

Inside the exporter (`dcc/__init__.py`, `getNodeData`, around line 591),
custom properties whose value matches the default from `SETTINGS_ATTRIBUTES`
are collected and deleted from the object. The default for
`i3D_referenceChildPath` is `""`. Our importer sets this property to `""`
(because the source XML almost never sets `referenceChildPath`). Result:
the property gets deleted, but later (`i3d_export.py` around line 900) it
is read with a hard `data["i3D_referenceChildPath"]` lookup that throws
KeyError.

### Fix — one-line change

**File:** `io_export_i3d_10_0_x/i3d_export.py`

**Find:**
```python
        refChildPath = data["i3D_referenceChildPath"]
```

**Replace with:**
```python
        refChildPath = data.get("i3D_referenceChildPath", "")
```

`.get()` with a `""` default makes the lookup robust. The existing
downstream `len > 0` check then skips the XML output properly.

---

## Patch 02 — `emissiveColor` default bug (Blender 4.x+)

### Symptom

Every material in the re-exported i3d contains:

```xml
<Material ... emissiveColor="1 1 1 1">
```

Even materials with no Emission set in Blender.

### Cause

In Blender 4.x+ the Principled BSDF input `Emission Color` defaults to
`(1, 1, 1, 1)` (white) while `Emission Strength` defaults to `0.0` — so
the material emits no actual light. The Giants exporter
(`dcc/dccBlender.py` around line 1700) only checks
`Emission Color != (0, 0, 0, 1)` and ignores `Emission Strength`. The
default white color falsely triggers the `emissiveColor` output.

### Fix — check `Emission Strength` too

**File:** `io_export_i3d_10_0_x/dcc/dccBlender.py`

**Find:**
```python
                if not (0, 0, 0, 1) == (emissiveRed, emissiveGreen, emissiveBlue, emissiveAlpha):
                    m_data["emissiveColor"] = ...
```

**Replace with:**
```python
                emStrength = surfaceNode.inputs['Emission Strength'].default_value if 'Emission Strength' in surfaceNode.inputs else 0
                if emStrength > 0 and not (0, 0, 0, 1) == (emissiveRed, emissiveGreen, emissiveBlue, emissiveAlpha):
                    m_data["emissiveColor"] = ...
```

> **Note:** Our i3d Importer already sets `Emission Color = (0, 0, 0, 1)`
> explicitly on every imported material, so even an unpatched exporter
> handles imported materials correctly. Patch 02 only matters for
> hand-built or otherwise-sourced materials.

---

## Applying the patches

### Variant A — Manual (recommended, no tools needed)

1. Open the target file in any text editor.
2. Search for the "Find" string above.
3. Replace it with the "Replace with" string.
4. Save the file.
5. Restart Blender (or disable + re-enable the Giants exporter add-on).

### Variant B — Using `patch` (Git Bash / Linux / WSL)

If `patch.exe` is available (it ships with Git for Windows):

```bash
cd "<path-to-Giants-exporter-folder>"
patch -p0 < "<path-to-this-repo>/blender_i3d_importer/patches/01-giants-exporter-referenceChildPath-keyerror.patch"
patch -p0 < "<path-to-this-repo>/blender_i3d_importer/patches/02-giants-exporter-emissive-color-default.patch"
```

The patch files use search-string anchors (no hard-coded line numbers),
so they keep working even if a future Giants exporter version shifts the
surrounding code around — as long as the relevant code snippet itself
stays the same.

---

## After a Giants Exporter update

The exporter installer overwrites its source files, so any patches you
applied are lost. Re-apply them after each update.

**Quick check whether the patches are still needed:** search inside
`i3d_export.py` for `data["i3D_referenceChildPath"]` (without `.get()`).
- Found → patch 01 still needed.
- Not found → Giants fixed it themselves; you can skip patch 01.
