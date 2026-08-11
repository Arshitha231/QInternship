import { useEffect, useState } from "react";
import { getPerson } from "../api";
import type { Identity, PersonDetail } from "../types";
import { DepartmentGraph } from "./graphs/DepartmentGraph";
import { TeamGraph } from "./graphs/TeamGraph";
import { SkillsGraph } from "./graphs/SkillsGraph";

type GraphKind = "department" | "team" | "skills";

function initials(name: string): string {
  return name.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
}

export function GraphPage({
  identity,
  focusId,
  onFocusChange,
  onOpenProfile,
}: {
  identity: Identity;
  focusId: string;
  onFocusChange: (id: string) => void;
  onOpenProfile: (id: string, name: string) => void;
}) {
  const [kind, setKind] = useState<GraphKind>("department");
  const [focusPerson, setFocusPerson] = useState<PersonDetail | null | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    setFocusPerson(undefined);
    getPerson(identity, focusId).then((p) => {
      if (!cancelled) setFocusPerson(p);
    });
    return () => {
      cancelled = true;
    };
  }, [identity, focusId]);

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
        {(["department", "team", "skills"] as GraphKind[]).map((k) => (
          <button key={k} role="tab" aria-selected={kind === k} className={`tab ${kind === k ? "active" : ""}`} onClick={() => setKind(k)}>
            {k === "department" ? "Department" : k === "team" ? "Team" : "Skills"}
          </button>
        ))}
      </div>

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

      {focusPerson === undefined ? (
        <div className="skel skel-card" style={{ height: 480 }} />
      ) : kind === "department" ? (
        <DepartmentGraph identity={identity} focusId={focusId} focusPerson={focusPerson ?? null} onNavigate={onFocusChange} />
      ) : kind === "team" ? (
        <TeamGraph
          identity={identity}
          focusId={focusId}
          focusPerson={focusPerson ?? null}
          onNavigate={onFocusChange}
          onOpenProfile={onOpenProfile}
        />
      ) : (
        <SkillsGraph identity={identity} focusId={focusId} focusName={name} focusRole={role} onNavigate={onFocusChange} />
      )}
    </div>
  );
}
