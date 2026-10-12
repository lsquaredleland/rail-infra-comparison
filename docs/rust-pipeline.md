# The extraction and build pipeline: design, code and optimizations

This document describes the pipeline that turns an OpenStreetMap planet file into the map data of this project, in enough detail to
reuse it in another project. It covers the Rust extractor (`extract/`, binary `ra-extract`), the Python build and station model
(`pipeline/`), the orchestration script, every optimization with its measured effect, how outputs were verified, and the pitfalls hit
on the way. All numbers were measured on a 14-core Apple Silicon Mac with 36 GB of RAM and an internal SSD, with planet-260928
(95 GB), on 2026-10-11 and 2026-10-12.

The code to copy is small:

| file | lines | what |
|---|---|---|
| `extract/Cargo.toml` | 18 | crate definition and release profile |
| `extract/src/pbf.rs` | 433 | PBF reader: blob index, decompression, protobuf parsing of blocks, nodes, ways, relations |
| `extract/src/main.rs` | 912 | the extractor: configuration, tag rules, four passes, assembly, JSON output |
| `pipeline/groups.json` | | region groups: boxes, core boxes, boundary relation ids |
| `pipeline/refresh.sh` | 42 | the whole pipeline in one command |
| `pipeline/run_all.py` | 43 | parallel build, one process per group, memory budget |
| `pipeline/build.py`, `new_regions.py`, `stations.py` | | project-specific build; the optimization patterns are described below |

`pbf.rs` is generic (any OSM PBF, any project). `main.rs` is generic in structure; the tag rules and output formats are specific to
this project and are the parts to change (section 9).

---

## 1. Results

| step | before | after |
|---|---|---|
| extraction, all 12 groups | about 1 hour (osmium cut per group, then Python) | 79 s, 4.7 GB peak memory |
| extraction, one large group (Greater Bay Area) | 484 s | 65 s, 3.7 GB peak |
| build, all 21 regions | about 9 min | 76 s |
| build, Greater Bay Area | 331 s alone (362 s in a full parallel run) | 26 s |
| station model, Greater Bay Area (1,514 stations) | 142 s | 21 s |
| everything (`pipeline/refresh.sh`) | | 167 s |

The build and station-model optimizations left the output byte-identical (verified on the Greater Bay Area, Zurich and Hong Kong). The
Rust extractor's output was compared with the Python and osmium extractors it replaced: roads, coastline and boundary ways identical for
the Greater Bay Area; rail ways identical up to 0.000001 degree rounding, plus improvements listed in section 7.

---

## 2. Architecture

```
planet.osm.pbf (95 GB, sorted)          pipeline/groups.json (boxes, core boxes, boundary ids)
          |                                        |
          v                                        v
  ra-extract (Rust, one run for all groups, ~80 s) ------------------------------+
          |                                                                        |
          v                                                                        |
  run2/rail_<group>.jsonl   rail nodes, ways (with geometry and node ids), relations |
  run2/geo_<group>.jsonl    roads, coastline, boundary member ways                  |
  run2/bnd_rels.json        boundary relations (tags, members)                     |
          |                                                                        |
          v                                                                        |
  run_all.py: one Python process per group (memory budget), build.py ONLY=<regions of the group>
          |       new_regions.py loads only that group's jsonl
          v
  site/data/<region>.json, <region>.roads.json, index.json
          |
          v
  stations.py <region>  ->  site/data/stations_<region>.json   (reads run2/rail_<group>.jsonl directly)
```

Concepts:

- **Group**: a rectangular box with everything needed to build one or more regions (for example group `sf` serves regions `sf` and
  `bay`). The extractor works per group; regions are clipped from their group's data by the build. Groups are defined once, in
  `pipeline/groups.json`, read by both Rust and Python.
- **Region**: what the map shows (a city, a city plus surroundings). Defined in `REGIONS` in `build.py`.
- One planet read serves every group, so adding a group costs almost nothing extra in the extractor.

`groups.json`:

```json
{
  "margin_deg": 0.1,
  "groups": {
    "prd": { "box": [22.13, 113.74, 22.87, 114.65], "core": [22.21, 113.83, 22.63, 114.35], "boundaries": [913110, 3464353] },
    "gba": { "box": [21.5, 111.3, 24.45, 115.45], "core": null, "boundaries": [3287346, 3464353, ...] }
  }
}
```

- `box`: south, west, north, east of the map in degrees. The extractor adds `margin_deg` for rail (track just outside the box is kept).
- `core`: where secondary and tertiary roads are kept (city regions); `null` for none.
- `boundaries`: OSM relation ids of the administrative boundaries the regions use; fetched whole, wherever their ways are.

