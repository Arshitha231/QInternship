import { useEffect, useState } from "react";
import { getPerson } from "../api";
import type { FocusHistory } from "../hooks";
import type { Identity, PersonDetail, ViewMode } from "../types";
import { DepartmentGraph } from "./graphs/DepartmentGraph";
import { TeamGraph } from "./graphs/TeamGraph";
import { SkillsGraph } from "./graphs/SkillsGraph";
import { CommunityPage } from "./CommunityPage";
import { ChevronLeft, ChevronRight, Home } from "../icons";
import { avatarStyle } from "../avatarHue";

type GraphKind = "department" | "team" | "skills" | "community";

const KIND_LABEL: Record<GraphKind, string> = {
  department: "Department",
  team: "Team",
  skills: "Skills",
  community: "Community",
};

// One line per view saying what the picture in front of you means. The tabs
// alone named the views but never said what distinguishes them, so which of
// the four to open was guesswork -- and three of them draw the same people
// in different relationships, which is exactly the case where a name is not
// enough.
const KIND_CAPTION: Record<GraphKind, string> = {
  department: "Who reports to whom — one level up, one level down. Open any manager to go deeper.",
  team: "Everyone sharing this person's org unit.",
  skills: "The shortest way to reach someone who has a skill you don't — through people you already share work with. Always computed from you.",
  community: "Who to contact for what. Private to you.",
};

function initials(name: string): string {
  return name.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
}

export function GraphPage({
  identity,
  viewMode,
  focus,
  onOpenProfile,
}: {
  identity: Identity;
  viewMode: ViewMode;
  // The focus person AND the trail behind them -- one object rather than a
  // value plus a setter, because every graph below re-centres by calling
  // through it and the history has to see those navigations to be a history.
  focus: FocusHistory;
  onOpenProfile: (id: string, name: string) => void;
}) {
  const { focusId } = focus;
  const onFocusChange = focus.go;
  const [kind, setKind] = useState<GraphKind>("department");
  const [focusPerson, setFocusPerson] = useState<PersonDetail | null | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    setFocusPerson(undefined);
    getPerson(identity, focusId, viewMode).then((p) => {
      if (cancelled) return;
      setFocusPerson(p);
      // The trail records ids; names only exist after this fetch. Handing
      // it back is what lets Back say "Back to Priya Sharma" rather than
      // just "Back".
      if (p) focus.rememberName(p.id, p.full_name);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity, viewMode, focusId]);

  const name = focusPerson?.full_name ?? "…";
  const role = focusPerson?.job_title ?? "";

  return (
    <div className="graph-page">
      {/* Two rows, not one. WHO is centred is a statement about the data;
          WHERE you are and WHICH view are controls. Cramming all of it onto
          one line put the retrace buttons, the person, the four view tabs
          and a button in a single strip where nothing had a neighbour it
          belonged to. Splitting them costs one row of height and makes both
          halves scannable.

          The retrace controls belong on the control row with the view tabs,
          not up beside the person: both answer "take me somewhere else",
          and they are still deliberately apart from the canvas's own
          +/-/fit cluster, which moves you around ONE drawing rather than
          between people. */}
      <div className="graph-toolbar">
        <div className="graph-focus-who">
          <span className="avatar" style={avatarStyle(name)} aria-hidden="true">{focusPerson ? initials(name) : ""}</span>
          <div className="graph-focus-text">
            <p className="graph-focus-name">{name}</p>
            {role && <p className="graph-focus-role">{role}</p>}
          </div>
        </div>

        <button className="btn" onClick={() => onOpenProfile(focusId, name)}>View profile</button>
      </div>

      <div className="graph-viewbar">
        <div className="graph-history" role="group" aria-label="Graph navigation">
          <button
            className="graph-history-btn"
            onClick={focus.back}
            disabled={!focus.canGoBack}
            title={focus.backLabel ? `Back to ${focus.backLabel}` : "Back"}
            aria-label={focus.backLabel ? `Back to ${focus.backLabel}` : "Back"}
          >
            <ChevronLeft size={16} />
          </button>
          <button
            className="graph-history-btn"
            onClick={focus.forward}
            disabled={!focus.canGoForward}
            title={focus.forwardLabel ? `Forward to ${focus.forwardLabel}` : "Forward"}
            aria-label={focus.forwardLabel ? `Forward to ${focus.forwardLabel}` : "Forward"}
          >
            <ChevronRight size={16} />
          </button>
          <button
            className="graph-history-btn graph-history-home"
            onClick={focus.home}
            disabled={focus.atHome}
            title={focus.atHome ? "Already centred on you" : "Recentre on me"}
            aria-label={focus.atHome ? "Already centred on you" : "Recentre on me"}
          >
            <Home size={15} />
          </button>
        </div>

        <div className="tabs graph-tabs" role="tablist" aria-label="Graph view">
          {(["department", "team", "skills", "community"] as GraphKind[]).map((k) => (
            <button
              key={k}
              role="tab"
              // Each view has its own help topic keyed to its own tab, so the
              // tour can walk the four without needing to drive `kind` from
              // outside, and click-to-learn explains whichever tab you click.
              data-help={`graph-${k}`}
              aria-selected={kind === k}
              className={`tab ${kind === k ? "active" : ""}`}
              onClick={() => setKind(k)}
            >
              {KIND_LABEL[k]}
            </button>
          ))}
        </div>
      </div>

      <div className="graph-caption-row">
        <p className="graph-caption">{KIND_CAPTION[kind]}</p>
        {kind === "community" ? (
          // Community Graph is always the logged-in identity's own private
          // list (app/community_links.py's visibility guarantee) — it never
          // follows the focus person above, unlike the other three tabs.
          // Made explicit here rather than left implicit, since this is the
          // one place on this page where "focused person" and "whose data is
          // shown" genuinely diverge.
          <p className="graph-legend">
            <span>Always your own graph, whoever is selected above.</span>
          </p>
        ) : kind === "skills" ? (
          // A path view, not a node/edge canvas: no colours to key, and the
          // pan/zoom hint would be describing controls this tab has not got.
          <p className="graph-legend">
            <span className="graph-legend-hint">Click anyone on a route to open their profile</span>
          </p>
        ) : (
          <p className="graph-legend">
            <span><i className="dot dot-focus" />Selected</span>
            {kind === "department" && <span><i className="dot dot-person" />Reporting chain</span>}
            {kind === "team" && <>
              <span><i className="dot dot-person" />Teammate</span>
              <span><i className="dot dot-hub" />Team</span>
            </>}
            <span className="graph-legend-hint">Click a person to re-centre · drag to pan · pinch to zoom</span>
          </p>
        )}
      </div>

      {kind === "community" ? (
        <CommunityPage identity={identity} viewMode={viewMode} onOpenProfile={onOpenProfile} />
      ) : focusPerson === undefined ? (
        <div className="skel skel-card" style={{ height: 480 }} />
      ) : kind === "department" ? (
        <DepartmentGraph identity={identity} viewMode={viewMode} focusId={focusId} focusPerson={focusPerson ?? null} onNavigate={onFocusChange} />
      ) : kind === "team" ? (
        <TeamGraph
          identity={identity}
          viewMode={viewMode}
          focusId={focusId}
          focusPerson={focusPerson ?? null}
          onNavigate={onFocusChange}
          onOpenProfile={onOpenProfile}
        />
      ) : (
        <SkillsGraph
          identity={identity}
          viewMode={viewMode}
          onNavigate={(id, personName) => onOpenProfile(id, personName)}
        />
      )}
    </div>
  );
}
