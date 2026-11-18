// Heat shading for comparable numbers, after Practical Data (github.com/philrenaud/practical-data, MIT): each
// metric is ranked across harnesses, 1 = the better end, and the cell is tinted toward a good or bad pole. Middling
// values stay plain (a "fade" ramp), and the tint backs off until the page's ink keeps 5:1 contrast. Practical
// Data asks a model which end is better; here every metric's direction is known, so no model is involved.
(() => {
  const SURFACE = '#191816', INK = '#EDE8E1', MIN_CONTRAST = 5;
  const POLES = { bad: '#e0533f', good: '#6f9bd6' }; // red–blue: safe for red–green colour blindness
  const hex = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
  const lum = (c) => {
    const [r, g, b] = hex(c).map((v) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const contrast = (a, b) => { const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
  const mix = (a, b, t) => '#' + hex(a).map((v, i) => Math.round(v + (hex(b)[i] - v) * t).toString(16).padStart(2, '0')).join('');

  // Position of each value on 0..1 after polarity (1 = better). Null where missing, when fewer than two
  // values exist, when they are equal, or when the best and worst differ by less than 10% of the larger
  // magnitude. A 3% gap must not paint a full red-to-blue swing.
  function rank(values, better) {
    const v = values.filter((x) => x != null && Number.isFinite(x));
    if (v.length < 2) return values.map(() => null);
    const lo = Math.min(...v), hi = Math.max(...v);
    const span = Math.max(Math.abs(hi), Math.abs(lo));
    if (span === 0 || (hi - lo) / span < 0.1) return values.map(() => null);
    return values.map((x) => (x == null || !Number.isFinite(x) ? null : better === 'low' ? (hi - x) / (hi - lo) : (x - lo) / (hi - lo)));
  }

  // The fill for position t, or null for a plain cell.
  // `texts`: [colour, min contrast] for every text on the cell. The ink keeps 5:1; a tile's small grey label 4.5:1 (AA).
  function fill(t, texts = [[INK, MIN_CONTRAST]], surface = SURFACE) {
    if (t == null) return null;
    const weight = Math.abs(2 * t - 1) ** 0.8;
    if (weight < 0.08) return null;
    const pole = t < 0.5 ? POLES.bad : POLES.good;
    for (let s = 0.6 * weight; s > 0.02; s -= 0.04) {
      const f = mix(surface, pole, s);
      if (texts.every(([c, min]) => contrast(f, c) >= min)) return f;
    }
    return null;
  }

  // Shade one element; `t` from rank(). Leaves a title saying what the shade means.
  function shade(node, t, label, texts) {
    const f = fill(t, texts);
    node.style.background = f || '';
    node.classList.toggle('heat', !!f);
    node.classList.toggle('best', t === 1);
    if (label) node.title = t == null ? label : `${label}: ${t === 1 ? 'best' : t === 0 ? 'worst' : `${Math.round(t * 100)}% of the way from worst to best`}`;
  }

  window.Heat = { rank, fill, shade, POLES };
  if (typeof module !== 'undefined') module.exports = window.Heat;  // tests/test_page.py runs the ranking in node
})();
