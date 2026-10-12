"""Station model (see "rail-atlas spec/docs/04-station-model.md") for one built region (Hong Kong and Shenzhen are published).

    cd pipeline && python3 stations.py hk

Input:  run2/rail_<group>.jsonl (written by ra-extract; ways carry their node ids) and site/data/<region>.json (anchor, group, boundary)
Output: site/data/stations_<region>.json
        {"stations": [summary per station], "complexes": [...], "detail": {station id: platforms, footprint, tracks, entrances, lines}}
Coordinates are integer metres in the region's azimuthal-equidistant projection (same anchor as build.py), so the site
can match them to the drawn stations by position.

Levels: Complex (the place a traveller names) > Station (one per mode) > Stop points (platforms, stop positions).
Rank is physical size only (mainline platforms, mainline tracks, footprint area, terminus); line counts never rank.
"""
import collections, json, math, os, re, sys
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon, MultiPolygon, mapping
from shapely.ops import polygonize, unary_union, transform
from shapely.strtree import STRtree
from functools import lru_cache
import shapely

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')

# Per-region overrides (any key of region_cfg); the anchor and group come from site/data/<id>.json, so a region needs none.
REGION = {}
RUN2 = os.environ.get('RA_NEW', os.path.join(ROOT, 'run2'))
SITE_DATA = os.environ.get('RA_OUT', os.path.join(ROOT, 'site', 'data'))   # same override as build.py

RAIL_MODES = ('metro', 'light_rail', 'tram', 'mainline', 'monorail', 'funicular')
MODE_TAG = {'subway': 'metro', 'light_rail': 'light_rail', 'tram': 'tram', 'train': 'mainline', 'monorail': 'monorail', 'funicular': 'funicular'}
ROUTE_MODE = {'subway': 'metro', 'light_rail': 'light_rail', 'tram': 'tram', 'train': 'mainline', 'railway': 'mainline', 'monorail': 'monorail', 'funicular': 'funicular'}
TRACK_RW = {'rail', 'narrow_gauge', 'subway', 'light_rail', 'tram', 'monorail', 'funicular'}
MAINLINE_RW = {'rail', 'narrow_gauge'}


def region_cfg(region):
    cfg = dict(r_main=300, r_light=100, out=f'stations_{region}.json')
    p = os.path.join(SITE_DATA, f'{region}.json')
    if os.path.exists(p):
        d = json.load(open(p))
        cfg.update(anchor=tuple(d['anchor']), group=d.get('group'))
    cfg.update(REGION.get(region, {}))
    return cfg


def load_jsonl(path):
    """rail_<group>.jsonl from ra-extract: nodes, ways with geometry and node ids ('n'), relations"""
    nodes, ways, rels = {}, {}, {}
    for line in open(path, encoding='utf-8'):
        d = json.loads(line)
        t = d['t']
        if t == 'n':
            nodes[d['id']] = (d['p'][1], d['p'][0], d['k'])
        elif t == 'w':
            ids = d['n']
            for nid, (lat, lon) in zip(ids, d['g']): nodes.setdefault(nid, (lon, lat, {}))
            ways[d['id']] = (ids, d['k'])
        else:
            rels[d['id']] = ([(m[0], m[1], m[2]) for m in d['m']], d['k'])
    return nodes, ways, rels


def load_input(cfg):
    if not cfg.get('group'): sys.exit('no group for this region: build it first (site/data/<id>.json)')
    return load_jsonl(os.path.join(RUN2, f"rail_{cfg['group']}.jsonl"))


@lru_cache(maxsize=None)
def norm(s):
    s = re.sub(r'\s*[(\uff08\[][^)\uff09\]]*[)\uff09\]]', '', s or '')      # "Futian (Line 2)" and "Futian" are one station
    s = re.sub(r'(\u7ad9|\u99c5|\uc5ed)$', '', s).lower()
    s = re.sub(r'\b(station|railway station|stop|terminal|interchange)\b', '', s)
    s = re.sub(r'[^\w一-鿿]+', '', s)
    return s


def track_index(tracks):
    """tracks_near(geom): the tracks (way id, geometry, railway value) intersecting geom, in the dict's order, found with an R-tree
    instead of testing every track against every station (40 million tests for the Greater Bay Area)"""
    ids = list(tracks)
    tree = STRtree([tracks[w][0] for w in ids]) if ids else None
    def tracks_near(geom):
        if tree is None: return []
        return [(ids[i], tracks[ids[i]][0], tracks[ids[i]][1]) for i in sorted(tree.query(geom, predicate='intersects'))]
    return tracks_near


def split_refs(r):
    return [x.strip() for x in re.split(r'[;,/]', r or '') if x.strip()]


def obj_modes(t):
    m = {MODE_TAG[k] for k in MODE_TAG if t.get(k) == 'yes'}
    st = t.get('station')
    if st in MODE_TAG: m.add(MODE_TAG[st])
    if t.get('railway') == 'tram_stop': m.add('tram')
    if t.get('railway') == 'halt': m.add('mainline')
    if st == 'light_rail': m.add('light_rail')
    return m


