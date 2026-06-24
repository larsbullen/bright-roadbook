#!/usr/bin/env python3
"""
build_roadbook.py — regenerate all route-derived data for the Bright Midnight roadbook
from a GPX file, so swapping in the final route is one command instead of hand-editing.

WHY THIS EXISTS
  Every shop / tunnel / climb / waypoint is positioned by its distance ALONG the route
  (km). Change the route geometry and *every* km shifts — even for unchanged features.
  So almost nothing can be hand-patched; it must be re-derived. This script does that.

WHAT IT REGENERATES
  - DATA (route geometry + cumulative km + smoothed elevation, climbs, totals)   [from GPX]
  - SHOPS / SHOPLIST / TUNNELS  km re-snapped or re-queried                        [GPX + OSM]
  - service gaps, chips/stats                                                       [derived]
  Hand-curated, non-GPX/non-OSM facts live in curated.json and are MERGED in, never lost.

USAGE
  python3 build_roadbook.py --gpx "../../Downloads/BM2026 final.gpx"          # full (needs network for OSM)
  python3 build_roadbook.py --gpx "..."  --no-network                          # geometry only (DATA/climbs), skip Overpass
  python3 build_roadbook.py --validate                                         # run on current GPX, compare to live index.html

OUTPUT
  tools/out/DATA.js, shoplist.js, tunnels.js, shops.js  + a console report.
  Paste each into index.html (replace the matching const ...= line), then do the
  manual-review items printed at the end (prose km, marquee names, ferry presence).
"""
import argparse, json, math, os, re, sys, time, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_GPX = os.path.join(ROOT, "..", "Downloads", "BM2026 v1.1.gpx")
GPXNS = "{http://www.topografix.com/GPX/1/1}"

# ---------- geometry ----------
def haversine(a, b):
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    d1, d2 = la2 - la1, lo2 - lo1
    h = math.sin(d1/2)**2 + math.cos(la1)*math.cos(la2)*math.sin(d2/2)**2
    return 2*R*math.asin(math.sqrt(h))

def parse_gpx(path):
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    pts = [(float(p.get("lat")), float(p.get("lon")), float(p.findtext(GPXNS+"ele") or 0))
           for p in root.iter(GPXNS+"trkpt")]
    if not pts:  # fall back to a <rtept> route (some exporters)
        pts = [(float(p.get("lat")), float(p.get("lon")), float(p.findtext(GPXNS+"ele") or 0))
               for p in root.iter(GPXNS+"rtept")]
    if len(pts) < 2:
        sys.exit("ERROR: GPX has no usable trkpt/rtept points")
    return pts

def cumulative_km(pts):
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + haversine(pts[i-1], pts[i]))
    return cum  # metres

def smooth(seq, w):
    out = []
    for i in range(len(seq)):
        a, b = max(0, i-w), min(len(seq), i+w+1)
        out.append(sum(seq[a:b])/(b-a))
    return out

# ---------- climbs ----------
def detect_climbs(pts, cum_m, ele_s, min_gain, dip=60):
    """Pair successive valley->summit ascents with net gain >= min_gain (allows small dips)."""
    climbs = []
    n = len(ele_s)
    i = 0
    DIP = dip  # m: a drop bigger than this ends the current climb
    while i < n-1:
        if ele_s[i+1] <= ele_s[i]:
            i += 1; continue
        start = i; lo = ele_s[i]; top = i
        j = i
        while j < n-1:
            if ele_s[j+1] >= ele_s[top]:
                top = j+1
            if ele_s[j+1] < ele_s[top] - DIP:
                break
            j += 1
        gain = ele_s[top] - ele_s[start]
        if gain >= min_gain:
            length = (cum_m[top]-cum_m[start])/1000.0
            # max ~1km pitch
            mp = 0.0
            k = start
            while k < top:
                m = k
                while m < top and (cum_m[m]-cum_m[k]) < 1000: m += 1
                seg = (ele_s[m]-ele_s[k])/max(1.0, cum_m[m]-cum_m[k])*100
                mp = max(mp, seg); k += max(1, m-k)
            climbs.append({
                "startKm": round(cum_m[start]/1000, 1), "topKm": round(cum_m[top]/1000, 1),
                "len": round(length, 1), "gain": round(gain),
                "grad": round(gain/max(1.0, length*1000)*100, 1), "maxPitch": round(mp),
                "fromEle": round(ele_s[start]), "topEle": round(ele_s[top]),
                "name": "Climb to %dm" % round(ele_s[top]),
            })
            i = top
        else:
            i = j if j > i else i+1
    return climbs

