//! Minimal, allocation-light reader for OSM PBF files (https://wiki.openstreetmap.org/wiki/PBF_Format).
//!
//! The file is a sequence of [4-byte big-endian header length][BlobHeader][Blob]. Blobs are zlib-compressed
//! PrimitiveBlocks. We index the blobs by reading only the small headers (the data is skipped), so later passes can
//! decode just the node, way or relation blobs they need, in parallel.

use libdeflater::Decompressor;
use std::cell::RefCell;
use std::fs::File;
use std::os::unix::fs::FileExt;

// ---------------------------------------------------------------- protobuf wire format

pub struct Pb<'a> {
    b: &'a [u8],
    p: usize,
}

impl<'a> Pb<'a> {
    #[inline]
    pub fn new(b: &'a [u8]) -> Self {
        Pb { b, p: 0 }
    }
    #[inline]
    pub fn done(&self) -> bool {
        self.p >= self.b.len()
    }
    #[inline]
    pub fn varint(&mut self) -> u64 {
        let mut r = 0u64;
        let mut s = 0u32;
        loop {
            let c = self.b[self.p];
            self.p += 1;
            r |= ((c & 0x7f) as u64) << s;
            if c < 0x80 {
                return r;
            }
            s += 7;
        }
    }
    #[inline]
    pub fn key(&mut self) -> (u32, u8) {
        let k = self.varint();
        ((k >> 3) as u32, (k & 7) as u8)
    }
    #[inline]
    pub fn bytes(&mut self) -> &'a [u8] {
        let l = self.varint() as usize;
        let s = &self.b[self.p..self.p + l];
        self.p += l;
        s
    }
    #[inline]
    pub fn skip(&mut self, wt: u8) {
        match wt {
            0 => {
                self.varint();
            }
            1 => self.p += 8,
            2 => {
                let l = self.varint() as usize;
                self.p += l;
            }
            5 => self.p += 4,
            _ => panic!("unsupported protobuf wire type {wt}"),
        }
    }
}

/// zigzag decoding of sint32/sint64
#[inline]
pub fn zz(n: u64) -> i64 {
    ((n >> 1) as i64) ^ -((n & 1) as i64)
}

/// append the varints of a packed field to `out`
#[inline]
fn push_packed_u32(b: &[u8], out: &mut Vec<u32>) {
    let mut p = Pb::new(b);
    while !p.done() {
        out.push(p.varint() as u32);
    }
}

/// decode a packed, delta-coded sint64 field into `out` (cleared first)
#[inline]
pub fn unpack_delta(b: &[u8], out: &mut Vec<i64>) {
    out.clear();
    let mut p = Pb::new(b);
    let mut acc = 0i64;
    while !p.done() {
        acc += zz(p.varint());
        out.push(acc);
    }
}

// ---------------------------------------------------------------- blob index

#[derive(Clone, Copy, Debug)]
pub struct BlobRef {
    pub off: usize,
    pub len: usize,
    pub data: bool, // OSMData (true) or OSMHeader (false)
}

/// Walk the blob headers with small positioned reads; the blob data itself is not read.
pub fn index(f: &File) -> Result<Vec<BlobRef>, String> {
    let len = f.metadata().map_err(|e| e.to_string())?.len() as usize;
    let mut v = Vec::new();
    let mut pos = 0usize;
    let mut hb = Vec::new();
    while pos + 4 <= len {
        let mut l4 = [0u8; 4];
        f.read_exact_at(&mut l4, pos as u64).map_err(|e| e.to_string())?;
        let hl = u32::from_be_bytes(l4) as usize;
        hb.resize(hl, 0);
        f.read_exact_at(&mut hb, pos as u64 + 4).map_err(|e| format!("truncated blob header at byte {pos}: {e}"))?;
        let mut pb = Pb::new(&hb);
        let mut is_data = false;
        let mut ds = 0usize;
        while !pb.done() {
            let (fno, wt) = pb.key();
            match (fno, wt) {
                (1, 2) => is_data = pb.bytes() == b"OSMData",
                (3, 0) => ds = pb.varint() as usize,
                _ => pb.skip(wt),
            }
        }
        let off = pos + 4 + hl;
        if off + ds > len {
            return Err(format!("truncated blob at byte {off}"));
        }
        v.push(BlobRef { off, len: ds, data: is_data });
        pos = off + ds;
    }
    Ok(v)
}

