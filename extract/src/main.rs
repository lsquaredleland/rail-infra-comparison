//! ra-extract: everything the build and the station model need, for every region group, from one OSM planet PBF.
//!
//!     ra-extract --planet ../osm-rail/planet-260928.osm.pbf --config pipeline/groups.json --out run2 [--groups sf,prd] [--threads 14]
//!
//! Writes, per group g in groups.json: run2/rail_<g>.jsonl (rail, stations, stop areas, platforms, under-construction stations),
//! run2/geo_<g>.jsonl (roads, coastline, boundary ways) and merges the boundary relations into run2/bnd_rels.json.
//! The formats are the ones pipeline/new_regions.py and pipeline/stations.py read.
//!
//! Why it is fast (the Python + osmium pipeline cut the 95 GB planet once per group, about 2 minutes each, an hour when run at once):
//! - All groups are handled in the same passes; every element is tested against every group box.
//! - The planet is sorted (nodes, then ways, then relations), so after indexing the blob headers each pass decodes only the blobs of
//!   its element type: nodes (twice: once to mark which nodes lie in a box, once to fetch the coordinates of the nodes that kept ways
//!   use), relations once and ways once. Way and relation blobs are never decompressed for the node passes and vice versa.
//! - Blobs are read with one positioned read each (no memory map: that caused a page-fault storm) and decoded in parallel (rayon) with libdeflate, by a hand-written protobuf reader that skips
//!   every field it does not need. Tag tests compare string-table indices, found once per block.
//! - Two bitmaps over node ids (about 1.7 GB each for the planet) replace hash sets; only kept elements are turned into owned strings.
//!
//! Selection (the same as the Python extractors it replaces):
//! - rail: nodes with railway, public_transport or construction:railway tags, proposed=station, building=train_station; ways with a
//!   railway tag, landuse=railway, building=train_station, building=construction + construction=train_station/station,
//!   public_transport=platform/station/stop_position, construction:railway; the untagged outlines of station multipolygons;
//!   relations route / route_master (train, subway, light_rail, tram, monorail, funicular, railway), type=public_transport
//!   (stop areas and their groups) and station multipolygons. A way or node belongs to a group when one of its nodes lies in the
//!   group box (plus margin); a relation when one of its members does. Ways keep their complete geometry, even outside the box.
//! - roads: motorway, trunk and primary in the box; secondary and tertiary only in the group's core box; no ramps (*_link).
//! - coastline in the box; boundary relations by id (from groups.json) with their complete member ways, wherever they are.

mod pbf;

use pbf::*;
use rayon::prelude::*;
use rustc_hash::{FxHashMap, FxHashSet};
use std::fs::{self, File};
use std::io::{self, BufWriter, Write};
use std::ops::Range;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

// way categories
const RAIL: u8 = 1;
const MAJOR: u8 = 2; // motorway, trunk, primary
const CORE: u8 = 4; // secondary, tertiary (core box only)
const COAST: u8 = 8;
const BND: u8 = 16; // member of a wanted boundary relation
const MPM: u8 = 32; // member (outline) of a station multipolygon

const ROUTES: &[&str] = &["train", "subway", "light_rail", "tram", "monorail", "funicular", "railway"];

// ---------------------------------------------------------------- configuration

struct Group {
    name: String,
    ext: [i64; 4],          // S,W,N,E in 1e-7 degrees, box plus margin (rail, and which ways to keep)
    bx: [i64; 4],           // S,W,N,E in 1e-7 degrees, the map box itself (roads and coast, as the Python extractor did)
    core: Option<[i64; 4]>, // S,W,N,E in 1e-7 degrees
    bnd: Vec<i64>,
}

fn deg7(d: f64) -> i64 {
    (d * 1e7).round() as i64
}

fn load_config(path: &Path, only: &Option<Vec<String>>) -> Vec<Group> {
    let v: serde_json::Value = serde_json::from_str(&fs::read_to_string(path).expect("read config")).expect("parse config");
    let margin = v["margin_deg"].as_f64().unwrap_or(0.1);
    let mut out = Vec::new();
    for (name, g) in v["groups"].as_object().expect("groups") {
        if let Some(o) = only {
            if !o.iter().any(|x| x == name) {
                continue;
            }
        }
        let b: Vec<f64> = g["box"].as_array().expect("box").iter().map(|x| x.as_f64().unwrap()).collect();
        let core = g["core"].as_array().map(|c| {
            let c: Vec<f64> = c.iter().map(|x| x.as_f64().unwrap()).collect();
            [deg7(c[0]), deg7(c[1]), deg7(c[2]), deg7(c[3])]
        });
        let bnd = g["boundaries"].as_array().map(|a| a.iter().map(|x| x.as_i64().unwrap()).collect()).unwrap_or_default();
        out.push(Group {
            name: name.clone(),
            ext: [deg7(b[0] - margin), deg7(b[1] - margin), deg7(b[2] + margin), deg7(b[3] + margin)],
            bx: [deg7(b[0]), deg7(b[1]), deg7(b[2]), deg7(b[3])],
            core,
            bnd,
        });
    }
    assert!(!out.is_empty(), "no groups selected");
    assert!(out.len() <= 32, "at most 32 groups per run");
    out
}

