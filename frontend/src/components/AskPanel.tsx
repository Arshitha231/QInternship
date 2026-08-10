import { useRef, useState } from "react";
import { ApiError, ask } from "../api";
import type {
  AskResponse,
  Identity,
  MentorCandidate,
  OrgChainNode,
  PersonDetail,
  PersonSummary,
  ProjectOwnerResult,
  SkillGapItem,
  SkillScarcityItem,
  ToolResult,
} from "../types";
import { Loader, Send, Sparkles } from "../icons";

// This panel exists to make the tool-calling architecture visible, not just
// the answer. Every turn renders as a chain: the raw question, the exact
// function + arguments the model routed to (or the fact that it routed to
// none), the structured data that call returned, and only then the phrased
// answer -- so someone watching can see it chained a real decision against
// the directory, not echoed a canned reply.

function initials(name: string): string {
  return name.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
}

interface Turn {
  id: number;
  query: string;
  loading: boolean;
  error: string | null;
  response: AskResponse | null;
}

const EXAMPLES = [
  "Who is my manager?",
  "Who could mentor me in Terraform?",
  "Who owns the Employee Directory Platform project?",
  "Is there a skill gap in Figma?",
];

type PersonLike = PersonSummary | OrgChainNode | MentorCandidate;

// Plain booleans rather than `r is X` predicates -- ToolResult's members
// are concrete array types (PersonSummary[] | OrgChainNode[] | ...), so a
// synthetic union type like PersonLike[] can never satisfy TS's "predicate
// type must be assignable to the parameter type" rule even though the
// runtime check is exactly right. Callers cast after checking instead.
function isPersonArray(r: ToolResult): boolean {
  return Array.isArray(r) && r.length > 0 && "full_name" in r[0];
}
function isSkillArray(r: ToolResult): boolean {
  return Array.isArray(r) && r.length > 0 && "skill" in r[0];
}
function isSinglePerson(r: ToolResult): boolean {
  return !!r && !Array.isArray(r) && "full_name" in r;
}
function isProjectOwner(r: ToolResult): boolean {
  return !!r && !Array.isArray(r) && "owner_name" in r;
}

// The backend deliberately leaves `message` null whenever a tool call
// succeeds (see app/tool_calling.py:answer) -- it hands back the raw,
// permission-filtered result rather than running a second LLM pass to
// re-summarize it, which is a real determinism/no-hallucination tradeoff,
// not an omission. This turns that structured result into a sentence
// client-side so the trace still ends on a phrased answer instead of
// "(no message returned)". It's built only from what's actually in the
// result, so it can't invent anything the tool didn't return.
function phraseAnswer(toolCall: string, args: Record<string, unknown> | null, result: ToolResult): string {
  switch (toolCall) {
    case "find_people": {
      if (!Array.isArray(result) || result.length === 0) return "No one in the directory matched that.";
      const people = result as PersonSummary[];
      const names = people.slice(0, 5).map((p) => p.full_name);
      const extra = people.length > 5 ? `, and ${people.length - 5} more` : "";
      return `Found ${people.length} match${people.length === 1 ? "" : "es"}: ${names.join(", ")}${extra}.`;
    }
    case "get_person": {
      if (!result || Array.isArray(result)) return "Couldn't find that person.";
      const p = result as PersonDetail;
      const bits = [`${p.full_name}${p.job_title ? `, ${p.job_title}` : ""}${p.org_unit ? ` (${p.org_unit})` : ""}.`];
      if (p.manager) bits.push(`Reports to ${p.manager.full_name}.`);
      if (p.availability_status === "away" && p.delegate) bits.push(`Currently away — ${p.delegate.full_name} is covering.`);
      return bits.join(" ");
    }
    case "get_org_chain": {
      const direction = args?.direction === "up" ? "above them" : "below them";
      const n = Array.isArray(result) ? result.length : 0;
      if (n === 0) return `Nobody found ${direction} in the org chart (or that direction is restricted for your role).`;
      return `${n} ${n === 1 ? "person" : "people"} ${direction} in the reporting chain.`;
    }
    case "find_project_owner": {
      if (!result || Array.isArray(result)) return "Couldn't find an owner for that.";
      const o = result as ProjectOwnerResult;
      return `${o.owner_name} owns ${o.project_name} (${o.project_type}).`;
    }
    case "find_mentor": {
      if (!Array.isArray(result) || result.length === 0) return "No mentor candidates matched that skill right now.";
      const candidates = result as MentorCandidate[];
      return `${candidates.length} potential mentor${candidates.length === 1 ? "" : "s"} found, starting with ${candidates[0].full_name} (${candidates[0].level}).`;
    }
    case "skill_gap": {
      if (!Array.isArray(result) || result.length === 0) return "No skill gap data came back.";
      const items = result as SkillGapItem[];
      const gaps = items.filter((i) => i.gap).map((i) => i.skill);
      return gaps.length > 0 ? `Gap found in: ${gaps.join(", ")}.` : "No gaps — every skill checked has real coverage.";
    }
    case "skill_scarcity": {
      if (!Array.isArray(result) || result.length === 0) return "No scarcity data came back.";
      const items = result as SkillScarcityItem[];
      const scarcest = [...items].sort((a, b) => a.capable_count - b.capable_count)[0];
      return `Scarcest: ${scarcest.skill} (${scarcest.capable_count} people capable).`;
    }
    default:
      return "Done.";
  }
}

