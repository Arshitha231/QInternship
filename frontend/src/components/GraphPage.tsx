import { useEffect, useState } from "react";
import { getPerson } from "../api";
import type { Identity, PersonDetail, ViewMode } from "../types";
import { DepartmentGraph } from "./graphs/DepartmentGraph";
import { TeamGraph } from "./graphs/TeamGraph";
import { SkillsGraph } from "./graphs/SkillsGraph";
import { CommunityPage } from "./CommunityPage";

type GraphKind = "department" | "team" | "skills" | "community";

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
      <div className="graph-focus-bar">
        <div className="graph-focus-who">
          <span className="avatar" aria-hidden="true">{focusPerson ? initials(name) : ""}</span>
          <div>
            <p className="graph-focus-name">{name}</p>
            {role && <p className="graph-focus-role">{role}</p>}
          </div>
        </div>
        <div className="graph-focus-actions">
          <button className="btn" onClick={() => onOpenProfile(focusId, name)}>View profile</button>
        </div>
      </div>

      <div className="tabs" role="tablist" aria-label="Graph view" style={{ borderRadius: "var(--radius-card)", border: "1px solid var(--border)", padding: "0 8px" }}>
        {(["department", "team", "skills", "community"] as GraphKind[]).map((k) => (
          <button key={k} role="tab" aria-selected={kind === k} className={`tab ${kind === k ? "active" : ""}`} onClick={() => setKind(k)}>
            {k === "department" ? "Department" : k === "team" ? "Team" : k === "skills" ? "Skills" : "Community"}
          </button>
        ))}
      </div>

      {kind === "community" ? (
        // Community Graph is always the logged-in identity's own private
        // list (app/community_links.py's visibility guarantee) — it never
        // follows the focus person above, unlike the other three tabs. Made
        // explicit here rather than left implicit, since this is the one
        // place on this page where "focused person" and "whose data is
        // shown" genuinely diverge.
        <p className="continuity-meta">
          Always your own private graph, whoever you're focused on above — no one else can see it.
        </p>
      ) : (
        <div className="graph-legend">
          <span><i className="dot" style={{ background: "var(--purple)", borderColor: "var(--purple-hover)" }} />Focus person</span>
          {kind === "department" && <span><i className="dot" style={{ background: "#D5D2EC", borderColor: "var(--purple-800)" }} />Reporting chain</span>}
          {kind === "team" && <>
            <span><i className="dot" style={{ background: "#D5D2EC", borderColor: "var(--purple-800)" }} />Teammate</span>
            <span><i className="dot" style={{ background: "var(--amber-100)", borderColor: "var(--amber)" }} />Team (org unit)</span>
          </>}
          {kind === "skills" && <>
            <span><i className="dot" style={{ background: "#D5D2EC", borderColor: "var(--purple-800)" }} />Person</span>
            <span><i className="dot" style={{ background: "var(--green-100)", borderColor: "var(--green-800)" }} />Skill</span>
          </>}
          <span style={{ marginLeft: "auto" }}>Click a person to re-center the graph on them</span>
        </div>
      )}

      {kind === "community" ? (
        <CommunityPage identity={identity} viewMode={viewMode} onOpenProfile={onOpenProfile} />
      ) : focusPerson === undefined ? (
        <div className="skel skel-card" style={{ height: 480 }} />
      ) : kind === "department" ? (
        <DepartmentGraph identity={identity} focusId={focusId} focusPerson={focusPerson ?? null} onNavigate={onFocusChange} />
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