---

## 3. The PBF format, as much as the reader needs

A PBF file is a sequence of:

```
[4-byte big-endian length L][BlobHeader, L bytes][Blob, BlobHeader.datasize bytes]
```

Protobuf messages and the field numbers used (wire types: 0 varint, 1 fixed64, 2 length-delimited, 5 fixed32):

| message | field | type | meaning |
|---|---|---|---|
| BlobHeader | 1 | string | `OSMHeader` or `OSMData` |
| BlobHeader | 3 | int32 | datasize (bytes of the Blob that follows) |
| Blob | 1 | bytes | raw (uncompressed) data |
| Blob | 2 | int32 | raw_size (size after decompression) |
| Blob | 3 | bytes | zlib data (planet uses zlib) |
| Blob | 4, 5, 6, 7 | bytes | lzma, bzip2, lz4, zstd (rejected by this reader) |
| HeaderBlock | 4, 5 | repeated string | required / optional features; `Sort.Type_then_ID` means sorted |
| PrimitiveBlock | 1 | StringTable | field 1: repeated bytes (all strings of the block) |
| PrimitiveBlock | 2 | repeated PrimitiveGroup | |
| PrimitiveBlock | 17 | int32 | granularity in nanodegrees (default 100) |
| PrimitiveBlock | 19, 20 | int64 | lat_offset, lon_offset in nanodegrees (default 0) |
| PrimitiveGroup | 1 / 2 / 3 / 4 | | nodes / dense nodes / ways / relations (one kind per group) |
| DenseNodes | 1 | packed sint64, delta | ids |
| DenseNodes | 8, 9 | packed sint64, delta | lat, lon |
| DenseNodes | 10 | packed int32 | keys_vals: key,val,key,val,...,0 per node (string-table indices) |
| Way | 1 | int64 | id |
| Way | 2, 3 | packed uint32 | keys, vals (string-table indices) |
| Way | 8 | packed sint64, delta | node refs |
| Relation | 1 | int64 | id |
| Relation | 2, 3 | packed uint32 | keys, vals |
| Relation | 8 | packed int32 | member roles (string-table indices) |
| Relation | 9 | packed sint64, delta | member ids |
| Relation | 10 | packed enum | member types: 0 node, 1 way, 2 relation |
| Node (plain) | 1, 8, 9 | sint64 | id, lat, lon (rare in planet files) |

Coordinates: `degrees = 1e-9 * (offset + granularity * value)`. The reader converts to integer 1e-7 degrees (`nano / 100`), which
fits `i32` for both lat and lon and is exactly the precision OSM stores.

The planet is sorted: all node blocks, then all way blocks, then all relation blocks, each by id. This is what makes the per-type
passes in section 4 possible.

---

## 4. ra-extract, step by step

### 4.1 The protobuf reader (`pbf.rs`)

A minimal cursor over a byte slice, with no allocation and no generated code:

```rust
pub struct Pb<'a> { b: &'a [u8], p: usize }

impl<'a> Pb<'a> {
    pub fn new(b: &'a [u8]) -> Self { Pb { b, p: 0 } }
    pub fn done(&self) -> bool { self.p >= self.b.len() }
    pub fn varint(&mut self) -> u64 {
        let mut r = 0u64; let mut s = 0u32;
        loop {
            let c = self.b[self.p]; self.p += 1;
            r |= ((c & 0x7f) as u64) << s;
            if c < 0x80 { return r; }
            s += 7;
        }
    }
    pub fn key(&mut self) -> (u32, u8) { let k = self.varint(); ((k >> 3) as u32, (k & 7) as u8) }
    pub fn bytes(&mut self) -> &'a [u8] { let l = self.varint() as usize; let s = &self.b[self.p..self.p + l]; self.p += l; s }
    pub fn skip(&mut self, wt: u8) {
        match wt { 0 => { self.varint(); } 1 => self.p += 8, 2 => { let l = self.varint() as usize; self.p += l; } 5 => self.p += 4,
                   _ => panic!("unsupported protobuf wire type {wt}") }
    }
}
pub fn zz(n: u64) -> i64 { ((n >> 1) as i64) ^ -((n & 1) as i64) }   // zigzag (sint64)
```

