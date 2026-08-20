import { useMemo, useState } from "react";
import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY, type SimulationNodeDatum } from "d3-force";

export type NodeKind = "focus" | "person" | "skill" | "hub" | "restricted";

export interface GraphNode {
  id: string;
  label: string;
  sublabel?: string;
  kind: NodeKind;
  // Skill nodes only: how many people hold it. Drives the node's radius, so
  // a skill twenty people share reads as a bigger hub than one only two
  // people have -- the single most useful thing this graph knows and the
  // one it used to draw at exactly the same size as everything else.
  weight?: number;
}

export interface GraphEdge {
  source: string;
  target: string;
}

interface SimNode extends GraphNode, SimulationNodeDatum {}

const BASE_RADIUS: Record<NodeKind, number> = { focus: 30, person: 20, skill: 20, hub: 24, restricted: 18 };
const KIND_FILL: Record<NodeKind, string> = {
  focus: "var(--purple)",
  person: "var(--graph-person-fill)",
  skill: "var(--green-100)",
  hub: "var(--amber-100)",
  restricted: "var(--surface-sunk)",
};
const KIND_STROKE: Record<NodeKind, string> = {
  focus: "var(--purple-hover)",
  person: "var(--purple-800)",
  skill: "var(--green-800)",
  hub: "var(--amber)",
  restricted: "var(--muted)",
};
const KIND_TEXT: Record<NodeKind, string> = {
  focus: "var(--purple-on)",
  person: "var(--purple-800)",
  skill: "var(--green-800)",
  hub: "var(--amber)",
  restricted: "var(--muted)",
};

// A skill's circle grows with the square root of its holder count, so AREA
// (not radius) tracks the number -- the standard rule for encoding a count
// as a disc, and the reason a 20-person skill doesn't end up looking ten
// times more important than a 2-person one.
function radiusOf(n: GraphNode): number {
  const base = BASE_RADIUS[n.kind];
  if (n.kind !== "skill" || !n.weight) return base;
  return Math.min(46, base + Math.sqrt(n.weight) * 4.5);
}

function layout(nodes: GraphNode[], edges: GraphEdge[], width: number, height: number) {
  const simNodes: SimNode[] = nodes.map((n) => ({ ...n }));
  const byId = new Map(simNodes.map((n) => [n.id, n]));
  const simLinks = edges
    .filter((e) => byId.has(e.source) && byId.has(e.target))
    .map((e) => ({ source: e.source, target: e.target }));

  // Repulsion and link distance scale with node count -- a 60-node manager
  // subtree needs much more breathing room than a 5-node team, or labels
  // stack on top of each other.
  const scale = Math.min(2.2, 1 + nodes.length / 40);
  const sim = forceSimulation(simNodes)
    .force("charge", forceManyBody().strength(-520 * scale))
    .force("link", forceLink(simLinks).id((d) => (d as SimNode).id).distance(95 * scale).strength(0.55))
    .force("center", forceCenter(width / 2, height / 2))
    // Collision radius includes the label band under each circle, so names
    // don't overlap neighbouring circles. Without the label allowance the
    // simulation packs circles perfectly and the TEXT is what collides.
    .force("collide", forceCollide().radius((d) => radiusOf(d as SimNode) + 46).strength(0.9))
    // A gentle pull toward the middle on both axes stops disconnected
    // clusters drifting arbitrarily far out, which is what turned this
    // layout into a wide sparse smear that then had to be scaled to
    // illegibility to fit.
    .force("x", forceX(width / 2).strength(0.05))
    .force("y", forceY(height / 2).strength(0.07))
    .stop();

  for (let i = 0; i < 500; i++) sim.tick();

  return simNodes;
}

