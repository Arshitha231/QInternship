// Chart colours, as CSS variables rather than literals.
//
// Every value here is defined twice in index.css -- once for the cream
// theme, once for dark -- and referenced through the variable so a chart
// never hardcodes a light-theme colour that survives into dark mode. That
// was a real bug in this codebase's graph canvas (see --graph-person-fill's
// comment), and a pie chart is a much bigger surface to make it on.

/** Skill levels. Bound to the same three tokens the profile's level pills
 *  use, so a person reading "Expert" as a purple pill on a profile and as a
 *  purple slice on a dashboard is reading one colour language. */
export const LEVEL_COLORS = {
  Expert: "var(--level-expert)",
  Working: "var(--level-working)",
  Learning: "var(--level-learning)",
} as const;

/** Training buckets. These ARE a traffic light, and unusually for this
 *  palette that is the right call: overdue/due-soon/complete is exactly the
 *  semantic a traffic light encodes, and inventing a neutral scheme for it
 *  would be decoration fighting meaning. `outstanding` stays deliberately
 *  grey -- not completed, but nothing says it is late. */
export const BUCKET_COLORS: Record<string, string> = {
  completed: "var(--status-good)",
  due_soon: "var(--status-warn)",
  overdue: "var(--status-bad)",
  outstanding: "var(--status-idle)",
};

export const BUCKET_LABELS: Record<string, string> = {
  completed: "Completed",
  overdue: "Overdue",
  due_soon: "Due soon",
  outstanding: "Not completed",
};

export const VERDICT_LABEL: Record<string, string> = {
  understaffed: "Understaffed",
  healthy: "Healthy",
  overrepresented: "Overrepresented",
  unused: "Unused",
};

/** Skill-health verdicts. */
export const VERDICT_COLORS: Record<string, string> = {
  understaffed: "var(--status-bad)",
  healthy: "var(--status-good)",
  overrepresented: "var(--chart-3)",
  unused: "var(--status-idle)",
};

/** A categorical ramp for things with no inherent status -- skill
 *  categories, department splits. Six is the ceiling on purpose: past six
 *  slices a donut stops being readable and the caller should reach for
 *  BarRow instead. */
export const CATEGORICAL = [
  "var(--chart-1)", "var(--chart-2)", "var(--chart-3)",
  "var(--chart-4)", "var(--chart-5)", "var(--chart-6)",
];

export function categorical(index: number): string {
  return CATEGORICAL[index % CATEGORICAL.length];
}
