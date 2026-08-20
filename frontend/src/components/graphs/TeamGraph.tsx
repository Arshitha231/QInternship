import { useEffect, useState } from "react";
import { ApiError, findPeople, getOrgChart } from "../../api";
import type { Identity, OrgChainNode, PersonDetail, ViewMode } from "../../types";
import { NodeBox, useTreeConnectors, wrapWidth, type TreeGroup } from "./treeShared";
import { useFitOnChange, useZoomPan, ZoomPanFrame } from "../ZoomPanFrame";
import { ChevronsDown, ChevronsUp, Users } from "../../icons";

// The team's own org unit on top, its members in a wrapped block directly
// below. Two things differ from DepartmentGraph's tree.
//
// The hub is an org unit, not a person, and it is NOT joined to each member
// by its own elbow connector: teammates are siblings, with no ordering and
// no reporting between them, and once the roster wraps into a grid a
// per-member elbow reads as row 2 reporting to row 1. One trunk from the
// hub into the banded members block says the true thing instead.
//
// Members who manage people DO expand -- a team roster is flat, but the
// people on it need not be, and "who works for this person" was otherwise
// only reachable by re-centring the whole view on them, which loses the
// roster you were reading.
//
// The opened sub-team gets its own full-width band UNDER the roster rather
// than being nested inside the member's grid cell, and only one member is
// open at a time. Nesting it in the cell was the obvious way and it laid
// out badly: one column grew three rows taller than its neighbours, the
// block went from 1164x922 to 1164x1487, and fit-to-view had to drop to 42%
// -- at which point no name on the card is readable. Both blocks stay wide
// and short this way, which is the shape the frame actually is.

const TEAM_CAP = 30;

interface Props {
  identity: Identity;
  viewMode: ViewMode;
  focusId: string;
  focusPerson: PersonDetail | null;
  onNavigate: (id: string) => void;
  onOpenProfile: (id: string, name: string) => void;
}

// The org unit itself, standing where a manager stands in DepartmentGraph.
// It carries the headcount because "how big is this team" is the first
// question the view is asked, and counting cards to answer it is exactly
// the kind of work a graph is supposed to save you.
function HubBox({
  label, count, registerRef,
}: {
  label: string;
  count: number;
  registerRef: (el: HTMLDivElement | null) => void;
}) {
  return (
    <div ref={registerRef} className="tree-node tree-node-hub">
      <span className="tree-hub-icon" aria-hidden="true"><Users size={18} /></span>
      <p className="tree-node-name">{label}</p>
      <p className="tree-node-role">{count} {count === 1 ? "person" : "people"}</p>
    </div>
  );
}

