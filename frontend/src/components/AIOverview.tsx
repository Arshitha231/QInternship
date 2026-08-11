import { useState } from "react";
import { ChevronDown, Sparkles } from "../icons";
import type { AIOverview as AIOverviewData } from "../types";

// The differentiating feature of the merged surface: the answer prose, with
// cited names linking down to their card, and a collapsed-by-default trace
// showing exactly which directory function answered and why — not a debug
// dump, a deliberate second-class citizen to the prose above it.

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function renderAnswerWithCitations(
  text: string,
  citations: { id: string; full_name: string }[],
  onJumpToCard: (id: string) => void,
) {
  if (citations.length === 0) return text;
  const names = [...new Set(citations.map((c) => c.full_name))].sort((a, b) => b.length - a.length);
  const pattern = new RegExp(`(${names.map(escapeRegExp).join("|")})`, "g");
  const parts = text.split(pattern);
  return parts.map((part, i) => {
    const citation = citations.find((c) => c.full_name === part);
    if (!citation) return part;
    return (
      <button key={i} type="button" className="citation-link" onClick={() => onJumpToCard(citation.id)}>
        {part}
      </button>
    );
  });
}

export function AIOverview({
  overview,
  onJumpToCard,
}: {
  overview: AIOverviewData;
  onJumpToCard: (id: string) => void;
}) {
  const [showReasoning, setShowReasoning] = useState(false);
  const uniqueCitations = [...new Map(overview.citations.map((c) => [c.id, c])).values()];

  return (
    <div className="ai-overview">
      <div className="ai-overview-head">
        <Sparkles size={14} />
        <span>AI Overview</span>
      </div>

      <p className="ai-overview-answer">{renderAnswerWithCitations(overview.answer, overview.citations, onJumpToCard)}</p>

      {uniqueCitations.length > 1 && (
        <ul className="ai-overview-list">
          {uniqueCitations.map((c) => (
            <li key={c.id}>
              <button type="button" className="citation-link" onClick={() => onJumpToCard(c.id)}>
                {c.full_name}
              </button>
            </li>
          ))}
        </ul>
      )}

      {overview.trace.length > 0 && (
        <>
          <div className="overview-toggle-row">
            <button type="button" className="overview-toggle" onClick={() => setShowReasoning((v) => !v)}>
              <ChevronDown size={13} className={showReasoning ? "rotated" : ""} />
              {showReasoning ? "Hide reasoning" : "Show reasoning"}
            </button>
          </div>

          {showReasoning && (
            <ul className="overview-trace">
              {overview.trace.map((step, i) => (
                <li key={i}>
                  <div className="overview-trace-tool">
                    <code>{step.tool}</code>
                    <span className="overview-trace-latency">{step.latency_ms}ms</span>
                  </div>
                  <p className="overview-trace-reason">{step.reason}</p>
                  <div className="overview-trace-args">
                    {Object.keys(step.args).length === 0 ? (
                      <span className="overview-arg-empty">no arguments</span>
                    ) : (
                      Object.entries(step.args).map(([k, v]) => (
                        <span key={k} className="overview-arg-chip">
                          <b>{k}</b>
                          <code>{JSON.stringify(v)}</code>
                        </span>
                      ))
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
