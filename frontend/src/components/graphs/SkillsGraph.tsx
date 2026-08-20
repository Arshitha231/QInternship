import { useEffect, useState } from "react";
import { GraphCanvas, type GraphEdge, type GraphNode } from "../GraphCanvas";
import { ApiError, findPeople, getPerson } from "../../api";
import type { Identity, ViewMode } from "../../types";
import { useFitOnChange, useZoomPan, ZoomPanFrame } from "../ZoomPanFrame";

interface Props {
  identity: Identity;
  viewMode: ViewMode;
  focusId: string;
  focusName: string;
  focusRole: string;
  onNavigate: (id: string) => void;
}

// How many holders of any one skill get their own node. find_people already
// returns them in relevance order, so the cap keeps the closest matches and
// drops the tail. Without it a person with eight common skills pulled in
// well over a hundred nodes and the graph became an unreadable hairball --
// every node a few pixels wide, every label collapsed to a smear. The count
// that got dropped is not lost: it is what sizes the skill node itself, and
// it's stated in the summary line above the canvas.
const HOLDERS_PER_SKILL = 8;

// Bipartite: the focus person's own skills, plus whoever else shares each
// one -- who to go find if you want to learn a skill, or who to loop in if
// you need it right now.
export function SkillsGraph({ identity, viewMode, focusId, focusName, focusRole, onNavigate }: Props) {
  const [nodes, setNodes] = useState<GraphNode[] | null>(null);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Totals for the summary line: how many skills, how many distinct
  // colleagues are drawn, and how many holders were left out by the cap.
  const [stats, setStats] = useState({ skills: 0, people: 0, hidden: 0 });

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
          setStats({ skills: 0, people: 0, hidden: 0 });
        }
        return;
      }

      let hidden = 0;
      // Sequential rather than Promise.all: these go through Search, and
      // firing eight at once got the whole batch rate-limited. Left as-is.
      for (const skill of skills) {
        const skillNodeId = `skill:${skill.name}`;
        const holders = (await findPeople(identity, { skill: skill.name }, viewMode))
          .filter((h) => h.id !== focusId);
        const shown = holders.slice(0, HOLDERS_PER_SKILL);
        hidden += holders.length - shown.length;

        nodeMap.set(skillNodeId, {
          id: skillNodeId,
          label: skill.name,
          // The level the FOCUS person holds it at, which is the useful
          // reading of this graph ("what am I strong in"), plus the true
          // total of colleagues who share it -- including any the cap
          // dropped, so the number never under-reports the org.
          sublabel: `${skill.level} · ${holders.length} other${holders.length === 1 ? "" : "s"}`,
          kind: "skill",
          weight: holders.length,
        });
        edgeList.push({ source: focusId, target: skillNodeId });

        for (const h of shown) {
          if (!nodeMap.has(h.id)) {
            nodeMap.set(h.id, { id: h.id, label: h.full_name, sublabel: h.job_title, kind: "person" });
          }
          edgeList.push({ source: h.id, target: skillNodeId });
        }
      }

      if (!cancelled) {
        setNodes(Array.from(nodeMap.values()));
        setEdges(edgeList);
        setStats({
          skills: skills.length,
          // -1 for the focus node itself, which is not a colleague.
          people: nodeMap.size - skills.length - 1,
          hidden,
        });
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
  }, [identity, viewMode, focusId, focusName, focusRole]);

  const zoomPan = useZoomPan();
  useFitOnChange(zoomPan.fit, zoomPan.frameRef, zoomPan.contentRef, `${focusId}:${nodes?.length ?? -1}`);

  if (error) {
    return <div className="state-block error" style={{ padding: "50px 20px" }}><strong>Couldn't load skills</strong><p>{error}</p></div>;
  }
  if (nodes === null) {
    return <div className="skel skel-card" style={{ height: 480 }} />;
  }
  if (stats.skills === 0) {
    return (
      <div className="state-block" style={{ padding: "50px 20px" }}>
        <p>{focusName} has no skills listed yet, so there's nothing to connect.</p>
      </div>
    );
  }
  return (
    <>
      <p className="graph-summary">
        <strong>{stats.skills}</strong> skill{stats.skills === 1 ? "" : "s"}, shared with{" "}
        <strong>{stats.people}</strong> colleague{stats.people === 1 ? "" : "s"}.
        {" "}Bigger squares are skills more people hold — the number inside is the count.
        {stats.hidden > 0 && ` Showing the closest ${HOLDERS_PER_SKILL} people per skill.`}
      </p>
      <ZoomPanFrame height="var(--graph-height)" {...zoomPan}>
        <GraphCanvas
          bare
          height={480}
          nodes={nodes}
          edges={edges}
          onNodeClick={(n) => n.id !== focusId && onNavigate(n.id)}
        />
      </ZoomPanFrame>
    </>
  );
}