export function GraphCanvas({
  nodes,
  edges,
  onNodeClick,
  height = 620,
  bare = false,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  onNodeClick?: (node: GraphNode) => void;
  height?: number;
  // Skips the outer .graph-canvas card div -- for a caller (SkillsGraph,
  // via ZoomPanFrame) that already renders that same bordered/clipped frame
  // around this component, so the two don't nest into a visible
  // double-bordered box.
  bare?: boolean;
}) {
  // The simulation's working area. Grows with the node count so a large
  // graph is laid out in a large space and then scaled down ONCE, by the
  // frame's fit -- rather than being crammed into a fixed 900px box where
  // every node ends up a few pixels wide and every label unreadable.
  const width = Math.max(900, Math.round(Math.sqrt(Math.max(nodes.length, 1)) * 190));
  const simHeight = Math.max(height, Math.round(Math.sqrt(Math.max(nodes.length, 1)) * 150));

  const [hoverId, setHoverId] = useState<string | null>(null);

  const positioned = useMemo(() => layout(nodes, edges, width, simHeight), [nodes, edges, width, simHeight]);
  const byId = useMemo(() => new Map(positioned.map((n) => [n.id, n])), [positioned]);

  // Hovering any node lights it, its direct neighbours and the edges between
  // -- the only practical way to read "who else has this skill" out of a
  // graph with a hundred nodes in it.
  const neighbours = useMemo(() => {
    if (!hoverId) return null;
    const ids = new Set<string>([hoverId]);
    for (const e of edges) {
      if (e.source === hoverId) ids.add(e.target);
      else if (e.target === hoverId) ids.add(e.source);
    }
    return ids;
  }, [hoverId, edges]);

  const bounds = useMemo(() => {
    if (positioned.length === 0) return { minX: 0, minY: 0, maxX: width, maxY: simHeight };
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of positioned) {
      const r = radiusOf(n) + 50;
      minX = Math.min(minX, (n.x ?? 0) - r);
      minY = Math.min(minY, (n.y ?? 0) - r);
      maxX = Math.max(maxX, (n.x ?? 0) + r);
      maxY = Math.max(maxY, (n.y ?? 0) + r);
    }
    return { minX, minY, maxX, maxY };
  }, [positioned, width, simHeight]);

  if (nodes.length === 0) {
    return <div className="state-block" style={{ padding: "50px 20px" }}><p>Nothing to show here.</p></div>;
  }

  const vbW = bounds.maxX - bounds.minX;
  const vbH = bounds.maxY - bounds.minY;

  // Rendered at the layout's OWN pixel size (width/height === viewBox size,
  // so 1 SVG unit is 1 CSS pixel at rest) and left for ZoomPanFrame's fit to
  // scale down as one piece. The previous version forced this into a fixed
  // 900px-wide element regardless of how big the layout actually was, which
  // is what shrank a 135-node skills graph into an unreadable smudge --
  // and, because the shrinking happened inside the SVG, zooming in couldn't
  // recover the detail either.
  const svg = (
    <svg
      viewBox={`${bounds.minX} ${bounds.minY} ${vbW} ${vbH}`}
      width={vbW}
      height={vbH}
      role="img"
      aria-label="Relationship graph"
      className={neighbours ? "graph-svg dimmed" : "graph-svg"}
    >
      <g className="graph-edges">
        {edges.map((e, i) => {
          const s = byId.get(e.source);
          const t = byId.get(e.target);
          if (!s || !t) return null;
          const on = neighbours ? neighbours.has(e.source) && neighbours.has(e.target) : undefined;
          return (
            <line
              key={i}
              x1={s.x} y1={s.y} x2={t.x} y2={t.y}
              className={`graph-edge ${on === undefined ? "" : on ? "edge-on" : "edge-off"}`}
            />
          );
        })}
      </g>
      <g className="graph-nodes">
        {positioned.map((n) => {
          const r = radiusOf(n);
          const clickable = !!onNodeClick && (n.kind === "person" || n.kind === "focus");
          const on = neighbours ? neighbours.has(n.id) : undefined;
          return (
            <g
              key={n.id}
              transform={`translate(${n.x},${n.y})`}
              className={`graph-node ${clickable ? "clickable" : ""} ${on === undefined ? "" : on ? "node-on" : "node-off"}`}
              onClick={() => clickable && onNodeClick?.(n)}
              onMouseEnter={() => setHoverId(n.id)}
              onMouseLeave={() => setHoverId(null)}
              tabIndex={clickable ? 0 : undefined}
              role={clickable ? "button" : undefined}
              onFocus={() => setHoverId(n.id)}
              onBlur={() => setHoverId(null)}
              onKeyDown={(e) => {
                if (clickable && (e.key === "Enter" || e.key === " ")) onNodeClick?.(n);
              }}
            >
              {/* A skill is drawn as a rounded square and a person as a
                  circle, so the two halves of this bipartite graph are
                  telling apart by SHAPE and not only by colour -- which
                  also makes it legible to anyone who can't separate the
                  green fill from the lilac one. */}
              {n.kind === "skill" ? (
                <rect
                  x={-r} y={-r} width={r * 2} height={r * 2} rx={9}
                  fill={KIND_FILL[n.kind]} stroke={KIND_STROKE[n.kind]} strokeWidth={1.5}
                />
              ) : (
                <circle
                  r={r}
                  fill={KIND_FILL[n.kind]}
                  stroke={KIND_STROKE[n.kind]}
                  strokeWidth={n.kind === "focus" ? 3 : 1.5}
                />
              )}
              <text
                textAnchor="middle" dominantBaseline="central"
                fontSize={n.kind === "skill" ? 11 : 12} fontWeight={600} fill={KIND_TEXT[n.kind]}
              >
                {n.kind === "skill" && n.weight ? String(n.weight) : initialsOrShort(n.label, n.kind)}
              </text>
              <text textAnchor="middle" y={r + 18} fontSize={13} fill="var(--ink)" fontWeight={n.kind === "focus" ? 700 : 600}>
                {truncate(n.label, 24)}
              </text>
              {n.sublabel && (
                <text textAnchor="middle" y={r + 34} fontSize={11.5} fill="var(--muted)">
                  {truncate(n.sublabel, 30)}
                </text>
              )}
            </g>
          );
        })}
      </g>
    </svg>
  );

  return bare ? svg : <div className="graph-canvas">{svg}</div>;
}

function initialsOrShort(label: string, kind: NodeKind): string {
  if (kind === "skill" || kind === "hub") return label.slice(0, 2).toUpperCase();
  return label.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
}
function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
