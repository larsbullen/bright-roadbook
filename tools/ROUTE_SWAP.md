# Route swap — what to do when the final GPX arrives

**Core principle:** every shop / tunnel / climb / waypoint is positioned by its distance
*along the route* (km). Change the geometry and **every km shifts** — even for unchanged
features. So almost nothing is hand-patchable; the data layer must be **re-derived**.
That's what `build_roadbook.py` does. Hand-curated facts that aren't in the GPX or OSM
live in `curated.json` and are merged back automatically.

## One-command regeneration

```bash
cd tools
python3 build_roadbook.py --gpx "/Users/olle/Downloads/<FINAL>.gpx"
```

Outputs to `tools/out/`: `DATA.js`, `shoplist.js`, `tunnels.js` (+ `shops.js` if added).
Add `--no-network` to regenerate only the GPX-derived `DATA` (route/climbs/totals) and skip
Overpass. Add `--validate` to compare against the current `index.html` (used on the v1.1 GPX).

Validated on `BM2026 v1.1.gpx`: totalKm, totalGain (17 857), max/min elevation reproduce
**exactly**; climb count is ~approximate (26 vs the live 23 — partly editorial).

## Paste into index.html (replace the whole matching line)

| Generated file | Replaces in index.html |
|---|---|
| `out/DATA.js`     | `const DATA={...};`     (line starting `<script>const DATA=`) |
| `out/shoplist.js` | `const SHOPLIST=[...];` |
| `out/tunnels.js`  | `const TUNNELS=[...];`  |
| `out/shops.js`    | `const SHOPS=[...];` (map pins — currently hand-kept; see note) |

The race-mode filter, schedule/ETA engine, ferry calculator and effort model all read off
these tables, so they **auto-adapt** — no JS logic changes needed.

## What's automatic vs what needs a human

**Automatic (regenerated):**
- Route geometry, cumulative km, smoothed elevation → `DATA.route`
- Climbs, totalKm, totalGain, max/min elevation
- `SHOPLIST` + `TUNNELS` re-queried from Overpass along the new line and snapped to km
- Curated waypoints re-snapped to new km; any now >400 m off-route are flagged in the report
- `curated.json` overlay merged: BK Oppdal, unmanned-store flags, manual shops

**Manual review (the script prints this list at the end):**
1. **Chips** at the top of the Schedule tab — distance / climbing / high point / #climbs / #tunnels.
2. **Prose with km in it** — search `index.html` and update:
   - Sleep-strategy chain (Sunndalsøra/Åndalsnes/Stranda/Lom/**Øvre Årdal**/Vågåmo/Alvdal + their km)
   - Cold-pass danger list (Sognefjellet 692, Valdresflye 860, Grotli 562, Dovre 989, Tyin/Filefjell 764)
   - "138 km of the route is above 1000 m"
   - Service-gaps table (the 7 rows: km ranges + last/first shop) — regenerate from new shop km
   - "3 towns the route does NOT enter" (Stryn −9 km, Folldal −6.5 km, Dombås −10 km) — recheck vs new route
   - Tunnels/Climbs intro blurbs ("All 20 tunnels…", "All 23 climbs…")
3. **Ferry** — confirm the route still crosses Storfjorden at Liabygda→Stranda. If the section
   changed, update/remove `#ferryPanel` + `FERRY_DEP` + `curated.ferry`. The ferry isn't
   auto-detectable (the GPX line snaps over land there).
4. **Marquee climb names** — Trollstigen / Sognefjellet / Aursjøvegen etc. come out as
   "Climb to Xm". Add `{name,lat,lon}` entries to `curated.climb_names` to auto-attach them
   (the script name-matches any climb whose summit is within 4 km).
5. **`SHOPS` map pins** (the 6 clutch markers) — currently hand-maintained; they have lat/lon,
   so pick the 6–8 most useful 24h/late shops from the new `SHOPLIST` and update by hand,
   or extend the script to emit them.
6. Re-run the validations / take before-after screenshots (planning + racing mode).

## curated.json — the things that must survive a swap

Non-GPX / non-OSM knowledge, edited here (never in the generated JS):
- `manual_shops` — e.g. **Burger King Oppdal** (only chain on route; not in OSM). *Verify its lat/lon.*
- `shop_flags` — unmanned døgnåpent stores (Coop Prix Bruvoll, Bøverdalen)
- `detour_towns` — Stryn / Folldal / Dombås off-route distances
- `ferry` — Fjord1 2026 timetable + the Liabygda→Stranda facts
- `wpts_base` — the 29 curated schedule waypoints (lat/lon + hand-written descriptions)
- `editorial_blocks` — sleep chain, cold-pass list, "km above 1000 m" (for re-review)
- `config` — smoothing window, climb thresholds, Overpass radii (calibrated to the live page)

## After pasting: ship it
Copy `index.html` → `~/Downloads/BM2026_roadbook.html`, commit, push (standing approval).
