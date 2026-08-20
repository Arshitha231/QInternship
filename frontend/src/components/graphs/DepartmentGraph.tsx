import { useEffect, useState } from "react";
import { ApiError, getOrgChart } from "../../api";
import type { Identity, OrgChainNode, PersonDetail, ViewMode } from "../../types";
import { NodeBox, useTreeConnectors, wrapWidth, type TreeGroup } from "./treeShared";
import { useFitOnChange, useZoomPan, ZoomPanFrame } from "../ZoomPanFrame";
import { ChevronsDown, ChevronsUp } from "../../icons";

// Strict hierarchical tree, not a force-directed radial layout: manager
// directly above, direct reports in a row directly below, connected with
// orthogonal (vertical/horizontal-only) elbow connectors. Default view is
// exactly one level up and one level down from the centered person -- no
// grandparents, no grandchildren, no siblings. Anyone in the reports row
// who has their own reports gets a manual expand toggle instead of being
// auto-expanded, and expanding one branch never auto-expands its siblings.
//
// The three tiers are labelled down the left edge (Manager / Selected /
// Direct reports) and banded behind the cards. Without them the tree was
// three rows of near-identical cards joined by thin lines, and which row
// meant "above" was left to be inferred from position alone -- the single
// most common complaint about this view. The labels state the relationship
// the geometry is only implying.

interface Props {
  identity: Identity;
  // Forwarded to getOrgChart: an hr/it caller previewing the ordinary view
  // loses the downward chain, so this graph shows what they'd actually see.
  viewMode: ViewMode;
  focusId: string;
  focusPerson: PersonDetail | null;
  onNavigate: (id: string) => void;
}

