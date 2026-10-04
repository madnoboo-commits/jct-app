"""
JCT・IC分岐点ごとに、案内板が見えやすいストリートビューのリンクを自動生成する。

流れ:
  1. OpenStreetMap(Overpass API)から、高速道路・自動車専用道路の本線と分岐ランプを取得
  2. ランプが本線から分かれる点(分岐点)を検出
  3. 本線を進行方向と逆にたどり、分岐点の手前 N m の地点と、分岐点へ向く方角を計算
  4. (任意) Street View メタデータAPI(無料)で、その付近に実在するパノラマへ寄せ、撮影年月を取得
  5. JSON と、確認用HTMLを出力

使い方:
  python jct_streetview.py --bbox 35.40,139.50,35.50,139.65        # 地域を指定
  python jct_streetview.py --bbox ... --key YOUR_GOOGLE_API_KEY     # パノラマ実在確認つき
  python jct_streetview.py --test                                  # 計算ロジックのテスト
"""
import argparse, json, math, sys, urllib.parse, urllib.request

OVERPASS = "https://overpass-api.de/api/interpreter"
DISTANCES = [300, 700]   # 分岐点の手前何mから見るか(直前の標識、予告標識を想定)
PITCH = 8                # 頭上の標識を見るため少し上向き
FOV = 75


def overpass_query(bbox):
    s, w, n, e = bbox
    q = f"""
    [out:json][timeout:300];
    (
      way["highway"="motorway"]({s},{w},{n},{e});
      way["highway"="trunk"]["motorroad"="yes"]({s},{w},{n},{e});
      way["highway"~"^(motorway|trunk)_link$"]({s},{w},{n},{e});
    );
    out body; >; out body qt;
    """
    data = urllib.parse.urlencode({"data": q}).encode()
    req = urllib.request.Request(OVERPASS, data, headers={"User-Agent": "jct-branch-app/0.1 (GitHub Actions)"})
    with urllib.request.urlopen(req, timeout=320) as r:
        return json.load(r)


def haversine(a, b):
    R = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def bearing(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def interp(a, b, f):
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


def directed_nodes(way):
    """一方通行の向きに沿ったノード列を返す(双方向の本線は対象外)"""
    t = way.get("tags", {})
    ow = t.get("oneway", "yes" if t.get("highway", "").startswith("motorway") else "no")
    if ow in ("yes", "1", "true"):
        return way["nodes"]
    if ow == "-1":
        return list(reversed(way["nodes"]))
    return None


def find_diverges(osm):
    nodes = {e["id"]: e for e in osm["elements"] if e["type"] == "node"}
    ways = [e for e in osm["elements"] if e["type"] == "way"]
    main, links = [], []
    for w in ways:
        hw = w.get("tags", {}).get("highway", "")
        (links if hw.endswith("_link") else main).append(w)

    # 本線の有向グラフ: ノード -> 直前ノード(逆向きにたどるため)
    pred, succ, main_name = {}, {}, {}
    for w in main:
        seq = directed_nodes(w)
        if not seq:
            continue
        for a, b in zip(seq, seq[1:]):
            pred.setdefault(b, a)
            succ.setdefault(a, b)
        for nid in seq:
            main_name.setdefault(nid, w.get("tags", {}).get("name", ""))

    results = []
    for lw in links:
        seq = directed_nodes(lw) or lw["nodes"]
        n0 = seq[0]
        # 本線の途中から分かれている(本線が分岐点の先へ続く)ものだけを分岐とみなす
        if n0 not in pred or n0 not in succ:
            continue
        node = nodes.get(n0, {})
        nt, lt = node.get("tags", {}), lw.get("tags", {})
        p0 = (node["lat"], node["lon"])
        views = []
        for D in DISTANCES:
            pt = walk_back(n0, D, pred, nodes)
            if pt:
                views.append({"distance_m": D, "lat": round(pt[0], 6), "lng": round(pt[1], 6),
                              "heading": round(bearing(pt, p0)), "pitch": PITCH})
        if not views:
            continue
        results.append({
            "diverge_node": n0,
            "lat": p0[0], "lng": p0[1],
            "junction": nt.get("name") or nt.get("ref") or "",
            "mainline": main_name.get(n0, ""),
            "destination": lt.get("destination") or lt.get("destination:ref") or lt.get("name") or "",
            "views": views,
        })
    return results


def walk_back(n0, D, pred, nodes):
    """分岐点から本線を逆方向に D m たどった地点"""
    cur, left, seen = n0, D, set()
    while cur in pred and cur not in seen:
        seen.add(cur)
        prv = pred[cur]
        a = (nodes[cur]["lat"], nodes[cur]["lon"])
        b = (nodes[prv]["lat"], nodes[prv]["lon"])
        seg = haversine(a, b)
        if seg >= left:
            return interp(a, b, left / seg)
        left -= seg
        cur = prv
    return None


def snap_to_pano(view, target, key):
    """Street View メタデータAPI(リクエスト無料)で実在パノラマに寄せる"""
    url = "https://maps.googleapis.com/maps/api/streetview/metadata?" + urllib.parse.urlencode(
        {"location": f'{view["lat"]},{view["lng"]}', "radius": 60, "source": "outdoor", "key": key})
    with urllib.request.urlopen(url, timeout=20) as r:
        m = json.load(r)
    if m.get("status") != "OK":
        view["pano"] = None
        return
    loc = (m["location"]["lat"], m["location"]["lng"])
    view.update(pano=m["pano_id"], pano_date=m.get("date"),
                heading=round(bearing(loc, target)))


def sv_link(v):
    p = {"api": 1, "map_action": "pano", "heading": v["heading"], "pitch": v["pitch"], "fov": FOV}
    if v.get("pano"):
        p["pano"] = v["pano"]
    else:
        p["viewpoint"] = f'{v["lat"]},{v["lng"]}'
    return "https://www.google.com/maps/@?" + urllib.parse.urlencode(p)


def write_html(results, path):
    rows = []
    for r in results:
        links = " ".join(f'<a href="{sv_link(v)}" target="_blank">手前{v["distance_m"]}m'
                         + (f' ({v["pano_date"]})' if v.get("pano_date") else "") + "</a>"
                         for v in r["views"])
        rows.append(f"<tr><td>{r['junction']}</td><td>{r['mainline']}</td>"
                    f"<td>{r['destination']}</td><td>{links}</td></tr>")
    html = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            "<style>body{font-family:sans-serif;padding:12px}td,th{border-bottom:1px solid #ddd;padding:6px;font-size:14px}</style>"
            "<table><tr><th>分岐</th><th>本線</th><th>行き先</th><th>ストリートビュー</th></tr>"
            + "".join(rows) + "</table>")
    open(path, "w", encoding="utf-8").write(html)


