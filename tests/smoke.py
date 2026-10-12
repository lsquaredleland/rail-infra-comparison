"""Browser smoke test: loads the site in Chrome (Playwright), opens a few regions and checks that they draw without errors,
that stations and lines carry built dates, that high-speed stops follow config.js, and that the line-color button works.

    cd site && python3 -m http.server 8765 &        # in another shell
    pip install -r requirements-dev.txt
    python3 tests/smoke.py [http://localhost:8765/index.html]
"""
import sys
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:8765/index.html'
REGIONS = ['sf', 'hk', 'sz', 'tky23', 'paris', 'zh']

def check(cond, msg):
    print(('ok   ' if cond else 'FAIL ') + msg)
    return cond

with sync_playwright() as p:
    b = p.chromium.launch(channel='chrome', headless=True)
    pg = b.new_page(viewport={'width': 1300, 'height': 800})
    errs = []
    pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto(URL); pg.wait_for_timeout(2500)
    pg.click('#slots .x >> nth=1'); pg.wait_for_timeout(300)
    ok = True
    for rid in REGIONS:
        pg.select_option('#slots select >> nth=0', rid); pg.wait_for_timeout(2500)
        n = pg.evaluate("(rid) => ({ st: cache[rid].stations.length, lines: cache[rid].lines.length, dated: cache[rid].lines.filter(l => l.built).length })", rid)
        ok &= check(n['st'] > 0 and n['lines'] > 0, f"{rid}: {n['st']} stations, {n['lines']} lines, {n['dated']} lines with a built date")
    # tram-like stops are hidden when zoomed out, drawn when zoomed in
    pg.select_option('#slots select >> nth=0', 'sf'); pg.wait_for_timeout(2500)
    def drawn(scale):
        pg.evaluate("(s) => { const c = cache.sf; state.view.cx = c.centroid[0]; state.view.cy = c.centroid[1]; state.view.r = 0; state.view.s = s; draw(); }", scale); pg.wait_for_timeout(400)
        return pg.evaluate("hitList.filter(h => h.st.tramlike).length")
    ok &= check(drawn(0.03) == 0, 'tram stops hidden at scale 0.03')
    ok &= check(drawn(0.12) > 0, 'tram stops drawn at scale 0.12')
    # line-color button and checkbox stay in sync
    pg.click('#lcol'); ok &= check(pg.get_attribute('#lcol', 'aria-pressed') == 'false' and not pg.evaluate("document.getElementById('official').checked"), 'line-color button turns colors off')
    pg.click('#lcol'); ok &= check(pg.get_attribute('#lcol', 'aria-pressed') == 'true', 'and on again')
    ok &= check(not errs, f'no page errors {errs[:2]}')
    b.close()
    sys.exit(0 if ok else 1)