function PersonRow({ p, onOpenProfile }: { p: PersonLike | PersonDetail; onOpenProfile: (id: string) => void }) {
  const role = "job_title" in p ? p.job_title : undefined;
  const level = "level" in p ? p.level : undefined;
  const reason = "reason" in p ? p.reason : undefined;
  return (
    <button className="ask-person-row" onClick={() => onOpenProfile(p.id)}>
      <span className="avatar" aria-hidden="true">{initials(p.full_name)}</span>
      <span className="ask-person-info">
        <span className="ask-person-name">{p.full_name}</span>
        {(role || level) && (
          <span className="ask-person-role">{[role, level].filter(Boolean).join(" · ")}</span>
        )}
        {reason && <span className="ask-person-reason">{reason}</span>}
      </span>
    </button>
  );
}

function ResultBody({ result, onOpenProfile }: { result: ToolResult; onOpenProfile: (id: string) => void }) {
  if (result === null) {
    return <p className="ask-empty">No structured data came back with this answer.</p>;
  }
  if (Array.isArray(result) && result.length === 0) {
    return <p className="ask-empty">The query ran, but matched nobody/nothing.</p>;
  }
  if (isPersonArray(result)) {
    const people = result as PersonLike[];
    return (
      <div className="ask-person-list">
        {people.map((p) => (
          <PersonRow key={p.id} p={p} onOpenProfile={onOpenProfile} />
        ))}
      </div>
    );
  }
  if (isSinglePerson(result)) {
    const person = result as PersonDetail;
    return <PersonRow p={person} onOpenProfile={onOpenProfile} />;
  }
  if (isProjectOwner(result)) {
    const owner = result as ProjectOwnerResult;
    return (
      <div className="ask-owner-card">
        <p className="ask-owner-project">{owner.project_name}</p>
        <p className="ask-owner-meta">{owner.project_type} · {owner.classification}</p>
        <p className="ask-owner-name">Owner: {owner.owner_name}</p>
      </div>
    );
  }
  if (isSkillArray(result)) {
    const skills = result as (SkillGapItem | SkillScarcityItem)[];
    return (
      <div className="ask-skill-list">
        {skills.map((s) => (
          <div key={s.skill} className="ask-skill-row">
            <span className="ask-skill-name">{s.skill}</span>
            <span className="ask-skill-counts">
              Expert {s.expert_count} · Working {s.working_count} · Learning {s.learning_count}
              {"gap" in s && s.gap && <span className="ask-gap-flag"> · gap</span>}
            </span>
          </div>
        ))}
      </div>
    );
  }
  return <pre className="ask-raw">{JSON.stringify(result, null, 2)}</pre>;
}