def self_test():
    """東向きの本線(4ノード)から、3番目のノードでランプが左へ分かれる架空データ"""
    lat = 35.45
    osm = {"elements": [
        {"type": "node", "id": 1, "lat": lat, "lon": 139.590},
        {"type": "node", "id": 2, "lat": lat, "lon": 139.595},
        {"type": "node", "id": 3, "lat": lat, "lon": 139.600,
         "tags": {"highway": "motorway_junction", "name": "テストJCT"}},
        {"type": "node", "id": 4, "lat": lat, "lon": 139.605},
        {"type": "node", "id": 5, "lat": lat + 0.003, "lon": 139.604},
        {"type": "way", "id": 10, "nodes": [1, 2, 3, 4],
         "tags": {"highway": "motorway", "oneway": "yes", "name": "テスト本線"}},
        {"type": "way", "id": 11, "nodes": [3, 5],
         "tags": {"highway": "motorway_link", "oneway": "yes", "destination": "テスト方面"}},
        {"type": "way", "id": 12, "nodes": [4, 1],   # 本線の終点から出る道は分岐とみなさない確認用
         "tags": {"highway": "motorway_link", "oneway": "yes"}},
    ]}
    res = find_diverges(osm)
    assert len(res) == 1, res
    r = res[0]
    assert r["junction"] == "テストJCT" and r["destination"] == "テスト方面"
    for v in r["views"]:
        d = haversine((v["lat"], v["lng"]), (r["lat"], r["lng"]))
        assert abs(d - v["distance_m"]) < 2, (d, v)       # 手前の距離が合っている
        assert v["lng"] < r["lng"]                          # 進行方向の手前(西側)にいる
        assert abs(v["heading"] - 90) <= 1, v               # 分岐点(東)を向いている
    print(json.dumps(res, ensure_ascii=False, indent=1))
    print(sv_link(r["views"][0]))
    print("テスト成功")


def update_index(outdir):
    import glob, os
    regs = []
    for f in sorted(glob.glob(os.path.join(outdir, "*.json"))):
        name = os.path.basename(f)[:-5]
        if name == "regions":
            continue
        n = len(json.load(open(f, encoding="utf-8")))
        regs.append({"id": name, "count": n})
    json.dump(regs, open(os.path.join(outdir, "regions.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", help="南緯,西経,北緯,東経")
    ap.add_argument("--key", help="Google Maps APIキー(任意)")
    ap.add_argument("--region", default="diverges", help="地域名(出力ファイル名になる)")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--from-file", help="Overpassの代わりに保存済みJSONを読む(テスト用)")
    a = ap.parse_args()
    if a.test or not (a.bbox or a.from_file):
        self_test()
        sys.exit()
    os.makedirs(a.outdir, exist_ok=True)
    osm = json.load(open(a.from_file, encoding="utf-8")) if a.from_file else \
        overpass_query([float(x) for x in a.bbox.split(",")])
    res = find_diverges(osm)
    if a.key:
        for r in res:
            for v in r["views"]:
                try:
                    snap_to_pano(v, (r["lat"], r["lng"]), a.key)
                except Exception as e:
                    print("metadata error:", e)
    for r in res:
        for v in r["views"]:
            v["url"] = sv_link(v)
    out = os.path.join(a.outdir, a.region + ".json")
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    update_index(a.outdir)
    print(f"{len(res)} 件の分岐を出力しました: {out}")