export function DepartmentGraph({ identity, viewMode, focusId, focusPerson, onNavigate }: Props) {
  const [manager, setManager] = useState<OrgChainNode | null | undefined>(undefined);
  const [reports, setReports] = useState<OrgChainNode[] | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [childrenCache, setChildrenCache] = useState<Record<string, OrgChainNode[]>>({});
  const [loadingIds, setLoadingIds] = useState<Set<string>>(new Set());
  // Which subtree the pointer is over, so the whole path from the focus
  // person down to it can be highlighted and everything else dimmed. Hover,
  // not click, because click already means "re-center on this person" --
  // giving the same gesture a second meaning would make one of the two
  // unreachable.
  const [hoverId, setHoverId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setManager(undefined);
    setReports(undefined);
    setError(null);
    setExpandedIds(new Set());
    setChildrenCache({});
    setLoadingIds(new Set());

    Promise.all([
      getOrgChart(identity, focusId, "up", viewMode, 1),
      getOrgChart(identity, focusId, "down", viewMode, 1),
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
  }, [identity, viewMode, focusId]);

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
      getOrgChart(identity, id, "down", viewMode, 1)
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

  // Refit on every change to what's drawn -- including expand/collapse.
  // Expanding a 14-person subtree roughly triples the tree's width, and
  // without a refit most of the team you just asked to see lands outside
  // the frame with nothing to say so. The viewport transition (see
  // .zoom-pan-viewport) makes the rescale readable rather than abrupt.
  //
  // The key covers both halves of an expand, because they land in separate
  // renders: clicking the toggle marks the branch expanded (and draws a
  // "Loading…" placeholder), then the fetched children arrive and the row
  // grows to its real width. Keying only on `expandedIds` fits the
  // placeholder and never refits for the children -- so the branch you just
  // opened ends up past the frame's edge after all. Hence the loaded-child
  // counts in the key too.
  const expandKey = Array.from(expandedIds).sort().join(",");
  const loadedKey = Object.entries(childrenCache)
    .map(([id, kids]) => `${id}=${kids.length}`)
    .sort()
    .join(",");
  useFitOnChange(
    zoomPan.fit,
    zoomPan.frameRef,
    zoomPan.contentRef,
    `${focusId}:${reports?.length ?? -1}:${manager?.id ?? ""}:${expandKey}:${loadedKey}`,
  );

  // The set of ids on the highlighted path: the hovered node, its ancestors
  // back up to the focus person, and its own descendants. Everything else
  // is dimmed, which is what turns a wall of cards into a readable line of
  // reporting.
  function pathIds(target: string): Set<string> {
    const ids = new Set<string>([target]);
    // Ancestors: walk the group list backwards from the target.
    let cursor = target;
    for (;;) {
      const parent = groups.find((g) => g.childIds.includes(cursor))?.parentId;
      if (!parent || ids.has(parent)) break;
      ids.add(parent);
      cursor = parent;
    }
    // Descendants: walk down through whatever is expanded.
    const queue = [target];
    while (queue.length) {
      const id = queue.pop()!;
      for (const c of childrenCache[id] ?? []) {
        if (expandedIds.has(id) && !ids.has(c.id)) {
          ids.add(c.id);
          queue.push(c.id);
        }
      }
    }
    return ids;
  }
  const highlighted = hoverId ? pathIds(hoverId) : null;
  function nodeState(id: string): "on" | "off" | undefined {
    if (!highlighted) return undefined;
    return highlighted.has(id) ? "on" : "off";
  }

  function renderBranch(node: OrgChainNode) {
    const expanded = expandedIds.has(node.id);
    const children = childrenCache[node.id];
    const loading = loadingIds.has(node.id);
    const count = children?.length;
    return (
      <div className="tree-branch" key={node.id} ref={registerBranch(node.id)}>
        <NodeBox
          node={node}
          onClick={() => handleNodeClick(node.id)}
          registerRef={registerNode(node.id)}
          state={nodeState(node.id)}
          onHover={setHoverId}
        />
        {node.has_reports && (
          <button
            type="button"
            className={`tree-expand-toggle ${expanded ? "open" : ""}`}
            aria-expanded={expanded}
            onClick={(e) => {
              e.stopPropagation();
              toggleExpand(node.id);
            }}
          >
            {expanded ? <ChevronsUp size={13} /> : <ChevronsDown size={13} />}
            {/* Once the children are loaded the count is known, so the
                control says how many rather than just "expand" -- the size
                of a subtree is the thing you actually want to know before
                deciding whether to open it. */}
            {expanded ? "Hide team" : count === undefined ? "Show team" : `Show ${count}`}
          </button>
        )}
        {expanded && (
          <div className="tree-children-row" style={wrapWidth((children ?? []).length)}>
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

  // Nobody above and nobody below is a real state (a one-person org unit, or
  // an ordinary colleague in employee view mode, where the downward chain is
  // withheld by policy). Saying so beats an empty panel that looks broken.
  const isolated = !manager && reports.length === 0;

  return (
    <ZoomPanFrame height="var(--graph-height)" {...zoomPan}>
      <div className="org-tree-wrap" ref={wrapRef}>
        <svg
          className="org-tree-lines"
          width={svgSize.width}
          height={svgSize.height}
          viewBox={`0 0 ${svgSize.width} ${svgSize.height}`}
        >
          {linePaths.map((p) => {
            // A connector is "on" only when both of its endpoints are, so a
            // dimmed sibling's line dims with it rather than staying lit and
            // pointing at nothing.
            const [from, to] = p.id.split("->");
            const on = highlighted ? highlighted.has(from) && highlighted.has(to) : undefined;
            return (
              <path
                key={p.id}
                d={p.d}
                className={`tree-edge ${on === undefined ? "" : on ? "edge-on" : "edge-off"}`}
              />
            );
          })}
        </svg>
        <div className="org-tree">
          {manager && (
            <div className="tree-tier tree-tier-manager" data-tier="Manager">
              <NodeBox
                node={manager}
                onClick={() => handleNodeClick(manager.id)}
                registerRef={registerNode(manager.id)}
                state={nodeState(manager.id)}
                onHover={setHoverId}
              />
            </div>
          )}
          <div className="tree-tier tree-tier-center" data-tier="Selected">
            <NodeBox node={center} focus registerRef={registerNode(center.id)} state={nodeState(center.id)} />
          </div>
          {reports.length > 0 && (
            <div
              className="tree-tier tree-tier-reports"
              data-tier={`${reports.length} direct report${reports.length === 1 ? "" : "s"}`}
              style={wrapWidth(reports.length)}
            >
              {reports.map((r) => renderBranch(r))}
            </div>
          )}
          {isolated && (
            <p className="tree-empty-note">
              No manager or direct reports to show for this person in this view.
            </p>
          )}
        </div>
      </div>
    </ZoomPanFrame>
  );
}