#[inline]
fn inside(b: &[i64; 4], lat: i64, lon: i64) -> bool {
    lat >= b[0] && lat <= b[2] && lon >= b[1] && lon <= b[3]
}

#[inline]
fn box_mask(groups: &[Group], lat: i64, lon: i64) -> u32 {
    let mut m = 0;
    for (i, g) in groups.iter().enumerate() {
        if inside(&g.ext, lat, lon) {
            m |= 1 << i;
        }
    }
    m
}

#[inline]
fn map_mask(groups: &[Group], lat: i64, lon: i64) -> u32 {
    let mut m = 0;
    for (i, g) in groups.iter().enumerate() {
        if inside(&g.bx, lat, lon) {
            m |= 1 << i;
        }
    }
    m
}

#[inline]
fn core_mask(groups: &[Group], lat: i64, lon: i64) -> u32 {
    let mut m = 0;
    for (i, g) in groups.iter().enumerate() {
        if let Some(c) = &g.core {
            if inside(c, lat, lon) {
                m |= 1 << i;
            }
        }
    }
    m
}

// ---------------------------------------------------------------- node id bitmap

struct Bitmap {
    w: Vec<AtomicU64>,
}

impl Bitmap {
    fn new(n_bits: u64) -> Self {
        let n = (n_bits / 64 + 1) as usize;
        let mut w = Vec::with_capacity(n);
        w.resize_with(n, || AtomicU64::new(0));
        Bitmap { w }
    }
    #[inline]
    fn set(&self, i: i64) {
        if i >= 0 {
            if let Some(x) = self.w.get((i as u64 / 64) as usize) {
                x.fetch_or(1 << (i as u64 % 64), Ordering::Relaxed);
            }
        }
    }
    /// is any bit set in [lo, hi]?
    fn any_in(&self, lo: i64, hi: i64) -> bool {
        let (a, b) = ((lo.max(0) as u64 / 64) as usize, (hi.max(0) as u64 / 64) as usize);
        self.w.get(a..=b.min(self.w.len().saturating_sub(1))).map_or(false, |s| s.iter().any(|x| x.load(Ordering::Relaxed) != 0))
    }
    #[inline]
    fn get(&self, i: i64) -> bool {
        i >= 0 && self.w.get((i as u64 / 64) as usize).map_or(false, |x| x.load(Ordering::Relaxed) & (1 << (i as u64 % 64)) != 0)
    }
}

// ---------------------------------------------------------------- tag tests on string-table indices

/// indices of the keys we test, found once per block (u32::MAX = not in this block)
struct K {
    railway: u32,
    highway: u32,
    natural: u32,
    landuse: u32,
    building: u32,
    pt: u32,
    construction: u32,
    construction_railway: u32,
    proposed: u32,
    type_: u32,
    route: u32,
    route_master: u32,
    name: u32,
    name_en: u32,
    ref_: u32,
    modes: [u32; 6], // train, subway, light_rail, tram, monorail, funicular (=yes marks a rail public-transport object)
}

impl K {
    fn new(blk: &Block) -> Self {
        let mut k = K {
            railway: u32::MAX,
            highway: u32::MAX,
            natural: u32::MAX,
            landuse: u32::MAX,
            building: u32::MAX,
            pt: u32::MAX,
            construction: u32::MAX,
            construction_railway: u32::MAX,
            proposed: u32::MAX,
            type_: u32::MAX,
            route: u32::MAX,
            route_master: u32::MAX,
            name: u32::MAX,
            name_en: u32::MAX,
            ref_: u32::MAX,
            modes: [u32::MAX; 6],
        };
        for (i, s) in blk.strings.iter().enumerate() {
            let i = i as u32;
            match *s {
                b"railway" => k.railway = i,
                b"highway" => k.highway = i,
                b"natural" => k.natural = i,
                b"landuse" => k.landuse = i,
                b"building" => k.building = i,
                b"public_transport" => k.pt = i,
                b"construction" => k.construction = i,
                b"construction:railway" => k.construction_railway = i,
                b"proposed" => k.proposed = i,
                b"type" => k.type_ = i,
                b"route" => k.route = i,
                b"route_master" => k.route_master = i,
                b"name" => k.name = i,
                b"name:en" => k.name_en = i,
                b"ref" => k.ref_ = i,
                b"train" => k.modes[0] = i,
                b"subway" => k.modes[1] = i,
                b"light_rail" => k.modes[2] = i,
                b"tram" => k.modes[3] = i,
                b"monorail" => k.modes[4] = i,
                b"funicular" => k.modes[5] = i,
                _ => {}
            }
        }
        k
    }
}

