import { useEffect, useState, useMemo } from "react";
import { GraphCanvas, type GraphEdge, type GraphNode } from "../GraphCanvas";
import { ApiError, findPeople, getPerson } from "../../api";
import type { Identity, ViewMode } from "../../types";

interface Props {
  identity: Identity;
  viewMode: ViewMode;
  focusId: string;
  focusName: string;
  focusRole: string;
  highlightedIds?: Set<string>; // <-- Added for Phase 3 Search Highlighting
  onNavigate: (id: string) => void;
}

export function SkillsGraph({ 
  identity, 
  viewMode, 
  focusId, 
  focusName, 
  focusRole, 
  highlightedIds = new Set(), // <-- Default to empty Set
  onNavigate 
}: Props) {
  const [nodes, setNodes] = useState<GraphNode[] | null>(null);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [error, setError] = useState<string | null>(null);

  // 1. Fetch data ONLY when the focused person changes
  useEffect(() => {
    let cancelled = false;
    setNodes(null);
    setError(null);

    async function build() {
      const person = await getPerson(identity, focusId, viewMode);
      const skills = person?.skills ?? [];
      const nodeMap = new Map<string, GraphNode>();
      
      nodeMap.set(focusId, { id: focusId, label: focusName, sublabel: focusRole, kind: "focus" });
      const edgeList: GraphEdge[] = [];

      if (skills.length === 0) {
        if (!cancelled) {
          setNodes([nodeMap.get(focusId)!]);
          setEdges([]);
        }
        return;
      }

      for (const skill of skills) {
        const skillNodeId = `skill:${skill.name}`;
        nodeMap.set(skillNodeId, { id: skillNodeId, label: skill.name, sublabel: skill.level, kind: "skill" });
        edgeList.push({ source: focusId, target: skillNodeId });

        const holders = await findPeople(identity, { skill: skill.name }, viewMode);
        for (const h of holders) {
          if (h.id === focusId) continue;
          if (!nodeMap.has(h.id)) {
            nodeMap.set(h.id, { id: h.id, label: h.full_name, sublabel: h.job_title, kind: "person" });
          }
          edgeList.push({ source: h.id, target: skillNodeId });
        }
      }

      if (!cancelled) {
        setNodes(Array.from(nodeMap.values()));
        setEdges(edgeList);
      }
    }

    build().catch((e) => {
      if (cancelled) return;
      setError(e instanceof ApiError ? e.message : "Unknown error");
      setNodes([]);
    });

    return () => {
      cancelled = true;
    };
  }, [identity, viewMode, focusId, focusName, focusRole]); // Do NOT include highlightedIds here!

  // 2. Apply search highlights efficiently without re-fetching data
  const displayNodes = useMemo(() => {
    if (!nodes) return null;
    return nodes.map((node) => ({
      ...node,
      // Pass a highlighted boolean so GraphCanvas knows which nodes match the search
      highlighted: highlightedIds.has(node.id) 
    }));
  }, [nodes, highlightedIds]);

  if (error) {
    return <div className="state-block error" style={{ padding: "50px 20px" }}><strong>Couldn't load skills</strong><p>{error}</p></div>;
  }
  
  if (displayNodes === null) {
    return <div className="skel skel-card" style={{ height: 480 }} />;
  }
  
  return (
    <GraphCanvas
      nodes={displayNodes}
      edges={edges}
      onNodeClick={(n) => n.id !== focusId && onNavigate(n.id)}
    />
  );
}
