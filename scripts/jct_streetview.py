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
import argparse, json, math, re, sys, time, urllib.parse, urllib.request
from collections import Counter

# 公開Overpassは混雑で 504/429 を返すことがあるため、複数のミラーを順に試す
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
]
OVERPASS = OVERPASS_MIRRORS[0]
DISTANCES = [100]        # 分岐点の手前何mから見るか
PICK_SPAN = 500          # 看板さがしの対象: 分岐点の手前何mまで
PICK_STEP = 20           # 候補地点の間隔(m)
PICK_BACK = 20           # 看板が見つかった地点から、さらに何m手前を撮影地点にするか
PITCH = 8                # 頭上の標識を見るため少し上向き
FOV = 75


def overpass_query(bbox):
    s, w, n, e = bbox
    return overpass_raw(f"""
    [out:json][timeout:300];
    (
      way["highway"="motorway"]({s},{w},{n},{e});
      way["highway"="trunk"]({s},{w},{n},{e});
      way["highway"~"^(motorway|trunk)_link$"]({s},{w},{n},{e});
    );
    out body; >; out body qt;
    """)


def overpass_raw(q, timeout=320, rounds=2):
    """timeout はミラー1か所あたりの待ち時間。小さな問い合わせには短い値を渡す"""
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for attempt in range(rounds):                 # ミラー一巡を rounds 回まで
        for url in OVERPASS_MIRRORS:
            req = urllib.request.Request(
                url, data, headers={"User-Agent": "jct-branch-app/0.1 (GitHub Actions)"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    print(f"Overpass 取得成功: {url}", file=sys.stderr)
                    return json.load(r)
            except Exception as e:
                last = e
                print(f"Overpass 失敗 ({url}): {e}", file=sys.stderr)
        if attempt < rounds - 1:
            wait = 30
            print(f"{wait}秒待って再試行します…", file=sys.stderr)
            time.sleep(wait)
    raise SystemExit(
        f"Overpass からデータを取得できませんでした（最後のエラー: {last}）。\n"
        "サーバーの混雑が原因のことが多いため、数分おいてもう一度実行してください。\n"
        "範囲(--bbox)を狭めると成功しやすくなります。")


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


def build_graph(osm):
    """OSMの生データから、本線の有向グラフと分岐ランプを組み立てる"""
    nodes = {e["id"]: e for e in osm["elements"] if e["type"] == "node"}
    ways = [e for e in osm["elements"] if e["type"] == "way"]
    main, links = [], []
    for w in ways:
        hw = w.get("tags", {}).get("highway", "")
        (links if hw.endswith("_link") else main).append(w)

    # 本線の有向グラフ: ノード -> 直前ノード(逆向きにたどるため)
    pred, succ, main_name = {}, {}, {}
    skipped = []                      # 向きが決まらず対象外になった本線
    for w in main:
        seq = directed_nodes(w)
        if not seq:
            skipped.append(w)
            continue
        for a, b in zip(seq, seq[1:]):
            pred.setdefault(b, a)
            succ.setdefault(a, b)
        for nid in seq:
            main_name.setdefault(nid, w.get("tags", {}).get("name", ""))
    return nodes, main, links, pred, succ, main_name, skipped


def is_expressway(way):
    """高速道路・自動車専用道の本線か。一般道(国道1号など)と区別する。

    motorway は無条件。trunk は motorroad=yes、または高速道路ナンバリング
    (E1 など)を持つものだけを自動車専用道とみなす。
    """
    t = way.get("tags", {})
    hw = t.get("highway", "")
    if hw == "motorway":
        return True
    if hw != "trunk":
        return False
    if t.get("motorroad") == "yes":
        return True
    return any(re.fullmatch(r"E\d+[A-Z]?", x.strip())
               for x in t.get("ref", "").split(";") if x.strip())


def reaches_expressway(lw, links, on_way, src_names, max_hops=12):
    """ランプをたどって、分岐元とは別の高速道路の本線に出るかを調べる。

    出れば JCT、どこにも出ずに終われば(=一般道へ降りる) IC とみなす。
    一般道は Overpass で取得していないため、データ上は自然に行き止まりになる。
    """
    seen, queue = {lw["id"]}, [(lw, 0)]
    while queue:
        w, hop = queue.pop(0)
        seq = directed_nodes(w) or w["nodes"]
        for nid in seq[1:]:                      # 始点は分岐元なので除く
            for other in on_way.get(nid, []):
                if other.get("tags", {}).get("highway", "").endswith("_link"):
                    continue
                if is_expressway(other) and other.get("tags", {}).get("name", "") not in src_names:
                    return True
        if hop >= max_hops:
            continue
        for nid in seq[1:]:
            for nxt in links.get(nid, []):
                if nxt["id"] in seen:
                    continue
                # ランプの始点がこのノードのものだけを前方向としてたどる
                if (directed_nodes(nxt) or nxt["nodes"])[0] != nid:
                    continue
                seen.add(nxt["id"])
                queue.append((nxt, hop + 1))
    return False


def candidates_for(n0, p0, pred, nodes, span=PICK_SPAN, step=PICK_STEP, back=PICK_BACK):
    """分岐点の手前 span m までを step m ごとに刻み、各地点の視点を作る。

    各候補には「そこから更に back m 下がった地点」(pick)を持たせる。
    看板が見えた最初の地点を選べば、その pick がそのまま撮影地点になる。
    """
    out = []
    for d in range(0, span + 1, step):
        pt = walk_back(n0, d, pred, nodes) if d else p0
        if not pt:
            break                      # 本線をそこまでさかのぼれない
        c = {"distance_m": d, "lat": round(pt[0], 6), "lng": round(pt[1], 6),
             "heading": round(bearing(pt, p0)) if d else None, "pitch": PITCH}
        bp = walk_back(n0, d + back, pred, nodes)
        if bp:
            c["pick"] = {"distance_m": d + back, "lat": round(bp[0], 6), "lng": round(bp[1], 6),
                         "heading": round(bearing(bp, p0)), "pitch": PITCH}
        out.append(c)
    return out


def find_diverges(osm, with_candidates=False, jct_only=False):
    nodes, main, links, pred, succ, main_name, _ = build_graph(osm)

    # ノード -> そのノードを含むway(本線・ランプとも)
    on_way, link_at = {}, {}
    for w in main + links:
        for nid in w["nodes"]:
            on_way.setdefault(nid, []).append(w)
    for w in links:
        for nid in w["nodes"]:
            link_at.setdefault(nid, []).append(w)
    # 本線wayを名前で引けるようにする(分岐元の路線名の集合を作るため)
    main_by_node = {}
    for w in main:
        for nid in w["nodes"]:
            main_by_node.setdefault(nid, []).append(w)

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
        if jct_only:
            # 分岐元の路線名(同じ本線の別wayを「別の高速」と誤判定しないため)
            src = {w.get("tags", {}).get("name", "") for w in main_by_node.get(n0, [])}
            if not reaches_expressway(lw, link_at, on_way, src):
                continue
        results.append({
            "diverge_node": n0,
            "lat": p0[0], "lng": p0[1],
            "junction": nt.get("name") or nt.get("ref") or "",
            "mainline": main_name.get(n0, ""),
            "destination": lt.get("destination") or lt.get("destination:ref") or lt.get("name") or "",
            "views": views,
        })
        if with_candidates:
            results[-1]["candidates"] = candidates_for(n0, p0, pred, nodes)
    return results


def probe_nodes(ids):
    """指定ノードが『どんな道路』に属しているかを、種別を問わず調べる"""
    q = ("[out:json][timeout:60];node(id:" + ",".join(str(i) for i in ids) +
         ")->.n;way(bn.n);out body;")
    osm = overpass_raw(q, timeout=45)
    ways = [e for e in osm["elements"] if e["type"] == "way"]
    want = set(ids)
    byn = {}
    for w in ways:
        for nid in w["nodes"]:
            if nid in want:
                byn.setdefault(nid, []).append(w)
    print(f"調べたノード {len(ids)}個 / 見つかったway {len(ways)}本\n")
    for nid in ids:
        print(f"node {nid}:")
        for w in byn.get(nid, []):
            t = w.get("tags", {})
            hw = t.get("highway", "(highwayタグなし)")
            extra = " ".join(f"{k}={t[k]}" for k in ("motorroad", "oneway") if k in t)
            print(f"    way {w['id']:>11}  highway={hw:<16} {t.get('name','')}  {extra}")
        if not byn.get(nid):
            print("    (属するwayが見つからない)")
        print()


def probe_ways(ids):
    """指定wayの全タグを表示する(本線の種別を見分ける手がかりを探す)"""
    q = "[out:json][timeout:60];way(id:" + ",".join(str(i) for i in ids) + ");out tags;"
    osm = overpass_raw(q, timeout=45)
    for w in [e for e in osm["elements"] if e["type"] == "way"]:
        t = w.get("tags", {})
        print(f"way {w['id']}  ({t.get('name','名称なし')})")
        for k in sorted(t):
            print(f"    {k} = {t[k]}")
        print()


def diagnose(osm, keyword=""):
    """各ランプが分岐として採用された/されなかった理由を一覧で出す(原因調査用)"""
    nodes, main, links, pred, succ, main_name, skipped = build_graph(osm)
    # ノードが、どの本線wayに含まれるか
    on_main = {}
    for w in main:
        for nid in w["nodes"]:
            on_main.setdefault(nid, []).append(w)
    # ランプの「始点以外」のノード: ここから分かれるのはランプ同士の枝分かれ
    on_link = set()
    for w in links:
        seq = directed_nodes(w) or w["nodes"]
        on_link.update(seq[1:])

    print(f"本線way {len(main)}本 / ランプway {len(links)}本 / ノード {len(nodes)}個")
    print(f"向きが決まらず本線グラフから除外されたway: {len(skipped)}本")
    for w in skipped[:15]:
        t = w.get("tags", {})
        print(f"    way {w['id']} highway={t.get('highway')} oneway={t.get('oneway')!r} name={t.get('name','')}")

    reasons = Counter()
    print("\n--- ランプごとの判定 ---")
    for lw in links:
        seq = directed_nodes(lw) or lw["nodes"]
        n0 = seq[0]
        lt = lw.get("tags", {})
        nt = nodes.get(n0, {}).get("tags", {})
        label = nt.get("name") or nt.get("ref") or lt.get("destination") or ""
        has_p, has_s = n0 in pred, n0 in succ
        if has_p and has_s:
            why = "採用"
        elif not on_main.get(n0):
            # 本線上にない場合、別のランプ上なのか、何にも繋がっていないのかを分ける
            if n0 in on_link:
                why = "不採用: ランプの途中(分岐点ではない)"
            else:
                why = "不採用: 接続先の道路をOverpassで取得していない"
        elif not has_p and not has_s:
            why = "不採用: 始点の本線が有向グラフに無い(onewayなし等)"
        elif not has_p:
            why = "不採用: 本線をさかのぼれない(始点が本線wayの先頭)"
        else:
            why = "不採用: 本線が先に続かない(始点が本線wayの末尾)"
        reasons[why] += 1
        if not keyword or keyword in (label + lt.get("name", "")):
            mains = on_main.get(n0, [])
            mn = "/".join(sorted({w.get("tags", {}).get("name", "?") for w in mains})) or "(なし)"
            print(f"  way {lw['id']:>11} 始点node {n0:>11} [{label or '名称なし'}] "
                  f"本線={mn} -> {why}")

    print("\n--- 判定の内訳 ---")
    for why, c in reasons.most_common():
        print(f"  {c:>4}本  {why}")

    # 取得もれで落ちた分岐のうち、名前がついているもの(本来ほしい分岐)
    missing = {}
    for lw in links:
        n0 = (directed_nodes(lw) or lw["nodes"])[0]
        if n0 in on_main or n0 in on_link:
            continue
        nt = nodes.get(n0, {}).get("tags", {})
        name = nt.get("name") or nt.get("ref")
        if name:
            missing.setdefault(name, []).append(n0)
    print(f"\n--- 取得もれで落ちた『名前つき』分岐: {len(missing)}種類 ---")
    for name, ids in sorted(missing.items()):
        print(f"  {name}  (node {', '.join(str(i) for i in sorted(set(ids)))})")


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


def esc(s):
    """OSMのタグは外部データなので、HTMLに入れる前にエスケープする"""
    return (str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


PICKER_TMPL = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>看板さがし</title>
<style>
:root{--sign:#0a6b3b;--on:#fff;--ink:#23272a;--sub:#5d666b;--bg:#eceeec;--card:#fff;--line:#d5d9d6;--mark:#e8b10a;
 font-family:"BIZ UDPGothic","Hiragino Sans","Yu Gothic UI",system-ui,sans-serif;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--ink:#e7eae8;--sub:#a3aca8;--bg:#161a19;--card:#1f2423;--line:#333a38}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink)}
header{position:sticky;top:0;z-index:5;background:var(--sign);color:var(--on);padding:12px 16px}
h1{margin:0 0 8px;font-size:18px}
.ctl{display:flex;gap:8px;flex-wrap:wrap}
select,input,button{font:inherit;font-size:14px;padding:7px 9px;border-radius:6px;border:2px solid var(--on);
 background:var(--sign);color:var(--on)}
input{flex:1;min-width:200px}
input::placeholder{color:#cfe3d7}
button{cursor:pointer;background:var(--on);color:var(--sign);font-weight:700;border-color:var(--on)}
.sw{display:flex;align-items:center;gap:6px;font-size:13px;white-space:nowrap}
.sw input{flex:none;min-width:0;width:16px;height:16px}
main{max-width:1100px;margin:0 auto;padding:12px}
.note{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px;
 font-size:13px;line-height:1.7;color:var(--sub)}
.note b{color:var(--ink)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}
.cell{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;cursor:pointer;
 position:relative;padding:0;text-align:left}
.cell.sel{outline:4px solid var(--mark);outline-offset:-4px}
.cell img{width:100%;aspect-ratio:4/3;object-fit:cover;display:block;background:#0003}
.cell .ph{width:100%;aspect-ratio:4/3;display:flex;align-items:center;justify-content:center;
 color:var(--sub);font-size:13px;text-align:center;padding:10px}
.cap{padding:7px 9px;font-size:13px;display:flex;justify-content:space-between;align-items:center;gap:6px}
.cap b{font-size:15px}
.cap a{color:var(--sub);font-size:12px}
.res{background:var(--card);border:2px solid var(--sign);border-radius:10px;padding:12px;margin:12px 0;font-size:14px;line-height:1.8}
.res code{font-size:12px;word-break:break-all}
footer{color:var(--sub);font-size:12px;text-align:center;padding:18px 12px 30px;line-height:1.7}
</style></head><body>
<header>
  <h1>看板さがし</h1>
  <div class="ctl">
    <select id="jct"></select>
    <input id="key" type="password" placeholder="Google Maps APIキー（この端末にのみ保存）">
    <button id="save">読み込む</button>
    <label class="sw"><input type="checkbox" id="back" checked> 20m下がる</label>
    <button id="exp">選択を書き出す</button>
  </div>
</header>
<main>
  <div class="note">
    分岐点の手前 <b>__SPAN__m</b> から分岐点までを <b>__STEP__m</b> ごとに並べています。
    画像を手前（左上）から順に見て、<b>分岐案内の看板が写っている地点のうち、分岐点に最も近いもの</b>を選んでください。
    選ぶと、そこから <b>__BACK__m</b> 手前に下がった地点が撮影ポイントとして確定します。<br>
    APIキーは <b>この端末のブラウザにのみ</b>保存され、送信も保存もされません。入れない場合は画像が出ず、リンクだけになります。
  </div>
  <div id="result"></div>
  <div class="grid" id="grid"></div>
</main>
<footer>道路データ &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>（ODbL）</footer>
<script>
const DATA = __DATA__;
const $ = i => document.getElementById(i);
const LSK = "jct_picker_key", LSP = "jct_picker_picks";
let picks = {};
try{ picks = JSON.parse(localStorage.getItem(LSP) || "{}"); }catch(e){ picks = {}; }
try{ $("key").value = localStorage.getItem(LSK) || ""; }catch(e){}

function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function svLink(v){
  const p = new URLSearchParams({api:1,map_action:"pano",viewpoint:v.lat+","+v.lng,heading:v.heading??0,pitch:v.pitch??0,fov:75});
  return "https://www.google.com/maps/@?"+p;
}
function imgUrl(v,key){
  const p = new URLSearchParams({size:"400x300",location:v.lat+","+v.lng,heading:v.heading??0,
                                pitch:v.pitch??0,fov:75,source:"outdoor",key:key});
  return "https://maps.googleapis.com/maps/api/streetview?"+p;
}

function render(){
  const i = +$("jct").value, r = DATA[i];
  if(!r){ $("grid").innerHTML = ""; return; }
  let key = ""; try{ key = localStorage.getItem(LSK) || ""; }catch(e){}
  const chosen = picks[r.id];
  $("grid").innerHTML = r.candidates.map(c => {
    const sel = chosen === c.distance_m ? " sel" : "";
    const body = key
      ? `<img loading="lazy" src="${esc(imgUrl(c,key))}" alt="手前${c.distance_m}m">`
      : `<div class="ph">APIキーを入れると<br>ここに画像が出ます</div>`;
    const far = c.distance_m === 0 ? "分岐点" : "手前 "+c.distance_m+"m";
    return `<div class="cell${sel}" data-d="${c.distance_m}">${body}
      <div class="cap"><b>${far}</b><a href="${esc(svLink(c))}" target="_blank" rel="noopener"
         onclick="event.stopPropagation()">実際に見る</a></div></div>`;
  }).join("");
  [...$("grid").children].forEach(el => el.onclick = () => {
    picks[r.id] = +el.dataset.d;
    try{ localStorage.setItem(LSP, JSON.stringify(picks)); }catch(e){}
    render();
  });
  showResult(r);
}

function showResult(r){
  const d = picks[r.id];
  if(d === undefined){ $("result").innerHTML = ""; return; }
  const c = r.candidates.find(x => x.distance_m === d);
  const useBack = $("back").checked;
  // 20m下がらない場合は、選んだ地点そのものを撮影ポイントにする
  const p = useBack ? (c && c.pick) : (c && c.heading !== null ? c : null);
  if(!p){ $("result").innerHTML = `<div class="res">手前${d}mは撮影ポイントにできません（本線をさかのぼれないか、分岐点そのものです）。別の地点を選んでください。</div>`; return; }
  const lead = useBack ? `看板は <b>手前${d}m</b>、撮影ポイントは <b>手前${p.distance_m}m</b>`
                       : `撮影ポイントは <b>手前${p.distance_m}m</b>（選んだ地点そのまま）`;
  $("result").innerHTML = `<div class="res">
    <b>${esc(r.junction||"名称なし")}</b> — ${lead}<br>
    <a href="${esc(svLink(p))}" target="_blank" rel="noopener">確定した撮影ポイントを開く</a><br>
    <code>${p.lat}, ${p.lng} / 方位${p.heading}度</code></div>`;
}

$("jct").innerHTML = DATA.map((r,i) =>
  `<option value="${i}">${esc(r.junction||"名称なし")}（${esc(r.mainline)}）</option>`).join("");
$("jct").onchange = render;
$("back").onchange = render;
$("save").onclick = () => {
  try{ localStorage.setItem(LSK, $("key").value.trim()); }catch(e){}
  render();
};
$("exp").onclick = () => {
  const out = DATA.filter(r => picks[r.id] !== undefined).map(r => {
    const c = r.candidates.find(x => x.distance_m === picks[r.id]);
    const v = $("back").checked ? (c&&c.pick) : (c&&c.heading!==null ? c : null);
    return {id:r.id, junction:r.junction, mainline:r.mainline,
            picked_distance_m:picks[r.id], back_off:$("back").checked, view:v||null};
  });
  const b = new Blob([JSON.stringify(out,null,1)], {type:"application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(b); a.download = "picks.json"; a.click();
};
render();
</script></body></html>
"""


def write_picker(results, path):
    """看板の位置を目で確かめて選ぶためのページを書き出す"""
    data = [{"id": r["diverge_node"], "junction": r["junction"], "mainline": r["mainline"],
             "candidates": r["candidates"]} for r in results if r.get("candidates")]
    html = (PICKER_TMPL
            .replace("__DATA__", json.dumps(data, ensure_ascii=False))
            .replace("__SPAN__", str(PICK_SPAN)).replace("__STEP__", str(PICK_STEP))
            .replace("__BACK__", str(PICK_BACK)))
    open(path, "w", encoding="utf-8").write(html)


def write_html(results, path):
    rows = []
    for r in results:
        links = " ".join(f'<a href="{esc(sv_link(v))}" target="_blank" rel="noopener">手前{v["distance_m"]}m'
                         + (f' ({esc(v["pano_date"])})' if v.get("pano_date") else "") + "</a>"
                         for v in sorted(r["views"], key=lambda v: -v["distance_m"]))
        rows.append(f"<tr><td>{esc(r['junction'])}</td><td>{esc(r['mainline'])}</td>"
                    f"<td>{esc(r['destination'].replace(';', ' ・ '))}</td><td>{links}</td></tr>")
    html = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>分岐一覧（{len(rows)}件）</title>"
            "<style>body{font-family:sans-serif;padding:12px}td,th{border-bottom:1px solid #ddd;padding:6px;font-size:14px}"
            "a{display:inline-block;margin-right:8px}</style>"
            f"<p>{len(rows)} 件の分岐</p>"
            "<table><tr><th>分岐</th><th>本線</th><th>行き先</th><th>ストリートビュー</th></tr>"
            + "".join(rows) + "</table>"
            "<p style='font-size:12px;color:#666'>道路データ &copy; "
            "<a href='https://www.openstreetmap.org/copyright'>OpenStreetMap contributors</a>（ODbL）</p>")
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
    ap.add_argument("--diagnose", action="store_true", help="分岐が検出されない原因を調べる")
    ap.add_argument("--picker", action="store_true", help="看板の位置を選ぶ確認用ページも出力する")
    ap.add_argument("--probe", help="指定ノードID(カンマ区切り)が属する道路を調べる")
    ap.add_argument("--probe-ways", dest="probe_ways", help="指定wayID(カンマ区切り)の全タグを表示する")
    ap.add_argument("--jct-only", dest="jct_only", action="store_true",
                    help="高速道路同士の分岐(JCT)だけに絞る")
    ap.add_argument("--keyword", default="", help="--diagnose で注目する分岐名")
    a = ap.parse_args()
    if a.probe:
        probe_nodes([int(x) for x in a.probe.split(",")])
        sys.exit()
    if a.probe_ways:
        probe_ways([int(x) for x in a.probe_ways.split(",")])
        sys.exit()
    if a.test or not (a.bbox or a.from_file):
        self_test()
        sys.exit()
    os.makedirs(a.outdir, exist_ok=True)
    osm = json.load(open(a.from_file, encoding="utf-8")) if a.from_file else \
        overpass_query([float(x) for x in a.bbox.split(",")])
    if a.diagnose:
        diagnose(osm, a.keyword)
        sys.exit()
    res = find_diverges(osm, with_candidates=a.picker, jct_only=a.jct_only)
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
    html = os.path.join(a.outdir, a.region + ".html")
    write_html(res, html)
    if a.picker:
        pick = os.path.join(a.outdir, a.region + "_picker.html")
        write_picker(res, pick)
        print(f"看板さがしのページ: {pick}")
    update_index(a.outdir)
    print(f"{len(res)} 件の分岐を出力しました: {out} / {html}")
