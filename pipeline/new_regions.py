import json, collections
import os
NEW = os.environ.get('RA_NEW', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'run2'))   # run2 jsonl files from the OSM extraction
# Region groups (map box, core box for secondary/tertiary roads, boundary relation ids) come from groups.json, shared with the
# Rust extractor (extract/, ra-extract), which writes rail_<g>.jsonl, geo_<g>.jsonl and bnd_rels.json for every group.
GROUPS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'groups.json')))['groups']
BBOX = {g: ','.join(str(x) for x in v['box']) for g, v in GROUPS.items()}          # 'S,W,N,E', the map box of each group
BND_OF = {g: v['boundaries'] for g, v in GROUPS.items()}
KEEP_RW = {'rail', 'light_rail', 'subway', 'tram', 'narrow_gauge', 'monorail', 'funicular'}
ROUTES = {'subway', 'train', 'light_rail', 'tram', 'monorail', 'funicular'}
RCLS = {'motorway': 0, 'motorway_link': 0, 'trunk': 0, 'trunk_link': 0, 'primary': 1, 'primary_link': 1,
        'secondary': 2, 'secondary_link': 2, 'tertiary': 3, 'tertiary_link': 3}
def cen(g):
    return [sum(p[0] for p in g) / len(g), sum(p[1] for p in g) / len(g)]