#[inline]
fn is(v: Option<&[u8]>, s: &str) -> bool {
    v == Some(s.as_bytes())
}

#[inline]
fn any_of(v: Option<&[u8]>, list: &[&str]) -> bool {
    v.map_or(false, |x| list.iter().any(|s| s.as_bytes() == x))
}

/// public_transport objects count only when they are for rail (bus stops and platforms are left out)
fn rail_mode<'a, F: Fn(u32) -> Option<&'a [u8]>>(get: &F, k: &K) -> bool {
    get(k.railway).is_some() || k.modes.iter().any(|&m| is(get(m), "yes"))
}

fn rail_node<'a, F: Fn(u32) -> Option<&'a [u8]>>(get: &F, k: &K) -> bool {
    get(k.railway).is_some()
        || (get(k.pt).is_some() && rail_mode(get, k))
        || get(k.construction_railway).is_some()
        || is(get(k.proposed), "station")
        || is(get(k.building), "train_station")
}

fn way_cat<'a, F: Fn(u32) -> Option<&'a [u8]>>(get: &F, k: &K) -> u8 {
    let mut c = 0;
    if get(k.railway).is_some()
        || is(get(k.landuse), "railway")
        || get(k.construction_railway).is_some()
        || (any_of(get(k.pt), &["platform", "station", "stop_position"]) && rail_mode(get, k))
        || is(get(k.building), "train_station")
        || (is(get(k.building), "construction") && any_of(get(k.construction), &["train_station", "station", "train_station;subway"]))
    {
        c |= RAIL;
    }
    match get(k.highway) {
        Some(b"motorway") | Some(b"trunk") | Some(b"primary") => c |= MAJOR,
        Some(b"secondary") | Some(b"tertiary") => c |= CORE,
        _ => {}
    }
    if is(get(k.natural), "coastline") {
        c |= COAST;
    }
    c
}

/// (keep as a rail relation, is a station multipolygon whose outline ways we need)
fn rail_rel<'a, F: Fn(u32) -> Option<&'a [u8]>>(get: &F, k: &K) -> (bool, bool) {
    let ty = get(k.type_);
    if is(ty, "route") && any_of(get(k.route), ROUTES) {
        return (true, false);
    }
    if is(ty, "route_master") && any_of(get(k.route_master), ROUTES) {
        return (true, false);
    }
    if is(ty, "public_transport") {
        return (true, false);
    }
    if is(ty, "multipolygon")
        && (any_of(get(k.railway), &["station", "platform", "halt"])
            || any_of(get(k.pt), &["station", "platform"])
            || is(get(k.building), "train_station"))
    {
        return (true, true);
    }
    (false, false)
}

type Tags = Vec<(String, String)>;

#[inline]
fn txt(b: &[u8]) -> String {
    String::from_utf8_lossy(b).into_owned()
}

fn owned_sep(blk: &Block, keys: &[u32], vals: &[u32]) -> Tags {
    keys.iter().zip(vals.iter()).map(|(&k, &v)| (txt(blk.s(k)), txt(blk.s(v)))).collect()
}

fn owned_kv(blk: &Block, kv: &[u32]) -> Tags {
    kv.chunks_exact(2).map(|p| (txt(blk.s(p[0])), txt(blk.s(p[1])))).collect()
}

// ---------------------------------------------------------------- kept elements

struct TNode {
    id: i64,
    lat: i32, // 1e-7 degrees
    lon: i32,
    mask: u32,
    tags: Tags,
}

struct WayRec {
    id: i64,
    cat: u8,
    tags: Tags,
    refs: Vec<i64>,
}

struct RelRec {
    id: i64,
    tags: Tags,
    mem: Vec<(u8, i64, String)>, // type: 0 node, 1 way, 2 relation
    bnd_mask: u32,
    rail: bool,
    mp: bool,
}

// ---------------------------------------------------------------- passes

