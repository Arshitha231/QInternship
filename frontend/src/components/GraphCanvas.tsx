import { useMemo } from "react";
import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, type SimulationNodeDatum } from "d3-force";

export type NodeKind = "focus" | "person" | "skill" | "hub" | "restricted";

export interface GraphNode {
  id: string;
  label: string;
  sublabel?: string;
  kind: NodeKind;
  highlighted?: boolean; // <-- Added to support Search Highlighting
}

export interface GraphEdge {
  source: string;
  target: string;
}

interface SimNode extends GraphNode, SimulationNodeDatum {}

const KIND_RADIUS: Record<NodeKind, number> = { focus: 22, person: 17, skill: 16, hub: 20, restricted: 16 };
const KIND_FILL: Record<NodeKind, string> = {
  focus: "var(--purple)",
  person: "#D5D2EC",
  skill: "var(--green-100)",
  hub: "var(--amber-100)",
  restricted: "#EDEDEA",
};
const KIND_STROKE: Record<NodeKind, string> = {
  focus: "var(--purple-hover)",
  person: "var(--purple-800)",
  skill: "var(--green-800)",
  hub: "var(--amber)",
  restricted: "var(--muted)",
};
const KIND_TEXT: Record<NodeKind, string> = {
  focus: "#fff",
  person: "var(--purple-800)",
  skill: "var(--green-800)",
  hub: "var(--amber)",
  restricted: "var(--muted)",
};

function layout(nodes: GraphNode[], edges: GraphEdge[], width: number, height: number) {
  const simNodes: SimNode[] = nodes.map((n) => ({ ...n }));
  const byId = new Map(simNodes.map((n) => [n.id, n]));
  const simLinks = edges
    .filter((e) => byId.has(e.source) && byId.has(e.target))
    .map((e) => ({ source: e.source, target: e.target }));

  const scale = Math.min(2.2, 1 + nodes.length / 40);
  const sim = forceSimulation(simNodes)
    .force("charge", forceManyBody().strength(-420 * scale))
    .force("link", forceLink(simLinks).id((d) => (d as SimNode).id).distance(70 * scale).strength(0.6))
    .force("center", forceCenter(width / 2, height / 2))
    .force("collide", forceCollide().radius((d) => KIND_RADIUS[(d as SimNode).kind] + 38))
    .stop();

  for (let i = 0; i < 400; i++) sim.tick();

  return simNodes;
}

export function GraphCanvas({
  nodes,
  edges,
  onNodeClick,
  height = 620,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  onNodeClick?: (node: GraphNode) => void;
  height?: number;
}) {
  const width = 900;

  const positioned = useMemo(() => layout(nodes, edges, width, height), [nodes, edges, height]);
  const byId = useMemo(() => new Map(positioned.map((n) => [n.id, n])), [positioned]);

  const bounds = useMemo(() => {
    if (positioned.length === 0) return { minX: 0, minY: 0, maxX: width, maxY: height };
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of positioned) {
      const r = KIND_RADIUS[n.kind] + 44;
      minX = Math.min(minX, (n.x ?? 0) - r);
      minY = Math.min(minY, (n.y ?? 0) - r);
      maxX = Math.max(maxX, (n.x ?? 0) + r);
      maxY = Math.max(maxY, (n.y ?? 0) + r);
    }
    return { minX, minY, maxX, maxY };
  }, [positioned, height]);

  if (nodes.length === 0) {
    return <div className="state-block" style={{ padding: "50px 20px" }}><p>Nothing to show here.</p></div>;
  }

  const vbW = bounds.maxX - bounds.minX;
  const vbH = bounds.maxY - bounds.minY;

  return (
    <div className="graph-canvas">
      <svg viewBox={`${bounds.minX} ${bounds.minY} ${vbW} ${vbH}`} width="100%" height={height} role="img" aria-label="Relationship graph">
        <g className="graph-edges">
          {edges.map((e, i) => {
            const s = byId.get(e.source);
            const t = byId.get(e.target);
            if (!s || !t) return null;
            return <line key={i} x1={s.x} y1={s.y} x2={t.x} y2={t.y} className="graph-edge" stroke="#cbd5e1" strokeWidth={1.5} />;
          })}
        </g>
        <g className="graph-nodes">
          {positioned.map((n) => {
            const r = KIND_RADIUS[n.kind];
            const clickable = !!onNodeClick && (n.kind === "person" || n.kind === "focus");
            const isHighlighted = !!n.highlighted;

            return (
              <g
                key={n.id}
                transform={`translate(${n.x},${n.y})`}
                className={`graph-node ${clickable ? "clickable" : ""} ${isHighlighted ? "highlighted" : ""}`}
                onClick={() => clickable && onNodeClick?.(n)}
                tabIndex={clickable ? 0 : undefined}
                role={clickable ? "button" : undefined}
                onKeyDown={(e) => {
                  if (clickable && (e.key === "Enter" || e.key === " ")) onNodeClick?.(n);
                }}
                style={{ cursor: clickable ? "pointer" : "default" }}
              >
                {/* SVG Highlight Halo - renders slightly larger behind the main circle */}
                {isHighlighted && (
                  <circle r={r + 6} fill="none" stroke="#0ea5e9" strokeWidth={4} opacity={0.5} />
                )}
                
                <circle r={r} fill={KIND_FILL[n.kind]} stroke={KIND_STROKE[n.kind]} strokeWidth={n.kind === "focus" ? 2.5 : 1.5} />
                <text textAnchor="middle" dominantBaseline="central" fontSize={n.kind === "skill" ? 10 : 11} fontWeight={600} fill={KIND_TEXT[n.kind]}>
                  {initialsOrShort(n.label, n.kind)}
                </text>
                <text textAnchor="middle" y={r + 17} fontSize={12} fill="var(--ink)" fontWeight={n.kind === "focus" ? 700 : 600}>
                  {truncate(n.label, 24)}
                </text>
                {n.sublabel && (
                  <text textAnchor="middle" y={r + 32} fontSize={10.5} fill="var(--muted)">
                    {truncate(n.sublabel, 30)}
                  </text>
                )}
              </g>
            );
          })}
        </g>
      </svg>
    </div>
  );
}

function initialsOrShort(label: string, kind: NodeKind): string {
  if (kind === "skill" || kind === "hub") return label.slice(0, 2).toUpperCase();
  return label.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
}
function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
