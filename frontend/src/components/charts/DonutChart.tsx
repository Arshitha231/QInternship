// ---------------------------------------------------------------------------
// Hand-rolled SVG charts. No charting library, deliberately: the only chart
// dependency this project has is d3-force for the graph canvas, and a donut
// with leader lines is a hundred lines of trigonometry against several
// hundred kilobytes of Recharts.
//
// The callout treatment -- a coloured pill sitting outside the arc, joined
// to its slice by a two-segment leader line -- is the layout the reference
// screenshot uses, and it earns its complexity: a slice's value is legible
// without a hover, which matters because these charts get screenshotted into
// decks and read on touch devices where there is no hover at all.
// ---------------------------------------------------------------------------

export interface Slice {
  key: string;
  label: string;
  value: number;
  /** A CSS colour -- pass a token, e.g. "var(--chart-1)". */
  color: string;
}

interface DonutChartProps {
  slices: Slice[];
  /** Square viewport for the arc itself; callouts are drawn outside it. */
  size?: number;
  /** 0 = pie, 0.6 = ring. The centre of a ring is where the total goes. */
  innerRatio?: number;
  centerValue?: string | number;
  centerLabel?: string;
  onSelect?: (key: string) => void;
  selectedKey?: string | null;
  /** Slices below this share get no callout -- they stay in the legend.
   *  Without a floor, a 0.4% slice's pill collides with its neighbours and
   *  the leader line points at something too thin to see. */
  minCalloutPct?: number;
  /** Renders the pill text. Defaults to "<value> (<pct>%)". */
  formatCallout?: (slice: Slice, pct: number) => string;
  ariaLabel?: string;
}

const TAU = Math.PI * 2;