/// read one blob (compressed) with a single positioned read
pub fn read_blob(f: &File, r: &BlobRef) -> Vec<u8> {
    let mut b = vec![0u8; r.len];
    f.read_exact_at(&mut b, r.off as u64).expect("read blob");
    b
}

thread_local! {
    static DEC: RefCell<Decompressor> = RefCell::new(Decompressor::new());
}

/// Decompress a Blob message into a fresh buffer (zlib or raw; other codecs are rejected).
pub fn decode(blob: &[u8]) -> Vec<u8> {
    let mut pb = Pb::new(blob);
    let (mut raw, mut z, mut size) = (None, None, 0usize);
    while !pb.done() {
        let (f, wt) = pb.key();
        match (f, wt) {
            (1, 2) => raw = Some(pb.bytes()),
            (2, 0) => size = pb.varint() as usize,
            (3, 2) => z = Some(pb.bytes()),
            (4..=7, 2) => panic!("blob compression other than zlib (lzma, bzip2, lz4, zstd) is not supported"),
            _ => pb.skip(wt),
        }
    }
    if let Some(r) = raw {
        return r.to_vec();
    }
    let z = z.expect("blob without data");
    let mut out = vec![0u8; size];
    let n = DEC
        .with(|d| d.borrow_mut().zlib_decompress(z, &mut out))
        .expect("zlib decompression failed");
    out.truncate(n);
    out
}

/// Does the OSMHeader say the file is sorted by type then id (nodes, then ways, then relations)? The planet is.
pub fn header_sorted(blob: &[u8]) -> bool {
    let raw = decode(blob);
    let mut pb = Pb::new(&raw);
    while !pb.done() {
        let (f, wt) = pb.key();
        if (f == 4 || f == 5) && wt == 2 {
            if pb.bytes() == b"Sort.Type_then_ID" {
                return true;
            }
        } else {
            pb.skip(wt);
        }
    }
    false
}

// ---------------------------------------------------------------- primitive blocks

pub struct Block<'a> {
    pub strings: Vec<&'a [u8]>,
    pub groups: Vec<&'a [u8]>,
    pub gran: i64,
    pub lat_off: i64,
    pub lon_off: i64,
}

impl<'a> Block<'a> {
    pub fn parse(b: &'a [u8]) -> Self {
        let mut blk = Block { strings: Vec::new(), groups: Vec::new(), gran: 100, lat_off: 0, lon_off: 0 };
        let mut pb = Pb::new(b);
        while !pb.done() {
            let (f, wt) = pb.key();
            match (f, wt) {
                (1, 2) => {
                    let mut st = Pb::new(pb.bytes());
                    while !st.done() {
                        let (f2, wt2) = st.key();
                        if f2 == 1 && wt2 == 2 {
                            blk.strings.push(st.bytes());
                        } else {
                            st.skip(wt2);
                        }
                    }
                }
                (2, 2) => blk.groups.push(pb.bytes()),
                (17, 0) => blk.gran = pb.varint() as i64,
                (19, 0) => blk.lat_off = pb.varint() as i64,
                (20, 0) => blk.lon_off = pb.varint() as i64,
                _ => pb.skip(wt),
            }
        }
        blk
    }
    #[inline]
    pub fn s(&self, i: u32) -> &'a [u8] {
        self.strings.get(i as usize).copied().unwrap_or(b"")
    }
}

