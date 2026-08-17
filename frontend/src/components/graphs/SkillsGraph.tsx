import { useEffect, useState } from "react";
import { GraphCanvas, type GraphEdge, type GraphNode } from "../GraphCanvas";
import { ApiError, findPeople, getPerson } from "../../api";
import type { Identity, ViewMode } from "../../types";
import { useZoomPan, ZoomPanFrame } from "../ZoomPanFrame";

interface Props {
  identity: Identity;
  viewMode: ViewMode;
  focusId: string;
  focusName: string;
  focusRole: string;
  onNavigate: (id: string) => void;
}

// Bipartite: the focus person's own skills, plus whoever else shares each
// one -- who to go find if you want to learn a skill, or who to loop in if
// you need it right now. find_people(skill=X) alone already routes through
// Search and gets the tight relevance cap, so this stays legible without
// its own separate limit.
export function SkillsGraph({ identity, viewMode, focusId, focusName, focusRole, onNavigate }: Props) {
  const [nodes, setNodes] = useState<GraphNode[] | null>(null);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [error, setError] = useState<string | null>(null);

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
  }, [identity, focusId, focusName, focusRole]);

  const zoomPan = useZoomPan();

  if (error) {
    return <div className="state-block error" style={{ padding: "50px 20px" }}><strong>Couldn't load skills</strong><p>{error}</p></div>;
  }
  if (nodes === null) {
    return <div className="skel skel-card" style={{ height: 480 }} />;
  }
  return (
    <ZoomPanFrame height={480} {...zoomPan}>
      <GraphCanvas
        bare
        height={480}
        nodes={nodes}
        edges={edges}
        onNodeClick={(n) => n.id !== focusId && onNavigate(n.id)}
      />
    </ZoomPanFrame>
  );
}