def load(D, TRACK, RRELS, STOPS, CONS, groups=None):
    """Load the given groups (default: all). A build process only needs the groups of the regions it builds."""
    bnd_rels = json.load(open(f'{NEW}/bnd_rels.json'))
    for g in BBOX: D['_meta']['bbox'][g] = BBOX[g]
    for g in (groups or list(BBOX)):
        TRACK[g] = {}
        nodes = {}; ways = {}; rels = []
        for l in open(f'{NEW}/rail_{g}.jsonl'):
            d = json.loads(l)
            if d['t'] == 'n': nodes[d['id']] = d
            elif d['t'] == 'w': ways[d['id']] = d
            else: rels.append(d)
        T = TRACK[g]; C = []; plats = []; stw = []     # stw: stations mapped as areas (ways), e.g. most MTR stations in Hong Kong
        D.setdefault('_extract_ways', {})[g] = {i: (w['k'].get('railway', ''), len(w['g'])) for i, w in ways.items()}   # for the route-member check in build.py
        for i, w in ways.items():
            k = w['k']; rw = k.get('railway', ''); gg = w['g']
            if len(gg) < 2 and rw != 'platform': continue
            if rw in KEEP_RW:
                T[i] = dict(id=i, sd=k.get('start_date') or k.get('opening_date') or '', rw=rw, svc=k.get('service', ''), u=k.get('usage', ''), hs=k.get('highspeed', ''),
                            ms=k.get('maxspeed', ''), tm=k.get('railway:traffic_mode', ''), n=k.get('name:en') or k.get('name', ''),
                            nl=k.get('name', ''), ref=k.get('ref', ''), g=gg)
            elif rw == 'construction':
                C.append(dict(t='w', id=i, c=k.get('construction', ''), n=k.get('name:en') or k.get('name', ''), nl=k.get('name', ''),
                              ref=k.get('ref', ''), od=k.get('opening_date', ''), hs=k.get('highspeed', ''), ms=k.get('maxspeed', ''),
                              svc=k.get('service', ''), u=k.get('usage', ''), st='', g=gg))
            elif rw in ('station', 'halt', 'tram_stop') and gg:
                stw.append((i, k, cen(gg)))
            elif rw == 'platform' and len(gg) > 1:
                plats.append(dict(t='w', rw='platform', tr=k.get('train', ''), hs=k.get('highspeed', ''), n=k.get('name', ''), p=cen(gg)))
        CONS[g] = C
        stn = []; sn = {}
        for i, n in nodes.items():
            k = n['k']; rw = k.get('railway', '')
            if rw in ('station', 'halt', 'tram_stop'):
                stn.append(dict(sd=k.get('start_date') or k.get('opening_date') or '', n=k.get('name:en', ''), nl=k.get('name', ''), st=k.get('station', ''), sub=k.get('subway', ''),
                                tr=k.get('train', ''), lr=k.get('light_rail', ''), tm=k.get('tram', ''), rw=rw,
                                pt=k.get('public_transport', ''), p=n['p']))
            if rw in ('stop', 'halt', 'station', 'tram_stop') or k.get('public_transport') in ('stop_position', 'platform') or rw == 'platform':
                sn[i] = n['p'] + [k.get('name', ''), k.get('name:en', '')]   # stop position and its names (build.py gives the stop to the station of that name)
            if k.get('railway') == 'stop' and k.get('train') == 'yes':
                plats.append(dict(t='n', rw='stop', tr='yes', hs=k.get('highspeed', ''), n=k.get('name', ''), p=n['p']))
        for i, k, c_ in stw:                      # station areas: the centre of the outline stands in for the station node
            if k.get('name') or k.get('name:en'):
                stn.append(dict(sd=k.get('start_date') or k.get('opening_date') or '', n=k.get('name:en', ''), nl=k.get('name', ''), st=k.get('station', ''), sub=k.get('subway', ''),
                                tr=k.get('train', ''), lr=k.get('light_rail', ''), tm=k.get('tram', ''), rw=k.get('railway', ''),
                                pt=k.get('public_transport', ''), p=c_))
        for r in rels:                            # station multipolygons: centre of their member outlines
            k = r['k']
            if k.get('railway') in ('station', 'halt') and k.get('type') == 'multipolygon' and (k.get('name') or k.get('name:en')):
                pts = [p_ for m in r['m'] if m[0] == 'w' and m[1] in ways for p_ in ways[m[1]]['g']]
                if pts:
                    stn.append(dict(sd=k.get('start_date') or k.get('opening_date') or '', n=k.get('name:en', ''), nl=k.get('name', ''), st=k.get('station', ''), sub=k.get('subway', ''),
                                    tr=k.get('train', ''), lr=k.get('light_rail', ''), tm=k.get('tram', ''), rw=k.get('railway', ''), pt=k.get('public_transport', ''), p=cen(pts)))
        D['st_' + g] = stn
        R = []; SR = []; used_nodes = {}
        for r in rels:
            k = r['k']
            if k.get('type') != 'route' or k.get('route') not in ROUTES: continue
            wids = [m[1] for m in r['m'] if m[0] == 'w' and m[1] in T]
            mem = r['m']
            stops = [m[1] for m in mem if m[0] == 'n' and m[2].startswith('stop') and m[1] in sn]
            if not stops: stops = [m[1] for m in mem if m[0] == 'n' and m[2].startswith('platform') and m[1] in sn]
            for s in stops: used_nodes[str(s)] = sn[s]
            nm = k.get('name:en') or k.get('name', '')
            R.append(dict(id=r['id'], sd=k.get('start_date') or k.get('opening_date') or '', ways_all=[m[1] for m in mem if m[0] == 'w' and not m[2].startswith(('platform', 'stop'))], route=k['route'], ref=k.get('ref', ''), name=nm, nl=k.get('name', ''), colour=k.get('colour', ''),
                          network=k.get('network:en') or k.get('network', ''), service=k.get('service', ''), hs=k.get('highspeed', ''), ways=wids))
            SR.append(dict(id=r['id'], route=k['route'], hs=k.get('highspeed', ''), svc=k.get('service', ''), name=nm, stops=stops, ways=wids))
        RRELS[g] = R
        STOPS[g] = dict(rels=SR, nodes=used_nodes, plats=plats)
        # geo
        roads = []; coast = []; bw = {}
        for l in open(f'{NEW}/geo_{g}.jsonl'):
            d = json.loads(l)
            if d['t'] == 'h':
                if not str(d['h']).endswith('_link'): roads.append([RCLS[d['h']], d['g']])   # ramps are not drawn
            elif d['t'] == 'c': coast.append(d['g'])
            else: bw[d['id']] = d['g']
        D['road_' + g] = roads; D['coast_' + g] = coast
        B = []
        miss = sum(1 for rid in BND_OF[g] for m in bnd_rels[str(rid)]['m'] if m[0] == 'w' and m[1] not in bw)
        print(g, 'missing boundary ways', miss)
        for rid in BND_OF[g]:
            r = bnd_rels[str(rid)]; k = r['k']
            B.append(dict(id=rid, name=k.get('name:en') or k.get('name', ''), name_local=k.get('name', ''),
                          ways=[dict(role=m[2], g=bw[m[1]]) for m in r['m'] if m[0] == 'w' and m[1] in bw]))
        D['bnd_' + g] = B
        print(g, 'track', len(T), 'cons', len(C), 'stations', len(stn), 'routes', len(R), 'roads', len(roads), 'coast', len(coast), 'bnd', [len(b['ways']) for b in B])
