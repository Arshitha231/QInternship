import { useLayoutEffect, useRef, useState } from "react";
import type { OrgChainNode } from "../../types";

// Shared between DepartmentGraph and TeamGraph: both render as a strict
// hierarchical tree (a fixed node above, its children in a row below,
// connected with orthogonal elbow lines measured from the actual rendered
// DOM) rather than a force-directed radial layout. What differs between
// them is just what the "parent -> children" groups are (manager/reports
// with recursive expand for Department; a single team hub -> teammates
// row for Team) -- the connector math and node card are identical.

export function initials(name: string): string {
  return name.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
}

export const STATUS_LABEL: Record<string, string> = {
  available: "Available",
  away: "Away",
  restricted: "Restricted",
};

export function NodeBox({
  node,
  focus,
  onClick,
  registerRef,
}: {
  node: OrgChainNode;
  focus?: boolean;
  onClick?: () => void;
  registerRef: (el: HTMLDivElement | null) => void;
}) {
  const status = node.availability_status;
  return (
    <div
      ref={registerRef}
      className={`tree-node ${focus ? "tree-node-focus" : ""}`}
      onClick={focus ? undefined : onClick}
      role={focus ? undefined : "button"}
      tabIndex={focus ? undefined : 0}
      onKeyDown={(e) => {
        if (!focus && onClick && (e.key === "Enter" || e.key === " ")) onClick();
      }}
    >
      <span className="avatar" aria-hidden="true">{initials(node.full_name)}</span>
      <p className="tree-node-name">{node.full_name}</p>
      <p className="tree-node-role">{node.job_title}</p>
      <p className="tree-node-status">
        <span className={`status-dot status-${status}`} />
        {STATUS_LABEL[status] ?? status}
      </p>
      {status === "away" && node.delegate && (
        <p className="tree-node-delegate">Covering: {node.delegate.full_name}</p>
      )}
    </div>
  );
}

export interface TreeGroup {
  parentId: string;
  childIds: string[];
}

export function useTreeConnectors(groups: TreeGroup[], deps: unknown[]) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const nodeRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const branchRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const [linePaths, setLinePaths] = useState<{ id: string; d: string }[]>([]);
  const [svgSize, setSvgSize] = useState({ width: 0, height: 0 });

  function registerNode(id: string) {
    return (el: HTMLDivElement | null) => {
      if (el) nodeRefs.current.set(id, el);
      else nodeRefs.current.delete(id);
    };
  }

  function registerBranch(id: string) {
    return (el: HTMLDivElement | null) => {
      if (el) branchRefs.current.set(id, el);
      else branchRefs.current.delete(id);
    };
  }

  useLayoutEffect(() => {
    // A sibling group that fits on one row gets one shared elbow bus. A
    // group wrapped across multiple visual rows (e.g. a 14-report row)
    // needs each row's bus computed against the row above it, not against
    // the true parent every time -- otherwise a later row's bus lands at
    // the naive parent/child midpoint, which sits inside an earlier row's
    // card band and gets hidden behind those cards. The vertical trunk
    // itself still runs at the parent's own x for every row, so it reads
    // as one spine forking at each row.
    function computeGroup(
      wrapRect: DOMRect,
      parentId: string,
      childIds: string[],
      paths: { id: string; d: string }[],
    ) {
      const p = nodeRefs.current.get(parentId);
      if (!p) return;
      const pr = p.getBoundingClientRect();
      const px = pr.left + pr.width / 2 - wrapRect.left;
      const py = pr.bottom - wrapRect.top;

      const items = childIds
        .map((id) => {
          const el = nodeRefs.current.get(id);
          if (!el) return null;
          const r = el.getBoundingClientRect();
          const branchEl = branchRefs.current.get(id);
          const branchBottom = branchEl
            ? branchEl.getBoundingClientRect().bottom - wrapRect.top
            : r.bottom - wrapRect.top;
          return {
            id,
            cx: r.left + r.width / 2 - wrapRect.left,
            cy: r.top - wrapRect.top,
            top: Math.round(r.top - wrapRect.top),
            branchBottom,
          };
        })
        .filter((x): x is NonNullable<typeof x> => x !== null);

      const rows = new Map<number, typeof items>();
      for (const it of items) {
        const row = rows.get(it.top) ?? [];
        row.push(it);
        rows.set(it.top, row);
      }
      const rowKeys = Array.from(rows.keys()).sort((a, b) => a - b);

      let prevBottom = py;
      for (const key of rowKeys) {
        const rowItems = rows.get(key)!;
        const busY = (prevBottom + key) / 2;
        for (const it of rowItems) {
          paths.push({ id: `${parentId}->${it.id}`, d: `M ${px} ${py} V ${busY} H ${it.cx} V ${it.cy}` });
        }
        prevBottom = Math.max(...rowItems.map((it) => it.branchBottom));
      }
    }

    function recompute() {
      const wrap = wrapRef.current;
      if (!wrap) return;
      const wrapRect = wrap.getBoundingClientRect();
      const paths: { id: string; d: string }[] = [];
      for (const g of groups) computeGroup(wrapRect, g.parentId, g.childIds, paths);
      setLinePaths(paths);
      setSvgSize({ width: wrap.scrollWidth, height: wrap.scrollHeight });
    }

    recompute();
    const ro = new ResizeObserver(recompute);
    if (wrapRef.current) ro.observe(wrapRef.current);
    window.addEventListener("resize", recompute);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", recompute);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { wrapRef, registerNode, registerBranch, linePaths, svgSize };
}
