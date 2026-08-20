// The Mel mark. One component, used by both the top bar and the sign-in
// card -- it was previously inline SVG duplicated in both, which is how a
// logo ends up updated in one place and not the other.
//
// Inline rather than an <img> to /mel-logo.svg: at 28px the mark sits next
// to text that has already rendered, and a separate request means a visible
// gap on a cold load. The same artwork is on disk as public/mel-logo.svg
// (and as the favicon) for anywhere that needs a file.
//
// The dots are generated rather than hand-listed for the same reason they
// are in the file version: 550-odd <circle> elements written by hand cannot
// be adjusted, only rewritten.

const BG = "#2B1B3F";
const INNER = [0xf0, 0x6c, 0xd0];
const OUTER = [0x8b, 0x53, 0xc6];

function mix(t: number): string {
  const c = INNER.map((a, i) => Math.round(a + (OUTER[i] - a) * t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

// Detail scales with rendered size, because a logo has to survive the size
// it is actually used at. The full pattern is 550-odd dots at r=2..3.5 in a
// 256-unit viewBox -- rendered into 28 physical pixels each dot is a third
// of a pixel, and the whole ring collapses into a flat purple smudge.
// Below SMALL_SIZE the ring is redrawn with far fewer, far chunkier dots:
// the same idea, at a density the pixels can actually carry.
const SMALL_SIZE = 64;

function dots(compact: boolean) {
  const out: { cx: number; cy: number; r: number; fill: string }[] = [];
  const C = 128;
  const [from, to, step, rBase, rDrop, spacing] = compact
    ? [66, 108, 21, 11, 3, 26]
    : [46, 118, 9, 3.5, 1.5, 8.4];
  for (let r = from; r <= to; r += step) {
    const t = (r - from) / (to - from);
    const n = Math.max(8, Math.round((2 * Math.PI * r) / spacing));
    // Half-step alternate rings so the dots quincunx rather than lining up
    // into visible radial spokes.
    const offset = Math.round((r - from) / step) % 2 ? Math.PI / n : 0;
    for (let i = 0; i < n; i++) {
      const a = offset + (2 * Math.PI * i) / n;
      out.push({
        cx: C + r * Math.cos(a),
        cy: C + r * Math.sin(a),
        r: rBase - rDrop * t,
        fill: mix(t),
      });
    }
  }
  return out;
}

const FULL = dots(false);
const COMPACT = dots(true);

export function BrandMark({ size = 28 }: { size?: number }) {
  const compact = size < SMALL_SIZE;
  return (
    <svg width={size} height={size} viewBox="0 0 256 256" role="img" aria-label="Mel">
      <rect width="256" height="256" rx="56" fill={BG} />
      {(compact ? COMPACT : FULL).map((d, i) => (
        <circle key={i} cx={d.cx.toFixed(2)} cy={d.cy.toFixed(2)} r={d.r.toFixed(2)} fill={d.fill} />
      ))}
      <path
        d="M96 148 V108 L128 134 L160 108 V148"
        fill="none"
        stroke="#FFFFFF"
        // Heavier at small sizes for the same reason the dots are: an 11-unit
        // stroke in a 256 viewBox is under a pixel and a half at 28px.
        strokeWidth={compact ? 16 : 11}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
