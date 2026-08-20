import { useState } from "react";
import { Sparkles, X } from "../icons";
import type { FollowUpSuggestion } from "../types";

// A deterministic, cross-surface follow-up suggestion (app.assistant_
// context) -- never the model's own idea. Dismissible, local state only:
// re-appears on the next answer if still applicable, same as the server
// recomputing it fresh every time rather than remembering it was dismissed.
//
// "Add to filter" only ever shows when the suggestion carries a skill
// AND the host page passed onAddToFilter -- PRDChat renders these too
// (surface: "prd") but has no Filters panel to add to, so it renders the
// label only, informational rather than actionable there.

interface Props {
  suggestion: FollowUpSuggestion;
  onAddToFilter?: (skill: string, level?: string | null) => void;
}

export function SuggestionChip({ suggestion, onAddToFilter }: Props) {
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) return null;

  return (
    <div className="suggestion-chip">
      <Sparkles size={13} />
      <span className="suggestion-chip-text">{suggestion.label}</span>
      {onAddToFilter && suggestion.skill && (
        <button
          type="button" className="link-btn"
          onClick={() => onAddToFilter(suggestion.skill!, suggestion.minimum_level)}
        >
          Add to filter
        </button>
      )}
      <button
        type="button" aria-label="Dismiss suggestion"
        onClick={() => setDismissed(true)}
      >
        <X size={12} />
      </button>
    </div>
  );
}
