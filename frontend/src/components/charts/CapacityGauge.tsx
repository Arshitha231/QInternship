import type { ReactNode } from "react";

// ---------------------------------------------------------------------------
// Supply against demand, as a gauge rather than as columns of numbers.
//
// The table this replaced put capable-count and project-count in adjacent
// numeric columns and left the reader to divide one by the other, fourteen
// times, to answer the only question the section is for: is there enough
// capacity for the work. Five columns of small tabular figures is a lot of
// ink for a comparison the eye can make instantly if you draw it.
//
// So the bar is NORMALISED TO DEMAND, not to a shared headcount scale. The
// "needed" line sits at the same x on every row, and the only thing that
// varies is how far the bar reaches. Short of the line is a shortfall, past
// it is slack -- and because the line never moves, rows are comparable
// down the column without reading a single number.
//
// A shared linear scale (the obvious alternative) fails on this data: one
// skill with 45 capable people against 24 projects flattens every other row
// to a sliver, and the rows that matter are the small ones.
// ---------------------------------------------------------------------------

/** Where the "needed" line sits, as a fraction of track width. Also the
 *  reciprocal of the widest ratio the track can show: at 0.36 a bar runs
 *  full-width at 2.8x supply, which is comfortably past the point where
 *  more depth stops being interesting. */
const NEEDED_AT = 0.36;
const MAX_RATIO = 1 / NEEDED_AT;

export interface CapacityGaugeProps {
  /** People who can actually do it — Expert + Working. */
  supply: number;
  /** Active projects depending on it. Zero means the ratio is undefined,
   *  which is a different state, not a ratio of zero. */
  demand: number;
  /** Drives the fill colour. Passed in rather than derived here so the bar
   *  and the pill beside it can never disagree about the verdict. */
  color: string;
  supplyLabel?: string;
  /** Singular and plural forms of the demand caption. Two props rather than
   *  an appended "s": a count of one is the common case on a small team's
   *  dashboard, and the row is meant to read as a sentence — "1 projects
   *  need it" breaks that on exactly the rows a manager looks at most. */
  demandLabel?: string;
  demandLabelPlural?: string;
}

export function CapacityGauge({
  supply, demand, color, supplyLabel = "capable",
  demandLabel = "project needs it", demandLabelPlural = "projects need it",
}: CapacityGaugeProps) {
  if (demand === 0) {
    // No demand means no ratio. Drawn as a flat, unfilled track rather than
    // a full or empty bar, both of which would read as a measurement.
    return (
      <div className="gauge">
        <div className="gauge-track gauge-track-idle">
          <span className="gauge-fill" style={{ width: `${Math.min(supply, 12) * 3}%`, background: "var(--status-idle)" }} />
        </div>
        <p className="gauge-caption">
          <strong>{supply}</strong> {supplyLabel} · <span className="muted">no active project needs it</span>
        </p>
      </div>
    );
  }

  const ratio = supply / demand;
  const clamped = Math.min(ratio, MAX_RATIO);
  const width = (clamped / MAX_RATIO) * 100;
  const short = demand - supply;

  return (
    <div className="gauge">
      <div className="gauge-track">
        <span className="gauge-fill" style={{ width: `${Math.max(width, 1.5)}%`, background: color }} />
        {/* The line, and the only fixed reference point on the row. */}
        <span className="gauge-needed" style={{ left: `${NEEDED_AT * 100}%` }} aria-hidden="true" />
        {ratio > MAX_RATIO && <span className="gauge-over" aria-hidden="true">›</span>}
      </div>
      <p className="gauge-caption">
        <strong>{supply}</strong> {supplyLabel} ·{" "}
        <strong>{demand}</strong> {demand === 1 ? demandLabel : demandLabelPlural}
        {short > 0 ? (
          <span className="gauge-delta gauge-delta-short">{short} short</span>
        ) : short === 0 ? (
          <span className="gauge-delta gauge-delta-level">exactly matched</span>
        ) : (
          <span className="gauge-delta gauge-delta-spare">{ratio.toFixed(1)}× cover</span>
        )}
      </p>
    </div>
  );
}

/** The one-time explanation of what the line means. Rendered once above a
 *  list of gauges, never per row -- a legend repeated fourteen times is the
 *  congestion this component exists to remove. */
export function CapacityGaugeKey({ children }: { children?: ReactNode }) {
  return (
    <p className="gauge-key">
      <span className="gauge-key-sample" aria-hidden="true">
        <span className="gauge-key-fill" />
        <span className="gauge-key-needed" />
      </span>
      The line is one capable person per active project needing the skill. A bar short of it means
      fewer people who can do the work than projects depending on it.
      {children}
    </p>
  );
}

/** Expert / Working / Learning as a thin unlabelled strip. Secondary to the
 *  gauge above it by design: the level mix is context for a skill you have
 *  already decided to look at, not something you scan a list for. The full
 *  breakdown is one click away in the detail popup. */
export function LevelStrip({
  expert, working, learning,
}: { expert: number; working: number; learning: number }) {
  const total = expert + working + learning;
  if (total === 0) return null;
  return (
    <span className="level-strip" title={`${expert} Expert, ${working} Working, ${learning} Learning`}>
      {expert > 0 && <i style={{ width: `${(expert / total) * 100}%`, background: "var(--level-expert)" }} />}
      {working > 0 && <i style={{ width: `${(working / total) * 100}%`, background: "var(--level-working)" }} />}
      {learning > 0 && <i style={{ width: `${(learning / total) * 100}%`, background: "var(--level-learning)" }} />}
    </span>
  );
}