Every parser is a loop `while !pb.done() { let (f, wt) = pb.key(); match (f, wt) { ... _ => pb.skip(wt) } }`, so fields that are not
needed (metadata, DenseInfo, timestamps, users) are skipped without decoding. Sub-messages are returned as byte slices (`pb.bytes()`)
and parsed only if needed. Strings stay `&[u8]` slices into the decompressed block until an element is kept.

Element iteration is callback-based and reuses buffers (keys, vals, refs) across elements of a block:

```rust
for_each_node(&blk, group, &mut kv_buf, |id, lat_nano, lon_nano, kv /* interleaved key,val string ids */| { ... });
for_each_way(group, &mut keys, &mut vals, |id, keys, vals, refs_raw /* still packed */| { ... });
for_each_relation(group, &mut rel_bufs, |id, bufs /* keys, vals, roles, types, memids */| { ... });
```

`for_each_way` hands over the refs still packed; the caller decodes them (`unpack_delta`) only for ways whose tags pass. Most ways of
the planet are rejected on tags alone, so their refs are never decoded.

### 4.2 Reading blobs: positioned reads, not a memory map

The first version memory-mapped the 95 GB file and let rayon threads read blob slices from the map. Zurich alone took 174 s, with 221 s
of system time (page faults) and only about 4.7 cores busy. The current version opens the file once and reads each blob with one
positioned read (`pread`, `FileExt::read_exact_at`) into a fresh buffer, from many threads at once:

```rust
pub fn read_blob(f: &File, r: &BlobRef) -> Vec<u8> {
    let mut b = vec![0u8; r.len];
    f.read_exact_at(&mut b, r.off as u64).expect("read blob");
    b
}
```

`File` is `Sync`, positioned reads need no shared cursor, and each blob (about 1.8 MB compressed on average) is one large sequential
read. The same Greater Bay Area run went to 65 s with 88 s of system time. Peak memory also dropped, since file pages no longer count
toward the process.

### 4.3 Decompression: libdeflate into a pre-sized buffer

```rust
thread_local! { static DEC: RefCell<Decompressor> = RefCell::new(Decompressor::new()); }

pub fn decode(blob: &[u8]) -> Vec<u8> {
    // parse the Blob message: raw (field 1), raw_size (2), zlib_data (3); others rejected
    ...
    let mut out = vec![0u8; size];   // raw_size is known, so the output buffer is allocated once at the right size
    let n = DEC.with(|d| d.borrow_mut().zlib_decompress(z, &mut out)).expect("zlib decompression failed");
    out.truncate(n);
    out
}
```

libdeflate decompresses a whole buffer at once and is faster than streaming zlib or miniz. One decompressor per thread (thread-local)
avoids re-allocating its state.

### 4.4 Indexing and finding the node / way / relation ranges

```rust
pub fn index(f: &File) -> Result<Vec<BlobRef>, String> {
    // loop: read 4 bytes (header length), read the header, parse type and datasize, skip the data
}
```

Only the headers are read (two small reads per blob, 53,028 blobs for the planet, about 8 to 10 s). The data is skipped with the file
offset, never read. Then:

1. The first blob is the OSMHeader. `header_sorted` decodes it and checks for `Sort.Type_then_ID`.
2. If sorted, a binary search over the data blobs finds where ways and relations start. `blob_kind` decodes one blob and reports the
   kind of its first group, so the search decodes about 2 x 16 blobs.

```rust
fn first_of(f: &File, data: &[BlobRef], kind: u8) -> usize {
    let (mut lo, mut hi) = (0usize, data.len());
    while lo < hi {
        let mid = (lo + hi) / 2;
        let k = blob_kind(f, &data[mid]);
        if k < kind || k == K_OTHER && kind != K_OTHER { lo = mid + 1; } else { hi = mid; }
    }
    lo
}
// nodes_r = 0..w0, ways_r = w0..r0, rels_r = r0..n
```

For the planet: nodes are blobs 0..34192, ways 34192..52553, relations 52553..53028. Node blobs are about 70% of the file, so a pass over
ways or relations reads only a fraction of it.

3. The highest node id comes from the last node blob (14,225,519,257 for this planet), which sizes the bitmaps.

If the file is not sorted, every pass decodes every blob and ignores the groups of other kinds. Correct, but about 4 times slower.

### 4.5 Tag tests on string-table indices

Each block has its own string table. Instead of comparing strings for every tag of every element, the extractor finds the index of
each interesting key once per block:

