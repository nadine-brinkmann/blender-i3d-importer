# Blender i3d Importer

Import Farming Simulator 22 & 25 `.i3d` files into Blender, with full geometry,
materials, lights, cameras, and round-trip support to the official Giants i3d Exporter.

## Features

- **Full scene hierarchy** — meshes, empties, lights, cameras, splines, reference nodes
- **Two material flavors per i3d material:**
  - *Re-export material* — round-trip compatible with the Giants i3d Exporter
  - *PBR debug material* (optional) — visually accurate preview similar to the Giants Editor view
- **Multi-UV and vertex colors** — UV1, UV2, UV3, UV4 plus corner vertex colors
- **Custom properties preserved** — all i3d node and material attributes carry over to Blender
- **Smart path resolution** — `$data/...` and relative paths, DDS preferred over PNG
- **Axis correction** — optional Y-up → Z-up conversion at import
- **Auto-hide** — invisible shapes hidden in the viewport, matching the Giants Editor behavior
- **N-panel controls** — switch between debug and re-export materials, edit shader parameters
  (color tints, dirt, snow, clear coat, etc.), inspect masks and vertex colors, show/hide snow heaps

## Requirements

- **Blender 5.1 or newer**
- **Farming Simulator 25** installed locally (or FS22) — needed for resolving texture and shader
  paths. The add-on never modifies your game files.
- **Windows** — the bundled extraction tool is a Windows binary
- *Optional:* the [Parallax Node Extension](https://extensions.blender.org/add-ons/parallax-node/)
  for real parallax occlusion mapping in debug materials (bump-mapping fallback otherwise)

## Installation

### Drag & Drop (recommended)

1. Download the latest `blender_i3d_importer.zip` from the [Releases](../../releases) page.
2. Open Blender.
3. Drag the `.zip` file from your file manager onto the Blender window.
4. Confirm the install prompt.

### Via Preferences

1. Download the latest `blender_i3d_importer.zip` from the [Releases](../../releases) page.
2. In Blender, go to `Edit` → `Preferences` → `Add-ons`.
3. Click the dropdown arrow in the top-right → `Install from Disk...`
4. Select the downloaded `.zip` file.
5. Enable the add-on by ticking the checkbox next to its name.

## First-time setup

1. Open `Edit` → `Preferences` → `Add-ons` and find **i3d Importer**.
2. Expand its preferences and set the **FS25 game data folder** to your FS25 installation root
   (the folder that contains the `data/` subfolder).
3. *Optional:* set the **Export folder** if you plan to re-export through the Giants i3d Exporter.

## How to use

`File` → `Import` → `Farming Simulator i3d (.i3d)`

In the import dialog you can toggle axis correction, auto-hide, debug material creation, and
whether debug materials should be attached to the mesh by default.

After import, the **3D viewport sidebar** (press `N`) offers four panels:

- **i3d Importer** — switch the selected mesh between debug and re-export materials
- **FS25 Material Settings** — edit shader parameters of the active material
- **FS25 Debug View** — overlay masks or vertex colors on the active material for inspection
- **FS25 Snow Heaps** — show or hide snow and icicle meshes

## Round-trip via the Giants i3d Exporter

The re-export materials are designed to round-trip cleanly through the official
[Giants i3d Exporter](https://gdn.giants-software.com/downloads.php) for Blender. Two
**optional** patches against the Giants exporter ship in the `patches/` folder, needed
only if you want to re-export an imported `.i3d`. They fix two issues:
1. ReferenceNodes cannot be re-exported out of the box. A small fix needs to be done to the Giants i3d Exporter for this to work. 
2. The Giants i3d Exporter exports all materials with a white emissive map by default although there is none. This is only a wrong default and kann be fixed easily.

See [`patches/README.md`](blender_i3d_importer/patches/README.md)
for the manual application steps (not difficult, few-line edits in two files, described in detail).

## Known limitations

- **Reference node recursion** — referenced sub-i3ds are not automatically loaded. They remain
  as empty placeholders with the original `i3D_referenceFilename` custom property, which the
  Giants exporter writes back correctly on re-export after you applied the fix in the patches (see above).
- **Skin bindings** — meshes bound to multiple transform groups via `skinBindNodeIds` are
  imported as info-only strings; skin animation is lost on re-export. Affects roughly 500
  base-game vehicle implements. Not in scope for the current version due to limitations in the Exporter.
- **Windows only** — the bundled extraction tool is a .NET 6 Windows binary. macOS and Linux
  would require rebuilding the tool natively.

## License and attribution

- **This add-on** — GPL-3.0-or-later. See `LICENSE`.
- **Bundled extraction tool** (`bin/i3dToObjx.exe`) — based on
  [I3DShapesTool-OBJx](https://github.com/VidhosticeSDK/I3DShapesTool-OBJx) by VidhosticeSDK,
  MIT-licensed. See `NOTICE` for the full attribution.

## Author

Nadine Brinkmann — [YouTube](https://www.youtube.com/@Nadine-Brinkmann)
