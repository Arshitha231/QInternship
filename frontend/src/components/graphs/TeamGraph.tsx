import { useEffect, useState } from "react";
import { ApiError, getTeamGraph, type TeamGraphResponse } from "../../api";
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

export function TeamGraph({ identity, 
  viewMode, 
  focusId, 
  focusPerson, 
  highlightedIds = new Set(), 
  onNavigate, 
  onOpenProfile }: Props) {
  const orgUnit = focusPerson?.org_unit ?? null;
  const [data, setData] = useState<TeamGraphResponse | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(undefined);
    setError(null);

    // Call the new project-based backend endpoint
    getTeamGraph(identity, focusId)
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof ApiError ? e.message : "Unknown error");
        setData(null);
      });

    return () => {
      cancelled = true;
    };
  }, [identity, focusId]); // orgUnit is no longer a dependency

  function handleNodeClick(id: string, name: string) {
    if (id === focusId) return;
    // Team has no drill-down like Department's expand/collapse -- recentering
    // alone doesn't surface who reports to someone, so the profile panel
    // opens alongside the recenter to give that detail immediately.
    onNavigate(id);
    onOpenProfile(id, name);
  }

  // ... (keep handleNodeClick the same) ...

  const groups: TreeGroup[] = [];
  const hubIds: string[] = [];

  if (data) {
    // 1. Connect every Project Hub to the Focus User
    data.projects.forEach((p) => {
      const pId = `proj-${p.id}`;
      hubIds.push(pId);
      groups.push({ parentId: pId, childIds: [focusId] });
    });

    // 2. Connect the Focus User to all Teammates
    if (data.teammates.length > 0) {
      groups.push({ 
        parentId: focusId, 
        childIds: data.teammates.map((t) => t.person.id) 
      });
    }
  }

  // Pass the dynamic hubIds to the connector dependencies
  const { wrapRef, registerNode, linePaths, svgSize } = useTreeConnectors(groups, [data, focusId]);

  if (error) {
    return (
      <div className="state-block error" style={{ padding: "50px 20px" }}>
        <strong>Couldn't load the team</strong>
        <p>{error}</p>
      </div>
    );
  }
  
  if (data === undefined || !focusPerson) {
    return <div className="skel skel-card" style={{ height: 480 }} />;
  }

 const focusNode: OrgChainNode = {
    id: focusId,
    full_name: focusPerson.full_name,
    job_title: focusPerson.job_title ?? "",
    org_unit: focusPerson.org_unit ?? "",
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
        
        {/* Render a HubBox for every active project */}
        {data.projects.length > 0 && (
          <div className="tree-tier tree-tier-manager" style={{ display: "flex", gap: "20px", justifyContent: "center" }}>
            {data.projects.map((p) => (
              <HubBox 
                key={`proj-${p.id}`} 
                label={`${p.name} (${p.classification})`} 
                registerRef={registerNode(`proj-${p.id}`)} 
              />
            ))}
          </div>
        )}
        
        {/* Center Node (with Highlighting) */}
        <div className="tree-tier tree-tier-center">
          <NodeBox 
            node={focusNode} 
            focus 
            highlighted={highlightedIds.has(focusId)}
            registerRef={registerNode(focusId)} 
          />
        </div>
        
        {/* Teammates Nodes (with Highlighting) */}
        {data.teammates.length > 0 && (
          <div className="tree-tier tree-tier-reports">
            {data.teammates.map((t) => (
              <NodeBox 
                key={t.person.id} 
                node={t.person} 
                highlighted={highlightedIds.has(t.person.id)}
                onClick={() => handleNodeClick(t.person.id, t.person.full_name)} 
                registerRef={registerNode(t.person.id)} 
              />
            ))}
          </div>
        )}

      </div>
    </div>
  );
}
