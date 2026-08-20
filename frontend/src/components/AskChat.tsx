import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { AlertCircle, ChevronDown, Loader, Send, Sparkles } from "../icons";
import { ApiError, askAssistant, getConversation } from "../api";
import { PersonCard } from "./PersonCard";
import { SuggestionChip } from "./SuggestionChip";
import type { AskHistoryTurn, AskStep, FollowUpSuggestion, Identity, PersonSummary, ViewMode } from "../types";

// Follow-up chat (Conversational Assistant plan, phase 1). Renders below
// the primary AIOverview answer, reusing its exact visual vocabulary
// (ask-turn/overview-trace/results-grid) so a follow-up reads as more of
// the same assistant, not a second, differently-styled one.
//
// The conversation is persisted server-side (AssistantConversation/
// AssistantTurn) -- conversationId is owned by App (shared with the main
// search bar's own assisted answers, which write to this same "search"
// conversation; see App.tsx's own comment on why). This component only
// rehydrates its OWN display list from GET /conversations/search on
// mount, and reports the id back up whenever a call opens or continues one.

interface Turn {
  question: string;
  answer: string;
  /** Loaded from server history rather than answered on this page, so it
   *  has no result to show -- see turnFromHistory. */
  replayed?: boolean;
  steps: AskStep[];
  people: PersonSummary[];
  suggestion: FollowUpSuggestion | null;
}

interface Props {
  identity: Identity;
  viewMode: ViewMode;
  conversationId: number | null;
  onConversationId: (id: number | null) => void;
  onSelect: (id: string, name: string) => void;
  // The result cards currently shown above this box -- their ids ride
  // along on every follow-up (askAssistant's contextPersonIds) so "who is
  // the best of these" has a "these" to resolve. Just ids: the server
  // re-resolves each one itself (app.people.resolve_context_people) rather
  // than trusting a name/id pair this client hands it.
  contextPeople: PersonSummary[];
  onAddToFilter: (skill: string, level?: string | null) => void;
}

function isPersonSummary(value: unknown): value is PersonSummary {
  return (
    typeof value === "object" && value !== null &&
    "id" in value && "full_name" in value && "job_title" in value
  );
}

// How many previously-asked questions to show above the composer.
//
// Two things make the raw list unusable. The main search bar writes to
// this SAME conversation (see App.tsx), so every query typed up there
// lands here too; and re-asking the same thing appends a row each time --
// a real session had five consecutive "who can do terraform" entries.
//
// So: collapse consecutive repeats of the same question, and keep only
// the last few. The full conversation is still on the server and still
// what the model replays; this is purely what is worth putting on screen
// above the box you are about to type into.
const MAX_REPLAYED = 3;

function recentHistory(turns: AskHistoryTurn[]): AskHistoryTurn[] {
  const collapsed = turns.filter(
    (t, i) => i === 0 || t.message.trim() !== turns[i - 1].message.trim(),
  );
  return collapsed.slice(-MAX_REPLAYED);
}

// A rehydrated turn carries only the PLAN it resolved to (see
// AskHistoryTurn) -- never a stored result or phrased answer. That is a
// deliberate backend property, not a gap: history stores the question and
// the tool, and the tool is re-run through the enforce()-gated dispatcher
// on every new turn, so a stale answer about a person can never sit in
// history or re-enter the model's context.
//
// The consequence is that a rehydrated tool-backed turn HAS no answer to
// show, and it must not pretend otherwise. It used to render
// "Resolved via find_people." in the answer slot -- a developer-facing
// string sitting exactly where a sentence about people belongs, which on
// a reloaded page turned the whole panel into a list of function names.
//
// So a replayed turn is now rendered as what it is: a question asked
// earlier, with a control to ask it again. Asking again produces a real
// answer, and does it fast, because the response cache already holds it.
//
// assistant_text is the one piece of real prose ever stored, and only for
// a turn with no tool call (a clarifying question, an out-of-scope reply)
// -- connective language with no factual claim about a person. Those
// replay verbatim, because there the stored text IS the whole answer.
function turnFromHistory(h: AskHistoryTurn): Turn {
  return {
    question: h.message,
    answer: h.assistant_text ?? "",
    replayed: h.assistant_text == null,
    steps: [], people: [], suggestion: null,
  };
}

