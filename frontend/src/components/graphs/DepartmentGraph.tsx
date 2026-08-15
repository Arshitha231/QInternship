import { useEffect, useState, useRef } from "react";
import { ApiError, getOrgChart } from "../../api";
import type { Identity, OrgChainNode, PersonDetail } from "../../types";
import { NodeBox, useTreeConnectors, type TreeGroup } from "./treeShared";

interface Props {
  identity: Identity;
  focusId: string;
  focusPerson: PersonDetail | null;
  highlightedIds?: Set<string>; // Phase 3: Search Highlighting
  onNavigate: (id: string) => void;
}

// Simple badge component for the zoomed-out view
function OrgUnitBadge({ unitName, headcount, registerRef }: { unitName: string; headcount: number; registerRef: (el: HTMLDivElement | null) => void }) {
  return (
    <div ref={registerRef} className="tree-node tree-node-hub" style={{ border: "2px solid #cbd5e1", background: "#f8fafc" }}>
      <p className="tree-node-name">{unitName}</p>
      <p className="tree-node-role">{headcount} members</p>
    </div>
  );
}

export function DepartmentGraph({ 
  identity, 
  focusId, 
  focusPerson, 
  highlightedIds = new Set(), 
  onNavigate 
}: Props) {
  const [manager, setManager] = useState<OrgChainNode | null | undefined>(undefined);
  const [reports, setReports] = useState<OrgChainNode[] | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [childrenCache, setChildrenCache] = useState<Record<string, OrgChainNode[]>>({});
  const [loadingIds, setLoadingIds] = useState<Set<string>>(new Set());

  // --- Zoom & Pan State ---
  const [zoomScale, setZoomScale] = useState<number>(1.0);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);
  
  const dragStart = useRef({ mouseX: 0, mouseY: 0, panX: 0, panY: 0 });
  const viewportRef = useRef<HTMLDivElement>(null);
  const COLLAPSE_THRESHOLD = 0.6; // Collapse into headcount badges below 60%

  // --- Data Fetching ---
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

  // --- Zoom: Scroll Wheel Listener ---
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;

    const handleWheel = (e: WheelEvent) => {
      e.preventDefault();
      const zoomSensitivity = 0.002;
      const delta = -e.deltaY * zoomSensitivity;

      setZoomScale((prev) => {
        const newScale = prev + delta;
        return Math.min(Math.max(0.3, newScale), 1.5);
      });
    };

    el.addEventListener("wheel", handleWheel, { passive: false });
    return () => el.removeEventListener("wheel", handleWheel);
  }, []);

  // --- Pan: Click-and-Drag Listeners ---
  const handleMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.button !== 0 && e.button !== 1) return;
    e.preventDefault();
    setIsDragging(true);
    dragStart.current = { mouseX: e.clientX, mouseY: e.clientY, panX: pan.x, panY: pan.y };
  };

  const handleMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!isDragging) return;
    const dx = e.clientX - dragStart.current.mouseX;
    const dy = e.clientY - dragStart.current.mouseY;
    setPan({ x: dragStart.current.panX + dx, y: dragStart.current.panY + dy });
  };

  const handleMouseUpOrLeave = () => {
    if (isDragging) setIsDragging(false);
  };

  // --- Roll-up Aggregation Logic ---
  const getCollapsedReports = () => {
    if (!reports) return [];
    
    const grouped = reports.reduce((acc, node) => {
      const unit = node.org_unit || "Unassigned";
      if (!acc[unit]) acc[unit] = 0;
      acc[unit]++;
      return acc;
    }, {} as Record<string, number>);

    return Object.entries(grouped).map(([unit, count]) => ({
      id: `org-unit-${unit}`, 
      unitName: unit,
      headcount: count
    }));
  };

  const isZoomedOut = zoomScale < COLLAPSE_THRESHOLD;
  const collapsedUnits = getCollapsedReports();

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

  // --- Grouping for the Connector Lines ---
  const groups: TreeGroup[] = [];
  if (manager) groups.push({ parentId: manager.id, childIds: [focusId] });
  
  if (reports && reports.length) {
    if (isZoomedOut) {
      // Connect to badges
      groups.push({ parentId: focusId, childIds: collapsedUnits.map((u) => u.id) });
    } else {
      // Connect to individual reports
      groups.push({ parentId: focusId, childIds: reports.map((r) => r.id) });
      for (const r of reports) collectExpandedGroups(r.id, groups);
    }
  }

  // Ensure zoomScale is passed to useTreeConnectors
  const { wrapRef, registerNode, registerBranch, linePaths, svgSize } = useTreeConnectors(
    groups, 
    zoomScale, 
    [manager, reports, expandedIds, childrenCache, focusId, isZoomedOut, zoomScale]
  );

  function renderBranch(node: OrgChainNode) {
    const expanded = expandedIds.has(node.id);
    const children = childrenCache[node.id];
    const loading = loadingIds.has(node.id);
    return (
      <div className="tree-branch" key={node.id} ref={registerBranch(node.id)}>
        <NodeBox 
          node={node} 
          highlighted={highlightedIds.has(node.id)} 
          onClick={() => handleNodeClick(node.id)} 
          registerRef={registerNode(node.id)} 
        />
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
    <div 
      className="org-tree-viewport" 
      ref={viewportRef}
      onMouseDown={handleMouseDown}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUpOrLeave}
      onMouseLeave={handleMouseUpOrLeave}
      style={{ 
        width: "100%", 
        height: "600px", 
        overflow: "hidden", 
        background: "#f8fafc",
        position: "relative",
        cursor: isDragging ? "grabbing" : "grab"
      }}
    >
      <div 
        className="org-tree-wrap" 
        ref={wrapRef}
        style={{
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoomScale})`,
          transformOrigin: "top center",
          transition: isDragging ? "none" : "transform 0.1s ease-out",
          width: "100%",
          height: "100%"
        }}
      >
        <svg
          className="org-tree-lines"
          width={svgSize.width}
          height={svgSize.height}
          viewBox={`0 0 ${svgSize.width} ${svgSize.height}`}
          style={{ position: "absolute", top: 0, left: 0, pointerEvents: "none" }}
        >
          {linePaths.map((p) => (
            <path key={p.id} d={p.d} className="tree-edge" stroke="#cbd5e1" strokeWidth={2} fill="none" />
          ))}
        </svg>
        <div className="org-tree">
          {manager && (
            <div className="tree-tier tree-tier-manager">
              <NodeBox 
                node={manager} 
                highlighted={highlightedIds.has(manager.id)} 
                onClick={() => handleNodeClick(manager.id)} 
                registerRef={registerNode(manager.id)} 
              />
            </div>
          )}
          
          <div className="tree-tier tree-tier-center">
            <NodeBox 
              node={center} 
              focus 
              highlighted={highlightedIds.has(center.id)} 
              registerRef={registerNode(center.id)} 
            />
          </div>
          
          {reports.length > 0 && (
            <div className="tree-tier tree-tier-reports">
              {isZoomedOut ? (
                <div style={{ display: "flex", gap: "20px", justifyContent: "center", width: "100%" }}>
                  {collapsedUnits.map((unit) => (
                    <div className="tree-branch" key={unit.id} ref={registerBranch(unit.id)}>
                      <OrgUnitBadge 
                        unitName={unit.unitName} 
                        headcount={unit.headcount} 
                        registerRef={registerNode(unit.id)} 
                      />
                    </div>
                  ))}
                </div>
              ) : (
                reports.map((r) => renderBranch(r))
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
