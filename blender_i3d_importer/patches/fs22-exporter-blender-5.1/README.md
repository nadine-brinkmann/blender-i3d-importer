# Running the GIANTS FS22 i3D Exporter (9.1.0) on Blender 5.1

The official GIANTS i3D Exporter **9.1.0** (the FS22 exporter) is built for Blender versions between 2.83-3.6 and 3.6.9 and refuses to load on Blender 5.1. **Three small changes are all that stand in the way of running it on Blender 5.1!**
This guide walks you through getting it running on Blender 5.1 so you can **re-export** an `.i3d` that you imported with the blender-i3d-importer.

> Why you might want this: the FS22 exporter writes FS22-style `.i3d` files (single `collisionMask`, FS22 collision groups, etc.). If you mod for Farming Simulator 22, this is the toolchain you need. For FS25, use the official 10.0.2 exporter instead - it already runs on Blender 5.1. without tweaking.

We make the three small edits **first** and only then install the add-on. That way you end up with a ready-patched `.zip` you can keep and reinstall any time (for example in a fresh Blender install) without redoing the edits.

You can also follow this video, it mirrors the contents of this page:
<a href="https://youtu.be/UQn2G0sv9Go" target="_blank" rel="noopener"><img src="https://img.youtube.com/vi/UQn2G0sv9Go/maxresdefault.jpg" alt="i3d Importer trailer" width="48%"></a>

---

## Step 1 - Download the exporter (9.1.0)

1. Go to the **GIANTS Developer Network**: https://gdn.giants-software.com
2. Go to the **Downloads** section, find **Blender Exporter Plugins**, and download version **9.1.0** (Windows installer `.exe`).

## Step 2 - Unpack the downloaded installer

1. Double click the downloaded installer exe. A menu will pop up where you can select the Blender version you want to install it on. No Blender version later that 3.6.9 is listed. Therefore:
2. On the same page, click `Extract Files...` instead (bottom left).

![Screenshot: the installer with the "Extract Files..." button](screenshots/01-extract-files.png)

4. A folder selection comes up. Select a folder where you want to extract the files to (2 files will be extracted, a zip and a text file) and confirm.
5. After extraction, you get a success message.

![Screenshot: Extract Files success message](screenshots/01a-extract-files-success.png)


## Step 3 - Unzip the add-on so you can edit it

The add-on itself sits inside the `io_export_i3d.zip` from Step 2. Unzip it so you can open the files.

1. Right-click the `io_export_i3d.zip` from Step 2 and choose **Extract All...** (or use your unzip tool).
2. You now have a folder named **`io_export_i3d`** containing the add-on's files. This is the folder you will edit in Step 4.

![Screenshot: the unzipped io_export_i3d folder](screenshots/02-unzipped-folder.png)

## Step 4 - Apply the three fixes manually

You will edit three text files inside the `io_export_i3d` folder. Open each file in a plain text editor (standard Notepad works just fine; **Notepad++** or **VS Code** are a bit nicer but optional).

I will explain the fixes manually, but there is also an advanced version below if you use git. In this case, skip the manual editing and use the patch files.

> Two things matter, even if the code means nothing to you:
> - Copy the **leading spaces** of each line exactly. The first letter must be exactly in the same place as the one you replace. Python relies on the structure of the file.
> - Change **only** the lines shown below. Leave everything else untouched.

### Fix 1 - file `i3d_ui.py`

1. **Open the file** `i3d_ui.py`. 

2. **Find these two lines:**

```python
    def __init__(self):
        global g_modalsRunning
```

3. **Replace them with:**

```python
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        global g_modalsRunning
```

4. Save, then close the file.

