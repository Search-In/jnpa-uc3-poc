# 2D / 3D Map Integration — UC-III

The reference 3D engine (ArcGIS SceneView + glTF object symbols + polygon
extrusion + day/dusk sun lighting + offline basemap fallback, from `jnpa_poc_2`)
is integrated **into the existing UC-III map** as a Google-Maps-style 2D/3D
toggle. There is **no separate page and no new sidebar menu**. Switching 2D↔3D
swaps only the map canvas; the 3D scene renders the **same live data props** the
2D map already receives — no simulator / mock / demo / sample / hardcoded assets.

## How it works

`ArcgisMap` (the single, data-driven map surface used by all 5 map screens)
gained a `mode` state and a toolbar toggle. In `2d` it renders the existing
`<arcgis-map>` MapView unchanged; in `3d` it renders the new `Scene3D`
SceneView, passing it the **identical** `corridor / gates / zones / snapshots /
trucks / parkingFacilities / highlights / focusPoint` props. Because every screen
already renders `<ArcgisMap>`, all five map screens get the toggle with **zero
per-screen changes**.

### Live-data → 3D mapping (same source as 2D)

| UC3 data | 2D symbol | 3D symbol |
|---|---|---|
| NH-348 corridor + snapshots | jam-coloured polyline | jam-coloured 3D ribbon |
| congestion | graduated halo | column that rises with jam |
| geofence zones | translucent polygon | extruded prism |
| parking facilities | status square | status block, height = occupancy |
| gates | utilisation dot | boom-barrier kiosk, canopy = utilisation |
| trucks (live positions + heading) | dot | glTF vehicle facing its heading |
| tour highlight / alert focus | halo ring | translucent spotlight beam |

## Files

**Created**
- `web/src/components/map/scene3d/Scene3D.tsx` — the 3D SceneView, fed the live UC3 props.
- `web/src/components/map/scene3d/sceneUtils.ts` — stable-oid + in-place FeatureLayer diff (reused engine helper).
- `web/src/components/map/scene3d/basemapFallback.ts` — offline basemap survival (reused from reference).
- `web/public/models/*.glb` — 24 CC0/CC-BY glTF models (truck/pickup/gate-boom used; see CREDITS.md).
- `docs/screenshots/live-2d*.png`, `live-3d*.png`, `command-center-*.png`, `parking-*.png`.

**Modified (minimal)**
- `web/src/components/map/ArcgisMap.tsx` — `mode` state + toolbar toggle + conditional canvas (+69/−14). No change to the 2D rendering path or props.

**Not modified** — every screen, dashboard, panel, KPI, filter, API, DB, auth,
router, nav. (App.tsx / auth.ts / i18n were briefly edited during an earlier
separate-page approach and fully reverted to net-zero.)

## Status
- **Typecheck**: `tsc -b --noEmit` — clean.
- **Runtime**: dev server (mock data), `/live` + `/command-center` verified in
  both 2D and 3D. 2D shows the road-snapped corridor + gates + trucks; 3D shows
  the corridor ribbon over satellite imagery with congestion/gate markers — same
  data, one source.
- **Existing features**: unaffected (only `ArcgisMap` touched; the 2D path is
  byte-identical apart from being wrapped in `mode === "2d"`).