def build(region):
    cfg = region_cfg(region)
    nodes, ways, rels = load_input(cfg)
    lat0, lon0 = cfg['anchor']
    fwd = Transformer.from_crs('EPSG:4326', f'+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m', always_xy=True)
    P = lambda lon, lat: fwd.transform(lon, lat)
    npos = {i: P(n[0], n[1]) for i, n in nodes.items()}
    gcache = {}

    def way_pts(wid):
        w = ways.get(wid)
        return [npos[n] for n in w[0] if n in npos] if w else []

    def geom(kind, oid):
        key = (kind, oid)
        if key in gcache: return gcache[key]
        g = None
        if kind == 'n':
            if oid in npos: g = Point(npos[oid])
        elif kind == 'w':
            pts = way_pts(oid); t = ways[oid][1] if oid in ways else {}
            if len(pts) >= 4 and pts[0] == pts[-1] and t.get('area') != 'no' and ways[oid][0][0] == ways[oid][0][-1] and (t.get('area') == 'yes' or t.get('building') or t.get('landuse') or t.get('railway') in ('station', 'platform') or t.get('public_transport') in ('platform', 'station')):
                try:
                    pg = Polygon(pts)
                    g = pg if pg.is_valid else pg.buffer(0)
                except Exception: g = None
            elif len(pts) >= 2: g = LineString(pts)
            elif len(pts) == 1: g = Point(pts[0])
        else:
            mem, t = rels.get(oid, ([], {}))
            lines = [LineString(way_pts(m[1])) for m in mem if m[0] == 'w' and m[2] in ('outer', '', 'inner') and len(way_pts(m[1])) >= 2]
            if lines:
                pols = [p for p in polygonize(unary_union(lines))]
                if pols: g = unary_union(pols)
        gcache[key] = g
        return g

    def pt(kind, oid):
        g = geom(kind, oid)
        return None if g is None or g.is_empty else (g.centroid if g.geom_type != 'Point' else g)

    def tags_of(kind, oid):
        return {'n': lambda: nodes[oid][2], 'w': lambda: ways[oid][1], 'r': lambda: rels[oid][1]}[kind]() if (
            (kind == 'n' and oid in nodes) or (kind == 'w' and oid in ways) or (kind == 'r' and oid in rels)) else {}

    # ---------- classify objects ----------
    def is_rail_platform(t):
        if t.get('highway') == 'bus_stop' and t.get('railway') != 'platform': return False
        if t.get('railway') == 'platform': return t.get('bus') != 'yes' or t.get('train') == 'yes' or t.get('subway') == 'yes'
        return t.get('public_transport') == 'platform' and bool(obj_modes(t) - set()) and any(t.get(k) == 'yes' for k in MODE_TAG)

    def is_rail_stoppos(t):
        if t.get('public_transport') != 'stop_position' and t.get('railway') != 'stop': return False
        return any(t.get(k) == 'yes' for k in MODE_TAG) or t.get('railway') == 'stop'

    def is_station_obj(t):
        if t.get('railway') in ('station', 'halt', 'tram_stop'):
            return t.get('station') not in ('bus',) and t.get('disused') != 'yes'
        return t.get('public_transport') == 'station' and any(t.get(k) == 'yes' for k in MODE_TAG)

    platforms = {}; stoppos = {}; stobjs = {}; entrances = {}
    cons_items = []   # stations under construction or proposed: dict(kind, oid, tags, type)
    for kind, d in (('n', nodes), ('w', ways), ('r', rels)):
        for oid, v in d.items():
            t = v[2] if kind == 'n' else v[1]
            if kind == 'r' and t.get('type') not in ('multipolygon', 'site', 'public_transport', None): continue
            cons_ = t.get('construction:railway') or (t.get('construction') if t.get('railway') == 'construction' else None)
            if t.get('railway') == 'proposed' and t.get('proposed') in ('station', 'halt', 'stop', None) and kind != 'r' and (t.get('name') or t.get('name:en')):
                cons_items.append(dict(kind=kind, oid=oid, tags=t, type='proposed')); continue
            if t.get('proposed') == 'station' and kind != 'r' and (t.get('name') or t.get('name:en')):
                cons_items.append(dict(kind=kind, oid=oid, tags=t, type='proposed')); continue
            if cons_ in ('station', 'halt', 'stop') and kind == 'n' and (t.get('name') or t.get('name:en')):
                cons_items.append(dict(kind=kind, oid=oid, tags=t, type='stop')); continue
            if t.get('building') == 'construction' and t.get('construction') in ('train_station', 'station', 'train_station;subway') and kind != 'n':
                cons_items.append(dict(kind=kind, oid=oid, tags=t, type='area')); continue
            if t.get('railway') == 'subway_entrance' or t.get('entrance') and t.get('railway'): entrances[(kind, oid)] = t
            if is_rail_platform(t): platforms[(kind, oid)] = t
            elif is_rail_stoppos(t) and kind == 'n': stoppos[(kind, oid)] = t
            elif is_station_obj(t) and kind != 'r' or (kind == 'r' and is_station_obj(t) and t.get('type') == 'multipolygon'): stobjs[(kind, oid)] = t
    # footprints: building=train_station and landuse=railway areas
    areas = []
    for wid, (nds, t) in ways.items():
        if t.get('building') == 'train_station' or t.get('landuse') == 'railway' or (t.get('railway') in ('station',) and nds and nds[0] == nds[-1]):
            g = geom('w', wid)
            if g is not None and g.geom_type == 'Polygon' and not g.is_empty:
                areas.append((g, 'land' if t.get('landuse') == 'railway' else 'building', wid))

    # ---------- track network ----------
    tracks = {}
    deg = collections.Counter()
    for wid, (nds, t) in ways.items():
        if t.get('railway') in TRACK_RW and t.get('service') not in ('yard', 'siding', 'crossover') and len(nds) >= 2:
            g = geom('w', wid)
            if g is not None and g.geom_type == 'LineString': tracks[wid] = (g, t.get('railway'))
            deg[nds[0]] += 1; deg[nds[-1]] += 1
            for n in nds[1:-1]: deg[n] += 2

    # ---------- 1. stations from stop_area relations ----------
    S = {}          # station id -> dict
    owner = {}      # (kind, oid) -> station id, every member id
    def new_station(sid, **kw):
        S[sid] = dict(id=sid, members=set(), platforms=set(), stoppos=set(), entr=set(), objs=set(), modes=set(), tags={}, grouping='inferred', **kw)
        return S[sid]

    for rid, (mem, t) in rels.items():
        if t.get('public_transport') != 'stop_area': continue
        mset = {MODE_TAG[k] for k in MODE_TAG if t.get(k) == 'yes'}
        kids = [(m[0], m[1], m[2]) for m in mem]
        sobjs = [(k, i) for k, i, r in kids if (k, i) in stobjs]
        plats = [(k, i) for k, i, r in kids if (k, i) in platforms]
        sps = [(k, i) for k, i, r in kids if (k, i) in stoppos]
        ents = [(k, i) for k, i, r in kids if (k, i) in entrances]
        if not mset and not (sobjs and any(obj_modes(stobjs[o]) for o in sobjs)) and not plats and not sps: continue
        if t.get('bus') == 'yes' and not mset: continue
        if not (sobjs or plats or sps): continue
        s = new_station(f'r{rid}', stop_area=rid)
        s['grouping'] = 'mapped'; s['tags'] = t
        s['modes'] = set(mset)
        mm = set()
        for o in sobjs: s['objs'].add(o); mm |= obj_modes(stobjs[o])
        for o in plats: s['platforms'].add(o); mm |= obj_modes(platforms[o])
        for o in sps: s['stoppos'].add(o); mm |= obj_modes(stoppos[o])
        for o in ents: s['entr'].add(o)
        if not s['modes']: s['modes'] = mm      # the relation's own mode tags win over what its members say
        for o in s['objs'] | s['platforms'] | s['stoppos']: owner.setdefault(o, s['id'])

    # ---------- 2. stations without a stop area ----------
    for o, t in stobjs.items():
        if o in owner: continue
        sid = f'{o[0]}{o[1]}'
        s = new_station(sid); s['tags'] = t; s['objs'].add(o); s['modes'] = obj_modes(t)
        owner[o] = sid

    def sg(s):   # station geometry point (centroid of station objs, else platforms)
        pts = [pt(*o) for o in s['objs']] or [pt(*o) for o in s['platforms']] or [pt(*o) for o in s['stoppos']]
        pts = [p for p in pts if p is not None]
        if not pts: return None
        return Point(sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts))

    for s in list(S.values()):
        p = sg(s)
        if p is None: del S[s['id']]; continue
        s['pt'] = p
    # attach unclaimed platforms / stop positions within radius (same mode, matching name when both have one)
    def radius(modes): return cfg['r_main'] if 'mainline' in modes else cfg['r_light']
    unclaimed = [o for o in list(platforms) + list(stoppos) if o not in owner]
    for o in unclaimed:
        t = platforms.get(o) or stoppos.get(o)
        g = pt(*o)
        if g is None: continue
        best = None; bd = 1e9
        for s in S.values():
            if s['modes'] and obj_modes(t) and not (s['modes'] & obj_modes(t)): continue
            d = s['pt'].distance(g)
            if d > radius(s['modes'] | obj_modes(t)): continue
            n1, n2 = norm(t.get('name:en') or t.get('name')), norm(s['tags'].get('name:en') or s['tags'].get('name'))
            if n1 and n2 and n1 != n2 and n1 not in n2 and n2 not in n1: continue
            if d < bd: best, bd = s, d
        if best:
            (best['platforms'] if o in platforms else best['stoppos']).add(o); owner[o] = best['id']

    # ---------- 3. merge duplicates ----------
    def keys(s):
        t = s['tags']; k = set()
        for f in ('wikidata', 'uic_ref', 'railway:ref'):
            if t.get(f): k.add((f, t[f]))
        return k
    par = {sid: sid for sid in S}
    def find(a):
        while par[a] != a: par[a] = par[par[a]]; a = par[a]
        return a
    ids = list(S)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            A, B = S[a], S[b]
            if not (A['modes'] & B['modes']) and not (not A['modes'] or not B['modes']): continue
            d = A['pt'].distance(B['pt'])
            na = norm(A['tags'].get('name:en') or A['tags'].get('name')); nb = norm(B['tags'].get('name:en') or B['tags'].get('name'))
            same_key = bool(keys(A) & keys(B))
            if (same_key and d < 1500) or (na and na == nb and d < 400):
                par[find(a)] = find(b)
    groups = collections.defaultdict(list)
    for sid in ids: groups[find(sid)].append(sid)
    M = {}
    for root, sids in groups.items():
        sids.sort()
        base = S[sids[0]]
        for o in sids[1:]:
            x = S[o]
            for f in ('platforms', 'stoppos', 'entr', 'objs', 'modes'): base[f] |= x[f]
            if 'stop_area' in x and 'stop_area' not in base: base['stop_area'] = x['stop_area']
            for k_, v_ in x['tags'].items(): base['tags'].setdefault(k_, v_)
            base.setdefault('merged', []).append(x['id'])
            if x['grouping'] == 'mapped': base['grouping'] = 'mapped'
        M[base['id']] = base
        base['sa_ids'] = [S[s].get('stop_area') for s in sids if S[s].get('stop_area')]
    S = M
    owner = {}
    for s in S.values():
        for f in ('objs', 'platforms', 'stoppos', 'entr'):
            for o in s[f]: owner.setdefault(o, s['id'])
        p = sg(s); s['pt'] = p
    sa_to_station = {rid: s['id'] for s in S.values() for rid in s['sa_ids']}
    for s in S.values(): s['status'] = 'built'

    # ---------- 3b. stations under construction or proposed ----------
    def item_modes(it):
        t = it['tags']; m = obj_modes(t)
        nm = (t.get('name', '') + ' ' + t.get('name:en', '')).lower()
        if it['type'] == 'area' and not m: m = {'mainline'}
        if not m and ('高铁' in nm or 'high speed' in nm or 'high-speed' in nm or 'intercity' in nm or '城际' in nm): m = {'mainline'}
        return m
    for it in cons_items:
        it['pt'] = pt(it['kind'], it['oid']); it['modes'] = item_modes(it)
        it['norm'] = norm(it['tags'].get('name:en') or it['tags'].get('name')); it['norm2'] = norm(it['tags'].get('name'))
    cons_items = [it for it in cons_items if it['pt'] is not None]
    cpar_ = list(range(len(cons_items)))
    def cf(a):
        while cpar_[a] != a: cpar_[a] = cpar_[cpar_[a]]; a = cpar_[a]
        return a
    for i, A in enumerate(cons_items):
        for j in range(i + 1, len(cons_items)):
            B = cons_items[j]
            d = A['pt'].distance(B['pt'])
            near_area = False
            for X, Y in ((A, B), (B, A)):
                if X['type'] == 'area':
                    g = geom(X['kind'], X['oid'])
                    if g is not None and g.geom_type in ('Polygon', 'MultiPolygon') and g.buffer(80).contains(Y['pt']) and (not Y['modes'] or not X['modes'] or Y['modes'] & X['modes']): near_area = True
            same = d < 500 and ((A['norm'] and A['norm'] == B['norm']) or (A['norm2'] and A['norm2'] == B['norm2']))
            if same or near_area: cpar_[cf(i)] = cf(j)
    cl = collections.defaultdict(list)
    for i, it in enumerate(cons_items): cl[cf(i)].append(it)
    n_cons_new = 0
    for items in cl.values():
        modes = set().union(*[it['modes'] for it in items])
        names = {it['norm'] for it in items if it['norm']} | {it['norm2'] for it in items if it['norm2']}
        centre = next((it['pt'] for it in items if it['type'] == 'area'), None) or Point(sum(it['pt'].x for it in items) / len(items), sum(it['pt'].y for it in items) / len(items))
        built = None; bd = 500
        for s in S.values():
            if s['status'] != 'built': continue
            sn = {norm(s['tags'].get('name:en') or s['tags'].get('name')), norm(s['tags'].get('name'))}
            if not (names & sn - {''}): continue
            if modes and s['modes'] and not (modes & s['modes']): continue    # e.g. a high-speed hub next to a metro station of the same name stays separate
            d = s['pt'].distance(centre)
            if d < bd: built, bd = s, d
        if built is not None: continue     # already open: the construction tags are leftovers or extra platforms
        typ = 'construction' if any(it['type'] in ('area', 'stop') for it in items) else 'proposed'
        items.sort(key=lambda it: {'area': 0, 'stop': 1, 'proposed': 2}[it['type']])
        tags = {}
        for it in items:
            for k_, v_ in it['tags'].items(): tags.setdefault(k_, v_)
        sid = f"{'c' if typ == 'construction' else 'p'}{items[0]['kind']}{items[0]['oid']}"
        new_station(sid)
        c = S[sid]; c['tags'] = tags; c['modes'] = modes; c['status'] = typ; c['grouping'] = 'inferred'
        for it in items:
            o = (it['kind'], it['oid'])
            if it['type'] == 'stop': c['stoppos'].add(o)
            else: c['objs'].add(o)
            owner.setdefault(o, sid)
        c['pt'] = centre
        c['aka'] = sorted({v_ for it in items for v_ in (it['tags'].get('name'), it['tags'].get('name:en')) if v_})
        n_cons_new += 1

    # ---------- 4. lines at each station ----------
    master_of = {}
    for mid, (mem, t) in rels.items():
        if t.get('type') == 'route_master':
            for k, i, r in mem:
                if k == 'r': master_of[i] = mid
    S_list = list(S.values())
    def nearest_station(g, modes, name):
        best = None; bd = 250
        for s in S_list:
            if modes and s['modes'] and not (modes & s['modes']): continue
            d = s['pt'].distance(g)
            if d < bd: best, bd = s, d
        return best
    route_lines = {}
    qa_unmapped = 0
    for rid, (mem, t) in rels.items():
        if t.get('type') != 'route' or t.get('route') not in ROUTE_MODE: continue
        mode = ROUTE_MODE[t['route']]
        if t.get('route') == 'railway': continue
        mid = master_of.get(rid)
        mt = rels[mid][1] if mid else t
        base_name = re.sub(r'[:：].*|\s*[=→⇒].*', '', t.get('name', ''))
        lkey = f'm{mid}' if mid else t.get('network', '') + '|' + (t.get('ref') or base_name)
        line = dict(key=lkey, mode=mode, ref=mt.get('ref') or t.get('ref', ''), name=mt.get('name:en') or mt.get('name') or t.get('name:en') or t.get('name', ''),
                    colour=mt.get('colour') or t.get('colour', ''), network=mt.get('network') or t.get('network', ''))
        route_lines.setdefault(lkey, line)
        LM = route_lines[lkey]
        LM.setdefault('rels', []).append(rid)
        if mid: LM['master'] = mid
        for f_ in ('from', 'to', 'from:en', 'to:en', 'from:zh', 'to:zh'):
            v_ = t.get(f_)
            if v_ and v_ not in LM.setdefault(f_.replace(':', '_') + 's', []): LM[f_.replace(':', '_') + 's'].append(v_)
        for f_ in LINE_META:
            v_ = mt.get(f_) or t.get(f_)
            if v_ and f_ not in LM: LM[f_] = v_
        for m_ in mem:
            if m_[0] == 'w' and m_[1] in ways: LM.setdefault('_w', set()).add(m_[1])
            if m_[0] == 'w' and m_[1] in ways:
                sp_ = [int(v) for v in re.findall(r'\d+', ways[m_[1]][1].get('maxspeed', ''))]
                if sp_ and 0 < max(sp_) <= 400: LM['maxspeed'] = max(LM.get('maxspeed', 0), max(sp_) * (1.609 if 'mph' in ways[m_[1]][1].get('maxspeed', '') else 1))
        seen = set(); order = []
        for k, i, r in mem:
            if not (r.startswith('stop') or r.startswith('platform')): continue
            sid = owner.get((k, i))
            if sid is None:
                g = pt(k, i)
                if g is None: continue
                st = nearest_station(g, {mode}, '')
                if st is None: qa_unmapped += 1; continue
                sid = st['id']
            if sid not in seen: seen.add(sid); order.append(sid)
        for sid in seen: S[sid].setdefault('line_keys', {}).setdefault(lkey, route_lines[lkey])
        if len(order) > len(LM.get('order', [])): LM['order'] = order
    # ---------- 4b. lines under construction (railway=construction ways), joined to planned stations ----------
    cons_lines = {}
    for wid, (nds, t) in ways.items():
        if t.get('railway') != 'construction' or len(nds) < 2 or t.get('service') in ('yard', 'siding', 'crossover'): continue
        nm = t.get('name:en') or t.get('name') or ''
        if not nm and not t.get('ref'): continue
        kind_ = t.get('construction') or t.get('construction:railway') or ''
        ck = f'x|{kind_}|{nm}'
        cons_lines.setdefault(ck, dict(key=ck, mode=CONS_MODE.get(kind_, 'mainline'), ref=t.get('ref', ''), name=nm, colour='', network=t.get('network:en') or t.get('network', ''), status='construction', _w=set()))
        cons_lines[ck]['_w'].add(wid)
    for ck, L in cons_lines.items():
        for wid in L['_w']:
            t = ways[wid][1]
            for f_ in ('operator', 'operator:en', 'name:zh', 'electrified', 'voltage', 'gauge', 'usage', 'owner', 'brand', 'passenger_lines', 'railway:traffic_mode'):
                if t.get(f_) and f_ not in L: L[f_] = t[f_]
            if t.get('opening_date'): L.setdefault('opening_dates', set()).add(t['opening_date'])
            if t.get('highspeed') == 'yes': L['hsr'] = True
            for f_ in ('maxspeed', 'maxspeed:designed'):
                v_ = [int(x) for x in re.findall(r'\d+', t.get(f_, ''))]
                if v_ and max(v_) <= 400:
                    L[f_.replace(':', '_')] = max(L.get(f_.replace(':', '_'), 0), max(v_))
                    if max(v_) >= 200: L['hsr'] = True
    cons_geo = {ck: unary_union([g for g in (geom('w', w) for w in L['_w']) if g is not None]) for ck, L in cons_lines.items()}
    for s in S.values():
        if s['status'] == 'built': continue
        spts = [s['pt']] + [pt(*o) for o in s['stoppos'] if pt(*o) is not None]
        zone = unary_union([p_.buffer(70) for p_ in spts])
        fpg = None
        for o in s['objs']:
            g_ = geom(*o)
            if g_ is not None and g_.geom_type in ('Polygon', 'MultiPolygon'): zone = unary_union([zone, g_.buffer(40)])
        for ck, cg in cons_geo.items():
            if cg is not None and not cg.is_empty and cg.intersects(zone):
                L = cons_lines[ck]
                s.setdefault('line_keys', {})[ck] = L
                if not s['modes']: s['modes'] = {L['mode']}
                if L.get('hsr'): s['hsr'] = True
                for od in L.get('opening_dates', ()): s.setdefault('line_openings', set()).add(od)
        route_lines.update({ck: L for ck, L in cons_lines.items() if ck in s.get('line_keys', {})})
    # physical facts about each line, aggregated from its member ways (all tracks are counted, so a double-track line shows twice its route length)
    for L in route_lines.values():
        wids = L.get('_w') or ()
        if not wids: continue
        km = tun = br = 0.0; c = {f_: collections.Counter() for f_ in ('gauge', 'electrified', 'voltage', 'frequency')}; sd = []; dsg = 0
        for w in wids:
            t = ways[w][1]; g_ = geom('w', w)
            ln = g_.length if g_ is not None and g_.geom_type in ('LineString', 'LinearRing') else 0
            km += ln
            if t.get('tunnel') and t['tunnel'] != 'no': tun += ln
            if t.get('bridge') and t['bridge'] != 'no': br += ln
            for f_ in c:
                if t.get(f_): c[f_][t[f_]] += ln or 1
            if t.get('start_date'): sd.append(t['start_date'])
            v_ = [int(x) for x in re.findall(r'\d+', t.get('maxspeed:designed', ''))]
            if v_: dsg = max(dsg, max(v_))
        L['track_km'] = round(km / 1000, 1)
        if tun: L['tunnel_km'] = round(tun / 1000, 1)
        if br: L['bridge_km'] = round(br / 1000, 1)
        for f_, cc in c.items():
            if cc: L[f_ if f_ not in L else f_ + '_track'] = cc.most_common(1)[0][0]
        if sd: L['track_start_date'] = min(sd)
        if dsg: L['maxspeed_designed'] = dsg
    # ---------- 5. complexes ----------
    cpar = {sid: sid for sid in S}
    def cfind(a):
        while cpar[a] != a: cpar[a] = cpar[cpar[a]]; a = cpar[a]
        return a
    for gid, (mem, t) in rels.items():
        if t.get('public_transport') != 'stop_area_group': continue
        sids = [sa_to_station[i] for k, i, r in mem if k == 'r' and i in sa_to_station]
        for a in sids[1:]: cpar[cfind(a)] = cfind(sids[0])
        for a in sids: S[a].setdefault('group_ids', []).append(gid)
    for i, A in enumerate(S_list):
        for B in S_list[i + 1:]:
            if cfind(A['id']) == cfind(B['id']): continue
            wa, wb = A['tags'].get('wikidata'), B['tags'].get('wikidata')
            na = norm(A['tags'].get('name:en') or A['tags'].get('name')); nb = norm(B['tags'].get('name:en') or B['tags'].get('name'))
            d = A['pt'].distance(B['pt'])
            if (wa and wa == wb and d < 1500) or (na and na == nb and d < 300 and A['modes'] != B['modes']):
                cpar[cfind(A['id'])] = cfind(B['id'])

    # ---------- 7. size ----------
    def footprint(s):
        polys = []
        for o in s['objs']:
            g = geom(*o)
            if g is not None and g.geom_type in ('Polygon', 'MultiPolygon'): polys.append(g)
        p = s['pt']
        best = None
        for g, kind, wid in areas:
            if kind == 'building' and g.contains(p): polys.append(g)
        if not polys:   # no station or building outline: fall back to a small railway-land area around the station
            for g, kind, wid in areas:
                if kind == 'land' and g.contains(p) and g.area < 40000: polys.append(g)
        return unary_union(polys) if polys else None
    tracks_near = track_index(tracks)
    for s in S_list:
        fp = footprint(s); s['fp'] = fp
        fpb = fp if fp is not None else s['pt'].buffer(40)
        near = fpb.buffer(15)
        refs = set(); nobj = 0
        for o in s['platforms']:
            t = platforms[o]
            rr = split_refs(t.get('ref') or t.get('local_ref'))
            if rr: refs |= {r for r in rr}
            else: nobj += 1
        s['n_platforms'] = len(refs) + nobj if (refs or nobj) else None
        s['platforms_src'] = 'mapped' if s['n_platforms'] else None
        if s['n_platforms'] is None and (s['tags'].get('platforms') or '').strip().isdigit():
            s['n_platforms'] = int(s['tags']['platforms']); s['platforms_src'] = 'tag'      # the number the mapper declared
        # tracks through / ending in the footprint
        thr = set(); term = 0
        for wid, g, rw in tracks_near(near):
            if rw in MAINLINE_RW: thr.add(wid)
            nds = ways[wid][0]
            for e in (nds[0], nds[-1]):
                if deg[e] == 1 and e in npos and near.contains(Point(npos[e])) and rw in MAINLINE_RW: term += 1
        s['n_tracks_main'] = len(thr)
        s['terminus'] = bool(term)
        main_plats = 0
        for o in s['platforms']:
            if 'mainline' in obj_modes(platforms[o]) or (platforms[o].get('train') == 'yes'): main_plats += 1
        if 'mainline' in s['modes'] and main_plats == 0 and s['platforms']: main_plats = len(s['platforms'])
        area = fp.area if fp is not None else 0
        s['area_m2'] = round(area)
        s['score'] = round(3 * (main_plats if 'mainline' in s['modes'] else 0) + 2 * min(len(thr), 12) + area / 1500 + (8 if s['terminus'] else 0), 1)
    return S, route_lines, cfind, tracks, platforms, stoppos, entrances, geom, pt, tags_of, qa_unmapped, cfg