pub const K_NODES: u8 = 0;
pub const K_WAYS: u8 = 1;
pub const K_RELS: u8 = 2;
pub const K_OTHER: u8 = 3;

/// element type of a PrimitiveGroup
pub fn group_kind(g: &[u8]) -> u8 {
    let mut pb = Pb::new(g);
    while !pb.done() {
        let (f, wt) = pb.key();
        match f {
            1 | 2 => return K_NODES,
            3 => return K_WAYS,
            4 => return K_RELS,
            _ => pb.skip(wt),
        }
    }
    K_OTHER
}

/// element type of the first group of a blob (used to find where nodes, ways and relations start in a sorted file)
pub fn blob_kind(f: &File, r: &BlobRef) -> u8 {
    let raw = decode(&read_blob(f, r));
    let blk = Block::parse(&raw);
    blk.groups.first().map(|g| group_kind(g)).unwrap_or(K_OTHER)
}

/// Every node of a group (dense or plain): f(id, lat_nano, lon_nano, interleaved key/value string ids).
/// Coordinates are in nanodegrees (lat_offset + granularity * value).
pub fn for_each_node<F: FnMut(i64, i64, i64, &[u32])>(blk: &Block, g: &[u8], kvbuf: &mut Vec<u32>, mut f: F) {
    let mut pb = Pb::new(g);
    while !pb.done() {
        let (fno, wt) = pb.key();
        match (fno, wt) {
            (2, 2) => {
                // DenseNodes
                let d = pb.bytes();
                let mut q = Pb::new(d);
                let (mut ids, mut lats, mut lons, mut kv): (&[u8], &[u8], &[u8], &[u8]) = (b"", b"", b"", b"");
                while !q.done() {
                    let (f2, wt2) = q.key();
                    match (f2, wt2) {
                        (1, 2) => ids = q.bytes(),
                        (8, 2) => lats = q.bytes(),
                        (9, 2) => lons = q.bytes(),
                        (10, 2) => kv = q.bytes(),
                        _ => q.skip(wt2),
                    }
                }
                kvbuf.clear();
                push_packed_u32(kv, kvbuf);
                let (mut pi, mut pla, mut plo) = (Pb::new(ids), Pb::new(lats), Pb::new(lons));
                let (mut id, mut la, mut lo) = (0i64, 0i64, 0i64);
                let mut ki = 0usize;
                while !pi.done() {
                    id += zz(pi.varint());
                    la += zz(pla.varint());
                    lo += zz(plo.varint());
                    let start = ki;
                    while ki < kvbuf.len() && kvbuf[ki] != 0 {
                        ki += 2;
                    }
                    let end = ki.min(kvbuf.len());
                    if ki < kvbuf.len() {
                        ki += 1; // the 0 that ends this node's tags
                    }
                    f(id, blk.lat_off + blk.gran * la, blk.lon_off + blk.gran * lo, &kvbuf[start..end]);
                }
            }
            (1, 2) => {
                // a plain Node (rare in planet files)
                let m = pb.bytes();
                let mut q = Pb::new(m);
                let (mut id, mut la, mut lo) = (0i64, 0i64, 0i64);
                let (mut keys, mut vals) = (Vec::new(), Vec::new());
                while !q.done() {
                    let (f2, wt2) = q.key();
                    match (f2, wt2) {
                        (1, 0) => id = zz(q.varint()),
                        (2, 2) => push_packed_u32(q.bytes(), &mut keys),
                        (3, 2) => push_packed_u32(q.bytes(), &mut vals),
                        (8, 0) => la = zz(q.varint()),
                        (9, 0) => lo = zz(q.varint()),
                        _ => q.skip(wt2),
                    }
                }
                let kv: Vec<u32> = keys.iter().zip(vals.iter()).flat_map(|(k, v)| [*k, *v]).collect();
                f(id, blk.lat_off + blk.gran * la, blk.lon_off + blk.gran * lo, &kv);
            }
            _ => pb.skip(wt),
        }
    }
}