export function AskChat({
  identity, viewMode, conversationId, onConversationId, onSelect, contextPeople, onAddToFilter,
}: Props) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openTraceIndex, setOpenTraceIndex] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    getConversation(identity, "search", viewMode).then((res) => {
      if (cancelled) return;
      setTurns(recentHistory(res.turns).map(turnFromHistory));
      onConversationId(res.conversation_id);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity, viewMode]);

  async function send(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const question = input.trim();
    if (!question || loading) return;
    setInput("");
    await ask(question);
  }

  /** Re-run a question. Used by the composer and by "Ask again" on a
   *  replayed turn, which is the only way a rehydrated turn can ever get a
   *  real answer -- the server deliberately stored none. */
  async function ask(question: string) {
    if (loading) return;
    setError(null);
    setLoading(true);
    try {
      const res = await askAssistant(
        identity, question, viewMode, conversationId, contextPeople.map((p) => p.id),
      );
      const people = Array.isArray(res.result) ? res.result.filter(isPersonSummary) : [];
      const answer = res.message ?? (
        people.length > 0
          ? `${people.length} ${people.length === 1 ? "person matches" : "people match"}.`
          : "Done."
      );
      setTurns((t) => [...t, { question, answer, replayed: false, steps: res.steps ?? [],
                               people, suggestion: res.suggestion ?? null }]);
      if (res.conversation_id !== undefined) onConversationId(res.conversation_id ?? null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't answer that follow-up — try rephrasing.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="ask-followup" data-help="ask-followup">
      <div className="ai-overview-head">
        <Sparkles size={14} />
        <span>Ask a follow-up</span>
      </div>

      {turns.length > 0 && (
        <div className="ask-turns">
          {turns.map((t, i) => (
            <div className={`ask-turn ${t.replayed ? "ask-turn-replayed" : ""}`} key={i}>
              <p className="ask-query-text">{t.question}</p>
              {t.replayed ? (
                // No answer exists for this turn -- the server stored the
                // plan, not the result. Offer to run it rather than print
                // a placeholder where a sentence about people belongs.
                <p className="ask-replayed-row">
                  <span className="ask-replayed-note">Asked earlier</span>
                  <button
                    type="button"
                    className="ask-again"
                    disabled={loading}
                    onClick={() => void ask(t.question)}
                  >
                    Ask again
                  </button>
                </p>
              ) : (
                <p className="ai-overview-answer">{t.answer}</p>
              )}

              {t.people.length > 0 && (
                <div className="results-grid">
                  {t.people.map((p) => (
                    <PersonCard key={p.id} person={p} onClick={() => onSelect(p.id, p.full_name)} />
                  ))}
                </div>
              )}

              {t.suggestion && <SuggestionChip suggestion={t.suggestion} onAddToFilter={onAddToFilter} />}

              {t.steps.length > 1 && (
                <>
                  <div className="overview-toggle-row">
                    <button
                      type="button"
                      className="overview-toggle"
                      onClick={() => setOpenTraceIndex((cur) => (cur === i ? null : i))}
                    >
                      <ChevronDown size={13} className={openTraceIndex === i ? "rotated" : ""} />
                      {openTraceIndex === i ? "Hide reasoning" : `Show reasoning (${t.steps.length} steps)`}
                    </button>
                  </div>
                  {openTraceIndex === i && (
                    <ul className="overview-trace">
                      {t.steps.map((s, j) => (
                        <li key={j}>
                          <div className="overview-trace-tool">
                            <code>{s.tool}</code>
                            <span className="overview-trace-latency">{s.latency_ms}ms</span>
                          </div>
                          <div className="overview-trace-args">
                            {Object.keys(s.arguments).length === 0 ? (
                              <span className="overview-arg-empty">no arguments</span>
                            ) : (
                              Object.entries(s.arguments).map(([k, v]) => (
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
          placeholder={turns.length > 0 ? "Ask another follow-up…" : "Ask a follow-up about these results…"}
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