LINE_META = ('operator', 'operator:en', 'operator:zh', 'operator:type', 'name:en', 'name:zh', 'wikidata', 'wikipedia', 'start_date', 'opening_date', 'service', 'electrified', 'voltage', 'gauge',
             'brand', 'network:en', 'network:wikidata', 'owner', 'ownership', 'fee', 'description', 'website', 'passenger_lines')
CONS_MODE = {'subway': 'metro', 'light_rail': 'light_rail', 'rail': 'mainline', 'narrow_gauge': 'mainline', 'tram': 'tram', 'monorail': 'monorail', 'funicular': 'funicular'}
STATION_META = ('platforms', 'fare_zone', 'website', 'owner', 'ownership', 'operator:short', 'wikimedia_commons', 'building:levels', 'building:levels:underground', 'location', 'description', 'note', 'construction:public_transport', 'demolished:end_date',
                'ref', 'operator', 'operator:en', 'network', 'network:en', 'brand', 'wikidata', 'wikipedia', 'uic_ref', 'railway:ref', 'start_date', 'opening_date', 'opening_date:planned',
                'construction', 'proposed', 'level', 'layer', 'wheelchair', 'tactile_paving', 'elevator', 'toilets', 'bicycle_parking', 'internet_access', 'description', 'note')
TIERS = ['Stop', 'Small', 'Medium', 'Large', 'Major']