```rust
struct K { railway: u32, highway: u32, natural: u32, landuse: u32, building: u32, pt: u32, construction: u32,
           construction_railway: u32, proposed: u32, type_: u32, route: u32, route_master: u32, name: u32, name_en: u32,
           ref_: u32, modes: [u32; 6] }

impl K {
    fn new(blk: &Block) -> Self {
        let mut k = K { railway: u32::MAX, /* ... all u32::MAX = not in this block */ };
        for (i, s) in blk.strings.iter().enumerate() {
            match *s { b"railway" => k.railway = i as u32, b"highway" => k.highway = i as u32, /* ... */ _ => {} }
        }
        k
    }
}
```

A tag lookup then scans an element's key ids for an integer, and only compares the value bytes when the key matches:

```rust
pub fn sep_get<'a>(blk: &Block<'a>, keys: &[u32], vals: &[u32], key: u32) -> Option<&'a [u8]> {
    for (i, &k) in keys.iter().enumerate() { if k == key { return vals.get(i).map(|&v| blk.s(v)); } }
    None
}
```

The rules are plain functions over a `get(key_id) -> Option<&[u8]>` closure, so the same rule works for dense nodes (interleaved
key/values) and ways or relations (separate lists):

```rust
fn way_cat<'a, F: Fn(u32) -> Option<&'a [u8]>>(get: &F, k: &K) -> u8 {
    let mut c = 0;
    if get(k.railway).is_some() || is(get(k.landuse), "railway") || /* ... */ { c |= RAIL; }
    match get(k.highway) {
        Some(b"motorway") | Some(b"trunk") | Some(b"primary") => c |= MAJOR,
        Some(b"secondary") | Some(b"tertiary") => c |= CORE,
        _ => {}
    }
    if is(get(k.natural), "coastline") { c |= COAST; }
    c
}
```

### 4.6 Group boxes as bitmasks

Boxes are integer 1e-7 degree rectangles. Each group has three: `ext` (box plus margin; decides what is kept), `bx` (the map box;
roads and coastline, matching the previous extractor) and `core`. An element's groups are a `u32` bitmask (bit i = group i), so up to
32 groups per run:

```rust
fn box_mask(groups: &[Group], lat: i64, lon: i64) -> u32 {
    let mut m = 0;
    for (i, g) in groups.iter().enumerate() { if inside(&g.ext, lat, lon) { m |= 1 << i; } }
    m
}
```

Testing 12 boxes per node is cheap compared with decoding. For hundreds of regions, replace this with a grid of tiles (section 8).

### 4.7 Pass 1: node blobs, mark nodes in any box

For each node blob in parallel: decode; for each node compute the box mask; if non-zero, set the node's bit in the `inbox` bitmap
(atomic `fetch_or`) and, if the node's tags pass `rail_node`, keep it (id, coordinates, mask, owned tags). Each blob also reports the
lowest and highest node id it contains (used by pass 4).

```rust
struct Bitmap { w: Vec<AtomicU64> }
impl Bitmap {
    fn set(&self, i: i64) { if let Some(x) = self.w.get((i as u64 / 64) as usize) { x.fetch_or(1 << (i as u64 % 64), Ordering::Relaxed); } }
    fn get(&self, i: i64) -> bool { /* load and test the bit */ }
    fn any_in(&self, lo: i64, hi: i64) -> bool { /* any non-zero word between lo/64 and hi/64 */ }
}
```

A bitmap over all node ids is 14.2 billion bits, 1.78 GB, and two are used. That is far smaller and faster than a hash set of the
hundreds of millions of node ids inside the boxes. Relaxed atomics are enough: bits are only set during the pass and only read after
it.

### 4.8 Pass 2: relation blobs

- Boundary relations are kept by id (the `boundaries` lists, combined into an id -> group mask map).
- Rail relations (routes, route masters, `type=public_transport` stop areas and their groups, station multipolygons) are kept only if a
  member node is in a box (`inbox`), or the relation has way members (decided later). This drops, for example, the many stop areas
  worldwide that only have node members outside every box.
- After the pass: the set of ways that are members of a wanted boundary (`bnd_ways`, with group masks) and of a station multipolygon
  (`mp_ways`, the untagged outlines). These must be kept in pass 3 even though their tags would not qualify.

Relations come before ways in this order of passes (although they come after ways in the file) because pass 3 needs to know which
untagged ways are boundary or outline members.

### 4.9 Pass 3: way blobs

For each way: compute its categories from tags (`RAIL`, `MAJOR`, `CORE`, `COAST`), add `BND` and `MPM` from the sets of pass 2. If no
category, skip it without decoding refs. Otherwise decode refs and keep the way if it is a boundary member, or if any of its nodes is in
`inbox`. For kept ways, set every node in the `needed` bitmap: complete geometry, including the parts outside the box (the previous
osmium "simple" cuts cut ways at the box edge). Tags are copied selectively: all tags for rail ways, only `highway`, `name`, `name:en`
and `ref` for roads, none for coastline and boundary ways.

