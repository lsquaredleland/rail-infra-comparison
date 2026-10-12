import os as _os
HERE = _os.path.dirname(_os.path.abspath(__file__))
OUTDIR = _os.environ.get('RA_OUT', _os.path.join(HERE, '..', 'site', 'data'))        # where the city JSON files are written
import gc, json, math, collections, os, re
import shapely
import numpy as np
from functools import lru_cache
from shapely.geometry import LineString, Polygon, MultiPolygon, box, Point, mapping
from shapely.ops import linemerge, polygonize, unary_union, transform
from shapely.strtree import STRtree
from pyproj import Transformer

D = {'_meta': {'bbox': {}}}           # filled by new_regions.load: rail, stops, stations, boundaries, coast and roads of every group, all from the planet
TRACK = collections.defaultdict(dict)
RRELS = {}; STOPS = {}; CONS = {}
# Chinese main stations are named after the city, optionally with a direction: 成都东, 北京西, 广州南 ...
MAIN_RE = re.compile(r'^(北京|成都|广州|深圳|佛山|东莞|珠海|中山|江门|惠州|肇庆|香港|天府|上海|杭州|苏州)(东|西|南|北|朝阳|虹桥)?站?$')
FUTURE = {'subway': 'metro', 'light_rail': 'light rail', 'tram': 'tram', 'monorail': 'monorail', 'rail': 'rail', 'narrow_gauge': 'rail', 'funicular': 'funicular'}
import sys; sys.path.insert(0, HERE); import new_regions
os.makedirs(OUTDIR, exist_ok=True)

def ll(g):  # [[lat,lon]] -> [(lon,lat)]
    return [(p[1], p[0]) for p in g]

def bbox_of(s):
    s_, w, n, e = map(float, s.split(','))
    return box(w, s_, e, n)

BB = {k: bbox_of(v) for k, v in new_regions.BBOX.items()}

NEW_IDS = {'seoul','seoul_p','london','paris','paris_p','nyc','nyc_p','shanghai','jiangnan'}
NEWREL = {i for v in new_regions.BND_OF.values() for i in v}

def rel_poly(rel):
    outer = [LineString(ll(w['g'])) for w in rel['ways'] if w['role'] == 'outer' and len(w['g']) > 1]
    inner = [LineString(ll(w['g'])) for w in rel['ways'] if w['role'] == 'inner' and len(w['g']) > 1]
    po = list(polygonize(linemerge(outer))) if outer else []
    pi = list(polygonize(linemerge(inner))) if inner else []
    P = unary_union([p.buffer(0) for p in po])
    if rel['id'] in NEWREL and outer:
        mm = linemerge(outer)
        chains = [l for l in getattr(mm, 'geoms', [mm]) if len(l.coords) >= 3]
        if any(not l.is_ring for l in chains):   # some boundary ways fall outside the extract: close open chains with a chord
            P = unary_union([Polygon(l.coords).buffer(0) for l in chains])
            pi = list(polygonize(linemerge(inner))) if inner else []
            if pi: P = P.difference(unary_union([p.buffer(0) for p in pi]))
            return P
    if P.area < 1e-6 and outer:
        # ring does not close (gap in data): close small gaps with a buffer round-trip
        e = 0.0004
        B = unary_union(outer).buffer(e, 4)
        holes = [Polygon(r) for g in getattr(B, 'geoms', [B]) for r in g.interiors]
        if holes:
            big_h = max(holes, key=lambda h: h.area)
            P = big_h.buffer(e, 4).buffer(0)
        else:  # ends too far apart: close each open chain with a straight chord
            mm = linemerge(outer)
            P = unary_union([Polygon(list(l.coords)).buffer(0) for l in getattr(mm, 'geoms', [mm]) if len(l.coords) >= 3])
    if pi:
        P = P.difference(unary_union([p.buffer(0) for p in pi]))
    return P

def land_from_coast(lines, bb):
    segs = []
    for g in lines:
        if len(g) < 2: continue
        L = LineString(ll(g))
        if L.intersects(bb):
            segs.append(L)
    coast = unary_union(segs).intersection(bb)
    noded = unary_union([coast, bb.boundary])
    faces = list(polygonize(noded))
    # classify faces: land is on the LEFT of coastline direction (OSM convention)
    coast_lines = []
    for g in segs:
        c = g.intersection(bb)
        for part in getattr(c, 'geoms', [c]):
            if part.geom_type == 'LineString' and len(part.coords) > 1:
                coast_lines.append(part)
    tree = STRtree(coast_lines)
    land = []
    for f in faces:
        rp = f.representative_point()
        idx = tree.nearest(rp)
        L = coast_lines[idx]
        d = L.project(rp)
        eps = min(1e-6, L.length / 10)
        a = L.interpolate(max(0, d - eps)); b = L.interpolate(min(L.length, d + eps))
        if a.equals(b):
            continue
        cross = (b.x - a.x) * (rp.y - a.y) - (b.y - a.y) * (rp.x - a.x)
        if cross > 0:
            land.append(f)
    return unary_union(land)

INLAND = {'zh', 'bj', 'cd', 'par'}   # no coastline: the land mask is the boundary itself
_LAND = {}
def land_of(g):
    """land mask of a group from its coastline, built on first use (only for the groups being built)"""
    if g not in _LAND:
        _LAND[g] = None if g in INLAND else land_from_coast(D['coast_' + g], BB[g])
        if _LAND[g] is not None: print(g, 'land area deg2', round(_LAND[g].area, 4), 'bbox', round(BB[g].area, 4))
    return _LAND[g]

import unicodedata
_STOP = {'london', 'station', 'railway', 'international', 'terminal', 'gare', 'de', 'du', 'la', 'le', 'les', 'des', 'd', 'l', 'paris', 'the', '站'}
@lru_cache(maxsize=None)          # called millions of times with the same few thousand names
def norm_st(s):
    s = re.sub(r'\s*[(\uff08\[][^)\uff09\]]*[)\uff09\]]', '', s or '')           # drop a bracketed qualifier: "Futian (Line 2)" -> "Futian"
    s = re.sub(r'(\u7ad9|\u99c5|\uc5ed)$', '', s)                                 # trailing station suffix in Chinese, Japanese, Korean
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower() if re.search(r'[A-Za-z]', s or '') else (s or '').replace('站', '')
    toks = [t for t in re.findall(r"\w+", s) if t not in _STOP]
    return ' '.join(toks)
def _year(s):
    m = re.search(r'\d{4}', s or '')
    return int(m.group()) if m else None
def earliest(dates):
    """The earliest of several OSM dates ('2004-12-28', '1998', ...), as written; '' when none has a year."""
    best = ''
    for d in dates:
        y = _year(d)
        if y is not None and (not best or (y, d) < (_year(best), best)): best = d
    return best

def qa_members(rid, g):
    """Check 1: route members that are in the extract but were dropped by the adapter (not rail, fewer than two nodes, ...) leave a hole in the line.
    Members that are not in the extract at all lie outside the bbox and are ignored."""
    T = TRACK[g]; cons_ids = {c.get('id') for c in CONS.get(g, [])}; ex = D.get('_extract_ways', {}).get(g, {})
    bad = []; total = 0
    for r in RRELS[g]:
        lost = [(w, ex[w]) for w in r.get('ways_all', ()) if w in ex and w not in T and w not in cons_ids]
        lost = [x for x in lost if x[1][0] not in ('platform', 'construction', 'proposed', 'abandoned', 'disused', 'razed', 'dismantled', 'miniature')]
        if lost:
            total += len(lost)
            bad.append(dict(relation=r['id'], name=r['name'], ref=r['ref'], dropped=len(lost), ways=[dict(id=w, railway=v[0], nodes=v[1]) for w, v in lost[:20]]))
    bad.sort(key=lambda b: -b['dropped'])
    return bad, total