fn decode_block(f: &File, r: &BlobRef) -> Vec<u8> {
    decode(&read_blob(f, r))
}

/// first blob index in `0..n` whose first group kind is >= `kind` (the file is sorted by type)
fn first_of(f: &File, data: &[BlobRef], kind: u8) -> usize {
    let (mut lo, mut hi) = (0usize, data.len());
    while lo < hi {
        let mid = (lo + hi) / 2;
        let k = blob_kind(f, &data[mid]);
        if k < kind || k == K_OTHER && kind != K_OTHER {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    lo
}

fn last_node_id(f: &File, r: &BlobRef) -> i64 {
    let raw = decode_block(f, r);
    let blk = Block::parse(&raw);
    let mut kv = Vec::new();
    let mut m = 0i64;
    for g in &blk.groups {
        for_each_node(&blk, g, &mut kv, |id, _, _, _| m = m.max(id));
    }
    m
}

// ---------------------------------------------------------------- JSON output

fn js<W: Write>(w: &mut W, s: &str) -> io::Result<()> {
    w.write_all(b"\"")?;
    let b = s.as_bytes();
    let mut start = 0;
    for (i, &c) in b.iter().enumerate() {
        let esc: Option<&[u8]> = match c {
            b'"' => Some(&b"\\\""[..]),
            b'\\' => Some(&b"\\\\"[..]),
            b'\n' => Some(&b"\\n"[..]),
            b'\r' => Some(&b"\\r"[..]),
            b'\t' => Some(&b"\\t"[..]),
            _ => None,
        };
        if esc.is_some() || c < 0x20 {
            w.write_all(&b[start..i])?;
            match esc {
                Some(e) => w.write_all(e)?,
                None => write!(w, "\\u{:04x}", c)?,
            }
            start = i + 1;
        }
    }
    w.write_all(&b[start..])?;
    w.write_all(b"\"")
}

fn js_opt<W: Write>(w: &mut W, s: Option<&str>) -> io::Result<()> {
    match s {
        Some(s) => js(w, s),
        None => w.write_all(b"null"),
    }
}

/// 1e-7 degrees -> decimal degrees with 6 decimals
fn coord<W: Write>(w: &mut W, v: i32) -> io::Result<()> {
    let v = v as i64;
    let r = if v >= 0 { (v + 5) / 10 } else { (v - 5) / 10 };
    let a = r.abs();
    write!(w, "{}{}.{:06}", if r < 0 { "-" } else { "" }, a / 1_000_000, a % 1_000_000)
}

fn tags_obj<W: Write>(w: &mut W, t: &Tags) -> io::Result<()> {
    w.write_all(b"{")?;
    for (i, (k, v)) in t.iter().enumerate() {
        if i > 0 {
            w.write_all(b",")?;
        }
        js(w, k)?;
        w.write_all(b":")?;
        js(w, v)?;
    }
    w.write_all(b"}")
}

fn geom<W: Write>(w: &mut W, g: &[(i64, i32, i32)]) -> io::Result<()> {
    w.write_all(b"[")?;
    for (i, (_, la, lo)) in g.iter().enumerate() {
        if i > 0 {
            w.write_all(b",")?;
        }
        w.write_all(b"[")?;
        coord(w, *la)?;
        w.write_all(b",")?;
        coord(w, *lo)?;
        w.write_all(b"]")?;
    }
    w.write_all(b"]")
}

fn tag<'t>(t: &'t Tags, k: &str) -> Option<&'t str> {
    t.iter().find(|(a, _)| a == k).map(|(_, v)| v.as_str())
}

// ---------------------------------------------------------------- main

struct Args {
    planet: PathBuf,
    config: PathBuf,
    out: PathBuf,
    groups: Option<Vec<String>>,
    threads: Option<usize>,
}

fn args() -> Args {
    let mut a = Args { planet: PathBuf::new(), config: PathBuf::from("pipeline/groups.json"), out: PathBuf::from("run2"), groups: None, threads: None };
    let v: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < v.len() {
        let val = v.get(i + 1).cloned().unwrap_or_default();
        match v[i].as_str() {
            "--planet" => a.planet = val.into(),
            "--config" => a.config = val.into(),
            "--out" => a.out = val.into(),
            "--groups" => a.groups = Some(val.split(',').map(String::from).collect()),
            "--threads" => a.threads = val.parse().ok(),
            "-h" | "--help" => {
                eprintln!("ra-extract --planet PLANET.osm.pbf [--config pipeline/groups.json] [--out run2] [--groups a,b] [--threads N]");
                std::process::exit(0);
            }
            o => panic!("unknown argument {o}"),
        }
        i += 2;
    }
    assert!(!a.planet.as_os_str().is_empty(), "--planet is required");
    a
}

