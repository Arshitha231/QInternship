import { useEffect, useState } from "react";
import { ApiError, getOrgChart } from "../../api";
import type { Identity, OrgChainNode, PersonDetail } from "../../types";
import { NodeBox, useTreeConnectors, type TreeGroup } from "./treeShared";
import { useZoomPan, ZoomPanFrame } from "../ZoomPanFrame";

// Strict hierarchical tree, not a force-directed radial layout: manager
// directly above, direct reports in a row directly below, connected with
// orthogonal (vertical/horizontal-only) elbow connectors. Default view is
// exactly one level up and one level down from the centered person -- no
// grandparents, no grandchildren, no siblings. Anyone in the reports row
// who has their own reports gets a manual expand toggle instead of being
// auto-expanded, and expanding one branch never auto-expands its siblings.

interface Props {
  identity: Identity;
  focusId: string;
  focusPerson: PersonDetail | null;
  onNavigate: (id: string) => void;
}

export function DepartmentGraph({ identity, focusId, focusPerson, onNavigate }: Props) {
  const [manager, setManager] = useState<OrgChainNode | null | undefined>(undefined);
  const [reports, setReports] = useState<OrgChainNode[] | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [childrenCache, setChildrenCache] = useState<Record<string, OrgChainNode[]>>({});
  const [loadingIds, setLoadingIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    setManager(undefined);
    setReports(undefined);
    setError(null);
    setExpandedIds(new Set());
    setChildrenCache({});
    setLoadingIds(new Set());

    Promise.all([
      getOrgChart(identity, focusId, "up", 1),
      getOrgChart(identity, focusId, "down", 1),
    ])
      .then(([up, down]) => {
        if (cancelled) return;
        setManager(up[0] ?? null);
        setReports(down);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof ApiError ? e.message : "Unknown error");
        setManager(null);
        setReports([]);
      });

    return () => {
      cancelled = true;
    };
  }, [identity, focusId]);

  function toggleExpand(id: string) {
    const wasExpanded = expandedIds.has(id);
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (wasExpanded) next.delete(id);
      else next.add(id);
      return next;
    });
    if (!wasExpanded && !childrenCache[id]) {
      setLoadingIds((prev) => new Set(prev).add(id));
      getOrgChart(identity, id, "down", 1)
        .then((children) => setChildrenCache((prev) => ({ ...prev, [id]: children })))
        .finally(() =>
          setLoadingIds((prev) => {
            const next = new Set(prev);
            next.delete(id);
            return next;
          }),
        );
    }
  }

  function handleNodeClick(id: string) {
    if (id !== focusId) onNavigate(id);
  }

  function collectExpandedGroups(id: string, groups: TreeGroup[]) {
    if (!expandedIds.has(id)) return;
    const children = childrenCache[id];
    if (!children || children.length === 0) return;
    groups.push({ parentId: id, childIds: children.map((c) => c.id) });
    for (const c of children) collectExpandedGroups(c.id, groups);
  }

  const groups: TreeGroup[] = [];
  if (manager) groups.push({ parentId: manager.id, childIds: [focusId] });
  if (reports && reports.length) {
    groups.push({ parentId: focusId, childIds: reports.map((r) => r.id) });
    for (const r of reports) collectExpandedGroups(r.id, groups);
  }
  const zoomPan = useZoomPan();
  const { wrapRef, registerNode, registerBranch, linePaths, svgSize } = useTreeConnectors(groups, [
    manager,
    reports,
    expandedIds,
    childrenCache,
    focusId,
  ], zoomPan.zoom);

  function renderBranch(node: OrgChainNode) {
    const expanded = expandedIds.has(node.id);
    const children = childrenCache[node.id];
    const loading = loadingIds.has(node.id);
    return (
      <div className="tree-branch" key={node.id} ref={registerBranch(node.id)}>
        <NodeBox node={node} onClick={() => handleNodeClick(node.id)} registerRef={registerNode(node.id)} />
        {node.has_reports && (
          <button
            type="button"
            className="tree-expand-toggle"
            onClick={(e) => {
              e.stopPropagation();
              toggleExpand(node.id);
            }}
          >
            {expanded ? "▴ collapse" : "▾ expand"}
          </button>
        )}
        {expanded && (
          <div className="tree-children-row">
            {loading && !children ? (
              <p className="tree-loading">Loading…</p>
            ) : (
              (children ?? []).map((c) => renderBranch(c))
            )}
          </div>
        )}
      </div>
    );
  }

  if (error) {
    return (
      <div className="state-block error" style={{ padding: "50px 20px" }}>
        <strong>Couldn't load the org chart</strong>
        <p>{error}</p>
      </div>
    );
  }
  if (manager === undefined || reports === undefined || !focusPerson) {
    return <div className="skel skel-card" style={{ height: 480 }} />;
  }

  const center: OrgChainNode = {
    id: focusPerson.id,
    full_name: focusPerson.full_name,
    job_title: focusPerson.job_title ?? "",
    org_unit: focusPerson.org_unit ?? "",
    depth: 0,
    availability_status: focusPerson.availability_status ?? "available",
    delegate: focusPerson.delegate,
    has_reports: false,
  };

  return (
    <ZoomPanFrame height={480} {...zoomPan}>
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
          {manager && (
            <div className="tree-tier tree-tier-manager">
              <NodeBox node={manager} onClick={() => handleNodeClick(manager.id)} registerRef={registerNode(manager.id)} />
            </div>
          )}
          <div className="tree-tier tree-tier-center">
            <NodeBox node={center} focus registerRef={registerNode(center.id)} />
          </div>
          {reports.length > 0 && (
            <div className="tree-tier tree-tier-reports">{reports.map((r) => renderBranch(r))}</div>
          )}
        </div>
      </div>
    </ZoomPanFrame>
  );
}
