import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(handle);
  }, [value, delayMs]);
  return debounced;
}

export type Theme = "light" | "dark";

// index.html sets document.documentElement.dataset.theme synchronously
// before React mounts (localStorage, falling back to system preference), so
// this just reads that back rather than deciding the initial value itself --
// deciding it here too would risk a flash of the wrong theme on first paint.
function initialTheme(): Theme {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("theme", theme);
  }, [theme]);

  function toggleTheme() {
    setTheme((t) => (t === "dark" ? "light" : "dark"));
  }

  return [theme, toggleTheme];
}


// ---------------------------------------------------------------------------
// Graph focus history — the < > trail and the home button.
//
// Every graph view is centred on one person, and clicking anybody re-centres
// on them. That is a navigation, and navigations you cannot undo are a trap:
// three clicks into someone else's org and the only way back to where you
// started was to remember the name and search for it.
//
// Deliberately NOT wired into browser history. The app already drives
// window.history for the profile stack (see App.tsx's popstate handler), and
// a second producer pushing graph focus onto the same stack would make the
// browser's own back button mean two different things depending on which tab
// you were last on. This is a self-contained trail, with its own controls,
// which is also what was asked for.
// ---------------------------------------------------------------------------

export interface FocusHistory {
  focusId: string;
  /** Navigate to someone, truncating any forward trail. */
  go: (id: string) => void;
  back: () => void;
  forward: () => void;
  /** Back to the signed-in person. Pushed as a normal entry, not a reset, so
   *  `back` still returns to wherever you were standing. */
  home: () => void;
  canGoBack: boolean;
  canGoForward: boolean;
  /** True when the focus is already the signed-in person, so the control can
   *  disable rather than becoming a no-op that looks broken. */
  atHome: boolean;
  /** Names for the entries either side of the cursor, when known, so the
   *  buttons can say "Back to Priya Sharma" instead of just "Back". */
  backLabel: string | null;
  forwardLabel: string | null;
  /** Called by whoever resolves a person, to label the trail after the fact:
   *  a navigation carries an id, and the name only arrives with the fetch. */
  rememberName: (id: string, name: string) => void;
}

// Long enough that nobody reaches the end while exploring, short enough that
// it cannot grow without bound in a long session.
const MAX_HISTORY = 50;

export function useFocusHistory(homeId: string): FocusHistory {
  const [entries, setEntries] = useState<string[]>([homeId]);
  const [index, setIndex] = useState(0);
  // A ref, not state: names arrive from a fetch that completes after the
  // navigation, and re-rendering the whole graph to relabel a tooltip would
  // be a lot of work for a tooltip. Read during render for the labels below,
  // which is safe because a name never changes once known.
  const names = useRef<Map<string, string>>(new Map());
  const [nameTick, setNameTick] = useState(0);

  const go = useCallback((id: string) => {
    setEntries((prev) => {
      // Re-selecting the person already centred is not a navigation, and
      // recording it would fill the trail with duplicates that each need a
      // separate press of Back to get through.
      if (prev[index] === id) return prev;
      const next = [...prev.slice(0, index + 1), id];
      const trimmed = next.length > MAX_HISTORY ? next.slice(next.length - MAX_HISTORY) : next;
      setIndex(trimmed.length - 1);
      return trimmed;
    });
  }, [index]);

  const back = useCallback(() => setIndex((i) => Math.max(0, i - 1)), []);
  const forward = useCallback(() => setIndex((i) => i + 1), []);
  const home = useCallback(() => go(homeId), [go, homeId]);

  const rememberName = useCallback((id: string, name: string) => {
    if (!name || names.current.get(id) === name) return;
    names.current.set(id, name);
    // Nudges a re-render so the button labels pick the new name up. Guarded
    // by the equality check above, so a resolved person can't loop.
    setNameTick((t) => t + 1);
  }, []);

  const focusId = entries[index] ?? homeId;
  return useMemo(() => ({
    focusId,
    go,
    back,
    forward,
    home,
    canGoBack: index > 0,
    canGoForward: index < entries.length - 1,
    atHome: focusId === homeId,
    backLabel: index > 0 ? names.current.get(entries[index - 1]) ?? null : null,
    forwardLabel: index < entries.length - 1 ? names.current.get(entries[index + 1]) ?? null : null,
    rememberName,
    // nameTick is a render trigger, not a value anything reads -- it belongs
    // in the dependency list so the labels above are recomputed when a name
    // lands, and nowhere else.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [focusId, go, back, forward, home, index, entries, homeId, rememberName, nameTick]);
}
