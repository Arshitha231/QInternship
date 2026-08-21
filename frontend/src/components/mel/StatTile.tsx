import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

interface Props {
  label: string;
  value: number;
  icon: ReactNode;
}

const DURATION_MS = 600;

function easeOut(t: number): number {
  return 1 - Math.pow(1 - t, 3);
}

// Counts up from 0 to `value` on first mount only -- a tile that re-animates
// every time its number ticks (a live headcount, say) would be busywork, not
// a signal. Remounting with a new `key` from the caller is how a genuinely
// new value gets to animate again.
export function StatTile({ label, value, icon }: Props) {
  const prefersReducedMotion = useRef(
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  ).current;
  const [display, setDisplay] = useState(prefersReducedMotion ? value : 0);

  useEffect(() => {
    if (prefersReducedMotion) {
      setDisplay(value);
      return;
    }
    let raf = 0;
    const start = performance.now();
    function tick(now: number) {
      const t = Math.min(1, (now - start) / DURATION_MS);
      setDisplay(Math.round(value * easeOut(t)));
      if (t < 1) raf = requestAnimationFrame(tick);
    }
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="mel-stat-tile">
      <span className="mel-stat-icon" aria-hidden="true">{icon}</span>
      <span className="mel-stat-value">{display.toLocaleString()}</span>
      <span className="mel-stat-label">{label}</span>
    </div>
  );
}
