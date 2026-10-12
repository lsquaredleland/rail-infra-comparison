"""Build all regions in parallel: one process per group, building all of that group's regions in sequence, so a group's
data (rail, roads, boundaries) is loaded once (San Francisco and the Bay Area, Tokyo and its 23 wards, ... share one group).
A memory budget limits how many processes run at once: a group with a big region costs 2 slots, others 1 (default 4 slots).

    python3 run_all.py              # every region
    python3 run_all.py hk,sz,zh     # some regions (grouped the same way)
    RA_SLOTS=6 python3 run_all.py   # a larger budget
"""
import os, re, subprocess, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, 'build.py')).read()
regions = []   # (id, group, big)
for m in re.finditer(r"dict\(id='(\w+)'.*?\n(?:.*\n)*?.*?streets=\d(?:, big=True)?\)", src):
    block = m.group(0)
    regions.append((m.group(1), re.search(r"group='(\w+)'", block).group(1), 'big=True' in block))
want = sys.argv[1].split(',') if len(sys.argv) > 1 else None
groups = {}
for rid, g, big in regions:
    if not want or rid in want:
        ids, b = groups.get(g, ([], False))
        groups[g] = (ids + [rid], b or big)
jobs = sorted(groups.items(), key=lambda kv: not kv[1][1])   # groups with a big region first
SLOTS = int(os.environ.get('RA_SLOTS', 4))
cv = threading.Condition(); free = SLOTS; failed = []
def run(g, ids, big):
    global free
    w = min(2 if big else 1, SLOTS)
    with cv:
        while free < w: cv.wait()
        free -= w
    t = time.time()
    p = subprocess.run([sys.executable, 'build.py'], cwd=HERE, env={**os.environ, 'ONLY': ','.join(ids)}, capture_output=True, text=True)
    print(f"{g} ({', '.join(ids)}): {'ok' if p.returncode == 0 else 'FAILED'} {time.time() - t:.0f}s", flush=True)
    if p.returncode: failed.extend(ids); print(p.stderr[-1500:])
    for line in p.stdout.splitlines():
        if line.startswith(('CHECK', 'MERGE')): print('  ' + line[:330], flush=True)   # route / stop checks from build.py (details in pipeline/qa/<id>.json)
    with cv:
        free += w; cv.notify_all()
t0 = time.time()
ts = [threading.Thread(target=run, args=(g, ids, big)) for g, (ids, big) in jobs]
for t in ts: t.start()
for t in ts: t.join()
print(f'failed: {failed or "none"}  ({time.time() - t0:.0f}s in all)')
