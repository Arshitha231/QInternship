import { useEffect, useState } from "react";
import {
  ApiError, autoAssignMentors, confirmSuggestedOfficialLink, generateSuggestedOfficialLinks,
  listSuggestedOfficialLinks, rejectSuggestedOfficialLink,
} from "../api";
import type { Identity, SuggestedOfficialLinkOut, ViewMode } from "../types";
import { CommunityGraphCanvas } from "./CommunityGraphCanvas";

// Community Graph (app/community_links.py): each employee's own private
// "who to contact for what" graph, rendered as a canvas by
// CommunityGraphCanvas. GET /community_links takes no id parameter at all
// -- whoever `identity` is, this page can only ever show THEIR graph, never
// a colleague's, regardless of role. The HR review section below is the
// one part of this page gated by role, exactly like Continuity/Review's
// own tab-level gating elsewhere in this app.

function errorMessage(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback;
}

// ---------------------------------------------------------------------------
// HR-only: review office/role -> candidate suggestions bootstrapped from
// existing office/job-title data. Never renders for a non-hr identity --
// the backend 403s every call here regardless, this is just the frontend
// not offering a form to a wall (same discipline as ReviewPage's IT gate).
// ---------------------------------------------------------------------------

function SuggestionReview({ identity, viewMode }: { identity: Identity; viewMode: ViewMode }) {
  const [suggestions, setSuggestions] = useState<SuggestedOfficialLinkOut[] | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    listSuggestedOfficialLinks(identity, viewMode).then((rows) => {
      if (!cancelled) setSuggestions(rows);
    });
    return () => {
      cancelled = true;
    };
  }, [identity, viewMode, refreshToken]);

  async function generate() {
    setGenerating(true);
    setError(null);
    try {
      await generateSuggestedOfficialLinks(identity, viewMode);
      setRefreshToken((t) => t + 1);
    } catch (e) {
      setError(errorMessage(e, "Couldn't generate suggestions — try again."));
    } finally {
      setGenerating(false);
    }
  }

  async function act(
    id: number, action: (identity: Identity, id: number, viewMode: ViewMode) => Promise<unknown>,
  ) {
    setBusyId(id);
    setError(null);
    try {
      await action(identity, id, viewMode);
      setRefreshToken((t) => t + 1);
    } catch (e) {
      setError(errorMessage(e, "Couldn't update — try again."));
    } finally {
      setBusyId(null);
    }
  }

  const pending = (suggestions ?? []).filter((s) => s.status === "pending");

  return (
    <section className="card">
      <div className="card-head">
        <h2>Suggested official links (HR)</h2>
        <button className="btn" disabled={generating} onClick={generate}>
          {generating ? "Scanning…" : "Generate suggestions"}
        </button>
      </div>
      <p className="continuity-meta">
        Derived from office and job-title data. Confirming one adds the official contact to every
        active employee's graph in that office — nothing here is real until you confirm it.
      </p>
      {error && <p className="bio-error">{error}</p>}
      {suggestions === null ? (
        <div className="skel skel-card" style={{ height: 90 }} />
      ) : pending.length === 0 ? (
        <p className="continuity-meta">No pending suggestions.</p>
      ) : (
        <ul className="review-candidate-list">
          {pending.map((s) => (
            <li key={s.id}>
              <div>
                <p className="job">{s.role_label} — office #{s.office_id}</p>
                <p className="job-meta">Candidate: {s.candidate_employee_id}</p>
              </div>
              <div className="review-change-actions">
                <button
                  className="btn btn-primary" disabled={busyId === s.id}
                  onClick={() => act(s.id, confirmSuggestedOfficialLink)}
                >
                  Confirm
                </button>
                <button
                  className="btn btn-danger-outline" disabled={busyId === s.id}
                  onClick={() => act(s.id, rejectSuggestedOfficialLink)}
                >
                  Reject
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// HR-only: run the mentor auto-assignment sweep. Unlike SuggestionReview
// above, this creates the official mentor link directly -- there is no
// confirm step to review, so this section is a single trigger + result
// summary rather than a queue (see app/community_links.py's
// auto_assign_mentors for why mentor pairing skips the suggest/confirm
// shape every other official-link kind uses).
// ---------------------------------------------------------------------------

function MentorSweep({ identity, viewMode }: { identity: Identity; viewMode: ViewMode }) {
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<{ count: number } | null>(null);

  async function run() {
    setRunning(true);
    setError(null);
    try {
      const created = await autoAssignMentors(identity, viewMode);
      setLastRun({ count: created.length });
    } catch (e) {
      setError(errorMessage(e, "Couldn't run the mentor sweep — try again."));
    } finally {
      setRunning(false);
    }
  }

  return (
    <section className="card">
      <div className="card-head">
        <h2>Mentor assignment (HR)</h2>
        <button className="btn" disabled={running} onClick={run}>
          {running ? "Assigning…" : "Assign mentors to new hires"}
        </button>
      </div>
      <p className="continuity-meta">
        Pairs any new hire who doesn't have one yet with an eligible colleague, and creates the
        official mentor link immediately — no review step, since a pairing is specific to one
        person rather than an office-wide contact. It expires automatically after the configured
        mentor period and becomes a normal editable personal link.
      </p>
      {error && <p className="bio-error">{error}</p>}
      {lastRun && !error && (
        <p className="continuity-meta">
          {lastRun.count === 0
            ? "No new hires needed a mentor."
            : `Assigned ${lastRun.count} mentor${lastRun.count === 1 ? "" : "s"}.`}
        </p>
      )}
    </section>
  );
}

export function CommunityPage({
  identity, viewMode, onOpenProfile,
}: { identity: Identity; viewMode: ViewMode; onOpenProfile: (id: string, name: string) => void }) {
  return (
    <div className="review-page">
      <section className="card">
        <h2>Your community graph</h2>
        <p className="continuity-meta">
          A private list of who to contact for what — only you can see this, whatever your role.
          Official contacts (payroll, HR, a mentor) are set by HR and read-only; use Edit to add
          your own personal connections.
        </p>
      </section>

      <CommunityGraphCanvas identity={identity} viewMode={viewMode} onOpenProfile={onOpenProfile} />

      {/* HR bootstrapping surfaces, so work mode only — an ordinary
          colleague has no suggestion queue to review or sweep to run, and
          the server refuses these calls in employee mode
          (app.community_links._authorize_hr). Same shape as the Continuity,
          Review and Admin tabs. */}
      {identity.role === "hr" && viewMode === "work" && (
        <>
          <MentorSweep identity={identity} viewMode={viewMode} />
          <SuggestionReview identity={identity} viewMode={viewMode} />
        </>
      )}
    </div>
  );
}
