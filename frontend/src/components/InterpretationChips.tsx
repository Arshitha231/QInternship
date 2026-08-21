import type { Interpretation, InterpretationEntity } from "../types";
import { X } from "../icons";

// The removable-chip row for a direct-mode search the backend re-read as a
// structured request (app.text_filters / app.query_entities --
// SEARCH_RANKING_PROPOSAL.md). Each chip names the real database value a
// piece of the query resolved to; removing one edits the search box's text
// and lets the next debounced /search call re-parse it from scratch -- no
// per-entity removal endpoint, no server-side session state (see the
// implementation plan's step 3 design note on chip removal).

const LABEL_TEXT: Record<InterpretationEntity["label"], string> = {
  role: "Role",
  seniority: "Seniority",
  skill: "Skill",
  office: "Office",
  org_unit: "Org unit",
};

interface Props {
  interpretation: Interpretation;
  onRemoveEntity: (entity: InterpretationEntity) => void;
}

export function InterpretationChips({ interpretation, onRemoveEntity }: Props) {
  const { entities, unparsed, weights } = interpretation;
  if (entities.length === 0 && unparsed.length === 0) return null;

  return (
    <div className="interpretation-row">
      {entities.length > 0 && (
        <div className="interpretation-chips">
          {entities.map((entity, i) => (
            <span key={`${entity.label}-${entity.value}-${i}`} className="interpretation-chip">
              <b>{LABEL_TEXT[entity.label]}:</b> {entity.value}
              <button
                type="button"
                aria-label={`Remove ${LABEL_TEXT[entity.label]} ${entity.value}`}
                onClick={() => onRemoveEntity(entity)}
              >
                <X size={11} />
              </button>
            </span>
          ))}
        </div>
      )}
      {unparsed.length > 0 && (
        // Not every real word in the query resolves to something the
        // directory recognises -- said plainly rather than silently
        // dropped, so a wrong or missed reading is visible, not just wrong.
        <p className="interpretation-unparsed">
          "{unparsed.join(", ")}" — no matching {unparsed.length === 1 ? "value" : "values"} in the directory
        </p>
      )}
      {weights && (
        <p className="interpretation-weights">
          Ranked by {Object.entries(weights).map(([field, pct]) => `${field} ${pct}%`).join(", ")}
        </p>
      )}
    </div>
  );
}