### 4.10 Pass 4: node blobs again, only those that contain a needed node

The coordinates of the needed nodes are fetched from the node blobs. A blob is skipped entirely (not read, not decompressed) when the
`needed` bitmap has no set bit between the blob's lowest and highest id, known from pass 1:

```rust
let todo: Vec<&BlobRef> = data[nodes_r.clone()].iter().zip(blob_ids.iter())
    .filter(|(_, &(lo, hi))| lo <= hi && needed.any_in(lo, hi))
    .map(|(r, _)| r).collect();
```

For one group (Greater Bay Area) this skipped 12,388 of 34,192 node blobs. For all 12 groups at once only 186 are skipped, because node
ids are spread over the whole id range in every region.

Results are collected per blob in file order (`par_iter().map().collect()` preserves order), so the concatenated id list is already
sorted and lookups are binary searches over a `Vec<i64>` with a parallel `Vec<(i32, i32)>` of coordinates (14.4 million nodes for all
groups, about 230 MB).

### 4.11 Assembly

In parallel over kept ways: look up each ref's coordinates, build the geometry, and compute three group masks from the points: `bmask`
(box plus margin), `mmask` (map box) and `cmask` (core box, only for secondary and tertiary roads).

Relation masks: a rail relation belongs to the groups of its members (tagged nodes, rail and outline ways). Route masters and stop-area
groups have relations as members, so masks are propagated in two rounds.

### 4.12 Output

Each group's two files are written in parallel (one rayon task per group) with a hand-written JSON writer into a 1 MB `BufWriter`. No
serde serialization of large structures; coordinates are formatted from integers:

```rust
fn coord<W: Write>(w: &mut W, v: i32) -> io::Result<()> {   // 1e-7 degrees -> 6 decimals, rounded half away from zero
    let v = v as i64;
    let r = if v >= 0 { (v + 5) / 10 } else { (v - 5) / 10 };
    let a = r.abs();
    write!(w, "{}{}.{:06}", if r < 0 { "-" } else { "" }, a / 1_000_000, a % 1_000_000)
}
```

Line formats (one JSON object per line):

```
rail_<g>.jsonl
{"t":"n","id":123,"p":[lat,lon],"k":{...tags}}
{"t":"w","id":456,"k":{...tags},"g":[[lat,lon],...],"n":[node ids aligned with g]}
{"t":"r","id":789,"k":{...tags},"m":[["n",123,"stop"],["w",456,""],...]}

geo_<g>.jsonl
{"id":456,"t":"h","h":"primary","n":"name","ne":"name:en","ref":"A1","g":[[lat,lon],...]}   roads
{"id":...,"t":"c",...}                                                                     coastline
{"id":...,"t":"b",...}                                                                     boundary member way

bnd_rels.json   {"<relation id>": {"k": {...tags}, "m": [["w", 456, "outer"], ...]}}   merged into the existing file
```

The `"n"` list on rail ways lets the station model rebuild node topology (track ends, shared nodes) without a separate extract.

### 4.13 Timings of one full run (all 12 groups)

```
[    8.8s] indexed 53028 blobs (nodes 0..34192, ways 34192..52553, relations 52553..53028); max node id 14225519257
[   39.3s] pass 1 (nodes): 247280 tagged rail / public-transport nodes
[   39.9s] pass 2 (relations): 282051 kept (93 boundaries), 5155 boundary ways, 44210 station outline ways
[   54.4s] pass 3 (ways): 1718680 kept
[   78.5s] pass 4 (nodes): coordinates of 14424723 nodes (186 node blobs skipped, no needed node in them)
[   78.8s] assembled geometry and group membership
[   79.1s] done
```

`/usr/bin/time -l`: 82 s wall, 694 s user, 106 s system, 4.65 GB maximum resident set. Average CPU use is about 10 of 14 cores; the
node passes are bound by read throughput.

### 4.14 Limits and assumptions

- zlib or raw blobs only (the planet and Geofabrik extracts are zlib).
- Sorted input for the fast path; unsorted input works but decodes every blob in every pass.
- At most 32 groups per run (bitmask width). Use `u64` masks for 64, or tiles for more.
- Bitmap memory grows with the highest node id: about 1.8 GB each at 14.2 billion.
- Untagged nodes are not emitted on their own; their coordinates are inside the ways that use them.
- Rail relations with way members are collected worldwide in pass 2 (about 280,000 records). Fine for this project; filter them earlier
  if memory matters.

