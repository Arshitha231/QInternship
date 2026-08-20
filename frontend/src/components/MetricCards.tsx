import type { ReactNode } from "react";

export interface Metric {
  id: string;
  icon?: ReactNode;
  /** Secondary figure — a share, a delta, a qualifier. Optional. */
  note?: string;
  value: string | number;
  label: string;
  /** Ties the figure to a status colour. Omit for a neutral tile. */
  tone?: "neutral" | "high" | "medium" | "low";
}

// ---------------------------------------------------------------------------
// A row of headline figures. Adapted from the WigglingCards demo: same
// anatomy (icon, secondary figure, value, label) and the same hover response,
// deliberately dialled down.
//
// The demo wiggles — a springy rotate on hover. That reads as playful, which
// is the opposite of what this particular row is for: these are continuity
// exposure counts, i.e. how many engagements are one resignation away from
// trouble. A number that jiggles when you point at it undercuts it. What's
// kept is the part that was doing real work — the hover state that lifts the
// tile and brings up the accent, so a pointable card looks pointable.
//
// If you want the full wiggle back it's one keyframe here; it was a judgement
// call about this page, not a limitation.
// ---------------------------------------------------------------------------

export function MetricCards({ metrics }: { metrics: Metric[] }) {
  return (
    <div className="metric-cards">
      {metrics.map((m) => (
        <article className={`metric-card metric-card-${m.tone ?? "neutral"}`} key={m.id}>
          <div className="metric-card-top">
            {m.icon && <span className="metric-card-icon" aria-hidden="true">{m.icon}</span>}
            {m.note && <span className="metric-card-note">{m.note}</span>}
          </div>
          <p className="metric-card-value">{m.value}</p>
          <p className="metric-card-label">{m.label}</p>
        </article>
      ))}
    </div>
  );
}