function polar(cx: number, cy: number, r: number, angle: number): [number, number] {
  return [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
}

/** One slice as an SVG path. Handles the whole-circle case separately: an
 *  arc from a point back to the same point draws nothing, so a 100% slice
 *  would vanish -- which is exactly what a fully-compliant team looks like,
 *  i.e. the case you least want rendering as an empty box. */
function slicePath(
  cx: number, cy: number, outerR: number, innerR: number, start: number, end: number,
): string {
  const full = end - start >= TAU - 1e-6;
  if (full) {
    // Two half-arcs, outer then inner, so the ring hole survives.
    const [ox1, oy1] = polar(cx, cy, outerR, 0);
    const [ox2, oy2] = polar(cx, cy, outerR, Math.PI);
    const [ix1, iy1] = polar(cx, cy, innerR, 0);
    const [ix2, iy2] = polar(cx, cy, innerR, Math.PI);
    if (innerR <= 0) {
      return `M ${ox1} ${oy1} A ${outerR} ${outerR} 0 1 1 ${ox2} ${oy2} A ${outerR} ${outerR} 0 1 1 ${ox1} ${oy1} Z`;
    }
    return (
      `M ${ox1} ${oy1} A ${outerR} ${outerR} 0 1 1 ${ox2} ${oy2} A ${outerR} ${outerR} 0 1 1 ${ox1} ${oy1} Z ` +
      `M ${ix1} ${iy1} A ${innerR} ${innerR} 0 1 0 ${ix2} ${iy2} A ${innerR} ${innerR} 0 1 0 ${ix1} ${iy1} Z`
    );
  }
  const large = end - start > Math.PI ? 1 : 0;
  const [ox1, oy1] = polar(cx, cy, outerR, start);
  const [ox2, oy2] = polar(cx, cy, outerR, end);
  if (innerR <= 0) {
    return `M ${cx} ${cy} L ${ox1} ${oy1} A ${outerR} ${outerR} 0 ${large} 1 ${ox2} ${oy2} Z`;
  }
  const [ix1, iy1] = polar(cx, cy, innerR, end);
  const [ix2, iy2] = polar(cx, cy, innerR, start);
  return (
    `M ${ox1} ${oy1} A ${outerR} ${outerR} 0 ${large} 1 ${ox2} ${oy2} ` +
    `L ${ix1} ${iy1} A ${innerR} ${innerR} 0 ${large} 0 ${ix2} ${iy2} Z`
  );
}

interface Callout {
  slice: Slice;
  pct: number;
  /** Where the leader line leaves the arc. */
  ax: number; ay: number;
  /** The elbow. */
  ex: number; ey: number;
  /** Pill anchor, after collision resolution. */
  lx: number; ly: number;
  side: "left" | "right";
}

/** Push overlapping pills apart along y, one side at a time.
 *
 *  Slices adjacent on the circle are adjacent in this list, so a single
 *  downward pass that keeps a minimum gap is enough -- no iterative
 *  relaxation. The pills stay in slice order, which is what makes the leader
 *  lines readable: crossing lines are worse than a pill a few pixels off its
 *  ideal position. */
function separate(callouts: Callout[], gap: number, top: number, bottom: number): Callout[] {
  const out = [...callouts].sort((a, b) => a.ly - b.ly);
  let cursor = top;
  for (const c of out) {
    c.ly = Math.max(c.ly, cursor);
    cursor = c.ly + gap;
  }
  // If the pass ran past the bottom, walk back up so the overflow is shared
  // rather than dumped entirely on the last pill.
  let floor = bottom;
  for (let i = out.length - 1; i >= 0; i--) {
    out[i].ly = Math.min(out[i].ly, floor);
    floor = out[i].ly - gap;
  }
  return out;
}

export function DonutChart({
  slices,
  size = 230,
  innerRatio = 0.58,
  centerValue,
  centerLabel,
  onSelect,
  selectedKey = null,
  minCalloutPct = 3,
  formatCallout,
  ariaLabel,
}: DonutChartProps) {
  const shown = slices.filter((s) => s.value > 0);
  const total = shown.reduce((sum, s) => sum + s.value, 0);

  // Callout pills need room either side of the arc, and HOW MUCH room
  // depends on the longest label -- "1432 Working (56%)" is nearly three
  // times the width of "8 (8%)". A fixed pad sized for the short case
  // clipped the long one off the left edge of the viewBox; sizing the pad
  // from the actual text means the chart grows to fit its labels instead of
  // cropping them. Measured with the same 6.6px-per-character estimate the
  // pill itself uses below, so the two cannot disagree.
  const widest = shown.reduce((max, slice) => {
    const pct = (slice.value / (total || 1)) * 100;
    if (pct < minCalloutPct) return max;
    const text = formatCallout ? formatCallout(slice, pct) : `${slice.value} (${Math.round(pct)}%)`;
    return Math.max(max, text.length * 6.6 + 14);
  }, 46);
  const padX = Math.round(widest) + 40;
  const padY = 12;
  const width = size + padX * 2;
  const height = size + padY * 2;
  const cx = width / 2;
  const cy = height / 2;
  const outerR = size / 2;
  const innerR = outerR * innerRatio;

  if (total === 0) {
    return (
      <div className="donut-empty" role="img" aria-label={`${ariaLabel ?? "Chart"}: no data`}>
        <p className="donut-empty-mark" aria-hidden="true">—</p>
        <p>Nothing to chart yet</p>
      </div>
    );
  }

  const callouts: Callout[] = [];
  const paths: { slice: Slice; d: string; pct: number }[] = [];
  let angle = -Math.PI / 2; // 12 o'clock

  for (const slice of shown) {
    const sweep = (slice.value / total) * TAU;
    const pct = (slice.value / total) * 100;
    paths.push({ slice, d: slicePath(cx, cy, outerR, innerR, angle, angle + sweep), pct });

    if (pct >= minCalloutPct) {
      const mid = angle + sweep / 2;
      const [ax, ay] = polar(cx, cy, outerR - 6, mid);
      const [ex, ey] = polar(cx, cy, outerR + 18, mid);
      const side: "left" | "right" = Math.cos(mid) >= 0 ? "right" : "left";
      callouts.push({
        slice, pct, ax, ay, ex, ey, side,
        lx: side === "right" ? cx + outerR + 34 : cx - outerR - 34,
        ly: ey,
      });
    }
    angle += sweep;
  }

  const right = separate(callouts.filter((c) => c.side === "right"), 30, 20, height - 20);
  const left = separate(callouts.filter((c) => c.side === "left"), 30, 20, height - 20);
  const placed = [...right, ...left];

  const label = ariaLabel ?? "Chart";
  const summary = shown.map((s) => `${s.label}: ${s.value}`).join(", ");

  return (
    <svg
      className="donut"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${label}. ${summary}.`}
      preserveAspectRatio="xMidYMid meet"
    >
      {paths.map(({ slice, d }) => {
        const dim = selectedKey !== null && selectedKey !== slice.key;
        return (
          <path
            key={slice.key}
            d={d}
            fill={slice.color}
            className={`donut-slice${onSelect ? " donut-slice-clickable" : ""}${dim ? " donut-slice-dim" : ""}${
              selectedKey === slice.key ? " donut-slice-on" : ""
            }`}
            onClick={onSelect ? () => onSelect(slice.key) : undefined}
            // Keyboard reach comes from the legend buttons beneath the
            // chart, not from the paths -- an SVG path with a tabindex is
            // announced as an unlabelled graphic by most screen readers,
            // and the legend is a real <button> with real text.
            tabIndex={-1}
          >
            <title>{`${slice.label}: ${slice.value} (${Math.round((slice.value / total) * 100)}%)`}</title>
          </path>
        );
      })}

      {innerR > 0 && (centerValue !== undefined || centerLabel) && (
        <>
          {centerValue !== undefined && (
            <text x={cx} y={cy - (centerLabel ? 2 : -6)} className="donut-center-value" textAnchor="middle">
              {centerValue}
            </text>
          )}
          {centerLabel && (
            <text x={cx} y={cy + 16} className="donut-center-label" textAnchor="middle">
              {centerLabel}
            </text>
          )}
        </>
      )}

      {placed.map((c) => {
        const endX = c.side === "right" ? c.lx - 6 : c.lx + 6;
        const text = formatCallout
          ? formatCallout(c.slice, c.pct)
          : `${c.slice.value} (${Math.round(c.pct)}%)`;
        const pillW = Math.max(46, text.length * 6.6 + 14);
        const pillX = c.side === "right" ? c.lx : c.lx - pillW;
        const dim = selectedKey !== null && selectedKey !== c.slice.key;
        return (
          <g key={c.slice.key} className={`donut-callout${dim ? " donut-callout-dim" : ""}`}>
            <polyline
              points={`${c.ax},${c.ay} ${c.ex},${c.ey} ${endX},${c.ly}`}
              fill="none"
              className="donut-leader"
            />
            <rect
              x={pillX} y={c.ly - 10} width={pillW} height={20} rx={3}
              fill={c.slice.color} className="donut-pill"
            />
            <text
              x={pillX + pillW / 2} y={c.ly + 4}
              textAnchor="middle" className="donut-pill-text"
            >
              {text}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

/** The legend, split out so a caller can place it under the chart or beside
 *  it. Buttons rather than list items: this is how the chart is reached by
 *  keyboard, and clicking a legend entry does the same thing as clicking its
 *  slice. */
export function ChartLegend({
  slices, onSelect, selectedKey = null, showValues = true,
}: {
  slices: Slice[];
  onSelect?: (key: string) => void;
  selectedKey?: string | null;
  showValues?: boolean;
}) {
  const total = slices.reduce((sum, s) => sum + s.value, 0);
  return (
    <ul className="chart-legend">
      {slices.map((s) => {
        const content = (
          <>
            <i className="chart-legend-swatch" style={{ background: s.color }} aria-hidden="true" />
            <span className="chart-legend-label">{s.label}</span>
            {showValues && (
              <span className="chart-legend-value">
                {s.value}
                {total > 0 && <span className="chart-legend-pct"> · {Math.round((s.value / total) * 100)}%</span>}
              </span>
            )}
          </>
        );
        return (
          <li key={s.key} className={selectedKey === s.key ? "on" : undefined}>
            {onSelect ? (
              <button type="button" className="chart-legend-btn" onClick={() => onSelect(s.key)}
                      aria-pressed={selectedKey === s.key}>
                {content}
              </button>
            ) : (
              <span className="chart-legend-btn">{content}</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** A horizontal proportion bar -- the "top skill domains" treatment. Used
 *  where a donut would be the wrong shape: more than six categories, or
 *  where the reader is comparing lengths rather than reading a share of a
 *  whole. */
export function BarRow({
  label, value, max, color = "var(--chart-1)", note, onClick, title,
}: {
  label: string;
  value: number;
  max: number;
  color?: string;
  note?: string;
  onClick?: () => void;
  title?: string;
}) {
  const pct = max > 0 ? Math.max(2, (value / max) * 100) : 0;
  const body = (
    <>
      <span className="bar-row-label">{label}</span>
      <span className="bar-row-track">
        <span className="bar-row-fill" style={{ width: `${pct}%`, background: color }} />
      </span>
      <span className="bar-row-value">{note ?? value}</span>
    </>
  );
  return onClick ? (
    <button type="button" className="bar-row bar-row-clickable" onClick={onClick} title={title}>{body}</button>
  ) : (
    <div className="bar-row" title={title}>{body}</div>
  );
}

/** One bar, several segments -- a proportion split inside a fixed width.
 *  The training buckets and a skill's Expert/Working/Learning mix are the
 *  same shape of data and share this. */
export function StackedBar({ segments, height = 8 }: { segments: Slice[]; height?: number }) {
  const total = segments.reduce((sum, s) => sum + s.value, 0);
  if (total === 0) return <div className="stacked-bar stacked-bar-empty" style={{ height }} />;
  return (
    <div className="stacked-bar" style={{ height }}>
      {segments.filter((s) => s.value > 0).map((s) => (
        <span
          key={s.key}
          style={{ width: `${(s.value / total) * 100}%`, background: s.color }}
          title={`${s.label}: ${s.value}`}
        />
      ))}
    </div>
  );
}