---

## 5. The build (Python): the optimizations and why they work

The build turns a group's jsonl into region files: projection, clipping to the city, line grouping, network lengths, stations, roads.
It uses shapely 2 and pyproj. The first step was profiling with `python -m cProfile -o out.prof build.py` and reading the top
functions by self time and their callers:

```python
import pstats
p = pstats.Stats('out.prof')
for (f, l, fn), (cc, nc, tt, ct, callers) in sorted(p.stats.items(), key=lambda kv: -kv[1][2])[:8]:
    print(f'{tt:7.1f}s {nc:9} {f}:{l} {fn}')
    for c, v in sorted(callers.items(), key=lambda kv: -kv[1][2])[:3]: print('     <-', c, round(v[2], 1))
```

Greater Bay Area, before: 339 s, of which shapely `intersection` 178 s (124,030 calls), `intersects` 86 s (114,207 calls), `buffer`
9 s, point interpolation and coordinate access about 20 s, station-name normalisation 10 s.

### 5.1 Prepare the big polygons, and make the prepared one the caller

Every rail segment and road is tested against a few very large polygons (the city land, the land plus a buffer). Unprepared, each
test walks the whole polygon. Preparing builds GEOS's spatial index once:

```python
import shapely
for g_ in (landm, landbuf_s, land_prep): shapely.prepare(g_)
```

The prepared geometry must be the one whose method is called. `land_prep.intersects(part)` uses the index;
`part.intersects(land_prep)` does not and is as slow as before. Flipping two such calls removed 81 s of `intersects` on its own.

### 5.2 Skip intersections for pieces wholly inside or outside

```python
def clip(g_, poly):
    """the same as geom_lines(g_.intersection(poly)), without the intersection when g_ lies wholly inside or outside poly"""
    if poly.contains(g_): return [g_]          # prepared contains: fast
    if not poly.intersects(g_): return []      # prepared intersects: fast
    return geom_lines(g_.intersection(poly))   # only pieces crossing the boundary pay for an intersection
```

Most segments are wholly inside or outside, so most intersections disappear.

### 5.3 Clip each segment once

The same segment was clipped against the city in three places (line selection, line length, network totals). A small cache keyed by
the segment object computes it once:

```python
_in_city = {}
def in_city(part):
    r = _in_city.get(id(part))
    if r is None: r = _in_city[id(part)] = clip(part, landm)
    return r
```

(The segments stay alive in a list for the whole region, so `id()` is stable.)

### 5.4 Build loop-invariant geometry once

`landbuf_s.buffer(500)` was computed inside a loop over route stops (11,046 buffers of a region-sized polygon). Computing it once,
prepared, outside the loop removed it from the profile. The same pattern in the station model (`legal.buffer(600)` per station) saved
another 14 s.

### 5.5 Query trees with a square, not a circle

`tree.query(point.buffer(d))` builds a 64-segment circle polygon per query only to take its bounding box. A box with the same extent
gives exactly the same candidates:

```python
def near_box(p, d):
    return box(p.x - d, p.y - d, p.x + d, p.y + d)
tree.query(near_box(pt, 700))   # instead of tree.query(pt.buffer(700))
```

### 5.6 Vectorize the per-sample loop

Network length samples every line every 20 m (1.8 million samples for the Greater Bay Area), one `interpolate` call per sample. One
vectorized call per line produces exactly the same points:

```python
pts = shapely.get_coordinates(shapely.line_interpolate_point(L, (np.arange(n) + 0.5) * seg)).tolist()
for px, py in pts:
    ...   # same dedupe grid as before
```

### 5.7 Cache pure functions called millions of times

```python
@lru_cache(maxsize=None)
def norm_st(s): ...      # 3.3 million calls, a few thousand distinct names
```

### 5.8 Results of 5.1 to 5.7, Greater Bay Area build

| change | time |
|---|---|
| before | 339 s |
| prepare, clip(), clip once, buffer once | 124 s |
| prepared polygon as caller, square tree queries | 43 s |
| vectorized sampling | 25.6 s |

Each step was checked byte for byte against the previous output. The one difference found (a station whose line list depended on dict
order when two lines were equally near) was made deterministic by sorting on `(distance, line index)`.

### 5.9 Load only what a process builds

