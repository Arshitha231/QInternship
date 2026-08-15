import { useState, useEffect, useMemo } from "react";
import { unifiedSearch } from "../../api";
import { useDebouncedValue } from "../../hooks";
import type { Identity, PersonDetail, UnifiedSearchResponse, ViewMode } from "../../types";
import { TeamGraph } from "./TeamGraph"; // Adjust path as needed

interface Props {
  identity: Identity;
  focusId: string;
  focusPerson: PersonDetail | null;
  viewMode: ViewMode;
  onNavigate: (id: string) => void;
  onOpenProfile: (id: string, name: string) => void;
}

export function GraphExplorer({ 
  identity, 
  focusId, 
  focusPerson, 
  viewMode, 
  onNavigate, 
  onOpenProfile 
}: Props) {
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<UnifiedSearchResponse | null>(null);

  // 1. Debounce the user's typing input by 300ms so we don't hit the API on every single keystroke
  const debouncedQuery = useDebouncedValue(searchQuery, 300);

  // 2. Fetch the search results whenever the debounced query changes
  useEffect(() => {
    let cancelled = false;

    if (!debouncedQuery.trim()) {
      setSearchResults(null);
      return;
    }

    unifiedSearch(identity, { q: debouncedQuery })
      .then((res) => {
        if (!cancelled) setSearchResults(res);
      })
      .catch((err) => {
        console.error("Search failed:", err);
        if (!cancelled) setSearchResults(null);
      });

    return () => {
      cancelled = true;
    };
  }, [debouncedQuery, identity]);

  // 3. Convert the results array into a Set of IDs for O(1) lookup in the graph components
  const highlightedIds = useMemo(() => {
    if (!searchResults || searchResults.results.length === 0) {
      return new Set<string>();
    }
    return new Set(searchResults.results.map((person) => person.id));
  }, [searchResults]);

  return (
    <div className="graph-explorer-container" style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      
      {/* Search Bar UI */}
      <div className="search-bar-wrap" style={{ padding: "16px", borderBottom: "1px solid #e2e8f0" }}>
        <input
          type="text"
          placeholder="Search for people, skills, or ask a question..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          style={{ width: "100%", padding: "10px", borderRadius: "6px", border: "1px solid #cbd5e1" }}
        />
        {searchResults && (
          <p style={{ fontSize: "0.85rem", color: "#64748b", margin: "8px 0 0 0" }}>
            Found {searchResults.results.length} matches
          </p>
        )}
      </div>

      {/* The Graph Canvas */}
      <div className="graph-canvas-wrap" style={{ flexGrow: 1, position: "relative" }}>
        <TeamGraph 
          identity={identity}
          viewMode={viewMode}
          focusId={focusId}
          focusPerson={focusPerson}
          highlightedIds={highlightedIds} // <-- Pass the derived Set down to the graph
          onNavigate={onNavigate}
          onOpenProfile={onOpenProfile}
        />
      </div>
      
    </div>
  );
}