def region_poly(region):
    """The region's official boundary (metres) from the built site data, so only stations inside it are kept."""
    p = os.path.join(SITE_DATA, f'{region}.json')
    if not os.path.exists(p): return None
    d = json.load(open(p)); u = d['unit']
    polys = []
    for poly in d['legal']:
        rings = []
        for r in poly:
            x = y = 0; pts = []
            for i in range(0, len(r), 2):
                x += r[i]; y += r[i + 1]; pts.append((x * u, y * u))
            rings.append(pts)
        polys.append(Polygon(rings[0], rings[1:]).buffer(0))
    return unary_union(polys)


def tier_of(score):
    return 4 if score >= 80 else 3 if score >= 35 else 2 if score >= 10 else 1 if score >= 4 else 0


def enc_pts(g):
    return [[round(x), round(y)] for x, y in g.coords]


def main(region):
    S, route_lines, cfind, tracks, platforms, stoppos, entrances, geom, pt, tags_of, qa_unmapped, cfg = build(region)
    stations = []; detail = {}
    tracks_near = track_index(tracks)
    comps = collections.defaultdict(list)
    for s in S.values(): comps[cfind(s['id'])].append(s['id'])
    for s in S.values():
        t = s['tags']
        s['tier'] = tier_of(s['score'])
    legal = region_poly(region)
    if legal is not None:
        legal600 = legal.buffer(600); shapely.prepare(legal600)          # once, not once per station
        for sid in [i for i, s in S.items() if not legal600.contains(s['pt'])]: del S[sid]
        comps = collections.defaultdict(list)
        for s in S.values(): comps[cfind(s['id'])].append(s['id'])
    cid_of = {}
    for root, sids in comps.items():
        big = max(sids, key=lambda i: S[i]['score'])
        for i in sids: cid_of[i] = 'c' + big
    for s in sorted(S.values(), key=lambda s: -s['score']):
        t = s['tags']; p = s['pt']
        lines = sorted(s.get('line_keys', {}).values(), key=lambda l: (l['mode'], l['ref'], l['name']))
        name_en = t.get('name:en') or ''; name_loc = t.get('name') or ''
        rec = dict(id=s['id'], status=s['status'], name=name_en or name_loc, local=name_loc if name_en and name_loc != name_en else '',
                   modes=sorted(s['modes']), x=round(p.x), y=round(p.y), platforms=s['n_platforms'], platforms_src=s.get('platforms_src'), hsr=bool(s.get('hsr')), tier=s['tier'], tier_name=TIERS[s['tier']],
                   score=s['score'], terminus=s['terminus'], complex=cid_of[s['id']], n_lines=len(lines), grouping=s['grouping'],
                   operator=t.get('operator:en') or t.get('operator', ''), wikidata=t.get('wikidata', ''))
        stations.append(rec)
        d = dict(rec)
        d['lines'] = [dict(key=l['key'], ref=l['ref'], name=l['name'], colour=l['colour'], mode=l['mode']) for l in lines]
        d['meta'] = {k_: t[k_] for k_ in STATION_META if t.get(k_)}
        d['names'] = {k_: v_ for k_, v_ in t.items() if k_.startswith('name:') or k_ in ('alt_name', 'old_name', 'int_name', 'official_name', 'short_name')}
        if s.get('aka') and len(s['aka']) > 2: d['names']['aka'] = '; '.join(s['aka'])
        d['opening'] = t.get('opening_date') or t.get('start_date') or t.get('opening_date:planned') or ''
        d['opening_src'] = 'station' if d['opening'] else ''
        if not d['opening'] and s.get('line_openings'): d['opening'] = ' / '.join(sorted(s['line_openings'])); d['opening_src'] = 'line'
        rec['opening'] = d['opening']
        d['area_m2'] = s['area_m2']; d['tracks_main'] = s['n_tracks_main']
        d['source_ids'] = sorted({f'{k}/{i}' for k, i in s['objs']} | {f'relation/{r}' for r in s.get('sa_ids', [])})
        pl = []
        for o in sorted(s['platforms']):
            g = geom(*o)
            if g is None: continue
            tt = platforms[o]
            pl.append(dict(ref=tt.get('ref') or tt.get('local_ref') or '', g=mapping(g)['type'], c=[enc_pts(g)] if g.geom_type == 'LineString' else [enc_pts(g.exterior)] if g.geom_type == 'Polygon' else [[round(g.x), round(g.y)]]))
        d['platform_geoms'] = pl[:40]
        d['stop_positions'] = [[round(pt(*o).x), round(pt(*o).y)] for o in sorted(s['stoppos']) if pt(*o) is not None][:60]
        d['entrances'] = [[round(pt(*o).x), round(pt(*o).y)] for o in sorted(s['entr']) if pt(*o) is not None][:40]
        if s['fp'] is not None:
            f = s['fp']; polys = [f] if f.geom_type == 'Polygon' else list(f.geoms)
            d['footprint'] = [enc_pts(q.exterior) for q in polys if q.geom_type == 'Polygon'][:4]
        else: d['footprint'] = []
        zone = (s['fp'] if s['fp'] is not None else p.buffer(30)).buffer(120)
        tr = []
        for wid, g, rw in tracks_near(zone):
                for q in ([g.intersection(zone)] if g.intersection(zone).geom_type == 'LineString' else list(getattr(g.intersection(zone), 'geoms', []))):
                    if q.geom_type == 'LineString' and q.length > 3: tr.append([rw, enc_pts(q.simplify(1.0))])
        d['tracks'] = tr[:120]
        detail[s['id']] = d
    complexes = []
    for root, sids in comps.items():
        big = max(sids, key=lambda i: S[i]['score'])
        b = S[big]
        complexes.append(dict(id='c' + big, name=b['tags'].get('name:en') or b['tags'].get('name', ''), stations=sorted(sids), x=round(b['pt'].x), y=round(b['pt'].y),
                              modes=sorted({m for i in sids for m in S[i]['modes']}), grouping='mapped' if any(S[i].get('group_ids') for i in sids) else 'inferred'))
    lines_out = {}
    for lk, L in route_lines.items():
        n_st = sum(1 for s in S.values() if lk in s.get('line_keys', {}))
        if not n_st: continue
        o = {k_: v_ for k_, v_ in L.items() if k_ not in ('order', '_w') and v_ not in ('', None, [])}
        if 'opening_dates' in o: o['opening_dates'] = sorted(o['opening_dates'])
        o['n_stations'] = n_st
        o['stations'] = [S[i]['tags'].get('name:en') or S[i]['tags'].get('name', '') for i in L.get('order', []) if i in S][:80]
        if 'maxspeed' in o: o['maxspeed'] = round(o['maxspeed'])
        lines_out[lk] = o
    out = dict(region=region, anchor=cfg['anchor'], tiers=TIERS, stations=stations, complexes=complexes, lines=lines_out, detail=detail)
    path = os.path.join(SITE_DATA, cfg['out'])
    js = json.dumps(out, separators=(',', ':'), ensure_ascii=False)
    open(path, 'w', encoding='utf-8').write(js)
    print(f'{region}: {len(stations)} stations, {len(complexes)} complexes, {len(js) // 1024} KB, unmapped route stops {qa_unmapped}')
    for r in stations[:25]: print(f"  {r['tier_name']:7}{r['score']:7} {r['name'][:28]:28} {','.join(r['modes']):18} plat={r['platforms']} lines={r['n_lines']} {r['grouping']} {r['complex']}")


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'hk')