export function AskPanel({ identity, onOpenProfile }: { identity: Identity; onOpenProfile: (id: string) => void }) {
  const [input, setInput] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const nextId = useRef(1);

  function submit(raw?: string) {
    const query = (raw ?? input).trim();
    if (!query) return;
    const id = nextId.current++;
    setTurns((prev) => [{ id, query, loading: true, error: null, response: null }, ...prev]);
    setInput("");
    ask(identity, query)
      .then((response) => {
        setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, loading: false, response } : t)));
      })
      .catch((e) => {
        const message = e instanceof ApiError ? e.message : "Something went wrong reaching the assistant.";
        setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, loading: false, error: message } : t)));
      });
  }

  return (
    <div className="ask-page">
      <div className="ask-intro">
        <h2 className="ask-title"><Sparkles size={17} /> Ask the directory</h2>
        <p className="ask-subtitle">
          Every question here is routed through a tool-calling layer, not a free-form chat model — the
          trace below shows exactly which directory function it called and why.
        </p>
      </div>

      <form
        className="ask-input-row"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about people, teams, skills, or projects…"
          aria-label="Ask the directory"
        />
        <button type="submit" className="btn btn-primary" disabled={!input.trim()}>
          <Send size={15} /> Ask
        </button>
      </form>

      {turns.length === 0 && (
        <div className="ask-examples">
          <p className="skill-label">Try asking</p>
          <div className="ask-example-chips">
            {EXAMPLES.map((ex) => (
              <button key={ex} type="button" className="ask-example-chip" onClick={() => submit(ex)}>
                {ex}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="ask-turns">
        {turns.map((t) => (
          <div className="ask-turn" key={t.id}>
            <ul className="timeline ask-trace">
              <li className="current">
                <p className="skill-label">Query</p>
                <p className="ask-query-text">"{t.query}"</p>
              </li>

              {t.loading && (
                <li>
                  <p className="skill-label">Routing</p>
                  <p className="ask-loading-row"><Loader size={14} /> Deciding which tool to call…</p>
                </li>
              )}

              {t.error && (
                <li>
                  <p className="skill-label">Error</p>
                  <p className="ask-error-text">{t.error}</p>
                </li>
              )}

              {t.response && (
                <>
                  <li>
                    <p className="skill-label">Tool call</p>
                    {t.response.tool_call ? (
                      <div className="ask-tool-call">
                        <code className="ask-fn-name">{t.response.tool_call}</code>
                        <div className="ask-args">
                          {Object.entries(t.response.arguments ?? {}).length === 0 ? (
                            <span className="ask-arg-chip ask-arg-empty">no arguments</span>
                          ) : (
                            Object.entries(t.response.arguments ?? {}).map(([k, v]) => (
                              <span key={k} className="ask-arg-chip">
                                <b>{k}</b> {JSON.stringify(v)}
                              </span>
                            ))
                          )}
                        </div>
                      </div>
                    ) : (
                      <p className="ask-no-tool">No tool invoked — declined, or answered without querying the directory.</p>
                    )}
                  </li>

                  <li>
                    <p className="skill-label">Result</p>
                    <ResultBody result={t.response.result} onOpenProfile={onOpenProfile} />
                  </li>

                  <li className="current">
                    <p className="skill-label">Answer</p>
                    <p className="ask-answer-text">
                      {t.response.message ??
                        (t.response.tool_call
                          ? phraseAnswer(t.response.tool_call, t.response.arguments, t.response.result)
                          : "(no message returned)")}
                    </p>
                  </li>
                </>
              )}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}
