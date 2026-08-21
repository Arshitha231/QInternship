import { useEffect, useRef, useState } from "react";
import { getDashboardOverview } from "../../api";
import type { DashboardOverview, Identity, ViewMode } from "../../types";
import { Award, GraduationCap, SearchIcon, Sparkles, Users, X } from "../../icons";
import { avatarStyle } from "../../avatarHue";
import type { ProfileStackEntry } from "../ProfilePage";
import { StatTile } from "./StatTile";

interface Props {
  identity: Identity;
  viewMode: ViewMode;
  canSeeDashboard: boolean;
  query: string;
  onQueryChange: (q: string) => void;
  stack: ProfileStackEntry[];
  onOpenPerson: (id: string, name: string) => void;
}

const EXAMPLE_QUERIES = [
  "Who could mentor me in Terraform?",
  "Search a name, skill, or ask a question",
  "Who's available in Bangalore this week?",
  "Find someone who has led a client migration project",
];

function initials(name: string): string {
  return name.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
}

function firstName(name: string): string {
  return name.split(" ")[0] || name;
}

export function HomePage({ identity, viewMode, canSeeDashboard, query, onQueryChange, stack, onOpenPerson }: Props) {
  const [overview, setOverview] = useState<DashboardOverview | null | undefined>(canSeeDashboard ? undefined : null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [exampleIndex, setExampleIndex] = useState(0);
  const reducedMotion = useRef(
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  ).current;

  useEffect(() => {
    if (!canSeeDashboard) {
      setOverview(null);
      return;
    }
    let cancelled = false;
    setOverview(undefined);
    getDashboardOverview(identity, viewMode, {})
      .then((res) => {
        if (!cancelled) setOverview(res);
      })
      .catch(() => {
        // Home degrades to the static-example fallback below rather than
        // showing an error block -- a stat tile is a nice-to-have here, not
        // the reason someone opened this page.
        if (!cancelled) setOverview(null);
      });
    return () => {
      cancelled = true;
    };
  }, [canSeeDashboard, identity, viewMode]);

  // Rotating example queries, only while the box is empty and unfocused --
  // the moment there's real input, or the moment it's focused, the example
  // is no longer the point. Disabled under reduced motion: one static
  // example instead of a cross-fading carousel.
  useEffect(() => {
    if (reducedMotion || query) return;
    const id = window.setInterval(() => {
      setExampleIndex((i) => (i + 1) % EXAMPLE_QUERIES.length);
    }, 3000);
    return () => window.clearInterval(id);
  }, [reducedMotion, query]);

  // "/" focuses the search box, unless the user is already typing somewhere
  // (an input, a textarea, a contentEditable region) -- the common
  // low-cost shortcut called out in the spec.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== "/") return;
      const target = e.target as HTMLElement | null;
      const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      if (typing) return;
      e.preventDefault();
      inputRef.current?.focus();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const recentlyViewed = (() => {
    const seen = new Set<string>();
    const out: ProfileStackEntry[] = [];
    for (let i = stack.length - 1; i >= 0 && out.length < 6; i--) {
      const entry = stack[i];
      if (entry.id === identity.id || seen.has(entry.id) || !entry.name) continue;
      seen.add(entry.id);
      out.push(entry);
    }
    return out;
  })();

  return (
    <div className="mel-home">
      <section className="mel-home-hero">
        <h1 className="mel-home-greeting">Find the right person, {firstName(identity.name)}</h1>
        <div className={`mel-home-search ${query ? "has-value" : ""}`}>
          <SearchIcon className="mel-home-search-icon" size={19} />
          <label className="sr-only" htmlFor="home-q" style={{ position: "absolute", left: -9999 }}>
            Search the directory
          </label>
          <input
            id="home-q"
            ref={inputRef}
            type="search"
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            placeholder="Search a name, skill, or ask a question"
          />
          {!query && (
            <span className="mel-home-search-example" aria-hidden="true">
              <span key={exampleIndex} className="mel-home-search-example-text">
                Try: &ldquo;{EXAMPLE_QUERIES[exampleIndex]}&rdquo;
              </span>
            </span>
          )}
          {query && (
            <button className="clear-btn" aria-label="Clear search" onClick={() => onQueryChange("")}>
              <X size={15} />
            </button>
          )}
        </div>
        <p className="mel-home-hint">
          Press <kbd>/</kbd> to search from anywhere on this page.
        </p>
      </section>

      {overview ? (
        <div className="mel-home-stats">
          <StatTile label="People" value={overview.headcount} icon={<Users size={20} />} />
          <StatTile label="Distinct skills" value={overview.skill_count} icon={<GraduationCap size={20} />} />
          <StatTile label="Experts" value={overview.expert_count} icon={<Award size={20} />} />
        </div>
      ) : overview === undefined ? (
        <div className="mel-home-stats">
          {[0, 1, 2].map((i) => <div key={i} className="skel skel-card mel-stat-tile-skel" />)}
        </div>
      ) : (
        <div className="mel-home-examples">
          <p className="mel-home-examples-label"><Sparkles size={14} /> Try asking</p>
          <div className="mel-home-example-chips">
            {EXAMPLE_QUERIES.map((ex) => (
              <button key={ex} className="ask-example-chip" onClick={() => onQueryChange(ex)}>
                {ex}
              </button>
            ))}
          </div>
        </div>
      )}

      {recentlyViewed.length > 0 && (
        <section className="mel-home-recent">
          <h2>Recently viewed</h2>
          <div className="mel-home-recent-list">
            {recentlyViewed.map((entry) => (
              <button key={entry.id} className="mel-home-recent-card" onClick={() => onOpenPerson(entry.id, entry.name)}>
                <span className="avatar" style={avatarStyle(entry.name)} aria-hidden="true">{initials(entry.name)}</span>
                <span className="mel-home-recent-name">{entry.name}</span>
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