def qa_stops(rid, g, line_list, used, stop_pts, stop_li, seen):
    """Check 2 (works for any data source): a stop of a route with no drawn track of any line within 300 m means track is missing there.
    Shenzhen Line 13 had stations 13.5 km from any drawn track when the Overpass pull lacked part of the line. Looking at the track of all lines
    (not just the stop's own line) avoids false alarms from route variants that share track under another name."""
    parts = [p for k_, v_ in used.items() for pin, p in v_
             if next((d['m'] for d in line_list if d['key'] == k_), 8) not in (8, 10)] if len(used) < 3000 else [p for v_ in used.values() for pin, p in v_]
    if not parts: return []
    tree = STRtree(parts)
    names = STRtree([Point(sv[0], sv[1]) for sv in seen]) if seen else None
    bad = {}
    for p_, li in zip(stop_pts, stop_li):
        if li >= len(line_list) or line_list[li]['m'] in (8, 10): continue
        d = parts[int(tree.nearest(p_))].distance(p_)
        if d > 300:
            nm = seen[int(names.nearest(p_))][2] if names is not None else ''
            k_ = (line_list[li]['name'], nm)
            if k_ not in bad or d > bad[k_]['metres']: bad[k_] = dict(line=line_list[li]['name'], station=nm, metres=round(d))
    return sorted(bad.values(), key=lambda b: -b['metres'])

_STREET = {'street', 'st', 'avenue', 'ave', 'road', 'rd', 'boulevard', 'blvd', 'drive', 'dr', 'way', 'lane', 'ln', 'place', 'pl', 'terrace', 'court', 'ct', 'station', 'stop'}
_DIR_END = re.compile(r'\s+(north|south|east|west|northbound|southbound|eastbound|westbound|inbound|outbound|nb|sb|eb|wb)$')
def name_parts(name):
    """'Market Street & Church Street' -> {{market}, {church}}: the cross streets of a stop name, order, street type and trailing direction ignored."""
    parts = re.split(r'\s*(?:&|/|\band\b|\bat\b|@)\s*', (name or '').lower())
    out = []
    for p in parts:
        p = _DIR_END.sub('', p.strip())
        toks = frozenset(t for t in re.findall(r'[\w\u4e00-\u9fff]+', p) if t not in _STREET)
        if toks: out.append(toks)
    return out