def name_climbs(climbs, curated, pts, cum_m):
    """Attach marquee pass names from curated.editorial_blocks/climb_names by proximity."""
    names = curated.get("climb_names", [])
    if not names:
        return
    # route point nearest a given km
    def at_km(km):
        target = km*1000
        lo, hi = 0, len(cum_m)-1
        while lo < hi:
            mid = (lo+hi)//2
            if cum_m[mid] < target: lo = mid+1
            else: hi = mid
        return pts[lo]
    for c in climbs:
        p = at_km(c["topKm"])
        for nm in names:
            if haversine((p[0], p[1]), (nm["lat"], nm["lon"])) < 4000:
                c["name"] = nm["name"]; break

# ---------- snapping ----------
def build_snapper(pts, cum_m):
    from collections import defaultdict
    buckets = defaultdict(list)
    for i, p in enumerate(pts):
        buckets[(round(p[0], 2), round(p[1], 2))].append(i)
    def snap(lat, lon):
        best_i, best_d = -1, 1e18
        for dla in (-0.01, 0, 0.01):
            for dlo in (-0.01, 0, 0.01):
                for i in buckets.get((round(lat+dla, 2), round(lon+dlo, 2)), []):
                    d = haversine(pts[i], (lat, lon))
                    if d < best_d: best_d, best_i = d, i
        if best_i < 0:  # brute force fallback
            for i, p in enumerate(pts):
                d = haversine(p, (lat, lon))
                if d < best_d: best_d, best_i = d, i
        return cum_m[best_i]/1000.0, best_d  # km, offset_m
    return snap

# ---------- Overpass ----------
UA = {"User-Agent": "BM2026-roadbook-build/1.0 (olle.larsson@framna.com)"}
def overpass(query):
    body = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request("https://overpass-api.de/api/interpreter", data=body, headers=UA)
    for attempt in range(5):
        try:
            return json.load(urllib.request.urlopen(req, timeout=180))
        except Exception as e:
            print("  overpass retry %d: %s" % (attempt, e)); time.sleep(8)
    sys.exit("ERROR: Overpass failed after retries")

def downsample_anchors(pts, cum_m, step_m):
    ds, last = [pts[0]], 0
    for i in range(1, len(pts)):
        if cum_m[i]-cum_m[last] >= step_m:
            ds.append(pts[i]); last = i
    return ds