export function TeamGraph({ identity, viewMode, focusId, focusPerson, onNavigate, onOpenProfile }: Props) {
  const orgUnit = focusPerson?.org_unit ?? null;
  const [teammates, setTeammates] = useState<OrgChainNode[] | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  // One open sub-team at a time -- see the header note. Opening another
  // replaces it rather than stacking, which keeps the band a fixed part of
  // the layout instead of an accumulating pile.
  const [openId, setOpenId] = useState<string | null>(null);
  const [childrenCache, setChildrenCache] = useState<Record<string, OrgChainNode[]>>({});
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setTeammates(undefined);
    setError(null);
    setOpenId(null);
    setChildrenCache({});
    setLoading(false);

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
              // Absent in employee view mode, where policy withholds the
              // downward chain -- so nobody gets an expand control there,
              // which is right: it would expand to nothing.
              has_reports: r.has_reports ?? false,
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
  }, [identity, viewMode, focusId, orgUnit]);

  function toggleExpand(id: string) {
    if (openId === id) {
      setOpenId(null);
      return;
    }
    setOpenId(id);
    if (childrenCache[id]) return;
    setLoading(true);
    getOrgChart(identity, id, "down", viewMode, 1)
      .then((children) => setChildrenCache((prev) => ({ ...prev, [id]: children })))
      .finally(() => setLoading(false));
  }

  function handleNodeClick(id: string, name: string) {
    if (id === focusId) return;
    // Re-centring alone doesn't surface who reports to someone -- that's
    // what the expand pill is for now -- so a click still opens the profile
    // panel alongside, for the detail the card can't carry.
    onNavigate(id);
    onOpenProfile(id, name);
  }

  const hubId = orgUnit ? `hub:${orgUnit}` : null;
  // Only the expanded members' own subtrees get elbow connectors. The
  // hub->members relationship is drawn as a single CSS trunk instead (see
  // .tree-tier-trunk and this file's header note).
  const openChildren = openId ? childrenCache[openId] : undefined;
  const groups: TreeGroup[] =
    openId && openChildren?.length
      ? [{ parentId: openId, childIds: openChildren.map((c) => c.id) }]
      : [];

  const zoomPan = useZoomPan();
  const { wrapRef, registerNode, registerBranch, linePaths, svgSize } = useTreeConnectors(
    groups,
    [hubId, teammates, focusId, openId, childrenCache],
  );
  useFitOnChange(
    zoomPan.fit,
    zoomPan.frameRef,
    zoomPan.contentRef,
    `${hubId ?? ""}:${focusId}:${teammates?.length ?? -1}:${openId ?? ""}:${openChildren?.length ?? -1}`,
  );

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
  if (!orgUnit) {
    return (
      <div className="state-block" style={{ padding: "50px 20px" }}>
        <p>This person isn't assigned to a team, so there's no roster to draw.</p>
      </div>
    );
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

  const teamSize = teammates.length + 1;

  // One member of the roster. Managers carry a toggle that opens their
  // sub-team in the band below (see openBand) rather than underneath the
  // card -- the card itself stays exactly the size every other card is, so
  // the roster grid never reflows when something opens.
  function renderMember(node: OrgChainNode) {
    const isOpen = openId === node.id;
    const count = childrenCache[node.id]?.length;
    return (
      <div className="tree-branch" key={node.id} ref={registerBranch(node.id)}>
        <NodeBox
          node={node}
          onClick={() => handleNodeClick(node.id, node.full_name)}
          registerRef={registerNode(node.id)}
        />
        {node.has_reports && (
          <button
            type="button"
            className={`tree-expand-toggle ${isOpen ? "open" : ""}`}
            aria-expanded={isOpen}
            onClick={(e) => {
              e.stopPropagation();
              toggleExpand(node.id);
            }}
          >
            {isOpen ? <ChevronsUp size={13} /> : <ChevronsDown size={13} />}
            {isOpen ? "Hide team" : count === undefined ? "Show team" : `Show ${count}`}
          </button>
        )}
      </div>
    );
  }

  const openMember = openId
    ? [focusNode, ...teammates].find((t) => t.id === openId) ?? null
    : null;

  return (
    <ZoomPanFrame height="var(--graph-height)" {...zoomPan}>
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
            <div className="tree-tier tree-tier-manager tree-tier-trunk" data-tier="Team">
              <HubBox label={orgUnit!} count={teamSize} registerRef={registerNode(hubId)} />
            </div>
          )}
          <div className="tree-tier tree-tier-reports" data-tier="Members" style={wrapWidth(teamSize)}>
            <NodeBox node={focusNode} focus registerRef={registerNode(focusId)} />
            {teammates.map((t) => renderMember(t))}
          </div>

          {/* The opened member's own reports, as a full-width band of its
              own. Labelled with whose team it is, because at this point the
              band is one step removed from the roster above it and "these
              report to that card up there" is not something the geometry
              can say on its own. */}
          {openMember && (
            <div
              className="tree-tier tree-tier-subteam"
              data-tier={`${openMember.full_name.split(" ")[0]}'s team`}
              style={wrapWidth(openChildren?.length ?? 0)}
            >
              {loading && !openChildren ? (
                <p className="tree-loading">Loading…</p>
              ) : openChildren && openChildren.length ? (
                openChildren.map((c) => (
                  <NodeBox
                    key={c.id}
                    node={c}
                    onClick={() => handleNodeClick(c.id, c.full_name)}
                    registerRef={registerNode(c.id)}
                  />
                ))
              ) : (
                <p className="tree-loading">No direct reports to show.</p>
              )}
            </div>
          )}
        </div>
      </div>
    </ZoomPanFrame>
  );
}