Each build process loaded all 12 groups (480 MB of jsonl) and built the land masks of all coastal groups, whatever region it built.
Zurich, the smallest region, took 24 s, almost all of it loading. Now the build first works out which groups its regions need, loads
only those, and builds land masks on first use:

```python
NEED = sorted({R['group'] for R in REGIONS if not ONLY or R['id'] in ONLY.split(',')})
new_regions.load(D, TRACK, RRELS, STOPS, CONS, groups=NEED)

_LAND = {}
def land_of(g):
    if g not in _LAND: _LAND[g] = None if g in INLAND else land_from_coast(D['coast_' + g], BB[g])
    return _LAND[g]
```

Zurich: 24 s to 2.8 s.

### 5.10 One process per group, under a memory budget

`run_all.py` starts one process per group, building all of that group's regions in sequence (`ONLY=sf,bay`), so shared data is loaded
once. A slot budget limits concurrency by memory: a group with a big region costs 2 slots, others 1, default 4 slots, big groups
first:

```python
def run(g, ids, big):
    w = min(2 if big else 1, SLOTS)
    with cv:
        while free < w: cv.wait()
        free -= w
    subprocess.run([sys.executable, 'build.py'], env={**os.environ, 'ONLY': ','.join(ids)}, ...)
    with cv:
        free += w; cv.notify_all()
```

`index.json` (stats of all regions) is merged by each process under an `fcntl` lock so parallel builds do not overwrite each other.

All 21 regions: about 9 min to 76 s.

### 5.11 The station model

Profiled the same way: 185 s under the profiler, 40.6 million `intersects` calls testing every track against every station's
footprint. An R-tree over the tracks returns only the candidates, in the original order so the output stays identical:

```python
def track_index(tracks):
    ids = list(tracks)
    tree = STRtree([tracks[w][0] for w in ids]) if ids else None
    def tracks_near(geom):
        if tree is None: return []
        return [(ids[i], tracks[ids[i]][0], tracks[ids[i]][1]) for i in sorted(tree.query(geom, predicate='intersects'))]
    return tracks_near
```

Plus 5.4 (one buffer) and 5.7 (name cache): 142 s to 21 s, byte-identical output.

---

## 6. Orchestration

`pipeline/refresh.sh`:

```bash
pipeline/refresh.sh                  # everything: extract all groups, build all regions, station models (about 3 minutes)
pipeline/refresh.sh prd hk,sz        # one-off: extract one group, build some regions
```

Steps: `cargo build --release` (a no-op when unchanged), `ra-extract` for the given groups (or all), `run_all.py` for the given
regions (or all), `stations.py` for the regions in `RA_STATIONS`. Environment: `RA_PLANET`, `PYTHON`, `RA_STATIONS`, `RA_SLOTS`.

Monthly refresh: download the new planet, set `RA_PLANET`, run `refresh.sh`, look at the site, run `tests/smoke.py`.

New city: add a group to `groups.json` and a region to `REGIONS`, then `refresh.sh <group> <region>`. A one-group extraction still
reads the whole planet (about 65 s); extracting all groups at once costs about the same.

---

## 7. How the new pipeline was verified

1. **Byte-identical where nothing should change.** Every build and station-model optimization was compared with `cmp` against the
   previous output for the same input.