fn main() {
    let a = args();
    if let Some(t) = a.threads {
        rayon::ThreadPoolBuilder::new().num_threads(t).build_global().expect("thread pool");
    }
    let groups = load_config(&a.config, &a.groups);
    let t0 = Instant::now();
    let lap = |what: &str| eprintln!("[{:7.1}s] {what}", t0.elapsed().as_secs_f64());

    // positioned reads (pread) from many threads: no page-fault storm as with a memory map, and nothing is read twice
    let file = File::open(&a.planet).expect("open planet");
    let buf = &file;

    // ---- index the blobs and find where nodes, ways and relations start
    let blobs = index(buf).expect("PBF index");
    let sorted = blobs.first().map_or(false, |b| !b.data && header_sorted(&read_blob(buf, b)));
    let data: Vec<BlobRef> = blobs.into_iter().filter(|b| b.data).collect();
    let n = data.len();
    let (nodes_r, rels_r, ways_r): (Range<usize>, Range<usize>, Range<usize>) = if sorted {
        let w0 = first_of(buf, &data, K_WAYS);
        let r0 = first_of(buf, &data, K_RELS);
        (0..w0, r0..n, w0..r0)
    } else {
        eprintln!("warning: the file is not marked Sort.Type_then_ID; every pass decodes every blob");
        (0..n, 0..n, 0..n)
    };
    let max_id = if sorted && nodes_r.end > 0 { last_node_id(buf, &data[nodes_r.end - 1]) + 1 } else { 1i64 << 34 };
    lap(&format!("indexed {n} blobs (nodes {:?}, ways {:?}, relations {:?}); max node id {max_id}", nodes_r, ways_r, rels_r));

    // ---- pass 1, nodes: mark nodes inside any group box; keep tagged rail / public-transport nodes
    let inbox = Bitmap::new(max_id as u64);
    let p1: Vec<(Vec<TNode>, i64, i64)> = data[nodes_r.clone()]
        .par_iter()
        .map(|r| {
            let raw = decode_block(buf, r);
            let blk = Block::parse(&raw);
            let k = K::new(&blk);
            let mut out = Vec::new();
            let mut kvb = Vec::new();
            let (mut lo_id, mut hi_id) = (i64::MAX, i64::MIN);
            for g in &blk.groups {
                if group_kind(g) != K_NODES {
                    continue;
                }
                for_each_node(&blk, g, &mut kvb, |id, latn, lonn, kv| {
                    lo_id = lo_id.min(id);
                    hi_id = hi_id.max(id);
                    let (lat, lon) = (latn / 100, lonn / 100);
                    let m = box_mask(&groups, lat, lon);
                    if m == 0 {
                        return;
                    }
                    inbox.set(id);
                    if !kv.is_empty() {
                        let get = |key: u32| kv_get(&blk, kv, key);
                        if rail_node(&get, &k) {
                            out.push(TNode { id, lat: lat as i32, lon: lon as i32, mask: m, tags: owned_kv(&blk, kv) });
                        }
                    }
                });
            }
            (out, lo_id, hi_id)
        })
        .collect();
    let blob_ids: Vec<(i64, i64)> = p1.iter().map(|x| (x.1, x.2)).collect();
    let tnodes: Vec<TNode> = p1.into_iter().flat_map(|x| x.0).collect();
    lap(&format!("pass 1 (nodes): {} tagged rail / public-transport nodes", tnodes.len()));

    // ---- pass 2, relations: boundaries by id, rail relations that touch a box (or have way members)
    let mut bnd_of: FxHashMap<i64, u32> = FxHashMap::default();
    for (i, g) in groups.iter().enumerate() {
        for &id in &g.bnd {
            *bnd_of.entry(id).or_insert(0) |= 1 << i;
        }
    }
    let rels: Vec<RelRec> = data[rels_r.clone()]
        .par_iter()
        .map(|r| {
            let raw = decode_block(buf, r);
            let blk = Block::parse(&raw);
            let k = K::new(&blk);
            let mut out = Vec::new();
            let mut b = RelBufs::new();
            for g in &blk.groups {
                if group_kind(g) != K_RELS {
                    continue;
                }
                for_each_relation(g, &mut b, |id, b| {
                    let bm = bnd_of.get(&id).copied().unwrap_or(0);
                    let get = |key: u32| sep_get(&blk, &b.keys, &b.vals, key);
                    let (rail, mp) = if bm == 0 { rail_rel(&get, &k) } else { (false, false) };
                    if bm == 0 && !rail {
                        return;
                    }
                    if bm == 0 {
                        // a rail relation is only of interest if a member node lies in a box, or it has way members (tested later)
                        let touches = b.types.iter().zip(b.memids.iter()).any(|(&t, &m)| t == 1 || (t == 0 && inbox.get(m)));
                        if !touches {
                            return;
                        }
                    }
                    let mem = b
                        .types
                        .iter()
                        .zip(b.memids.iter())
                        .zip(b.roles.iter())
                        .map(|((&t, &m), &ro)| (t as u8, m, txt(blk.s(ro))))
                        .collect();
                    out.push(RelRec { id, tags: owned_sep(&blk, &b.keys, &b.vals), mem, bnd_mask: bm, rail, mp });
                });
            }
            out
        })
        .flatten_iter()
        .collect();
    let mut bnd_ways: FxHashMap<i64, u32> = FxHashMap::default();
    let mut mp_ways: FxHashSet<i64> = FxHashSet::default();
    for r in &rels {
        for &(t, m, _) in &r.mem {
            if t != 1 {
                continue;
            }
            if r.bnd_mask != 0 {
                *bnd_ways.entry(m).or_insert(0) |= r.bnd_mask;
            }
            if r.mp {
                mp_ways.insert(m);
            }
        }
    }
    lap(&format!(
        "pass 2 (relations): {} kept ({} boundaries), {} boundary ways, {} station outline ways",
        rels.len(),
        rels.iter().filter(|r| r.bnd_mask != 0).count(),
        bnd_ways.len(),
        mp_ways.len()
    ));

    // ---- pass 3, ways: keep by tags (or boundary / outline membership) when a node lies in a box; mark the nodes we need
    let needed = Bitmap::new(max_id as u64);
    let ways: Vec<WayRec> = data[ways_r.clone()]
        .par_iter()
        .map(|r| {
            let raw = decode_block(buf, r);
            let blk = Block::parse(&raw);
            let k = K::new(&blk);
            let mut out = Vec::new();
            let (mut keys, mut vals, mut refs) = (Vec::new(), Vec::new(), Vec::new());
            for g in &blk.groups {
                if group_kind(g) != K_WAYS {
                    continue;
                }
                for_each_way(g, &mut keys, &mut vals, |id, keys, vals, refs_raw| {
                    let get = |key: u32| sep_get(&blk, keys, vals, key);
                    let mut cat = way_cat(&get, &k);
                    if bnd_ways.contains_key(&id) {
                        cat |= BND;
                    }
                    if mp_ways.contains(&id) {
                        cat |= MPM;
                    }
                    if cat == 0 {
                        return;
                    }
                    unpack_delta(refs_raw, &mut refs);
                    if cat & BND == 0 && !refs.iter().any(|&nd| inbox.get(nd)) {
                        return;
                    }
                    for &nd in refs.iter() {
                        needed.set(nd); // complete geometry, also outside the box
                    }
                    let tags = if cat & (RAIL | MPM) != 0 {
                        owned_sep(&blk, keys, vals)
                    } else if cat & (MAJOR | CORE) != 0 {
                        [k.highway, k.name, k.name_en, k.ref_]
                            .iter()
                            .filter_map(|&key| get(key).map(|v| (txt(blk.s(key)), txt(v))))
                            .collect()
                    } else {
                        Vec::new()
                    };
                    out.push(WayRec { id, cat, tags, refs: refs.clone() });
                });
            }
            out
        })
        .flatten_iter()
        .collect();
    drop(inbox);
    lap(&format!("pass 3 (ways): {} kept", ways.len()));

    // ---- pass 4, nodes again: coordinates of the needed nodes (in file order, so already sorted by id)
    let todo: Vec<&BlobRef> = data[nodes_r.clone()]
        .iter()
        .zip(blob_ids.iter())
        .filter(|(_, &(lo, hi))| lo <= hi && needed.any_in(lo, hi))
        .map(|(r, _)| r)
        .collect();
    let skipped = nodes_r.len() - todo.len();
    let parts: Vec<Vec<(i64, i32, i32)>> = todo
        .par_iter()
        .map(|r| {
            let raw = decode_block(buf, r);
            let blk = Block::parse(&raw);
            let mut out = Vec::new();
            let mut kvb = Vec::new();
            for g in &blk.groups {
                if group_kind(g) != K_NODES {
                    continue;
                }
                for_each_node(&blk, g, &mut kvb, |id, latn, lonn, _| {
                    if needed.get(id) {
                        out.push((id, (latn / 100) as i32, (lonn / 100) as i32));
                    }
                });
            }
            out
        })
        .collect();
    drop(needed);
    let total: usize = parts.iter().map(|p| p.len()).sum();
    let mut ids: Vec<i64> = Vec::with_capacity(total);
    let mut ll: Vec<(i32, i32)> = Vec::with_capacity(total);
    for p in parts {
        for (i, la, lo) in p {
            ids.push(i);
            ll.push((la, lo));
        }
    }
    if !sorted {
        let mut z: Vec<(i64, (i32, i32))> = ids.into_iter().zip(ll).collect();
        z.par_sort_unstable_by_key(|x| x.0);
        ids = z.iter().map(|x| x.0).collect();
        ll = z.iter().map(|x| x.1).collect();
    }
    lap(&format!("pass 4 (nodes): coordinates of {total} nodes ({skipped} node blobs skipped, no needed node in them)"));

    // ---- assemble: geometry and group masks of every kept way
    struct WayOut {
        geom: Vec<(i64, i32, i32)>,
        bmask: u32, // box plus margin
        cmask: u32, // core box
        mmask: u32, // map box
    }
    let wout: Vec<WayOut> = ways
        .par_iter()
        .map(|w| {
            let mut g = Vec::with_capacity(w.refs.len());
            let (mut bm, mut cm, mut mm) = (0u32, 0u32, 0u32);
            for &r in &w.refs {
                if let Ok(i) = ids.binary_search(&r) {
                    let (la, lo) = ll[i];
                    g.push((r, la, lo));
                    bm |= box_mask(&groups, la as i64, lo as i64);
                    mm |= map_mask(&groups, la as i64, lo as i64);
                    if w.cat & CORE != 0 {
                        cm |= core_mask(&groups, la as i64, lo as i64);
                    }
                }
            }
            WayOut { geom: g, bmask: bm, cmask: cm, mmask: mm }
        })
        .collect();

    // group masks of rail relations: from member nodes and ways, then from member relations (route masters, stop-area groups)
    let mut node_mask: FxHashMap<i64, u32> = FxHashMap::default();
    for t in &tnodes {
        node_mask.insert(t.id, t.mask);
    }
    let mut way_mask: FxHashMap<i64, u32> = FxHashMap::default();
    for (w, o) in ways.iter().zip(&wout) {
        if w.cat & (RAIL | MPM) != 0 && o.bmask != 0 {
            way_mask.insert(w.id, o.bmask);
        }
    }
    let mut rel_mask: FxHashMap<i64, u32> = FxHashMap::default();
    for _round in 0..2 {
        for r in rels.iter().filter(|r| r.rail) {
            let mut m = 0;
            for (t, id, _) in &r.mem {
                m |= match t {
                    0 => node_mask.get(id).copied().unwrap_or(0),
                    1 => way_mask.get(id).copied().unwrap_or(0),
                    _ => rel_mask.get(id).copied().unwrap_or(0),
                };
            }
            if m != 0 {
                *rel_mask.entry(r.id).or_insert(0) |= m;
            }
        }
    }
    lap("assembled geometry and group membership");

    // ---- write every group in parallel
    fs::create_dir_all(&a.out).expect("create output folder");
    let results: Vec<String> = groups
        .par_iter()
        .enumerate()
        .map(|(gi, g)| {
            let bit = 1u32 << gi;
            let res = (|| -> io::Result<String> {
                let mut rail = BufWriter::with_capacity(1 << 20, File::create(a.out.join(format!("rail_{}.jsonl", g.name)))?);
                let (mut nn, mut nw, mut nr) = (0, 0, 0);
                for t in tnodes.iter().filter(|t| t.mask & bit != 0) {
                    write!(rail, "{{\"t\":\"n\",\"id\":{},\"p\":[", t.id)?;
                    coord(&mut rail, t.lat)?;
                    rail.write_all(b",")?;
                    coord(&mut rail, t.lon)?;
                    rail.write_all(b"],\"k\":")?;
                    tags_obj(&mut rail, &t.tags)?;
                    rail.write_all(b"}\n")?;
                    nn += 1;
                }
                for (w, o) in ways.iter().zip(&wout) {
                    if w.cat & (RAIL | MPM) == 0 || o.bmask & bit == 0 || o.geom.is_empty() {
                        continue;
                    }
                    write!(rail, "{{\"t\":\"w\",\"id\":{},\"k\":", w.id)?;
                    tags_obj(&mut rail, &w.tags)?;
                    rail.write_all(b",\"g\":")?;
                    geom(&mut rail, &o.geom)?;
                    rail.write_all(b",\"n\":[")?; // node ids, aligned with g (stations.py needs them)
                    for (i, (id, _, _)) in o.geom.iter().enumerate() {
                        if i > 0 {
                            rail.write_all(b",")?;
                        }
                        write!(rail, "{id}")?;
                    }
                    rail.write_all(b"]}\n")?;
                    nw += 1;
                }
                for r in rels.iter().filter(|r| r.rail && rel_mask.get(&r.id).map_or(false, |m| m & bit != 0)) {
                    write!(rail, "{{\"t\":\"r\",\"id\":{},\"k\":", r.id)?;
                    tags_obj(&mut rail, &r.tags)?;
                    rail.write_all(b",\"m\":[")?;
                    for (i, (t, id, role)) in r.mem.iter().enumerate() {
                        if i > 0 {
                            rail.write_all(b",")?;
                        }
                        let ty = match t {
                            0 => "n",
                            1 => "w",
                            _ => "r",
                        };
                        write!(rail, "[\"{ty}\",{id},")?;
                        js(&mut rail, role)?;
                        rail.write_all(b"]")?;
                    }
                    rail.write_all(b"]}\n")?;
                    nr += 1;
                }
                rail.flush()?;

                let mut geo = BufWriter::with_capacity(1 << 20, File::create(a.out.join(format!("geo_{}.jsonl", g.name)))?);
                let (mut nh, mut nc, mut nb) = (0, 0, 0);
                for (w, o) in ways.iter().zip(&wout) {
                    if o.geom.len() < 2 {
                        continue;
                    }
                    let kind = if (w.cat & MAJOR != 0 && o.mmask & bit != 0) || (w.cat & CORE != 0 && o.cmask & bit != 0) {
                        nh += 1;
                        Some("h")
                    } else if w.cat & COAST != 0 && o.mmask & bit != 0 {
                        nc += 1;
                        Some("c")
                    } else {
                        None
                    };
                    let bnd_here = w.cat & BND != 0 && bnd_ways.get(&w.id).map_or(false, |m| m & bit != 0);
                    for t in kind.into_iter().chain(if bnd_here { Some("b") } else { None }) {
                        if t == "b" {
                            nb += 1;
                        }
                        write!(geo, "{{\"id\":{},\"t\":\"{t}\",\"h\":", w.id)?;
                        js_opt(&mut geo, if t == "h" { tag(&w.tags, "highway") } else { None })?;
                        geo.write_all(b",\"n\":")?;
                        js_opt(&mut geo, tag(&w.tags, "name"))?;
                        geo.write_all(b",\"ne\":")?;
                        js_opt(&mut geo, tag(&w.tags, "name:en"))?;
                        geo.write_all(b",\"ref\":")?;
                        js_opt(&mut geo, tag(&w.tags, "ref"))?;
                        geo.write_all(b",\"g\":")?;
                        geom(&mut geo, &o.geom)?;
                        geo.write_all(b"}\n")?;
                    }
                }
                geo.flush()?;
                Ok(format!("{}: rail {nn} nodes, {nw} ways, {nr} relations; geo {nh} roads, {nc} coast, {nb} boundary ways", g.name))
            })();
            res.unwrap_or_else(|e| panic!("writing group {}: {e}", g.name))
        })
        .collect();
    for r in &results {
        eprintln!("  {r}");
    }

    // ---- boundary relations: merge into run2/bnd_rels.json ({id: {k: tags, m: [[type, ref, role]]}})
    let path = a.out.join("bnd_rels.json");
    let mut all: serde_json::Map<String, serde_json::Value> = fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default();
    for r in rels.iter().filter(|r| r.bnd_mask != 0) {
        let k: serde_json::Map<String, serde_json::Value> = r.tags.iter().map(|(a, b)| (a.clone(), serde_json::Value::String(b.clone()))).collect();
        let m: Vec<serde_json::Value> = r
            .mem
            .iter()
            .map(|(t, id, role)| {
                let ty = match t {
                    0 => "n",
                    1 => "w",
                    _ => "r",
                };
                serde_json::json!([ty, id, role])
            })
            .collect();
        all.insert(r.id.to_string(), serde_json::json!({ "k": k, "m": m }));
    }
    let missing: Vec<i64> = groups.iter().flat_map(|g| g.bnd.iter().copied()).filter(|id| !all.contains_key(&id.to_string())).collect();
    fs::write(&path, serde_json::to_string(&serde_json::Value::Object(all)).expect("json")).expect("write bnd_rels.json");
    if !missing.is_empty() {
        eprintln!("warning: boundary relations not found in the planet: {missing:?}");
    }
    lap("done");
}