def merge_close_stations(rows, line_list, rid):
    """Second dedupe pass, after each station's lines are known. A stop is often mapped once per direction or once per street corner.
    Rules (all need the two stops close together):
      A  same cross streets in any order, or same name apart from street type / direction word   <= 150 m
      B  one stop is named after a single street of the other's 'X & Y' name and they share a line   <= 80 m
      C  two tram, light-rail or cable-car stops with exactly the same lines (opposite platforms)   <= 50 m
      D  two stops that share a line (other than an 'Other ... track' bucket) and are on the same spot (<= 12 m), or <= 35 m when one is named 'X & Y'
    The merged stop keeps the more important row, the plainer name (without '&'), the union of lines and the earliest date.
    Returns the merged rows and a log of what was merged."""
    n = len(rows)
    # lines are compared by name without direction words, so 'Muni Metro N inbound' and '... N outbound' count as the same line
    base = lambda li: re.sub(r'\b(inbound|outbound|northbound|southbound|eastbound|westbound|clockwise|anticlockwise|counterclockwise)\b|\([^)]*\)', '', line_list[li]['name'].lower()).strip()
    real = lambda r: {base(li) for li in r[6] if not line_list[li]['other']}
    parts = [name_parts(r[2]) for r in rows]
    tramlike = [bool(r[6]) and all(line_list[li]['m'] in (2, 3, 5) for li in r[6]) for r in rows]
    par = list(range(n))
    def find(a):
        while par[a] != a: par[a] = par[par[a]]; a = par[a]
        return a
    grid = collections.defaultdict(list)
    for i, r in enumerate(rows): grid[(int(r[0] * UNIT // 200), int(r[1] * UNIT // 200))].append(i)
    why = {}
    for i, r in enumerate(rows):
        cx, cy = int(r[0] * UNIT // 200), int(r[1] * UNIT // 200)
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for j in grid.get((gx, gy), ()):
                    if j <= i: continue
                    q = rows[j]
                    d = math.hypot((r[0] - q[0]) * UNIT, (r[1] - q[1]) * UNIT)
                    if d > 150: continue
                    shared = real(r) & real(q)
                    rule = None
                    if parts[i] and parts[j] and set(parts[i]) == set(parts[j]): rule = 'A'
                    elif d <= 80 and shared and ((len(parts[i]) == 1 and parts[i][0] in parts[j]) or (len(parts[j]) == 1 and parts[j][0] in parts[i])): rule = 'B'
                    elif d <= 50 and tramlike[i] and tramlike[j] and real(r) and real(r) == real(q): rule = 'C'
                    elif shared and (d <= 12 or (d <= 35 and ('&' in r[2] or '&' in q[2]))): rule = 'D'
                    if rule and find(i) != find(j):
                        par[find(j)] = find(i); why[(i, j)] = (rule, round(d))
    groups = collections.defaultdict(list)
    for i in range(n): groups[find(i)].append(i)
    out = []; log = []
    for ids in groups.values():
        if len(ids) == 1: out.append(rows[ids[0]]); continue
        rs = [rows[i] for i in ids]
        rep = max(rs, key=lambda r: (r[7], r[8], len(r[6]), -r[4]))
        plain = [r for r in rs if '&' not in r[2] and ' and ' not in r[2].lower()]
        nm = (plain[0] if plain else rep)
        m = list(rep)
        m[0] = round(sum(r[0] for r in rs) / len(rs)); m[1] = round(sum(r[1] for r in rs) / len(rs))
        m[2], m[3] = nm[2], nm[3]
        m[4] = min(r[4] for r in rs); m[5] = 1 if any(r[5] for r in rs) else 0
        m[6] = sorted({li for r in rs for li in r[6]}); m[7] = max(r[7] for r in rs); m[8] = max(r[8] for r in rs)
        m[9] = earliest([r[9] for r in rs])
        out.append(m)
        log.append(dict(kept=m[2], merged=[r[2] for r in rs], rules=sorted({v[0] for (a, b), v in why.items() if a in ids or b in ids})))
    os.makedirs(os.path.join(HERE, 'qa'), exist_ok=True)
    json.dump(log, open(os.path.join(HERE, 'qa', f'{rid}_merged_stations.json'), 'w'), ensure_ascii=False, indent=1)
    return out, log

def write_qa(rid, g, members, total, stops):
    os.makedirs(os.path.join(HERE, 'qa'), exist_ok=True)
    json.dump(dict(region=rid, group=g, dropped_member_ways=total, relations_with_dropped_ways=members, stops_far_from_track=stops),
              open(os.path.join(HERE, 'qa', f'{rid}.json'), 'w'), ensure_ascii=False, indent=1)
    top = '; '.join(f"{b['line'][:28]}: {b['station']} ({b['metres']} m)" for b in stops[:3])
    print(f"CHECK {rid}: {len(stops)} stops with no drawn track within 300 m" + (f" [{top}]" if stops else '') +
          f"; {total} route member ways dropped from the extract in {len(members)} relations")

MAIN_BY_GROUP = {
    'sel': ['Seoul Station', 'Yongsan', 'Yeongdeungpo', 'Cheongnyangni', 'Suseo', 'Gwangmyeong', 'Suwon', 'Incheon', 'Bucheon', 'Uijeongbu', '서울역', '용산', '영등포', '청량리', '수서', '광명', '수원', '인천'],
    'ldn': ['Paddington', 'Euston', "King's Cross", 'St Pancras', 'Liverpool Street', 'Fenchurch Street', 'London Bridge', 'Cannon Street', 'Waterloo', 'Victoria', 'Charing Cross', 'Blackfriars', 'Marylebone', 'Clapham Junction', 'Stratford',
            'Reading', 'Milton Keynes Central', 'Ebbsfleet', 'Ashford', 'Gatwick Airport', 'Stansted Airport', 'Watford Junction', 'Guildford', 'Slough', 'Chelmsford'],
    'par': ['Gare du Nord', "Gare de l'Est", 'Saint-Lazare', 'Montparnasse', 'Montparnasse 1 - 2', 'Gare de Lyon', 'Gare dAusterlitz', 'Austerlitz', 'Bercy', 'Chatelet - Les Halles', 'La Defense', 'Aeroport Charles de Gaulle 2 TGV', 'Marne-la-Vallee - Chessy',
            'Versailles-Chantiers', 'Massy TGV', 'Juvisy', 'Melun'],
    'nyc': ['Grand Central', 'Grand Central-42nd Street', 'New York Penn', 'Jamaica', 'Atlantic Terminal', 'Hoboken', 'Newark Penn', 'Secaucus Junction', 'New Rochelle', 'White Plains', 'Ronkonkoma', 'Huntington', 'Metropark', 'New Brunswick', 'Stamford'],
    'sh': ['Shanghai Hongqiao', 'Shanghai', 'Shanghai South', 'Shanghai West', 'Pudong Airport', 'Hangzhou East', 'Hangzhou', 'Hangzhou South', 'Suzhou', 'Suzhou North', 'Kunshan South', 'Jiaxing South',
           '上海虹桥', '上海', '上海南', '上海西', '浦东机场', '杭州东', '杭州', '杭州南', '苏州', '苏州北', '昆山南', '嘉兴南'],
}
MAIN_SET = {g: {norm_st(x) for x in v} for g, v in MAIN_BY_GROUP.items()}
MAJOR_MIN = {'nyc': 6, 'nyc_p': 6}   # interchange size that counts as "major"; default 4. New York subway services overlap on shared track, so 4 is far too low
def rels(group):
    return {r['id']: r for r in D['bnd_' + group]}

SZ_CLIP = box(113.6, 22.0, 114.66, 22.9)  # drops the far-off Shenshan cooperation zone
REL_CLIP = {3464353: SZ_CLIP, 568660: box(116.2, 39.8, 116.5, 40.05)}  # Xicheng: drop remote exclaves
GBA = [3287346,3464353,3464829,3464719,3209912,3464319,3464878,3463901,3205802,913110,1867188]
BJ6 = [288600,568660,2988933,2988946,5505984,5505985]
CD5 = [4734760,4734707,3579975,4734743,4734807]
TOKYO_WARDS = [1543055,1543056,1758858,1758878,1758888,1758891,1758897,1758936,1758947,1759474,1759477,1759506,1760038,1760040,1760078,1760119,1760124,1761717,1761718,1761742,1761743,3554015,3554304]
BAY = [111968,396461,396468,396463,396513,396462,396499,396501,396498]

REGIONS = [
    dict(id='sf', name='San Francisco', sub='City and County', group='sf', rels=[111968], clip=box(-122.62, 37.6, -122.3, 37.9),
         anchor=(37.7936, -122.3965), anchor_name='Market St at Embarcadero', streets=3),
    dict(id='bay', name='SF Bay Area', sub='Nine counties', group='sf', rels=BAY, subs=True,
         anchor=(37.7936, -122.3965), anchor_name='Downtown San Francisco', streets=2, big=True),
    dict(id='hk', name='Hong Kong', sub='Special Administrative Region', group='prd', rels=[913110],
         anchor=(22.2820, 114.1588), anchor_name='Central', streets=3),
    dict(id='sz', name='Shenzhen', sub='Prefecture-level city, Shenshan zone excluded', group='prd', rels=[3464353], clip=BB['prd'],
         anchor=(22.5367, 114.0540), anchor_name='Futian CBD', streets=3),
    dict(id='tky23', name='Tokyo', sub='23 special wards', group='tky', rels=TOKYO_WARDS, subs=True,
         anchor=(35.6812, 139.7671), anchor_name='Tokyo Station', streets=3),
    dict(id='tky', name='Tokyo Metropolis', sub='Mainland, islands excluded', group='tky', rels=[1543125], clip=BB['tky'],
         anchor=(35.6812, 139.7671), anchor_name='Tokyo Station', streets=2, big=True),
    dict(id='zh', name='Zurich', sub='City of Zürich', group='zh', rels=[1682248],
         anchor=(47.3779, 8.5403), anchor_name='Hauptbahnhof', streets=3),
    dict(id='gba', name='Greater Bay Area', sub='Nine Guangdong cities plus Hong Kong and Macau', group='gba', rels=GBA, subs=True, clip=BB['gba'],
         anchor=(23.1200, 113.3240), anchor_name='Zhujiang New Town, Guangzhou', streets=1, big=True),
    dict(id='bj6', name='Beijing', sub='Six central districts', group='bj', rels=BJ6, subs=True,
         anchor=(39.9087, 116.3975), anchor_name='Tiananmen', streets=3),
    dict(id='bj', name='Beijing Municipality', sub='All 16 districts, remote exclaves excluded', group='bj', rels=[912940], clip=box(115.3, 39.4, 117.6, 41.1),
         anchor=(39.9087, 116.3975), anchor_name='Tiananmen', streets=2, big=True),
    dict(id='cd5', name='Chengdu', sub='Five central districts', group='cd', rels=CD5, subs=True,
         anchor=(30.6574, 104.0658), anchor_name='Tianfu Square', streets=3),
    dict(id='cd', name='Chengdu Prefecture', sub='Whole prefecture-level city', group='cd', rels=[2110264],
         anchor=(30.6574, 104.0658), anchor_name='Tianfu Square', streets=2, big=True),

    dict(id='seoul', name='Seoul', sub='Seoul Special City', group='sel', rels=[2297418],
         clip=BB['sel'],
         anchor=(37.5663, 126.9779), anchor_name='Seoul City Hall', streets=3),
    dict(id='seoul_p', name='Seoul + Surrounding', sub='Seoul, Incheon and Gyeonggi', group='sel', rels=[2297418, 2297419, 2306392], subs=True,
         clip=BB['sel'],
         anchor=(37.5663, 126.9779), anchor_name='Seoul City Hall', streets=1, big=True),
    dict(id='london', name='London', sub='Greater London', group='ldn', rels=[175342],
         clip=BB['ldn'],
         anchor=(51.5074, -0.1278), anchor_name='Charing Cross', streets=3),
    dict(id='paris', name='Paris', sub='Paris and the three inner departments', group='par', rels=[71525, 7449, 7389, 7458], subs=True,
         clip=BB['par'],
         anchor=(48.8530, 2.3499), anchor_name='Notre-Dame', streets=3),
    dict(id='paris_p', name='Paris + Surrounding', sub='All of Ile-de-France, eight departments', group='par', rels=[71525, 7383, 7457, 7401, 7449, 7389, 7458, 7433], subs=True,
         clip=BB['par'],
         anchor=(48.8530, 2.3499), anchor_name='Notre-Dame', streets=1, big=True),
    dict(id='nyc', name='New York', sub='Five boroughs', group='nyc', rels=[2552485, 369518, 369519, 2552450, 962876], subs=True,
         clip=BB['nyc'],
         anchor=(40.7580, -73.9855), anchor_name='Times Square', streets=3),
    dict(id='nyc_p', name='New York + Surrounding', sub='NYC, Long Island and the NJ/NY suburbs', group='nyc', rels=[2552485, 369518, 369519, 2552450, 962876, 2552442, 404243, 1838027, 1835363, 1838022, 958930, 957239, 957235, 957236, 2560070, 2560069, 961153, 963378, 961390], subs=True,
         clip=BB['nyc'],
         anchor=(40.7580, -73.9855), anchor_name='Times Square', streets=1, big=True),
    dict(id='shanghai', name='Shanghai', sub='Whole municipality', group='sh', rels=[913067], clip=BB['sh'],
         anchor=(31.2304, 121.4737), anchor_name="People's Square", streets=2, big=True),
    dict(id='jiangnan', name='Jiangnan', sub='Shanghai, Hangzhou and Suzhou', group='sh', rels=[913067, 3221112, 4430941], subs=True, clip=BB['sh'],
         anchor=(31.2304, 121.4737), anchor_name="People's Square", streets=1, big=True),
]

MODE_OF_ROUTE = {'subway': 0, 'train': 1, 'light_rail': 2, 'tram': 3, 'monorail': 4, 'funicular': 5}
MODE_OF_RW = {'subway': 0, 'rail': 1, 'narrow_gauge': 1, 'light_rail': 2, 'tram': 3, 'monorail': 4, 'funicular': 5}
PRIO = [6, 0, 2, 4, 1, 3, 5]  # preference when a way is in several routes
KEEP_RW = set(MODE_OF_RW)
DROP_SVC = {'yard', 'siding', 'crossover'}
UNIT = 5.0  # meters per integer unit (reset per region: 10 m for big regions)

def enc_line(coords):
    out = []; px = py = 0
    for x, y in coords:
        ix = int(round(x / UNIT)); iy = int(round(y / UNIT))
        out += [ix - px, iy - py]; px, py = ix, iy
    return out

def near_box(p, d):
    """the square around point p reaching d metres: the same tree candidates as p.buffer(d), without building a circle polygon"""
    return box(p.x - d, p.y - d, p.x + d, p.y + d)

def geom_lines(g):
    if g.is_empty: return []
    if g.geom_type == 'LineString': return [g]
    if g.geom_type in ('MultiLineString', 'GeometryCollection'):
        r = []
        for p in g.geoms: r += geom_lines(p)
        return r
    return []

def polys(g):
    if g.is_empty: return []
    if g.geom_type == 'Polygon': return [g]
    if g.geom_type in ('MultiPolygon', 'GeometryCollection'):
        r = []
        for p in g.geoms: r += polys(p)
        return r
    return []

def enc_poly(P, tol):
    out = []
    for p in polys(P):
        p = p.simplify(tol, preserve_topology=True)
        if p.is_empty or p.area < 2000: continue
        rings = [enc_line(list(p.exterior.coords)[:-1])]
        for r in p.interiors:
            rr = Polygon(r)
            if rr.area < 2000: continue
            rings.append(enc_line(list(r.coords)[:-1]))
        out.append(rings)
    return out

def corridor_km(lines, step=20.0, near=35.0):
    # length of track corridor: sample every `step` m; a sample counts unless it lies within `near` m
    # of track already counted from a different piece (so parallel tracks count once)
    from shapely.geometry import MultiLineString
    if not lines: return 0.0
    merged = linemerge(MultiLineString([l for l in lines if l.length > 0]))
    pieces = list(getattr(merged, 'geoms', [merged]))
    cell = near
    grid = {}
    total = 0.0
    for L in sorted(pieces, key=lambda l: -l.length):
        n = max(1, int(L.length // step))
        seg = L.length / n
        new = []
        # all samples of a piece in one vectorised call (the same points as L.interpolate((i + 0.5) * seg), 1.8 million calls for GBA)
        pts = shapely.get_coordinates(shapely.line_interpolate_point(L, (np.arange(n) + 0.5) * seg)).tolist()
        for px, py in pts:
            cx, cy = int(px // cell), int(py // cell)
            dup = False
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for (qx, qy) in grid.get((cx + dx, cy + dy), ()):
                        if (qx - px) ** 2 + (qy - py) ** 2 < near * near:
                            dup = True; break
                    if dup: break
                if dup: break
            if not dup:
                new.append((cx, cy, px, py)); total += seg
        for cx, cy, x, y in new:
            grid.setdefault((cx, cy), []).append((x, y))
    return total / 1000

# load only the groups of the regions being built (all 12 groups are 480 MB of jsonl; Zurich alone took 24 s mostly loading them)
_ONLY = os.environ.get('ONLY')
NEED = sorted({R['group'] for R in REGIONS if not _ONLY or R['id'] in _ONLY.split(',')})
new_regions.load(D, TRACK, RRELS, STOPS, CONS, groups=NEED)
for _w in filter(None, os.environ.get('RA_TEST_DROP_WAYS', '').split(',')):   # test hook for the CHECK lines: drop these way ids from the track data
    for _g in TRACK: TRACK[_g].pop(int(_w), None)
index = []
ONLY = os.environ.get('ONLY')
for R in REGIONS:
    if ONLY and R['id'] not in ONLY.split(','): continue
    g = R['group']
    RR = rels(g)
    qa_mem, qa_mem_total = qa_members(R['id'], g)
    parts = [RR[i] for i in R['rels']]
    polys_ll = [rel_poly(p) for p in parts]
    polys_ll = [pp.intersection(REL_CLIP[rel['id']]) if rel['id'] in REL_CLIP else pp for rel, pp in zip(parts, polys_ll)]
    legal = unary_union(polys_ll)
    if 'clip' in R:
        legal = legal.intersection(R['clip'])
    land = legal
    LAND_g = land_of(g)
    if LAND_g is not None:
        bbg = BB[g]
        inside = legal.intersection(bbg).intersection(LAND_g)
        outside = legal.difference(bbg)
        land = unary_union([inside, outside])
    land = unary_union([p for p in polys(land) if p.area > 1e-7])

    UNIT = 10.0 if R.get('big', False) else 5.0
    lat0, lon0 = R['anchor']
    fwd = Transformer.from_crs('EPSG:4326', f'+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m', always_xy=True)
    P = lambda geom: transform(fwd.transform, geom)
    from shapely.validation import make_valid
    landm = make_valid(P(land)); legalm = make_valid(P(legal))
    big_ = R.get('big', False)
    if big_ and R['id'] in NEW_IDS:
        landm = unary_union([p for p in polys(landm) if p.area > 3e5]).simplify(10, preserve_topology=True)
    has_water = legalm.area - landm.area > 0.03 * legalm.area
    cen = landm.centroid
    big = R.get('big', False)
    ctx_buf = max(2500, 0.12 * math.sqrt(landm.area))
    landbuf = landm.simplify(30).buffer(ctx_buf, 4) if big else landm.buffer(ctx_buf, 8)
    landbuf_s = landbuf.simplify(50)
    land_prep = landm.buffer(1).simplify(20)
    # These big polygons are tested against every rail segment, road and stop. Preparing them builds GEOS's spatial index once, so
    # intersects/contains are fast; clip() then skips the costly intersection for pieces wholly inside or outside (most of them).
    for g_ in (landm, landbuf_s, land_prep): shapely.prepare(g_)
    landbuf_500 = landbuf_s.buffer(500); shapely.prepare(landbuf_500)
    def clip(g_, poly):
        """the same as geom_lines(g_.intersection(poly)), without the intersection when g_ lies wholly inside or outside poly"""
        if poly.contains(g_): return [g_]
        if not poly.intersects(g_): return []
        return geom_lines(g_.intersection(poly))
    _in_city = {}
    def in_city(part):
        """pieces of a rail segment inside the city limits, computed once per segment"""
        r = _in_city.get(id(part))
        if r is None: r = _in_city[id(part)] = clip(part, landm)
        return r
    tol_b = 40 if big else 12

    out = dict(id=R['id'], group=g, name=R['name'], sub=R['sub'], anchor_name=R['anchor_name'],
               anchor=[lat0, lon0], unit=UNIT,
               centroid=[round(cen.x / UNIT), round(cen.y / UNIT)])
    out['major_min'] = MAJOR_MIN.get(R['id'], 4)
    out['land'] = enc_poly(landm, tol_b)
    out['legal'] = enc_poly(legalm, tol_b) if has_water else []
    if R.get('subs'):
        # internal division lines: boundaries of each part, intersected with land, minus the outer edge
        edges = unary_union([make_valid(P(p)).boundary for p in polys_ll])
        inner_edges = edges.intersection(landm.buffer(-30)).simplify(tol_b)
        out['divs'] = [enc_line(list(l.coords)) for l in geom_lines(inner_edges) if l.length > 50]
        out['div_names'] = []
        for rel, pp in zip(parts, polys_ll):
            pm = make_valid(P(pp)).intersection(landm)
            if pm.is_empty: continue
            rp = pm.representative_point()
            nm = rel['name'].replace(' County', '').replace(' City', '').replace(' District', '')
            out['div_names'].append([round(rp.x / UNIT), round(rp.y / UNIT), nm, round(pm.area / 1e6, 1)])

    # --- rail from track infrastructure (OpenRailwayMap-style), grouped into named lines ---
    def clean(n):
        n = re.split(r'\s*[:：]\s*', n)[0]
        n = re.sub(r'\s*[\(（][^\)）]*[\)）]', '', n)
        n = re.split(r'\s*(?:→|->|=>|⇒|↔|<=>|⟷)\s*', n)[0]
        return n.strip()
    def speed(ms):
        m_ = re.match(r'\s*(\d+)', ms or '')
        v = int(m_.group(1)) if m_ else 0
        if 'mph' in (ms or ''): v = v * 1.609
        return v
    rels_g = RRELS[g]
    SD = STOPS[g]
    _rs = {r['id']: r['stops'] for r in SD['rels']}
    _cm = {}
    def commuter(rid):
        # a route that stops every few km is a commuter / metro-style service, even if it shares track tagged highspeed=yes
        if rid in _cm: return _cm[rid]
        pts = [SD['nodes'].get(str(n)) for n in _rs.get(rid, [])]
        pts = [p for p in pts if p]
        v = False
        if len(pts) >= 4:
            tot = sum(math.hypot((a[0] - b_[0]) * 111.0, (a[1] - b_[1]) * 111.0 * math.cos(math.radians(a[0]))) for a, b_ in zip(pts, pts[1:]))
            v = tot / (len(pts) - 1) < 6.0
        _cm[rid] = v
        return v
    way_routes = collections.defaultdict(list)
    for r in rels_g:
        for wid in r['ways']: way_routes[wid].append(r)
    LINES = {}
    def get_line(key, m, name, colour):
        L = LINES.get(key)
        if L is None:
            L = LINES[key] = dict(m=m, names=collections.Counter(), colour='', hs=0, n=0)
        if name: L['names'][name] += 1
        if not L['colour'] and colour: L['colour'] = colour
        return key
    OTHER = {0: 'Other metro track', 1: 'Other regional rail track', 2: 'Other light rail track', 3: 'Other tram track', 4: 'Other monorail track', 5: 'Other funicular track', 6: 'Other high-speed track', 7: 'Other main-line track', 8: 'Other track under construction', 9: 'Other freight track', 10: 'Other high-speed track under construction'}
    segs = []
    for wid, w in TRACK[g].items():
        if len(w['g']) < 2: continue
        routes = [r for r in way_routes.get(wid, []) if r['route'] in MODE_OF_ROUTE]
        # freight-only track (no passenger route) is kept as its own type; it never counts toward the rail network totals
        freight = w['tm'] == 'freight' and not routes and w['rw'] in ('rail', 'narrow_gauge')
        if w['svc'] in ('yard', 'siding', 'crossover'): continue
        if w['u'] in ('military', 'test'): continue
        if not freight:
            if w['svc'] == 'spur' and not routes: continue
            if w['u'] == 'industrial': continue
            if w['tm'] == 'freight' and not routes: continue
        is_hs = (not freight) and w['rw'] == 'rail' and (w['hs'] == 'yes' or speed(w['ms']) >= 200)
        keys = set()
        for r in routes:
            m = MODE_OF_ROUTE[r['route']]
            if m == 1 and (r['hs'] == 'yes' or (is_hs and not commuter(r['id']))): m = 6
            ref = r['ref'].strip(); nm = clean(r['name'])
            key = (m, r['network'], ref) if ref else (m, nm)
            keys.add(get_line(key, m, nm or ref, r['colour']))
            LINES[key].setdefault('rd', set()).add(r.get('sd', ''))
            if r['route'] == 'train' and r['hs'] != 'yes' and commuter(r['id']): LINES[key]['comm'] = True
        if not keys:
            if freight: m = 9
            elif is_hs: m = 6
            elif w['rw'] in ('rail', 'narrow_gauge'): m = 7
            else: m = MODE_OF_RW[w['rw']]
            if m in (6, 7) and w['svc'] == '' and w['u'] == '' and not w['n'] and m == 7:
                continue  # unnamed, untagged track with no route: most likely industrial
            nm = clean(w['n']) if w['n'] else ''
            key = (m, 'name', nm) if nm and m in (1, 6, 7, 9) else (m, '__other')
            keys.add(get_line(key, m, nm if key[1] == 'name' else OTHER[m], ''))
        wspd = max([int(v) for v in re.findall(r'\d+', w['ms'] or '')] or [0]) * (1.609 if 'mph' in (w['ms'] or '') else 1)
        for k in keys:
            LINES[k]['n'] += 1
            if is_hs: LINES[k]['hs'] += 1
            if w.get('sd'): LINES[k].setdefault('wd', set()).add(w['sd'])
            if 0 < wspd <= 400: LINES[k]['ms'] = max(LINES[k].get('ms', 0), round(wspd))   # fastest tagged way of the line, km/h
        if is_hs and not (routes and all(r['hs'] != 'yes' and commuter(r['id']) for r in routes)): im = 6
        elif freight: im = 9
        elif w['rw'] in ('rail', 'narrow_gauge'): im = 1 if routes else 7
        else: im = MODE_OF_RW[w['rw']]
        L = P(LineString(ll(w['g'])))
        for part in clip(L, landbuf_s):
            if part.length < 5: continue
            segs.append((keys, land_prep.intersects(part), part, im))
    # track under construction: its own type (8), grouped by name, never counted in the network totals
    for w in CONS.get(g, []):
        if w['t'] != 'w' or len(w['g']) < 2: continue
        if w['svc'] in ('yard', 'siding', 'crossover', 'spur') or w['u'] in ('industrial', 'military'): continue
        if w['c'] and w['c'] not in FUTURE: continue
        nm = clean(w['n']) if w['n'] else ''
        fut = FUTURE.get(w['c'], '')
        hsr_c = w['c'] == 'rail' and (w['hs'] == 'yes' or speed(w['ms']) >= 200)
        if hsr_c: fut = 'high-speed rail'
        cm = 10 if hsr_c else 8     # high-speed construction is kept apart from other rail under construction
        label = f"{nm} ({fut})" if nm and fut else (nm or (f"Other {fut} under construction" if fut else OTHER[cm]))
        key = (cm, 'cons', label if nm else (fut or ''))
        keys = {get_line(key, cm, label, '')}
        if w.get('od'): LINES[key].setdefault('od', set()).add(w['od'])
        if not nm: LINES[key]['other'] = True
        L = P(LineString(ll(w['g'])))
        for part in clip(L, landbuf_s):
            if part.length < 5: continue
            segs.append((keys, land_prep.intersects(part), part, cm))
    # keep only lines that serve the city: >= 200 m of track inside the limits. Track of other lines (e.g. a neighbouring
    # city's metro that merely lies in the context buffer) is dropped; lines that do enter keep their track beyond the limits.
    in_len = collections.Counter()
    for keys, pin, part, im in segs:
        if pin:
            l_in = sum(q.length for q in in_city(part))
            for k in keys: in_len[k] += l_in
    serves = {k for k, v in in_len.items() if v >= 200}
    n_before = len({k for keys, *_ in segs for k in keys})
    # pooled 'Other ... track' buckets mix unrouted track from everywhere, so judge them per segment: only track touching the city stays
    is_other = lambda k: k[1] == '__other' or LINES[k].get('other', False)
    segs = [(kk, pin, part, im) for keys, pin, part, im in segs
            for kk in [{k for k in keys & serves if pin or not is_other(k)}] if kk]
    print(R['id'], 'lines kept', len(serves), 'of', n_before, '(dropped lines that never enter the limits)')
    used = {}
    for keys, pin, part, im in segs:
        for k in keys: used.setdefault(k, []).append((pin, part))
    line_list = []
    for k, parts in used.items():
        Ld = LINES[k]
        m = Ld['m']
        if m == 1 and Ld['n'] and Ld['hs'] / Ld['n'] > 0.5 and not Ld.get('comm'): m = 6
        name = Ld['names'].most_common(1)[0][0] if Ld['names'] else OTHER[m]
        ref = k[2] if len(k) == 3 and k[1] != 'name' else ''
        if ref and ref not in name and len(ref) <= 4: name = f"{name} ({ref})"
        inside_parts = [q for pin, p in parts if pin for q in in_city(p)]
        km = corridor_km(inside_parts) if inside_parts else 0.0
        bt = bs = ''
        if k[1] != '__other' and not Ld.get('other', False) or m in (8, 10):
            if m in (8, 10): bt = earliest(Ld.get('od', ())); bs = 'planned' if bt else ''
            else:
                bt = earliest(Ld.get('rd', ())); bs = 'route' if bt else ''
                if not bt: bt = earliest(Ld.get('wd', ())); bs = 'track' if bt else ''
        line_list.append(dict(key=k, m=m, name=name, colour=Ld['colour'], km=round(km, 1), built=bt, bsrc=bs, ms=0 if k[1] == '__other' else Ld.get('ms', 0), other=k[1] == '__other' or Ld.get('other', False)))
    ORDER = [6, 0, 2, 4, 1, 3, 5, 7, 9, 10, 8]
    line_list.sort(key=lambda d: (ORDER.index(d['m']), d['other'], -d['km'], d['name']))
    lidx = {d['key']: i for i, d in enumerate(line_list)}
    out['rail'] = [[sorted(lidx[k] for k in keys), 1 if pin else 0, enc_line(list(part.simplify(15 if big else 6).coords))] for keys, pin, part, im in segs]
    out['lines'] = [[d['m'], d['name'], d['colour'], d['km'], d['ms'], d['built'], d['bsrc']] for d in line_list]   # ms: max tagged speed in km/h, 0 if unknown; built: start_date of the route (else of its earliest track, 'track'; planned opening for lines under construction)
    rail_out = out['rail']
    GROUP_OF = {0: 'metro', 2: 'metro', 4: 'metro', 5: 'metro', 1: 'rail', 6: 'hsr', 3: 'tram', 7: 'other', 8: 'cons', 9: 'freight', 10: 'hsrcons'}
    bucket = collections.defaultdict(list); all_in = []
    for keys, pin, part, im in segs:
        if not pin: continue
        ps = in_city(part)
        bucket[GROUP_OF[im]] += ps
        if im not in (8, 9, 10): all_in += ps
    net = {gn: round(corridor_km(bucket[gn]), 1) if bucket[gn] else 0.0 for gn in ('metro', 'rail', 'hsr', 'tram', 'other', 'cons', 'hsrcons', 'freight')}
    net['total'] = round(corridor_km(all_in), 1) if all_in else 0.0
    lines_in = [d for d in line_list if d['km'] > 0.2 and not d['other'] and d['m'] not in (7, 8, 9, 10)]
    cons_lines = sum(1 for d in line_list if d['m'] == 8 and d['km'] > 0.2 and not d['other'])
    hsrcons_lines = sum(1 for d in line_list if d['m'] == 10 and d['km'] > 0.2 and not d['other'])
    line_counts = collections.Counter(GROUP_OF[d['m']] for d in lines_in)

    # --- stations: which lines serve each one, and which are high-speed stops ---
    KIND_MODES = {0: {0, 2, 4, 5}, 1: {1, 6, 7}, 2: {2, 3, 4, 5}}
    seg_geoms = [part for keys, pin, part, im in segs]
    seg_lines = [[lidx[k] for k in keys] for keys, pin, part, im in segs]
    seg_tree = STRtree(seg_geoms) if seg_geoms else None
    hs_geoms = [part for keys, pin, part, im in segs if im == 6 or any(line_list[lidx[k]]['m'] == 6 for k in keys)]
    hs_tree = STRtree(hs_geoms) if hs_geoms else None
    def near_hs(pt, d):
        if hs_tree is None: return False
        idx = hs_tree.query(near_box(pt, d))
        return any(hs_geoms[i].distance(pt) <= d for i in idx)
    # high-speed stop points: stops of HSR routes; where HSR track has no mapped route, platforms / stop positions on that track
    is_hs_way = {wid: (w['rw'] == 'rail' and (w['hs'] == 'yes' or speed(w['ms']) >= 200)) for wid, w in TRACK[g].items()}
    hs_pts = []; covered = set(); term_pts = []
    for r in SD['rels']:
        if r['route'] != 'train': continue
        hw = [is_hs_way.get(wid) for wid in r['ways'] if wid in is_hs_way]
        if r['hs'] != 'yes' and commuter(r['id']):
            covered.update(r['ways']); continue      # commuter service on high-speed-tagged track: not a high-speed stop
        if not (r['hs'] == 'yes' or (hw and sum(1 for v in hw if v) / len(hw) > 0.5)): continue
        covered.update(r['ways'])
        for nid in (r['stops'][:1] + r['stops'][-1:]):   # first and last stop: termini
            c = SD['nodes'].get(str(nid))
            if c: term_pts.append(Point(*fwd.transform(c[1], c[0])))
        for nid in r['stops']:
            c = SD['nodes'].get(str(nid))
            if c: hs_pts.append(Point(*fwd.transform(c[1], c[0])))
    unc = []
    for wid, w in TRACK[g].items():
        if is_hs_way[wid] and wid not in covered and len(w['g']) > 1:
            L_ = P(LineString(ll(w['g'])))
            if landbuf_s.intersects(L_): unc.append(L_)
    unc_tree = STRtree(unc) if unc else None
    def near_unc(pt, d):
        return unc_tree is not None and any(unc[i].distance(pt) <= d for i in unc_tree.query(near_box(pt, d)))
    for pl in SD['plats']:
        pt = Point(*fwd.transform(pl['p'][1], pl['p'][0]))
        if not landbuf_s.contains(pt): continue
        if (pl['t'] == 'w' and near_unc(pt, 6)) or (pl['t'] == 'n' and near_unc(pt, 2)):
            hs_pts.append(pt)

    # which lines stop where: map each route relation to its line(s), then index its stop nodes
    def keys_for_rel(r):
        m = MODE_OF_ROUTE.get(r['route'])
        if m is None: return []
        ref = r['ref'].strip(); nm = clean(r['name'])
        out_ = []
        for mm in ((m, 6) if m == 1 else (m,)):
            k = (mm, r['network'], ref) if ref else (mm, nm)
            if k in lidx: out_.append(lidx[k])
        return out_
    rel_lines = {r['id']: keys_for_rel(r) for r in RRELS[g]}
    stop_pts = []; stop_li = []; stop_nm = []; has_stops = set()
    for r in SD['rels']:
        lis = rel_lines.get(r['id'], [])
        if not lis or not r['stops']: continue
        for nid in r['stops']:
            c = SD['nodes'].get(str(nid))
            if not c: continue
            p_ = Point(*fwd.transform(c[1], c[0]))
            if not landbuf_500.contains(p_): continue
            for li in lis:
                stop_pts.append(p_); stop_li.append(li); has_stops.add(li); stop_nm.append(tuple(c[2:4]))
    stop_tree = STRtree(stop_pts) if stop_pts else None

    st_out = []; seen = []; n_in = 0; n_hs = 0
    for s_ in D['st_' + g]:
        kind = None
        st, sub, tr, lr, tm = s_['st'], s_['sub'], s_['tr'], s_['lr'], s_['tm']
        if st == 'subway' or sub == 'yes': kind = 0
        elif st in ('light_rail', 'monorail') or lr == 'yes' or st == 'tram' or tm == 'yes': kind = 2
        elif st in ('train', '') and (s_['rw'] in ('station', 'halt') or tr == 'yes'): kind = 1
        if st == 'funicular': kind = 2
        if kind is None: continue
        if s_['rw'] not in ('station', 'halt') and not (sub or tr or lr or tm or st): continue
        kinds = {kind}
        if tr == 'yes': kinds.add(1)
        if sub == 'yes': kinds.add(0)
        if lr == 'yes' or tm == 'yes': kinds.add(2)
        x, y = fwd.transform(s_['p'][1], s_['p'][0])
        pt = Point(x, y)
        if not landbuf_s.contains(pt): continue
        en, nl = s_['n'], s_['nl']
        nm = en or nl
        if not nm: continue
        dup = False
        for sv in seen:
            if (sv[2] == nm or (nl and sv[3] == nl) or (norm_st(nm) and norm_st(sv[2]) == norm_st(nm))) and abs(sv[0] - x) < 600 and abs(sv[1] - y) < 600:
                sv[4] = min(sv[4], kind); sv[6] |= kinds; sv[7].append((x, y)); sv[8] = earliest([sv[8], s_.get('sd', '')]); dup = True; break
        if dup: continue
        inside = land_prep.contains(pt)
        seen.append([x, y, nm, nl, kind, inside, kinds, [(x, y)], s_.get('sd', '') if earliest([s_.get('sd', '')]) else ''])
    write_qa(R['id'], g, qa_mem, qa_mem_total, qa_stops(R['id'], g, line_list, used, stop_pts, stop_li, seen))
    hs_idx = set()
    rail_st = [(i, Point(sv[0], sv[1])) for i, sv in enumerate(seen) if 1 in sv[6]]
    if rail_st and hs_pts:
        rs_tree = STRtree([p for _, p in rail_st])
        for hp in hs_pts:
            j = rs_tree.nearest(hp)
            if rail_st[j][1].distance(hp) <= 400: hs_idx.add(rail_st[j][0])
    # where HSR routes are thinly mapped (much of China), a big rail station next to HSR track counts too
    hs_len = sum(g_.length for g_ in hs_geoms) or 1
    unc_len = sum(g_.intersection(landbuf_s).length for g_ in unc)
    if unc_len / hs_len > 0.4 and rail_st:
        plat_pts = [Point(*fwd.transform(pl['p'][1], pl['p'][0])) for pl in SD['plats'] if pl['t'] == 'w' and pl['rw'] == 'platform']
        pl_tree = STRtree(plat_pts) if plat_pts else None
        cand = []
        for i, p in rail_st:
            if i in hs_idx or pl_tree is None: continue
            npl = sum(1 for k in pl_tree.query(near_box(p, 350)) if plat_pts[k].distance(p) <= 350)
            if near_hs(p, 120) or (npl >= 6 and near_hs(p, 1500)) or (npl >= 10 and near_hs(p, 6000)): cand.append((-npl, i, p))
        for _, i, p in sorted(cand):
            if any(Point(seen[j][0], seen[j][1]).distance(p) < 1000 for j in hs_idx): continue
            hs_idx.add(i)
    # major HSR stations (shown when zoomed out): termini of HSR routes, big stations by platform count,
    # or where two or more high-speed corridors meet (counted below)
    all_plat = [Point(*fwd.transform(pl['p'][1], pl['p'][0])) for pl in SD['plats'] if pl['t'] == 'w' and pl['rw'] == 'platform']
    ap_tree = STRtree(all_plat) if all_plat else None
    hs_major = set()
    for i in hs_idx:
        p = Point(seen[i][0], seen[i][1])
        npl = sum(1 for k in ap_tree.query(near_box(p, 400)) if all_plat[k].distance(p) <= 400) if ap_tree is not None else 0
        term = any(tp.distance(p) <= 700 for tp in term_pts)
        named_main = bool(MAIN_RE.match(seen[i][3] or ''))
        if named_main or npl >= 8 or (term and npl >= 4): hs_major.add(i)
    # Route stops -> stations. A stop is owned by the nearest station within reach that can serve its kind of line, and is shared with the
    # other stations within reach only if they serve that kind of line themselves (their own stops), have no stop data at all, or are the
    # same place (one name contains the other, or within 40 m: Civic Center BART and Muni, Zurich Central and the Polybahn). Sharing
    # keeps complexes such as Times Square (one station point per line) as interchanges; the mode test stops a cable car stop on Powell
    # Street from inheriting the Muni T (light rail) from its Chinatown and Union Square stops 150 m away.
    def stop_radius(si, m):
        return {0: 200, 1: 330, 2: 160, 3: 110, 4: 200, 5: 150, 6: 700 if si in hs_idx else 330, 7: 330}[m]
    stop_lines_of = collections.defaultdict(set)
    if stop_pts and seen:
        st_pts = [(si, Point(mx, my)) for si, sv in enumerate(seen) for (mx, my) in sv[7]]
        st_tree_ = STRtree([q for _, q in st_pts])
        st_allowed_ = [set().union(*(KIND_MODES[k] for k in sv[6])) for sv in seen]
        def toks(si):
            return {t for nm_ in (seen[si][2], seen[si][3]) for t in (norm_st(nm_ or '') or '').split()}
        def name_toks(names):
            return {t for nm_ in names for t in (norm_st(nm_ or '') or '').split()}
        cands = []; owned_modes = collections.defaultdict(set)
        for p_, li, snm in zip(stop_pts, stop_li, stop_nm):
            m = line_list[li]['m']
            best = {}                                       # station -> distance (a station may have several points)
            for j in st_tree_.query(near_box(p_, 700)):
                si, q = st_pts[j]
                d = q.distance(p_)
                if d <= stop_radius(si, m) and d < best.get(si, 1e18): best[si] = d
            if not best: continue
            ok = {si: d for si, d in best.items() if m in st_allowed_[si]} or best
            nt = name_toks(snm)                              # prefer the station named like the stop ('Civic Center'), else the nearest
            named = {si: d for si, d in ok.items() if nt and toks(si) and (toks(si) <= nt or nt <= toks(si))}
            owner = min(named or ok, key=lambda si: ((named or ok)[si], si))
            owned_modes[owner].add(m); cands.append((li, m, best, owner))
        def same_place(si, oi):    # one complex: one name contains the other ('Civic Center', 'Central' + 'Central Polybahn'), or on the same spot
            a_, b_ = toks(si), toks(oi)
            if a_ and b_ and (a_ <= b_ or b_ <= a_): return True
            return math.hypot(seen[si][0] - seen[oi][0], seen[si][1] - seen[oi][1]) <= 40
        for li, m, best, owner in cands:
            for si in best:
                if si == owner or m in (owned_modes.get(si) or st_allowed_[si]) or same_place(si, owner): stop_lines_of[si].add(li)
    for si, (x, y, nm, nl, kind, inside, kinds, mpts, sdate) in enumerate(seen):
      allowed = set().union(*(KIND_MODES[k] for k in kinds))
      hs = si in hs_idx
      near = {}; seg_hits = []; stopped = set()
      SR = {0: 200, 1: 330, 2: 160, 3: 110, 4: 200, 5: 150, 6: 700 if hs else 330, 7: 330}
      for (mx, my) in mpts:
        pt = Point(mx, my)
        if seg_tree is not None:
            rad = 250 if 1 in kinds else 160
            if hs: rad = 600
            for i in seg_tree.query(near_box(pt, rad)):
                d = seg_geoms[i].distance(pt)
                if d > rad: continue
                for li in seg_lines[i]:
                    m = line_list[li]['m']
                    lim = rad if (m == 6 and hs) or 1 in kinds and m in (1, 6, 7) else 160
                    if d <= lim:
                        seg_hits.append((i, li))
                        if d < near.get(li, 1e9): near[li] = d
      # lines that actually stop here (route stops owned by this station), plus nearby lines that have no stop data at all
      stopped = set(stop_lines_of.get(si, ()))
      if True:
        st_allowed = {li for li in stopped if line_list[li]['m'] in allowed}
        if st_allowed: stopped = st_allowed
        # lines without stop data count by proximity; for street-level modes (light rail, tram, monorail, funicular) only when the station is
        # on the track (60 m), otherwise a parallel street's line is picked up (Powell & Geary got the Muni T, which runs under Stockton)
        prox = [li for li in near if line_list[li]['m'] in allowed and li not in has_stops and (line_list[li]['m'] not in (2, 3, 4, 5) or near[li] <= 60)]
        lines = sorted(stopped | set(prox))
        named = [li for li in lines if not line_list[li]['other']]
        if named: lines = named        # the 'Other ... track' buckets only when nothing else serves the station
        if not hs: lines = [li for li in lines if line_list[li]['m'] != 6]
        if not lines:  # data gap: fall back to the nearest lines of any type
            lines = sorted([li for li in near if line_list[li]['m'] not in (8, 9, 10) and (line_list[li]['m'] != 6 or hs)], key=lambda li: (near[li], li))[:3]   # ties broken by line index, so the result does not depend on dict order
        # physical lines: route variants that share the same track near the station count once
        real = {li for li in lines if not line_list[li]['other'] and line_list[li]['m'] != 7}
        par = {li: li for li in real}
        def find(a):
            while par[a] != a:
                par[a] = par[par[a]]; a = par[a]
            return a
        by_seg = collections.defaultdict(list)
        for i, li in seg_hits:
            if li in real: by_seg[i].append(li)
        for ls in by_seg.values():
            for li in ls[1:]: par[find(li)] = find(ls[0])
        nphys = len({find(li) for li in real})
        n_hs_lines = len({find(li) for li in real if line_list[li]['m'] == 6})
        tier = (3 if si in hs_major else 2) if hs else 0
        if not tier and inside and (norm_st(nm) in MAIN_SET.get(g, ()) or (nl and norm_st(nl) in MAIN_SET.get(g, ()))): tier = 4   # main station of the region
        if not inside and not lines: continue   # outside the limits and on none of the kept lines
        n_in += inside; n_hs += inside and hs
        loc = nl if (nl and nl != nm) else ''
        st_out.append([round(x / UNIT), round(y / UNIT), nm, loc, kind, 1 if inside else 0, sorted(lines), tier, nphys, sdate])   # sdate: start_date of the station as written in OSM, '' if none
    n_before = len(st_out)
    st_out, merge_log = merge_close_stations(st_out, line_list, R['id'])
    n_in = sum(r_[5] for r_ in st_out); n_hs = sum(1 for r_ in st_out if r_[5] and r_[7] in (2, 3))
    print(f"MERGE {R['id']}: {n_before} -> {len(st_out)} stations ({len(merge_log)} groups merged)")
    n_ic = sum(1 for r_ in st_out if r_[5] and r_[8] >= 2)
    out['stations'] = st_out

    # --- roads ---
    roads = D.get('road_' + g, []) + D.get('road_' + g + '3', [])
    maxcls = R['streets']
    if big and maxcls >= 2: maxcls -= 1   # 'plus surrounding' regions: drop the lowest street class
    road_out = [[] for _ in range(4)]
    rtol = 50 if big else 12
    minlen = (400, 400, 400, 400) if big else (120, 120, 80, 60)   # metres: pieces shorter than this (after merging) are dropped
    clipped = [[] for _ in range(4)]
    for cls, gg in roads:
        if cls > maxcls or len(gg) < 2: continue
        L = P(LineString(ll(gg)))
        clipped[cls] += clip(L, landbuf_s)
    for cls in range(4):
        if not clipped[cls]: continue
        for part in geom_lines(linemerge(clipped[cls])):   # OSM cuts a road at every junction; joined into long lines there are far fewer polylines
            if part.length < minlen[cls]: continue
            road_out[cls].append(enc_line(list(part.simplify(rtol).coords)))
    out['roads'] = road_out

    # --- stats ---
    ext = landm.bounds
    out['stats'] = dict(land_km2=round(landm.area / 1e6, 1), legal_km2=round(legalm.area / 1e6, 1),
                        lines=len(lines_in), stations=n_in, hsr_stations=n_hs, interchanges=n_ic, cons_lines=cons_lines, hsrcons_lines=hsrcons_lines, net_km=net, line_counts=dict(line_counts),
                        width_km=round((ext[2] - ext[0]) / 1000, 1), height_km=round((ext[3] - ext[1]) / 1000, 1))
    out['extent'] = [round(v / UNIT) for v in landbuf.bounds]
    # roads go to <id>.roads.json, which the site loads only when the Streets layer is switched on
    open(f"{OUTDIR}/{R['id']}.roads.json", 'w').write(json.dumps(dict(unit=UNIT, roads=out['roads']), separators=(',', ':')))
    out['roads'] = [[] for _ in range(4)]; out['roads_file'] = f"{R['id']}.roads.json"
    js = json.dumps(out, separators=(',', ':'), ensure_ascii=False)
    open(f"{OUTDIR}/{R['id']}.json", 'w').write(js)
    print(R['id'], out['stats'], 'rail', len(rail_out), 'st', len(st_out), 'roads', [len(r) for r in road_out], 'size', len(js) // 1024, 'KB', 'water', has_water)
    index.append(dict(id=R['id'], name=R['name'], sub=R['sub'], stats=out['stats']))

import fcntl
with open(f'{OUTDIR}/.index.lock', 'w') as _lk:   # parallel builds (run_all.py) merge into index.json one at a time
    fcntl.flock(_lk, fcntl.LOCK_EX)
    old = json.load(open(f'{OUTDIR}/index.json')) if os.path.exists(f'{OUTDIR}/index.json') else []
    new_ids = {e['id'] for e in index}
    index = [e for e in old if e['id'] not in new_ids] + index
    json.dump(index, open(f'{OUTDIR}/index.json', 'w'), ensure_ascii=False)
