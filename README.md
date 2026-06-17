# Blender i3d Importer

**The first and only Blender add-on that imports Farming Simulator 22 & 25 `.i3d` files directly — full scene, materials, textures, ready for editing, re-export-clean to the official Giants i3d Exporter.**

> 📖 **New here? Read the [Wiki](https://github.com/nadine-brinkmann/blender-i3d-importer/wiki)** for the full guide: installation, the import/export workflow, the N-panel tools and preferences, all with screenshots.

No external extraction tool, no command-line gymnastics: the `.i3d.shapes` binary is decoded natively in Python. Geometry, splines, skin weights, lights, cameras, references, notes, terrain — everything that ships in an FS22/FS25 i3d file becomes proper Blender datablocks you can inspect,
modify, and (mostly) round-trip back into the Giants Editor through the Giants i3d Exporter.

## Watch it in action

<a href="https://youtu.be/GZyfNMfoyGQ" target="_blank" rel="noopener"><img src="https://img.youtube.com/vi/GZyfNMfoyGQ/maxresdefault.jpg" alt="i3d Importer trailer" width="48%"></a>   <a href="https://youtu.be/t-Kmea4K3zU" target="_blank" rel="noopener"><img src="https://img.youtube.com/vi/t-Kmea4K3zU/maxresdefault.jpg" alt="i3d Importer tutorial" width="48%"></a>

▶ [Trailer](https://youtu.be/GZyfNMfoyGQ) · [Tutorial](https://youtu.be/t-Kmea4K3zU)

## What it can do

### Geometry & hierarchy

- Full scene tree: meshes, materials, textures, lights, cameras, splines, reference nodes (with patch for the exporter), also terrain!
- Native Python decoder for `.i3d.shapes` v7 / v9 / v10 (no external tool required)
- Shared meshes are split up into individuals + merged back upon export
- Skin weights for vehicle-implement bones - verified in-game. Joints round-trip back to their original place in the scenegraph via a shared armature + "Child Of" constraints (a leftover empty `zzz_armature` group can be deleted in the Giants Editor after import)
- **Tree import** - full FS25 trees: detailed trunk (LOD0) and the leaf/branch attachments (LOD0Attachments), with a seasonal leaf debug material and a **Tree Season** switch (Summer/Autumn/Winter/Spring)
- Optional **scenegraph order preservation** - adds a sort-order name prefix so the Giants Exporter reproduces the original Giants Editor node order on re-export (toggle in preferences / the import dialog)
- Merge groups & merge-children splitting
- Splines (open and closed) 
- Heightmap-based terrain as a Plane + Displace modifier (terrain as preview only, not exportable, see [Known limitations](#known-limitations))

### Materials — two flavors per i3d material

- **Re-export material** — clean round-trip with the Giants Exporter, preserves every `<CustomParameter>` and `<Custommap>` and also options on the shape
- **PBR debug material** (optional) — visually mimics the Giants Editor look, with editable sliders for vehicle brand colors, multitints, clear coat, scratches/dirt/snow/wetness, parallax, etc.
- Pair-aware: switch any mesh between debug and re-export material with a click; changes to debug sliders can be synced back to the export material before re-export
- **Multiple imports in the same scene possible** — per-import UUIDs disambiguate materials that share `material_id` across files

### UV, vertex data, custom properties

- UV1, UV2, UV3, UV4 channels
- Corner vertex colors
- Every i3d attribute (visibility, weather masks, navMesh masks, collision flags, clip distance, ...) survives as a Blender custom  property — re-export-ready

### Smart path resolution

- `$data/...` resolved against the configured FS25 game folder
- DDS preferred over PNG when both exist
- $-substitution also works for terrain heightmap files


### Axis correction & visibility

- Optional Y-up → Z-up bake at import 
- Auto-hide objects flagged `visibility="false"` or `nonRenderable="true"` (matches the Giants Editor render-view behavior)

### N-panel workflow tools (`N` in the 3D viewport, tab "i3d Importer")

- **Material Switch** — toggle the selected meshes between debug and re-export materials
- **FS25 Material Settings** — sliders for every `material parameter` of the active debug material, plus a **Sync to Export Material** button that copies the slider values back to the paired export material's `custom Parameter` properties (required before re-export)
- **FS25 Debug View** — overlay masks or vertex colors on the active material for inspection
- **FS25 Snow + Ice** — show/hide all snow/icicle meshes; reminds you to un-hide them before re-export
- **FS25 Invisible GE-objects** — show/hide all objects that were auto-hidden because they are invisible in the Giants Editor (e.g. collision volumes); reminds you to un-hide them before re-export
- **Tree Season** - for imported trees, switch the seasonal leaf debug look (Summer / Autumn / Winter / Spring); appears only when a tree-branch material is present in the scene

### Convenience after import

- Viewport switches to Material Preview shading
- All imported objects are framed (Numpad-`.`-equivalent)
- Clip-end is bumped to 10000 when an imported object is larger than 500 units (whole maps no longer disappear behind the default far clip)
- prefills Game path & export path in the Giants id3 exporter so you don't have to set it every time

## Requirements

- **Blender 5.1 or newer**
- **Farming Simulator 25** (or FS22) installed locally — needed to resolve `$data/...` texture and shader paths. The add-on never modifies your game files.
- **Operating system:** Windows is regularly tested and supported. Linux and macOS should work in principle since the decoder is pure Python and the add-on has no native binaries, but they are **untested** — please open an issue if you try them.
- *Optional:* the [Parallax Node Extension](https://extensions.blender.org/add-ons/parallax-node/) for true parallax occlusion mapping in debug materials (a simpler bump-mapping fallback is used otherwise)

## Installation

### Drag & Drop (recommended)

1. Download the latest `blender_i3d_importer.zip` from the [Releases](../../releases) page.
2. Open Blender.
3. Drag the `.zip` file from your file manager onto the Blender window.
4. Confirm the install prompt.

### Via Preferences

1. Download the latest `blender_i3d_importer.zip` from the [Releases](../../releases) page.
2. In Blender, `Edit` → `Preferences` → `Add-ons`.
3. Click the dropdown arrow in the top-right → `Install from Disk...`
4. Select the downloaded `.zip` file.
5. Enable the add-on by ticking the checkbox next to its name.

## First-time setup

1. `Edit` → `Preferences` → `Add-ons` → find **i3d Importer**.
2. Expand its preferences. They are grouped in three sections:
   - **Paths** — set **FS25 game data folder** to your FS25 installation root (the folder that contains the `data/` subfolder). Optionally set **Re-export output folder**.
   - **Import Defaults** — defaults for the per-import operator options (axis correction, auto-hide, debug materials, etc.).
   - **Terrain** — default LOD, base color for the terrain preview, and the comma-separated list of `<CombinedLayer>` names to load (up to 5; default covers ASPHALT, GRASS, MUD, FOREST_LEAVES, FOREST_GRASS).

## How to use

`File` → `Import` → `Farming Simulator i3d (.i3d)`

In the import dialog you can override per-import options that default from the add-on preferences (axis correction, auto-hide, debug materials, terrain LOD, base color, layer names).

After import, the **3D viewport sidebar** (press `N`) offers the **i3d Importer** tab with the workflow panels listed under [N-panel workflow tools](#n-panel-workflow-tools-n-in-the-3d-viewport-tab-i3d-importer).

**Read the [Wiki](https://github.com/nadine-brinkmann/blender-i3d-importer/wiki) for the full user guide. 
Or watch the ▶[Tutorial](https://youtu.be/t-Kmea4K3zU) video:**

<a href="https://youtu.be/t-Kmea4K3zU" target="_blank" rel="noopener"><img src="https://img.youtube.com/vi/t-Kmea4K3zU/maxresdefault.jpg" alt="i3d Importer tutorial" width="48%"></a>


## Round-trip via the Giants i3d Exporter

The re-export materials are designed to round-trip cleanly through the official [Giants i3d Exporter](https://gdn.giants-software.com/downloads.php) for Blender. Two **optional** patches against the Giants exporter ship in [`blender_i3d_importer/patches/`](blender_i3d_importer/patches/) and fix the following issues in the exporter:

1. `KeyError: 'i3D_referenceChildPath'` on ReferenceNodes (most vehicles
   and many buildings have these)
2. Every re-exported material gets `emissiveColor="1 1 1 1"` even when
   the original had none — a default-value bug in the exporter

See [`blender_i3d_importer/patches/README.md`](blender_i3d_importer/patches/README.md) for application steps (a few-line edit in two files of the exporter, detailed instructions included).

**Before re-export, remember:**

- If you used the **Material Settings** sliders, click **Sync to Export Material** so the changes are persisted on the export materials.
- If you used the **Snow + Ice** or **Invisible GE-objects** hide toggles, un-hide them — the Giants Exporter writes `visibility="false"` to the XML based on the Outliner eye state.
- If you used the **Material Switch** to Debug, switch back to Export (debug materials carry extra preview nodes that are not round-trip-clean).

## Known limitations

- **Terrain is one-way.** The Giants Blender Exporter cannot emit a `<TerrainTransformGroup>`. The terrain mesh is for in-Blender preview / backgroundMesh-snapping only. The importer prints a WARNING in the log when terrain is loaded.
- **Reference-node recursion.** Sub-i3ds referenced by a node are not loaded automatically; they remain as empties with the original `i3D_referenceFilename` custom property. The Giants exporter writes them back correctly on re-export, but only if you apply the `referenceChildPath`-patch (see above).
- **Skinned-mesh armature leftover.** Re-exporting a skinned mesh leaves one empty `armature` transform group in the scenegraph (the Giants exporter does not collapse armatures). It is harmless and can be deleted in the Giants Editor; the joints themselves round-trip to their original place via "Child Of" constraints.

## License and attribution

- **This add-on** — GPL-3.0-or-later. See `LICENSE`.
- **`.i3d.shapes` decoder** — Python port of the C# logic from  [I3DShapesTool by Donkie](https://github.com/Donkie/I3DShapesTool),  MIT-licensed. See `NOTICE` for the full attribution.

## Author

Nadine Brinkmann — [YouTube](https://www.youtube.com/@Nadine-Brinkmann)

If this add-on saves you time or unlocks a workflow you could not do before, a star on the repo or a comment on the channel is appreciated.

You can also support the project here: https://ko-fi.com/nadinebrinkmann