/// Every way of a group: f(id, key ids, value ids, raw packed refs). Refs are decoded by the caller only when needed.
pub fn for_each_way<'a, F: FnMut(i64, &[u32], &[u32], &'a [u8])>(g: &'a [u8], keys: &mut Vec<u32>, vals: &mut Vec<u32>, mut f: F) {
    let mut pb = Pb::new(g);
    while !pb.done() {
        let (fno, wt) = pb.key();
        if fno == 3 && wt == 2 {
            let m = pb.bytes();
            let mut w = Pb::new(m);
            let mut id = 0i64;
            let mut refs: &'a [u8] = b"";
            keys.clear();
            vals.clear();
            while !w.done() {
                let (f2, wt2) = w.key();
                match (f2, wt2) {
                    (1, 0) => id = w.varint() as i64,
                    (2, 2) => push_packed_u32(w.bytes(), keys),
                    (2, 0) => keys.push(w.varint() as u32),
                    (3, 2) => push_packed_u32(w.bytes(), vals),
                    (3, 0) => vals.push(w.varint() as u32),
                    (8, 2) => refs = w.bytes(),
                    _ => w.skip(wt2),
                }
            }
            f(id, &keys[..], &vals[..], refs);
        } else {
            pb.skip(wt);
        }
    }
}

pub struct RelBufs {
    pub keys: Vec<u32>,
    pub vals: Vec<u32>,
    pub roles: Vec<u32>,
    pub types: Vec<u32>,
    pub memids: Vec<i64>,
}

impl RelBufs {
    pub fn new() -> Self {
        RelBufs { keys: Vec::new(), vals: Vec::new(), roles: Vec::new(), types: Vec::new(), memids: Vec::new() }
    }
}

/// Every relation of a group: f(id, bufs) with keys, vals, member roles (string ids), member types (0 node, 1 way, 2 relation), member ids.
pub fn for_each_relation<F: FnMut(i64, &RelBufs)>(g: &[u8], b: &mut RelBufs, mut f: F) {
    let mut pb = Pb::new(g);
    while !pb.done() {
        let (fno, wt) = pb.key();
        if fno == 4 && wt == 2 {
            let m = pb.bytes();
            let mut r = Pb::new(m);
            let mut id = 0i64;
            b.keys.clear();
            b.vals.clear();
            b.roles.clear();
            b.types.clear();
            b.memids.clear();
            let mut memraw: &[u8] = b"";
            while !r.done() {
                let (f2, wt2) = r.key();
                match (f2, wt2) {
                    (1, 0) => id = r.varint() as i64,
                    (2, 2) => push_packed_u32(r.bytes(), &mut b.keys),
                    (3, 2) => push_packed_u32(r.bytes(), &mut b.vals),
                    (8, 2) => push_packed_u32(r.bytes(), &mut b.roles),
                    (9, 2) => memraw = r.bytes(),
                    (10, 2) => push_packed_u32(r.bytes(), &mut b.types),
                    _ => r.skip(wt2),
                }
            }
            unpack_delta(memraw, &mut b.memids);
            f(id, b);
        } else {
            pb.skip(wt);
        }
    }
}

/// value of `key` in interleaved key/value ids (dense nodes)
#[inline]
pub fn kv_get<'a>(blk: &Block<'a>, kv: &[u32], key: u32) -> Option<&'a [u8]> {
    let mut i = 0;
    while i + 1 < kv.len() {
        if kv[i] == key {
            return Some(blk.s(kv[i + 1]));
        }
        i += 2;
    }
    None
}

/// value of `key` in separate key and value id lists (ways, relations)
#[inline]
pub fn sep_get<'a>(blk: &Block<'a>, keys: &[u32], vals: &[u32], key: u32) -> Option<&'a [u8]> {
    for (i, &k) in keys.iter().enumerate() {
        if k == key {
            return vals.get(i).map(|&v| blk.s(v));
        }
    }
    None
}
