import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { AlertCircle, Loader, Send, Sparkles } from "../icons";
import { ApiError, getConversation, prdAsk } from "../api";
import { SuggestionChip } from "./SuggestionChip";
import type {
  AskHistoryTurn, AskResponse, FollowUpSuggestion, Identity, ProjectRequirementsOut,
  ProjectRequirementsSummaryItem, ViewMode,
} from "../types";

// The PRD assistant's own chat surface -- a genuinely separate component
// from AskChat, not a reused one, because it is a separate conversation
// (app.tool_calling.PRD_PROFILE) with its own tool vocabulary that has no
// people-search tool at all. It answers only from what's on record for a
// project's requirements, never from the general directory.
//
// Scoped to one project: AssistantConversation.project_id partitions PRD
// conversations per-project server-side, so switching the picker's
// selected project must show THAT project's thread, not whatever was most
// recently discussed. The host remounts this via `key={projectId}`, same
// technique AskChat's parent uses for `key={debouncedQuery}`.

interface Turn {
  question: string;
  answer: string;
  suggestion: FollowUpSuggestion | null;
}

interface Props {
  identity: Identity;
  viewMode: ViewMode;
  projectId: number;
  projectName: string;
}

function turnFromHistory(h: AskHistoryTurn): Turn {
  const answer = h.assistant_text ?? (h.tool_call ? `Resolved via ${h.tool_call}.` : "");
  return { question: h.message, answer, suggestion: null };
}

// tool_calling.answer() has no deterministic _phrase()-template tier for
// this surface (see the plan's own note on why importing unified_search's
// back in would be circular) -- when the phrasing model isn't configured,
// fails, or its output doesn't pass grounding, `message` stays null and
// this fallback IS the entire answer, not a supplement to one. Built
// directly from `result`, so it can't fabricate a name or count that
// isn't there, the same guarantee AskChat's "N people match." fallback
// gives for search -- but shaped for requirements, not people, since "N
// people match." is actively wrong here.
function fallbackAnswer(res: AskResponse): string {
  if (res.message !== null) return res.message;
  if (res.tool_call === "get_project_requirements" && res.result && typeof res.result === "object") {
    const r = res.result as ProjectRequirementsOut;
    const skillCount = r.skills?.length ?? 0;
    const noteCount = r.notes?.length ?? 0;
    return (
      `${r.project_name}: ${skillCount} required skill${skillCount === 1 ? "" : "s"}, ` +
      `${noteCount} note${noteCount === 1 ? "" : "s"} on file.`
    );
  }
  if (res.tool_call === "list_project_requirements_summary" && Array.isArray(res.result)) {
    const items = res.result as ProjectRequirementsSummaryItem[];
    if (items.length === 0) return "No projects have requirements on file yet.";
    return `${items.length} project${items.length === 1 ? "" : "s"} with requirements on file.`;
  }
  return "Done.";
}

export function PRDChat({ identity, viewMode, projectId, projectName }: Props) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getConversation(identity, "prd", viewMode, projectId).then((res) => {
      if (cancelled) return;
      setTurns(res.turns.map(turnFromHistory));
      setConversationId(res.conversation_id);
    });
    return () => {
      cancelled = true;
    };
  }, [identity, viewMode, projectId]);

  async function send(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const question = input.trim();
    if (!question || loading) return;
    setInput("");
    setError(null);
    setLoading(true);
    try {
      const res = await prdAsk(identity, question, viewMode, projectId, conversationId);
      setTurns((t) => [...t, { question, answer: fallbackAnswer(res), suggestion: res.suggestion ?? null }]);
      if (res.conversation_id !== undefined) setConversationId(res.conversation_id ?? null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't answer that — try rephrasing.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="ask-followup" data-help="prd-chat">
      <div className="ai-overview-head">
        <Sparkles size={14} />
        <span>Ask about {projectName}'s requirements</span>
      </div>

      {turns.length > 0 && (
        <div className="ask-turns">
          {turns.map((t, i) => (
            <div className="ask-turn" key={i}>
              <p className="ask-query-text">{t.question}</p>
              <p className="ai-overview-answer">{t.answer}</p>
              {t.suggestion && <SuggestionChip suggestion={t.suggestion} />}
            </div>
          ))}
        </div>
      )}

      {loading && (
        <p className="ask-loading-row">
          <Loader size={14} className="spin" /> Thinking…
        </p>
      )}
      {error && (
        <p className="ask-error-text">
          <AlertCircle size={13} /> {error}
        </p>
      )}

      <form className="ask-input-row" onSubmit={send}>
        <input
          type="text"
          placeholder={turns.length > 0 ? "Ask another question…" : "e.g. what does this project need?"}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={loading}
        />
        <button type="submit" className="btn" disabled={loading || !input.trim()}>
          <Send size={14} /> Ask
        </button>
      </form>
    </div>
  );
}