2. **Element-level diff of the extractor.** For one group, the old and new jsonl were loaded and compared by element id and type:
   counts of shared, only-old and only-new elements, and for shared ways whether geometry was identical, identical up to rounding, or
   longer (no longer cut at the box). Every difference was traced to a cause before accepting it:
   - roads, coastline and boundary ways identical (Greater Bay Area);
   - shared rail ways identical, or within 0.000001 degree (rounding differs between Python's `round` and the integer rounding);
   - 59 ways complete where the box cut had truncated them; 84 more train routes (kept because a stop is in the box);
   - bus stops and bus stop areas were leaking in through the `public_transport` tag: fixed by requiring a rail mode tag;
   - roads had been taken with the margin: fixed by using the map box, as before;
   - objects only in the old output were ones nothing reads (crossing nodes, `landuse=railway` multipolygons, signalling relations).
3. **Region stats, all regions.** Land area, stations, lines, network km and file size, old against new. Land and stations identical,
   network km within 0.3 km.
4. **Visual side-by-side.** The old site served from a copy on another port; the same views (whole region, city, a zoomed station area,
   the streets layer) rendered headless in Chrome with Playwright and pasted side by side with Pillow. Differences were explained (for
   example Tokyo: the old extract had marked 16 stations as high-speed stops wrongly; the new one marks only Tokyo, Ueno and Shinagawa).
5. **Checks printed by every build.** Stops with no drawn track within 300 m, and route members dropped from the extract; a test hook
   (`RA_TEST_DROP_WAYS=id,id`) removes ways on purpose to show the check firing.

---

## 8. Pitfalls and lessons (with the numbers that taught them)

- **Do not run planet cuts in parallel.** Seven `osmium extract` jobs at once took 55 minutes; one alone takes 109 s. Seven readers of
  the same 95 GB file fight over the disk and the page cache. One multi-threaded process reading once is the right shape.
- **osmium's multi-box extract and `complete_ways` strategy were slow at this scale** (about 50 minutes, and over 58 minutes stopped).
  Not profiled; avoided by the design above.
- **Memory-mapping a file much larger than RAM, read from many threads, was I/O bound** (174 s, 221 s system time). Positioned reads
  fixed it.
- **Never let a temporary directory hold expensive intermediate results.** A crash after a 50-minute cut deleted the cut along with the
  `TemporaryDirectory`. Keep slow artifacts in a persistent cache folder with a "done" marker.
- **Box cuts clip boundaries.** County boundaries crossing the box edge lost land (Bay Area: 701 km2 missing). Fetch boundary relations
  whole, by id.
- **`GROUPS` is a reserved bash variable** (it holds the user's group ids), so `GROUPS="${1:-}"` silently becomes `20`. Use another name.
- **Count elements, not just totals, when comparing pipelines.** A station count that "barely changed" hid 55 Hong Kong metro stations
  that had disappeared (stations mapped only as areas, which the adapter did not read).
- **Prepared geometries only help as the caller** (5.1).
- **Profile before optimizing.** The build's time was not where it was expected (corridor sampling was 30 s; geometry predicates were
  264 s).
- **For one-off additions, a one-group extraction costs a full planet read.** For a few dozen cities that is fine (about 65 s). For
  hundreds, or for very frequent additions, build a tile cache once per planet: run the same passes with no box (all of the world, the
  wanted tags only), store ways with inline coordinates in 1-degree tiles, and serve each region from the tiles it overlaps.

---

## 9. Porting checklist for another project

1. Copy `extract/` (Cargo.toml, `src/pbf.rs`, `src/main.rs`). `pbf.rs` needs no changes.
2. In `main.rs`, change the selection to your project:
   - the keys you test: fields of `K` and the `match` in `K::new` (any key you call `get(k.<key>)` on must be listed there);
   - `rail_node` (which tagged nodes to keep), `way_cat` (way categories; add bits as needed, up to 8 in a `u8`), `rail_rel`
     (which relations to keep, and which ones need their untagged member ways, like station multipolygons);
   - which tags to copy for each category (`tags = if cat & ...` in pass 3) and which categories go to which output file;
   - the output writers in the per-group block, if your consumer wants a different format.
3. Keep the pass structure: nodes (inbox) -> relations -> ways (needed) -> nodes (coordinates). Only reorder if you do not need relation
   membership to keep ways.
4. Write your `groups.json` (boxes, cores, boundary ids) or replace `load_config` with your own region source.
5. Build: `cargo build --release` (Rust 1.99 used here; crates rayon 1.12, libdeflater 1.26, rustc-hash 2.1, serde_json 1.0). The
   release profile uses `lto = "fat"`, `codegen-units = 1`, `panic = "abort"`.
6. Run on one small group first and compare with your current output element by element (section 7, point 2) before switching.
7. In your Python (or other) build, apply section 5: profile, prepare big polygons and call through them, clip only boundary-crossing
   pieces, compute loop invariants once, square tree queries, vectorize per-sample loops, cache pure functions, load only what a
   process builds, one process per data group under a memory budget.
8. Keep the old pipeline until a visual side-by-side and a stats comparison are clean, then delete it.

---

## 10. Reference

```toml
# extract/Cargo.toml
[package]
name = "ra-extract"
version = "0.1.0"
edition = "2021"

[dependencies]
rayon = "1"            # parallel blob decoding
libdeflater = "1"      # fastest zlib decompression into a pre-sized buffer
rustc-hash = "2"       # fast hash maps for id lookups
serde_json = "1"       # reading groups.json and merging bnd_rels.json

[profile.release]
opt-level = 3
lto = "fat"
codegen-units = 1
panic = "abort"
```

```
ra-extract --planet PLANET.osm.pbf [--config pipeline/groups.json] [--out run2] [--groups a,b] [--threads N]
```

Data sources and licence: OpenStreetMap, ODbL; keep the attribution in anything built from the output.
