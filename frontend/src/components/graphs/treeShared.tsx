import { useLayoutEffect, useRef, useState } from "react";
import type { OrgChainNode } from "../../types";
import { avatarStyle } from "../../avatarHue";

// Shared between DepartmentGraph and TeamGraph: both render as a strict
// hierarchical tree (a fixed node above, its children in a row below,
// connected with orthogonal elbow lines measured from the actual rendered
// DOM) rather than a force-directed radial layout. What differs between
// them is just what the "parent -> children" groups are (manager/reports
// with recursive expand for Department; a single team hub -> teammates
// row for Team) -- the connector math and node card are identical.
//
// All connector geometry is measured in LAYOUT pixels (offsetLeft/offsetTop,
// see offsetWithin below), never in screen pixels. These trees sit inside a
// CSS scale() that ANIMATES, so any screen-pixel measurement is a race
// against the transition -- which is exactly the bug that made every line
// land in the wrong place. Layout coordinates are immune to it, and there is
// no zoom term in this file at all as a result.

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
  // "on"/"off" drive the hover-path highlight (see DepartmentGraph);
  // undefined means no highlight is active and every card renders normally.
  state,
  onHover,
}: {
  node: OrgChainNode;
  focus?: boolean;
  onClick?: () => void;
  registerRef: (el: HTMLDivElement | null) => void;
  state?: "on" | "off";
  onHover?: (id: string | null) => void;
}) {
  const status = node.availability_status;
  return (
    <div
      ref={registerRef}
      className={`tree-node ${focus ? "tree-node-focus" : ""} ${state ? `node-${state}` : ""}`}
      onClick={focus ? undefined : onClick}
      role={focus ? undefined : "button"}
      tabIndex={focus ? undefined : 0}
      onMouseEnter={() => onHover?.(node.id)}
      onMouseLeave={() => onHover?.(null)}
      // Keyboard users get the same path highlight as the mouse, so the
      // relationship cue isn't pointer-only.
      onFocus={() => onHover?.(node.id)}
      onBlur={() => onHover?.(null)}
      onKeyDown={(e) => {
        if (!focus && onClick && (e.key === "Enter" || e.key === " ")) onClick();
      }}
    >
      <span className="avatar" style={avatarStyle(node.full_name)} aria-hidden="true">{initials(node.full_name)}</span>
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
  /** How to get from the parent to the children.
   *
   *  "direct" (the default) assumes the children sit immediately below the
   *  parent, so a vertical drop between them crosses nothing.
   *
   *  "gutter" is for when they do not. Team's opened sub-team gets its own
   *  band UNDER the whole roster, while the member it belongs to stays in
   *  the grid above -- so a direct drop runs from a card in the middle of
   *  the grid straight down through every card beneath it (measured: 42
   *  crossings on an 18-person roster). Routing out to the left of
   *  everything and down instead keeps the relationship visible and crosses
   *  nothing. The caller picks, because the caller is what knows whether
   *  its own layout put anything in between. */
  route?: "direct" | "gutter";
}

// Card footprint including its gap, in layout px -- must match .tree-node's
// width and .tree-tier-reports' gap in index.css. Used both for wrapping a
// long row (wrapWidth, below) and for placing a wrapped row's connector
// gutter clear of the cards (useTreeConnectors).
const CARD_PITCH = 154 + 18;

// Fallback step below a parent's bottom edge when there is nothing beneath
// it to measure a gap against. The measured path (see `laneY`) is preferred
// wherever possible: a fixed drop is only safe if every card in the row is
// the same height, and they are not -- a two-line job title makes one card
// taller than its neighbours, so a constant that clears the gap under one
// card lands inside the row below another. That is exactly how the first
// version of this put its horizontal run straight through a row of cards.
const LANE_DROP = 14;

/** An element's position relative to `ancestor`, in LAYOUT pixels.
 *
 *  This is the whole reason the connectors are correct. The obvious way to
 *  measure -- getBoundingClientRect() on both, subtract -- reports
 *  POST-TRANSFORM screen pixels, and these trees live inside ZoomPanFrame's
 *  `scale()`. The previous version compensated by dividing by the current
 *  zoom, which is right only while the DOM's actual scale and the zoom state
 *  agree. They don't during a transition: .zoom-pan-viewport animates
 *  transform over .18s, useLayoutEffect runs before that animation has
 *  moved, so every delta got divided by the TARGET scale while the DOM was
 *  still at the OLD one. Result: every path uniformly off by the ratio
 *  between them (measured at 1.47x on a fit-after-expand), and nothing
 *  recomputed once the transition settled, because a paint-only transform
 *  changes no layout box and so fires no ResizeObserver.
 *
 *  offsetLeft/offsetTop are layout properties. A transform on an ancestor
 *  does not affect them, which makes this measurement correct at any zoom,
 *  mid-animation or at rest, with no scale term anywhere -- and it is
 *  already the same unit as svgSize's scrollWidth/scrollHeight, so the paths
 *  and the viewBox agree by construction rather than by arithmetic.
 *
 *  Walks the offsetParent chain rather than reading offsetLeft once, since
 *  that is relative to the nearest POSITIONED ancestor, which is not
 *  necessarily `ancestor` itself. .org-tree-wrap is position:relative so it
 *  is in the chain; the hop bound is belt-and-braces against a future CSS
 *  change quietly taking it out, in which case the connectors degrade to
 *  slightly-off rather than looping forever.
 */
function offsetWithin(el: HTMLElement, ancestor: HTMLElement): { x: number; y: number } {
  let x = 0;
  let y = 0;
  let node: HTMLElement | null = el;
  let hops = 0;
  while (node && node !== ancestor && hops < 32) {
    x += node.offsetLeft;
    y += node.offsetTop;
    node = node.offsetParent as HTMLElement | null;
    hops += 1;
  }
  return { x, y };
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
      wrap: HTMLElement,
      parentId: string,
      childIds: string[],
      paths: { id: string; d: string }[],
      route: "direct" | "gutter" = "direct",
    ) {
      const p = nodeRefs.current.get(parentId);
      if (!p) return;
      const po = offsetWithin(p, wrap);
      const px = po.x + p.offsetWidth / 2;
      const py = po.y + p.offsetHeight;

      const items = childIds
        .map((id) => {
          const el = nodeRefs.current.get(id);
          if (!el) return null;
          const o = offsetWithin(el, wrap);
          const branchEl = branchRefs.current.get(id);
          const branchBottom = branchEl
            ? offsetWithin(branchEl, wrap).y + branchEl.offsetHeight
            : o.y + el.offsetHeight;
          return {
            id,
            cx: o.x + el.offsetWidth / 2,
            cy: o.y,
            top: Math.round(o.y),
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

      // Where a wrapped row's trunk drops. Running it at the parent's own x
      // is right for the FIRST row and wrong for every row after it: the
      // parent sits centred over the group, so a vertical from the parent
      // down to row 2 passes straight through whichever row-1 card is also
      // centred there -- which, with an 8-report row wrapping to 7 + 1, is
      // exactly what happened. Rows after the first therefore leave the bus
      // sideways and drop down a gutter to the left of every card in the
      // group, where there is nothing to cross.
      const leftMost = Math.min(...items.map((it) => it.cx), px);
      const gutterX = leftMost - CARD_PITCH / 2;

      // Where a detached connector may run horizontally: below every card in
      // the parent's own row, above every card in the next one. Measured
      // from the registered nodes rather than assumed from a constant,
      // because rows are ragged -- cards differ in height with the length of
      // a job title, so the gap under the parent is not the gap under its
      // neighbour.
      // A detached route needs TWO clear horizontal lanes, and both have to
      // be measured against everything on screen rather than against the
      // parent alone:
      //
      //   laneY   just below the parent's own row, to get out to the gutter.
      //   busY    just above the children, to come back in. Computing this
      //           one from the parent's bottom (as the direct route does,
      //           correctly, since nothing is in between there) put it in
      //           the middle of the roster -- the parent is far above, so
      //           the midpoint between them lands on whatever is stacked in
      //           between rather than in a gap.
      let laneY = py + LANE_DROP;
      let detachedBusY: number | null = null;
      if (route === "gutter") {
        const parentTop = po.y;
        const childTop = Math.min(...items.map((it) => it.cy));
        let rowBottom = py;
        let nextTop = Infinity;
        let aboveBottom = -Infinity;
        for (const el of nodeRefs.current.values()) {
          const o = offsetWithin(el, wrap);
          const top = o.y;
          const bottom = o.y + el.offsetHeight;
          // Same row as the parent: within a few px of its top edge.
          if (Math.abs(top - parentTop) < 4) rowBottom = Math.max(rowBottom, bottom);
          else if (top > py) nextTop = Math.min(nextTop, top);
          // Anything that ends above the children, wherever it sits.
          if (bottom <= childTop - 1) aboveBottom = Math.max(aboveBottom, bottom);
        }
        laneY = nextTop === Infinity ? rowBottom + LANE_DROP : (rowBottom + nextTop) / 2;
        detachedBusY = aboveBottom === -Infinity
          ? childTop - LANE_DROP
          : (aboveBottom + childTop) / 2;
      }

      let prevBottom = py;
      let prevBusY: number | null = null;
      for (const key of rowKeys) {
        const rowItems = rows.get(key)!;
        // The bus this row is actually drawn against. For a detached first
        // row that is the measured lane above the children, NOT the midpoint
        // to the far-away parent -- and it has to be the same value the next
        // wrapped row starts from, which is why prevBusY is assigned from
        // here rather than from the raw midpoint. Recording the raw one left
        // a second row of children leaving the bus at a y the first row had
        // already moved off, dragging it back across the roster.
        const busY: number = prevBusY === null && detachedBusY !== null
          ? detachedBusY
          : (prevBottom + key) / 2;
        for (const it of rowItems) {
          let d: string;
          if (route === "gutter" && prevBusY === null) {
            // Detached children. Step down into the lane below the
            // parent's own row, out to the gutter left of everything, down
            // past whatever sits between, and back in along this row's bus.
            d = `M ${px} ${py} V ${laneY} H ${gutterX} V ${busY} H ${it.cx} V ${it.cy}`;
          } else if (prevBusY === null) {
            // First row: straight down from the parent, then out along the
            // bus. A child already centred under the parent needs no elbow
            // at all -- the H segment would be zero-length and the bus just
            // a redundant kink in what should read as one clean drop.
            d = Math.abs(it.cx - px) < 0.5
              ? `M ${px} ${py} V ${it.cy}`
              : `M ${px} ${py} V ${busY} H ${it.cx} V ${it.cy}`;
          } else {
            // Wrapped row: join the previous row's bus (which the parent
            // trunk already reaches), run out to the gutter, drop clear of
            // the cards above, then back in along this row's bus.
            d = `M ${px} ${prevBusY} H ${gutterX} V ${busY} H ${it.cx} V ${it.cy}`;
          }
          paths.push({ id: `${parentId}->${it.id}`, d });
        }
        prevBottom = Math.max(...rowItems.map((it) => it.branchBottom));
        prevBusY = busY;
      }
    }

    function recompute() {
      const wrap = wrapRef.current;
      if (!wrap) return;
      const paths: { id: string; d: string }[] = [];
      for (const g of groups) computeGroup(wrap, g.parentId, g.childIds, paths, g.route);
      setLinePaths(paths);
      setSvgSize({ width: wrap.scrollWidth, height: wrap.scrollHeight });
    }

    recompute();
    // Observes the wrap AND every node: a card that reflows because its name
    // wrapped to a second line changes the geometry without changing the
    // wrap's own box, and that used to leave the lines pointing at where the
    // card had been.
    const ro = new ResizeObserver(recompute);
    if (wrapRef.current) ro.observe(wrapRef.current);
    for (const el of nodeRefs.current.values()) ro.observe(el);
    window.addEventListener("resize", recompute);
    // Fonts land after first paint and change card heights. Without this the
    // very first render of a tree draws its lines against pre-font metrics.
    document.fonts?.ready.then(recompute).catch(() => {});
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", recompute);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { wrapRef, registerNode, registerBranch, linePaths, svgSize };
}

// The widest a wrapped row of cards is allowed to get. Sized to a typical
// frame's inner width (~1300px on a maximised laptop window), so a big row
// wraps to roughly the frame's own proportions.
const MAX_ROW_COLS = 7;

// A max-width for a row of sibling cards, so a long row wraps into a block
// instead of running off in one line.
//
// Widest-that-fits, NOT a target aspect ratio for the block on its own. The
// ratio version looked right in isolation and fell apart once a view stacked
// two of these (Team's roster plus an opened sub-team): each block was
// individually well-proportioned, the stack of them was far taller than the
// frame, and fit-to-view had to drop to 0.41 to show it -- unreadable. The
// frame is wide and short, so what actually matters is spending its width
// first and keeping every block as few rows tall as possible.
export function wrapWidth(count: number): React.CSSProperties | undefined {
  if (count <= MAX_ROW_COLS) return undefined;
  return { maxWidth: MAX_ROW_COLS * CARD_PITCH };
}
