import { useEffect, useState } from "react";
import { getPerson } from "../api";
import type { Identity, PersonDetail, ViewMode } from "../types";
import { DepartmentGraph } from "./graphs/DepartmentGraph";
import { TeamGraph } from "./graphs/TeamGraph";
import { SkillsGraph } from "./graphs/SkillsGraph";
import { CommunityPage } from "./CommunityPage";
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
  skills: "Skills this person has, and the colleagues who share them.",
  community: "Who to contact for what. Private to you.",
};

function initials(name: string): string {
  return name.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
}

export function GraphPage({
  identity,
  viewMode,
  focusId,
  onFocusChange,
  onOpenProfile,
}: {
  identity: Identity;
  viewMode: ViewMode;
  focusId: string;
  onFocusChange: (id: string) => void;
  onOpenProfile: (id: string, name: string) => void;
}) {
  const [kind, setKind] = useState<GraphKind>("department");
  const [focusPerson, setFocusPerson] = useState<PersonDetail | null | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    setFocusPerson(undefined);
    getPerson(identity, focusId, viewMode).then((p) => {
      if (!cancelled) setFocusPerson(p);
    });
    return () => {
      cancelled = true;
    };
  }, [identity, viewMode, focusId]);

  const name = focusPerson?.full_name ?? "…";
  const role = focusPerson?.job_title ?? "";

  return (
    <div className="graph-page">
      {/* One toolbar, not three stacked rows. Who is centred, which view,
          and the legend used to occupy three full-width bands above the
          canvas, which pushed the graph itself under the fold on a laptop
          -- the graph is the point of this page, so the chrome around it
          is now a single line. */}
      <div className="graph-toolbar">
        <div className="graph-focus-who">
          <span className="avatar" style={avatarStyle(name)} aria-hidden="true">{focusPerson ? initials(name) : ""}</span>
          <div className="graph-focus-text">
            <p className="graph-focus-name">{name}</p>
            {role && <p className="graph-focus-role">{role}</p>}
          </div>
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

        <button className="btn" onClick={() => onOpenProfile(focusId, name)}>View profile</button>
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
        ) : (
          <p className="graph-legend">
            <span><i className="dot dot-focus" />Selected</span>
            {kind === "department" && <span><i className="dot dot-person" />Reporting chain</span>}
            {kind === "team" && <>
              <span><i className="dot dot-person" />Teammate</span>
              <span><i className="dot dot-hub" />Team</span>
            </>}
            {kind === "skills" && <>
              <span><i className="dot dot-person" />Person</span>
              <span><i className="dot dot-skill" />Skill</span>
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
        <SkillsGraph identity={identity} viewMode={viewMode} focusId={focusId} focusName={name} focusRole={role} onNavigate={onFocusChange} />
      )}
    </div>
  );
}
