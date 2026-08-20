import { useState } from "react";
import type { FormEvent } from "react";
import { AlertCircle, ChevronDown, Loader, Send, Sparkles } from "../icons";
import { ApiError, askAssistant } from "../api";
import { PersonCard } from "./PersonCard";
import type { AskHistoryTurn, AskStep, Identity, PersonSummary, ViewMode } from "../types";

// Follow-up chat (Conversational Assistant plan, phase 1). Renders below
// the primary AIOverview answer, reusing its exact visual vocabulary
// (ask-turn/overview-trace/results-grid) so a follow-up reads as more of
// the same assistant, not a second, differently-styled one.
//
// `history` holds only PLANS (tool + arguments) from prior turns, per
// HistoryTurn/_history_messages on the backend -- never a turn's result.
// Every turn, first or fifth, is re-authorized fresh server-side; nothing
// client-side is ever trusted as a stand-in for that.

interface Turn {
  question: string;
  answer: string;
  steps: AskStep[];
  people: PersonSummary[];
}

interface Props {
  identity: Identity;
  viewMode: ViewMode;
  onSelect: (id: string, name: string) => void;
  // The result cards currently shown above this box -- their ids ride
  // along on every follow-up (askAssistant's contextPersonIds) so "who is
  // the best of these" has a "these" to resolve. Just ids: the server
  // re-resolves each one itself (app.people.resolve_context_people) rather
  // than trusting a name/id pair this client hands it.
  contextPeople: PersonSummary[];
}

function isPersonSummary(value: unknown): value is PersonSummary {
  return (
    typeof value === "object" && value !== null &&
    "id" in value && "full_name" in value && "job_title" in value
  );
}

export function AskChat({ identity, viewMode, onSelect, contextPeople }: Props) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [history, setHistory] = useState<AskHistoryTurn[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openTraceIndex, setOpenTraceIndex] = useState<number | null>(null);

  async function send(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const question = input.trim();
    if (!question || loading) return;
    setInput("");
    setError(null);
    setLoading(true);
    try {
      const res = await askAssistant(identity, question, viewMode, history, contextPeople.map((p) => p.id));
      const people = Array.isArray(res.result) ? res.result.filter(isPersonSummary) : [];
      const answer = res.message ?? (
        people.length > 0
          ? `${people.length} ${people.length === 1 ? "person matches" : "people match"}.`
          : "Done."
      );
      setTurns((t) => [...t, { question, answer, steps: res.steps ?? [], people }]);
      // Next turn's history entry -- the PLAN this turn resolved to, never
      // its result. A turn with no tool call (a clarifying question, an
      // out-of-scope reply) carries its text instead; see HistoryTurn.
      setHistory((h) => [...h, {
        message: question,
        tool_call: res.tool_call,
        arguments: res.arguments,
        assistant_text: res.tool_call ? null : res.message,
      }]);
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
            <div className="ask-turn" key={i}>
              <p className="ask-query-text">{t.question}</p>
              <p className="ai-overview-answer">{t.answer}</p>

              {t.people.length > 0 && (
                <div className="results-grid">
                  {t.people.map((p) => (
                    <PersonCard key={p.id} person={p} onClick={() => onSelect(p.id, p.full_name)} />
                  ))}
                </div>
              )}

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