*(This fix lets the Exporter's panel start correctly on Blender 4.0+.)*

### Fix 2 - file `util/selectionUtil.py` in **two** places

This file needs **two edits**, both in the same file. Open `util/selectionUtil.py` once, make both changes, then save.

#### Edit 2a - function `getSelectedObjects`

1. Go into the `util` folder and **open the file** `selectionUtil.py`.
2. **Find the block** near the bottom of the file that starts with `def getSelectedObjects(context):`. It looks like this:

```python
def getSelectedObjects(context):
    # get active object from outliner context
    # this also includes hidden objects like collision etc
    for area in context.screen.areas:
        if area.type == 'OUTLINER':
            override = context.copy()
            override['area'] = area
            with bpy.context.temp_override(area=area):
                if len(context.selected_ids) == 0:
                    if len(context.selected_objects) > 0:
                        return context.selected_objects
                else:
                    return context.selected_ids
    return []
```

3. **Replace the whole block with:**

```python
def getSelectedObjects(context):
    # get active object from outliner context
    # this also includes hidden objects like collision etc
    # Blender 4.x/5.x: context.selected_ids is only available when the
    # temp_override also carries the OUTLINER region (not just the area).
    # Mirrors the fix in the official 10.0.2 exporter.
    selected_items = []
    for area in context.screen.areas:
        if area.type == 'OUTLINER':
            try:
                context_override = {}
                context_override['area'] = area
                context_override['region'] = [region for region in area.regions if region.type == 'WINDOW'][0]

                with bpy.context.temp_override(**context_override):
                    selected_items.extend(context.selected_ids)
            except Exception as e:
                print(f"An exception occurred: {e}")
    if not selected_items:
        selected_items.extend(context.selected_objects)

    return selected_items
```

*(This lets the exporter read your selected objects on Blender 4.x/5.x.)*

#### Edit 2b - function `get_outliner_selected_nodes`

> **Be aware! This part is not in the video** because it was found only after the video. But it's the same procedure, search, highlight, replace.

1. In the **same file**, **find the function** `get_outliner_selected_nodes`. It is a long block that looks like this:

```python
def get_outliner_selected_nodes():
    """ Returns all hidden objects selected in the outliner for Blender 2.83 LTS"""

    _so = None
    for win in bpy.context.window_manager.windows:
        for ar in win.screen.areas:
            if ar.type == 'OUTLINER':
                _so = ar.spaces.active
    if _so is None:
        print('so is None')
        return
    root = TreeElement.from_outliner(_so)
    # wmstruct = wmWindowManager.from_address(bpy.context.window_manager.as_pointer())
    # Track processed objects to prevent those that appear in multiple
    # collections from being processed again.
    walked = set()
    types = {ID_OB, ID_LAYERCOLL}
    try:
        for tree in subtrees_get(root):
            if tree.select and tree.idcode in types:
                obj = tree.as_object(root)
                if obj in walked:
                    continue
                walked.add(obj)
    except ValueError as e:
        pass
        #print(e)
    except KeyError as e:
        pass
        #print(e)
    root = None
    return list(walked)
```

2. **Replace the whole function with:**

```python
def get_outliner_selected_nodes():
    """ Returns objects selected in the outliner, including hidden ones.
    Uses obj.select_get() which reflects selection state regardless of visibility,
    avoiding context-override quirks across Blender versions. """

    return [obj for obj in bpy.context.view_layer.objects if obj.select_get()]
```

3. Save, then close the file.

*(Edit 2b fixes the crash on "Export Selected". This export reads your selection through this function. In the 9.1.0 exporter it pokes Blender's internal memory (ctypes), which **crashes Blender 5.1**. We replace it with the safe one-liner the official 10.0.2 exporter uses. Both edits mirror the official 10.0.2 exporter.)*

### Fix 3 - file `dcc/dccBlender.py` in **two** places

1. Go back to the `io_export_i3d folder`, from there into the `dcc` folder, and **open the file** `dccBlender.py`.
2. This same line appears **twice** in the file. So you have to do the find & replace two times.

3. **Find:**
```python
    m_meshGen.calc_normals_split()
```

4. **Replace with:**
```python
    if bpy.app.version < (4, 1):
        m_meshGen.calc_normals_split()
```
5. Make sure you changed it in **both** places. If you search again for the line in nr. 3, each occurence should have one new line `if bpy.app.version < (4,1):` above it. 
(If you see that line twice above it that means to edited the same place twice. Close the file without saving and try again.)
5. Save, then close the file.

*(Blender 4.1 removed this call; the `if` line simply skips it on newer Blender.)*

![Screenshot: Find & Replace in the text editor](screenshots/03-find-replace.png)



## Step 4 - Apply the three fixes with a patch tool (Advanced alternative (optional!))
***If you already did the manual fixes above, skip this step! You are done with the fixes. Proceed to Step 5!***

Instead of editing by hand you can apply the supplied `.patch` files with the `patch` tool (Git Bash / Linux / WSL). The patch files use Windows (CRLF) line endings, so `--binary` is required:
> ```bash
> cd io_export_i3d
> patch --binary -p0 < 01-panel-init-args.patch
> patch --binary -p0 < 02-getselectedobjects-outliner-region.patch
> patch --binary -p0 < 03-calc-normals-split-guard.patch
> ```
> The `--binary` flag is required: without it `patch` strips the carriage returns from the patch and the hunks no longer match the CRLF source files. If you still run into trouble, use the manual edits above instead.

## Step 5 - Zip it back up and install it in Blender 5.1

1. Go into the **`io_export_i3d` folder** so that you see the __init__.py file. Then go up **exactly one level** in the folder structure, so that you see the **`io_export_i3d` folder**.
2. Zip this **`io_export_i3d` folder** back up by right-clicking it -> *Send to* -> *Compressed (zipped) folder* (or use your own zip tool). 
3. Make sure the `io_export_i3d` folder sits at the **top level** of the new `.zip` file. Also make sure that you have **exactly one** folder named `io_export_i3d` inside the zip file, and not another folder `io_export_i3d` below it. (If you are unsure, compare the structure of your zip file to the original zip file before you applied the fixes. It must be the same.)
5. **Keep this `.zip`** file somewhere safe - this is your reusable, ready-patched exporter!

   ![Screenshot: zipping the patched io_export_i3d folder](screenshots/04-zip-folder.png)

2. Start **Blender 5.1**.
3. There are two alternative ways of installing the add-on:
   a. Drag and drop the zip file onto your Blender Window. Confirm the installation dialog. 
   b. Open `Edit` -> `Preferences` -> `Add-ons`. Then Click the **down-arrow (v)** in the top-right of the Add-ons panel and choose **Install from Disk...**. Select your zip file and confirm.

   ![Screenshot: the Install from Disk menu](screenshots/05-install-from-disk.png)


## Step 6 - Enable and test

1. In `Edit` -> `Preferences` -> `Add-ons`, search for `i3d` (or `GIANTS`) and
   tick the **GIANTS I3D Exporter** entry. Because it is already patched, it
   should enable **without** an error in the console.

   ![Screenshot: enabling the GIANTS I3D Exporter add-on](screenshots/06-enable-addon.png)

2. Make sure that you have only one Giants Exporter active at the same time. The exporter for FS25 may be installed at the same time, but must be inactive because both have the same name. That would lead to errors. You can distinguish the two entries by looking at the version number when you expand them.
   ![Screenshot: two GIANTS I3D Exporter add-ons in the preferences](screenshots/07-two-exporter-addons.png)
2. In the 3D viewport press **N** to open the side panel - a **GIANTS I3D
   Exporter** tab should appear.

   ![Screenshot: the exporter panel in the N side panel](screenshots/08-exporter-panel.png)

3. Quick export test:
   - Select a test object (e.g. the default cube, or an object you imported).
   - In the exporter panel set an **Export File** path.
   - Run the export.
   - Confirm an `.i3d` and `.i3d.shapes` were written, and that the `.i3d`
     opens in the **Giants Editor** without errors.


**If all six steps pass, the exporter is working for you on Blender 5.1!**

---

## Reinstalling later / after an update

Your patched `.zip` from Step 5 already contains the fixes, so installing it again (in a new Blender, on another PC, ...) needs **no** extra work - just *Install from Disk* or via *Drag & Drop* with that zip file.

This will also work with newer Blender versions **until** Blender changes something which breaks it.
