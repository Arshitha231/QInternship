import { useState } from "react";
import { ApiError, autoAssignMentors } from "../api";
import type { Identity, ViewMode } from "../types";
import { CommunityGraphCanvas } from "./CommunityGraphCanvas";

// Community Graph (app/community_links.py): each employee's own private
// "who to contact for what" graph, rendered as a canvas by
// CommunityGraphCanvas. The seven standard roles are resolved from org data
// on every read (app/community_roles.py) rather than staged for HR to
// confirm one office at a time — which is why the suggestion queue that
// used to live on this page is gone. GET /community_links takes no id parameter at all
// -- whoever `identity` is, this page can only ever show THEIR graph, never
// a colleague's, regardless of role. The HR review section below is the
// one part of this page gated by role, exactly like Continuity/Review's
// own tab-level gating elsewhere in this app.

function errorMessage(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback;
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
      {/* Collapsed by default. This was a full card of prose sitting above
          the canvas, which pushed the graph itself below the fold on a
          laptop -- on a page whose entire point is the graph. The one-line
          summary is already in GraphPage's caption row; the detail is still
          here for anyone who wants it, just not in front of the thing it
          describes. */}
      <details className="graph-explainer">
        <summary>How these connections are worked out</summary>
        <p className="continuity-meta">
          Who to contact for what — only you can see this, whatever your role. The seven standard
          roles are worked out from the directory itself (your reporting line, your office, your
          skills and your projects), so they stay right when people move. If nobody in your office
          fills a role, the graph points you at the nearest office that does. Use <strong>Edit</strong>
          {" "}to search the directory and add your own connections alongside them.
        </p>
      </details>

      <CommunityGraphCanvas identity={identity} viewMode={viewMode} onOpenProfile={onOpenProfile} />

      {/* HR bootstrapping surfaces, so work mode only — an ordinary
          colleague has no suggestion queue to review or sweep to run, and
          the server refuses these calls in employee mode
          (app.community_links._authorize_hr). Same shape as the Continuity,
          Review and Admin tabs. */}
      {identity.role === "hr" && viewMode === "work" && (
        <MentorSweep identity={identity} viewMode={viewMode} />
      )}
    </div>
  );
}
