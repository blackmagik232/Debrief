"""Condition waits for the Playwright tests (instead of fixed sleeps).

settle(pg) returns once the page has loaded, two frames have been painted and the DOM has had no changes for
`quiet` ms (longer than the pages' own 120 ms resize-redraw debounce), or after `cap` ms at the latest, so a
page that never goes quiet behaves as a fixed wait did.
"""
from __future__ import annotations

_JS = """([quiet, cap]) => new Promise(done => {
  let t = null, end = null;
  const finish = () => { clearTimeout(t); clearTimeout(end); obs.disconnect(); done(true); };
  const obs = new MutationObserver(() => { clearTimeout(t); t = setTimeout(finish, quiet); });
  obs.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  end = setTimeout(finish, cap);
  requestAnimationFrame(() => requestAnimationFrame(() => { clearTimeout(t); t = setTimeout(finish, quiet); }));
})"""


def settle(pg, cap_ms: int = 2000, quiet: int = 200) -> None:
    pg.wait_for_load_state("load")
    pg.evaluate(_JS, [quiet, max(cap_ms, quiet * 2)])
