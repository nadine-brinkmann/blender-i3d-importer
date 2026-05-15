# Patches für den Giants i3d Exporter

Diese Patches sind nötig, damit unser **FS25 i3d Importer** Roundtrips über
den offiziellen Giants i3d Exporter (`io_export_i3d_10_0_2`) korrekt
durchführen kann. Sie patchen den Giants-Exporter direkt — analog zu unseren
Patches am `I3DShapesTool-OBJx`-Fork (siehe Phase-A-Notiz im Vault).

## Übersicht

| Patch                                                            | Symptom                                                  | Status     |
| ---------------------------------------------------------------- | -------------------------------------------------------- | ---------- |
| `01-giants-exporter-referenceChildPath-keyerror.patch`           | `KeyError: 'i3D_referenceChildPath'` beim Export         | erforderlich, wenn die i3d ReferenceNodes enthält |
| `02-giants-exporter-emissive-color-default.patch`                | `emissiveColor="1 1 1 1"` an jedem Material in der i3d   | empfohlen für alle Workflows |

---

## Patch 01 — `referenceChildPath` KeyError

**Bug-Symptom:** Beim Re-Export einer importierten i3d, die `<ReferenceNode>`-
Einträge enthält (z.B. `precea4500.i3d` mit Light-Referenzen), bricht der
Giants-Exporter mit dieser Meldung ab:

```
'i3D_referenceChildPath'
```

**Ursache** (verifiziert im Giants-Exporter-Source):

1. Beim Sammeln der Node-Daten iteriert der Exporter über alle Custom Properties
   des Blender-Objects (`dcc/__init__.py`, `getNodeData`, ~Zeile 591).
2. Properties, deren Wert dem **Default** aus `SETTINGS_ATTRIBUTES` entspricht,
   werden in `propsToDelete` gesammelt und anschließend **vom Object gelöscht**
   (Zeilen 598-602).
3. Für `i3D_referenceChildPath` ist der Default `""`. Unser Importer setzt diese
   Property auf `""` (weil im Source-XML `referenceChildPath` praktisch nie
   gesetzt ist). → Property wird gelöscht.
4. Später in `i3d_export.py` Zeile 900 wird die Property aber per
   `data["i3D_referenceChildPath"]` ohne `.get()` gelesen → KeyError.

**Fix:** Eine Zeile in `i3d_export.py`:

```diff
-        refChildPath = data["i3D_referenceChildPath"]
+        refChildPath = data.get("i3D_referenceChildPath", "")
```

`.get()` mit Default `""` macht es robust. Das nachfolgende `len > 0`-Check
überspringt die XML-Ausgabe dann ordnungsgemäß.

---

---

## Patch 02 — `emissiveColor` Default-Bug in Blender 4.x

**Bug-Symptom:** Jedes über den Giants-Exporter exportierte Material in der i3d enthält fälschlich:

```xml
<Material ... emissiveColor="1 1 1 1">
```

Auch bei Materialien, die im Blender überhaupt keine Emission haben.

**Ursache** (verifiziert in `dcc/dccBlender.py` ~Z. 1699-1706):

1. In Blender 4.x ist der Default für den Principled-BSDF-Input `Emission Color` `(1, 1, 1, 1)` (weiß), aber `Emission Strength` ist `0.0` — d.h. das Material strahlt kein Licht ab.
2. Der Exporter prüft jedoch nur `Emission Color != (0, 0, 0, 1)` und ignoriert `Emission Strength`. → Default-Color triggert die Ausgabe von `emissiveColor`.

**Fix:** `Emission Strength`-Check ergänzen:

```diff
-                if not (0, 0, 0, 1) == (emissiveRed,emissiveGreen,emissiveBlue,emissiveAlpha):
+                emStrength = surfaceNode.inputs['Emission Strength'].default_value if 'Emission Strength' in surfaceNode.inputs else 0
+                if emStrength > 0 and not (0, 0, 0, 1) == (emissiveRed,emissiveGreen,emissiveBlue,emissiveAlpha):
                     m_data["emissiveColor"]  = "..."
```

**Zielpfad:** `C:\Users\nadin\AppData\Roaming\Blender Foundation\Blender\5.1\scripts\addons\io_export_i3d_10_0_2\dcc\dccBlender.py`

**Ergänzender Workaround im FS25-i3d-Importer:** Beim Anlegen von Materialien wird `Emission Color = (0, 0, 0, 1)` explizit gesetzt (siehe `importer.py._build_material`). Damit funktioniert auch ein ungepatchter Exporter für die von uns importierten Materialien. **Aber:** ohne Patch 02 sind selbst erstellte / hand-erstellte Materialien immer noch betroffen.

---

## Anwendung

**Zielpfad Patch 01:** `C:\Users\nadin\AppData\Roaming\Blender Foundation\Blender\5.1\scripts\addons\io_export_i3d_10_0_2\i3d_export.py`

**Zielpfad Patch 02:** `C:\Users\nadin\AppData\Roaming\Blender Foundation\Blender\5.1\scripts\addons\io_export_i3d_10_0_2\dcc\dccBlender.py`

### Variante A — Manuell (empfohlen)

1. Datei in einem Editor öffnen.
2. Nach `data["i3D_referenceChildPath"]` suchen (eine einzelne Trefferstelle,
   aktuell Zeile 900).
3. Ersetzen durch `data.get("i3D_referenceChildPath", "")`.
4. Datei speichern. Blender neu starten oder Addon deaktivieren+aktivieren.

### Variante B — Mit `patch.exe` (z.B. via Git-Bash)

```cmd
cd "C:\Users\nadin\AppData\Roaming\Blender Foundation\Blender\5.1\scripts\addons\io_export_i3d_10_0_2"
patch -p0 < "E:\Nextcloud\Blender\add-ons eigen\fs25_i3d_importer\patches\01-giants-exporter-referenceChildPath-keyerror.patch"
```

Die Patch-Datei nutzt Suchstrings als Anker (kein hartes Zeilennummern-
Hardcoding), funktioniert also auch, wenn der Giants-Exporter in einer
zukünftigen Version andere Zeilennummern hat — solange der Code-Schnipsel
selbst unverändert bleibt.

---

## Bei Giants-Exporter-Updates

Nach jedem Update von `io_export_i3d_*` muss der Patch erneut angewendet
werden (die Update-Installation überschreibt den Exporter-Source).

**Prüfung, ob Patch noch nötig ist:** in `i3d_export.py` nach
`data["i3D_referenceChildPath"]` (ohne `.get()`) suchen. Wenn der String
gefunden wird → Patch nötig. Wenn nicht → Giants hat den Bug selbst gefixt
und der Patch kann übersprungen werden.

---

## Verwandt

- Vault-Notiz: `3-BEREICHE/Modding/LS/Blender-Addon i3d-Import Phase C.md`
  (Stolperstein 10)
- Analoge Patch-Stelle: `C:\GiantsTools\I3DShapesTool-OBJx-FS25_source\patches\`
  (Patches am `I3DShapesTool-OBJx`-Fork)
