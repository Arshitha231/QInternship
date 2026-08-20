import { useMemo } from "react";
import type { ProposedRole, TeamProposal } from "../../types";
import { initials, useTreeConnectors, type TreeGroup } from "./treeShared";
import { useFitOnChange, useZoomPan, ZoomPanFrame } from "../ZoomPanFrame";
import { avatarStyle } from "../../avatarHue";
import { Sparkles } from "../../icons";

/** The AI-proposed team, drawn with the same connector engine as the real
 *  org charts and deliberately NOT the same styling.
 *
 *  This graph is the one place in the app where a picture of people joined
 *  by lines does not mean "reports to". Somebody glancing at it must not be
 *  able to mistake it for the hierarchy two tabs away, so the difference is
 *  carried by four things at once rather than by a caption alone:
 *
 *    - dashed card borders and dashed connectors (the real charts are solid)
 *    - a violet proposal tint instead of the directory's card white
 *    - a persistent "AI-Proposed Team" banner inside the frame, so it is
 *      still on screen when the graph is scrolled or screenshotted
 *    - the root is the PROJECT, not a person
 *
 *  That last one matters most. The obvious layout puts a Project Manager on
 *  top with everyone beneath, which draws exactly the reporting relationship
 *  this graph does not represent -- and invents a manager the plan never
 *  proposed. Rooting it at the project says the true thing: these people are
 *  joined by the work, not by a chain of command.
 */

const ROOT_ID = "__proposed_project__";

export function ProposedTeamGraph({
  proposal,
  onOpenProfile,
  onReplace,
}: {
  proposal: TeamProposal;
  onOpenProfile: (id: string, name: string) => void;
  onReplace: (roleIndex: number) => void;
}) {
  const zoomPan = useZoomPan();

  const filled = useMemo(
    () => proposal.roles.map((r, i) => ({ role: r, index: i })),
    [proposal.roles],
  );

  const groups: TreeGroup[] = useMemo(
    () => [{ parentId: ROOT_ID, childIds: filled.map((f) => `role-${f.index}`) }],
    [filled],
  );

  const { wrapRef, registerNode, linePaths, svgSize } = useTreeConnectors(groups, [
    proposal.project_type,
    // Re-measure when the roster changes identity, not just length -- a
    // Replace swaps one name for another and can change a card's height.
    proposal.roles.map((r) => r.candidate?.employee_id ?? "-").join(","),
  ]);

  useFitOnChange(zoomPan.fit, zoomPan.frameRef, zoomPan.contentRef,
    proposal.roles.map((r) => r.candidate?.employee_id ?? "-").join(","),
    zoomPan.fitIfNeeded, `${svgSize.width}x${svgSize.height}`);

  return (
    <div className="proposed-team">
      {/* Inside the frame, not above it: a label that scrolls away is a
          label that isn't there when it matters. */}
      <p className="proposed-team-banner">
        <Sparkles size={14} />
        <strong>AI-Proposed Team</strong>
        <span>Not a reporting hierarchy — these people do not report to each other.</span>
      </p>

      <ZoomPanFrame height="var(--graph-height)" {...zoomPan}>
        <div className="org-tree-wrap proposed-tree-wrap" ref={wrapRef}>
          <svg
            className="org-tree-lines"
            width={svgSize.width}
            height={svgSize.height}
            viewBox={`0 0 ${svgSize.width} ${svgSize.height}`}
          >
            {linePaths.map((p) => (
              <path key={p.id} d={p.d} className="tree-edge proposed-edge" />
            ))}
          </svg>

          <div className="org-tree">
            <div className="tree-tier tree-tier-center" data-tier="Project">
              <div className="proposed-root" ref={registerNode(ROOT_ID)}>
                <Sparkles size={16} />
                <p className="proposed-root-name">{proposal.project_type}</p>
                <p className="proposed-root-meta">
                  {proposal.roles.length} role{proposal.roles.length === 1 ? "" : "s"}
                  {" · "}
                  {proposal.coverage.coverage_pct}% coverage
                </p>
              </div>
            </div>

            <div className="tree-tier tree-tier-reports" data-tier="Proposed">
              {filled.map(({ role, index }) => (
                <ProposedNode
                  key={index}
                  role={role}
                  registerRef={registerNode(`role-${index}`)}
                  onOpenProfile={onOpenProfile}
                  onReplace={() => onReplace(index)}
                />
              ))}
            </div>
          </div>
        </div>
      </ZoomPanFrame>
    </div>
  );
}

function ProposedNode({
  role,
  registerRef,
  onOpenProfile,
  onReplace,
}: {
  role: ProposedRole;
  registerRef: (el: HTMLDivElement | null) => void;
  onOpenProfile: (id: string, name: string) => void;
  onReplace: () => void;
}) {
  const c = role.candidate;

  // An unfilled role is a real answer, not a rendering failure -- it means
  // nobody in the caller's authorized pool holds any of its skills. Drawn as
  // a node so the shape of the team stays honest.
  if (!c) {
    return (
      <div className="proposed-node proposed-node-empty" ref={registerRef}>
        <p className="proposed-node-role">{role.role}</p>
        <p className="proposed-node-vacant">No match in scope</p>
        <p className="proposed-node-skills">{role.required_skills.join(" · ")}</p>
      </div>
    );
  }

  const keySkills = c.matched_skills.filter((s) => s.required).slice(0, 3);

  return (
    <div className="proposed-node" ref={registerRef}>
      <p className="proposed-node-role">{role.role}</p>

      <button
        className="proposed-node-person"
        onClick={() => onOpenProfile(c.employee_id, c.full_name)}
        title={`Open ${c.full_name}'s profile`}
      >
        <span className="avatar" style={avatarStyle(c.full_name)} aria-hidden="true">
          {initials(c.full_name)}
        </span>
        <span className="proposed-node-name">{c.full_name}</span>
      </button>

      <p className="proposed-node-title">{c.job_title}</p>

      <p className={`proposed-node-match ${matchClass(c.match_pct)}`}>
        <span className="proposed-match-bar" aria-hidden="true">
          <span style={{ width: `${Math.min(100, c.match_pct)}%` }} />
        </span>
        {c.match_pct}% match
      </p>

      {keySkills.length > 0 && (
        <p className="proposed-node-skills">
          {keySkills.map((s) => (
            <span key={s.skill} className={`proposed-chip lvl-${s.level.toLowerCase()}`}>
              {s.skill}
            </span>
          ))}
        </p>
      )}

      {c.missing_skills.length > 0 && (
        <p className="proposed-node-gap">Missing {c.missing_skills.join(", ")}</p>
      )}

      <button className="proposed-node-replace" onClick={onReplace}>
        Replace
      </button>
    </div>
  );
}

// Bands rather than a gradient: a reader comparing two cards needs to see
// "these are in different classes", which a continuous ramp does not give
// them at a glance.
function matchClass(pct: number): string {
  if (pct >= 75) return "match-strong";
  if (pct >= 50) return "match-partial";
  return "match-weak";
}