def query_tunnels(pts, cum_m, snap, cfg):
    anchors = downsample_anchors(pts, cum_m, 120)
    seen = {}
    CHUNK = 900
    for s in range(0, len(anchors), CHUNK):
        seg = anchors[s:s+CHUNK+1]
        coords = ",".join("%.5f,%.5f" % (la, lo) for la, lo, *_ in seg)
        q = "[out:json][timeout:120];way(around:%d,%s)[tunnel=yes][highway];out tags geom;" % (cfg["tunnel_overpass_radius_m"], coords)
        for e in overpass(q)["elements"]:
            seen[e["id"]] = e
        time.sleep(3)
    rows = []
    for e in seen.values():
        g = e.get("geometry") or []
        if len(g) < 2: continue
        L = sum(haversine((g[i]["lat"], g[i]["lon"]), (g[i+1]["lat"], g[i+1]["lon"])) for i in range(len(g)-1))
        if L < cfg["tunnel_min_len_m"]: continue
        mid = g[len(g)//2]
        km, off = snap(mid["lat"], mid["lon"])
        if off > 60: continue
        tg = e.get("tags", {})
        lit = 1 if tg.get("lit") == "yes" else (0 if tg.get("lit") == "no" else None)
        ref = tg.get("ref", ""); hw = tg.get("highway", "")
        road = ("fv"+ref) if ref.isdigit() else (ref or {"cycleway": "cycleway", "footway": "path",
                "track": "track", "service": "service road", "unclassified": "minor road"}.get(hw, hw))
        rows.append([round(km, 1), round(L), tg.get("name", ""), road, lit, round(mid["lat"], 5), round(mid["lon"], 5)])
    rows.sort(key=lambda r: r[0])
    return rows

def query_shops(pts, cum_m, snap, cfg):
    anchors = downsample_anchors(pts, cum_m, 200)
    seen = {}
    CHUNK = 700
    flt = ('nwr(around:%d,{C})["shop"~"supermarket|convenience|general"];'
           'nwr(around:%d,{C})["amenity"="fuel"];'
           'nwr(around:%d,{C})["amenity"="fast_food"];'
           'nwr(around:%d,{C})["shop"~"bicycle|sports"];'           # bike repair / sports retailers
           'nwr(around:%d,{C})["service:bicycle:repair"="yes"];')
    r = cfg["shop_overpass_radius_m"]; rb = cfg.get("bike_overpass_radius_m", 4500)
    for s in range(0, len(anchors), CHUNK):
        seg = anchors[s:s+CHUNK+1]
        coords = ",".join("%.5f,%.5f" % (la, lo) for la, lo, *_ in seg)
        q = "[out:json][timeout:150];(" + flt.replace("{C}", coords) % (r, r, r, rb, rb) + ");out tags center;"
        for e in overpass(q)["elements"]:
            seen[(e["type"], e["id"])] = e
        time.sleep(3)
    rows = []
    for e in seen.values():
        c = e.get("center") or {"lat": e.get("lat"), "lon": e.get("lon")}
        if not c.get("lat"): continue
        km, off = snap(c["lat"], c["lon"])
        tg = e.get("tags", {})
        shop = tg.get("shop", ""); amen = tg.get("amenity", "")
        if amen == "fuel": cat = "f"
        elif amen == "fast_food": cat = "h"
        elif shop in ("bicycle", "sports") or tg.get("service:bicycle:repair") == "yes": cat = "b"
        elif shop in ("supermarket", "convenience", "general"): cat = "g"
        else: cat = "o"
        typ = {"f": "Fuel", "h": "Hot food", "g": "Grocery", "b": "Bike/sport", "o": "Shop"}[cat]
        name = tg.get("name", "?")
        hrs = tg.get("opening_hours", "")
        rows.append([round(km), round(off), cat, typ, name, hrs, tg.get("addr:city", "") or tg.get("addr:place", "")])
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows

# ---------- merge / gaps ----------
def merge_curated_shops(shoplist, curated):
    for ms in curated.get("manual_shops", []):
        # caller fills km via snap; here we just trust pre-snapped manual entries appended elsewhere
        pass
    # apply name-based flags / hour overrides
    flags = {f["match"]: f for f in curated.get("shop_flags", [])}
    for row in shoplist:
        for key, f in flags.items():
            if key.lower() in row[4].lower():
                if f.get("flag") == "unmanned" and "ⓤ" not in row[4]:
                    row[4] = "ⓤ " + row[4]
                if f.get("hours") is not None:
                    row[5] = f["hours"]  # override OSM hours (e.g. self-service 24h that needs Norwegian BankID)
    return shoplist

def js_const(name, arr):
    return "const %s=%s;" % (name, json.dumps(arr, ensure_ascii=False))

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpx", default=DEFAULT_GPX)
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--validate", action="store_true", help="run on current GPX and compare to live index.html")
    args = ap.parse_args()

    curated = json.load(open(os.path.join(HERE, "curated.json")))
    cfg = curated["config"]
    print("== parsing GPX: %s ==" % args.gpx)
    pts = parse_gpx(args.gpx)
    cum_m = cumulative_km(pts)
    totalKm = round(cum_m[-1]/1000, 1)
    ele_s = smooth([p[2] for p in pts], cfg["ele_smooth_window"])
    totalGain = round(sum(max(0, ele_s[i]-ele_s[i-1]) for i in range(1, len(ele_s))))
    maxEle, minEle = round(max(ele_s)), round(min(ele_s))
    print("   %d pts | %.1f km | +%d m | max %d / min %d m" % (len(pts), totalKm, totalGain, maxEle, minEle))

    climbs = detect_climbs(pts, cum_m, ele_s, cfg["climb_min_gain_m"], cfg.get("climb_merge_dip_m", 60))
    name_climbs(climbs, curated, pts, cum_m)
    print("   climbs (>=%dm): %d" % (cfg["climb_min_gain_m"], len(climbs)))

    # downsampled DATA.route
    route = []
    last = -1e9
    for i, p in enumerate(pts):
        if cum_m[i]-last >= cfg["downsample_m"] or i == 0 or i == len(pts)-1:
            route.append([round(p[0], 5), round(p[1], 5), round(ele_s[i]), round(cum_m[i]/1000, 1)])
            last = cum_m[i]

    snap = build_snapper(pts, cum_m)

    # re-snap coordinate-bearing curated waypoints
    wpts = []
    off_route = []
    cumGain_at = lambda km: round(sum(max(0, ele_s[i]-ele_s[i-1]) for i in range(1, len(pts)) if cum_m[i] <= km*1000))
    for w in curated["wpts_base"]:
        km, off = snap(w["lat"], w["lon"])
        nw = dict(w); nw["km"] = round(km, 1)
        if off > 400:
            off_route.append((w["name"], round(off)))
        wpts.append(nw)
    wpts.sort(key=lambda w: w["km"])

    DATA = {"route": route, "climbs": climbs, "wpts": wpts,
            "totalKm": totalKm, "totalGain": totalGain, "maxEle": maxEle, "minEle": minEle}

    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    open(os.path.join(HERE, "out", "DATA.js"), "w").write("const DATA=" + json.dumps(DATA, ensure_ascii=False) + ";")
    print("   wrote out/DATA.js")

    shoplist = tunnels = shops = None
    if not args.no_network:
        print("== Overpass: tunnels ==")
        tunnels = query_tunnels(pts, cum_m, snap, cfg)
        open(os.path.join(HERE, "out", "tunnels.js"), "w").write(js_const("TUNNELS", tunnels))
        print("   %d tunnels -> out/tunnels.js" % len(tunnels))
        print("== Overpass: shops/fuel/food ==")
        shoplist = query_shops(pts, cum_m, snap, cfg)
        # append manual shops (snap their km) — skip if OSM already lists it (data drift)
        for ms in curated.get("manual_shops", []):
            km, off = snap(ms["lat"], ms["lon"])
            key = ms["name"].split()[0].lower()
            if any(key in r[4].lower() and abs(r[0]-round(km)) <= 3 for r in shoplist):
                print("   manual shop '%s' now in OSM near km %d — skipped (dedup)" % (ms["name"], round(km)))
                continue
            shoplist.append([round(km), round(off), ms["cat"], ms.get("type", "Shop"), ms["name"], ms.get("hours", ""), ms.get("town", "")])
        shoplist = merge_curated_shops(sorted(shoplist, key=lambda r: (r[0], r[1])), curated)
        open(os.path.join(HERE, "out", "shoplist.js"), "w").write(js_const("SHOPLIST", shoplist))
        print("   %d shops (incl. %d manual) -> out/shoplist.js" % (len(shoplist), len(curated.get("manual_shops", []))))
    else:
        print("== --no-network: skipped Overpass (SHOPLIST/TUNNELS/SHOPS unchanged) ==")

    # ---- report ----
    print("\n================ REPORT ================")
    if off_route:
        print("⚠ curated waypoints now >400 m off the route (re-check / move):")
        for n, d in off_route: print("    %-28s %d m off" % (n, d))
    else:
        print("✓ all curated waypoints snap onto the route")
    print("\nMANUAL REVIEW after pasting the generated JS:")
    print("  • Chips:  %d km · +%s m · %d m high · %d climbs · %s tunnels"
          % (totalKm, "{:,}".format(totalGain), maxEle, len(climbs), len(tunnels) if tunnels else "?"))
    print("  • Prose km refs (search & update): sleep chain, cold-pass list, service-gaps table,")
    print("    'km above 1000 m', the '3 towns the route skips' detours, ferry km (~%s)." % curated["ferry"]["approx_km"])
    print("  • Confirm the Liabygda→Stranda ferry is still on the route (see curated.ferry).")
    print("  • Marquee climb names (Trollstigen/Sognefjellet/Aursjøvegen) — add to curated.climb_names if missing.")

    if args.validate:
        print("\n================ VALIDATE vs live index.html ================")
        h = open(os.path.join(ROOT, "index.html")).read()
        i = h.index("const DATA=")+len("const DATA="); j = h.index("};</script>", i)+1
        L = json.loads(h[i:j])
        def cmp(label, got, exp, tol=0):
            ok = abs(got-exp) <= tol
            print("  %s %-12s got %s  vs live %s  (tol %s)" % ("✓" if ok else "✗", label, got, exp, tol))
        cmp("totalKm", totalKm, L["totalKm"], 1)
        cmp("totalGain", totalGain, L["totalGain"], 400)
        cmp("maxEle", maxEle, L["maxEle"], 8)
        cmp("minEle", minEle, L["minEle"], 5)
        cmp("climbs", len(climbs), len(L["climbs"]), 6)  # climb list is approximate/editorial
        print("  (geometry should match closely; OSM-derived counts re-derive fresh by design)")

if __name__ == "__main__":
    main()
