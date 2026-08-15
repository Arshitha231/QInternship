import { useEffect, useState } from "react";
import { ApiError, findPeople } from "../../api";
import type { Identity, OrgChainNode, PersonDetail, ViewMode } from "../../types";
import { NodeBox, useTreeConnectors, type TreeGroup } from "./treeShared";

// Same hierarchical tree as DepartmentGraph -- a fixed node on top (here,
// the team itself) with its members in a row directly below, connected
// with orthogonal elbow connectors -- just with a team hub standing in
// for "manager" instead of an actual person, and no expand/collapse since
// a team roster is already flat (one level, not a reporting chain).

const TEAM_CAP = 30;

interface Props {
  identity: Identity;
  viewMode: ViewMode;
  focusId: string;
  focusPerson: PersonDetail | null;
  onNavigate: (id: string) => void;
  onOpenProfile: (id: string, name: string) => void;
}

function HubBox({ label, registerRef }: { label: string; registerRef: (el: HTMLDivElement | null) => void }) {
  return (
    <div ref={registerRef} className="tree-node tree-node-hub">
      <p className="tree-node-name">{label}</p>
    </div>
  );
}

export function TeamGraph({ identity, viewMode, focusId, focusPerson, onNavigate, onOpenProfile }: Props) {
  const orgUnit = focusPerson?.org_unit ?? null;
  const [teammates, setTeammates] = useState<OrgChainNode[] | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setTeammates(undefined);
    setError(null);

    if (!orgUnit) {
      setTeammates([]);
      return;
    }

    findPeople(identity, { org_unit: orgUnit }, viewMode)
      .then((results) => {
        if (cancelled) return;
        setTeammates(
          results
            .filter((r) => r.id !== focusId)
            .slice(0, TEAM_CAP)
            .map((r) => ({
              id: r.id,
              full_name: r.full_name,
              job_title: r.job_title,
              org_unit: r.org_unit,
              depth: 1,
              availability_status: r.availability_status,
              delegate: r.delegate,
              has_reports: false,
            })),
        );
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof ApiError ? e.message : "Unknown error");
        setTeammates([]);
      });

    return () => {
      cancelled = true;
    };
  }, [identity, focusId, orgUnit]);

  function handleNodeClick(id: string, name: string) {
    if (id === focusId) return;
    // Team has no drill-down like Department's expand/collapse -- recentering
    // alone doesn't surface who reports to someone, so the profile panel
    // opens alongside the recenter to give that detail immediately.
    onNavigate(id);
    onOpenProfile(id, name);
  }

  const hubId = orgUnit ? `hub:${orgUnit}` : null;
  const groups: TreeGroup[] = hubId
    ? [{ parentId: hubId, childIds: [focusId, ...(teammates ?? []).map((t) => t.id)] }]
    : [];
  const { wrapRef, registerNode, linePaths, svgSize } = useTreeConnectors(groups, [hubId, teammates, focusId]);

  if (error) {
    return (
      <div className="state-block error" style={{ padding: "50px 20px" }}>
        <strong>Couldn't load the team</strong>
        <p>{error}</p>
      </div>
    );
  }
  if (teammates === undefined || !focusPerson) {
    return <div className="skel skel-card" style={{ height: 480 }} />;
  }

  const focusNode: OrgChainNode = {
    id: focusId,
    full_name: focusPerson.full_name,
    job_title: focusPerson.job_title ?? "",
    org_unit: orgUnit ?? "",
    depth: 0,
    availability_status: focusPerson.availability_status ?? "available",
    delegate: focusPerson.delegate,
    has_reports: false,
  };

  return (
    <div className="org-tree-wrap" ref={wrapRef}>
      <svg
        className="org-tree-lines"
        width={svgSize.width}
        height={svgSize.height}
        viewBox={`0 0 ${svgSize.width} ${svgSize.height}`}
      >
        {linePaths.map((p) => (
          <path key={p.id} d={p.d} className="tree-edge" />
        ))}
      </svg>
      <div className="org-tree">
        {hubId && (
          <div className="tree-tier tree-tier-manager">
            <HubBox label={orgUnit!} registerRef={registerNode(hubId)} />
          </div>
        )}
        <div className="tree-tier tree-tier-reports">
          <NodeBox node={focusNode} focus registerRef={registerNode(focusId)} />
          {teammates.map((t) => (
            <NodeBox key={t.id} node={t} onClick={() => handleNodeClick(t.id, t.full_name)} registerRef={registerNode(t.id)} />
          ))}
        </div>
      </div>
    </div>
  );
}
