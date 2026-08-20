import type { CSSProperties, ReactNode } from "react";
import {
  Award,
  Briefcase,
  GraduationCap,
  Network,
  SearchIcon,
  Sparkles,
  UserReports,
  Users,
  AlertCircle,
} from "../icons";

export interface AgentDef {
  id: string;
  title: string;
  blurb: string;
  example: string;
  icon: ReactNode;
  group: "core" | "new";
}

export const AGENTS: AgentDef[] = [
  {
    id: "people",
    title: "People Finder",
    blurb: "Find people by name, skill, team, or office.",
    example: "Who knows Python in Bangalore?",
    icon: <SearchIcon size={18} />,
    group: "core",
  },
  {
    id: "org",
    title: "Org Navigator",
    blurb: "Walk reporting lines up or down the org chart.",
    example: "Who reports to Sean Wilson?",
    icon: <Network size={18} />,
    group: "core",
  },
  {
    id: "mentor",
    title: "Mentor Match",
    blurb: "Ranked mentors for a skill you want to learn.",
    example: "Who could mentor me in Terraform?",
    icon: <GraduationCap size={18} />,
    group: "core",
  },
  {
    id: "expert",
    title: "Problem Expert",
    blurb: "People who have solved a problem like yours.",
    example: "Our deploy pipeline keeps failing — who has solved this?",
    icon: <AlertCircle size={18} />,
    group: "core",
  },
  {
    id: "owner",
    title: "Project Owner",
    blurb: "Who owns a project, system, or policy.",
    example: "Who owns the Billing API?",
    icon: <Briefcase size={18} />,
    group: "core",
  },
  {
    id: "coverage",
    title: "Coverage",
    blurb: "Who is covering if someone is away.",
    example: "Who's covering for Diego Hernandez?",
    icon: <Users size={18} />,
    group: "new",
  },
  {
    id: "escalation",
    title: "Escalation Path",
    blurb: "Who to escalate to first for a person or system.",
    example: "Escalation path for the Billing API",
    icon: <UserReports size={18} />,
    group: "new",
  },
  {
    id: "training",
    title: "Training Coach",
    blurb: "Outstanding required courses to take next.",
    example: "What training should I take next?",
    icon: <GraduationCap size={18} />,
    group: "new",
  },
  {
    id: "skill-upskill",
    title: "Skill Upskill",
    blurb: "Who should learn a skill next, based on related project work.",
    example: "Who should be trained next for Terraform / Kubernetes?",
    icon: <Award size={18} />,
    group: "new",
  },
  {
    id: "brief",
    title: "Profile Concierge",
    blurb: "A short brief on someone and who sits above them.",
    example: "Brief me on Diego Hernandez",
    icon: <Sparkles size={18} />,
    group: "new",
  },
];

interface Props {
  onPick: (example: string) => void;
}

export function AgentGallery({ onPick }: Props) {
  const core = AGENTS.filter((a) => a.group === "core");
  const newer = AGENTS.filter((a) => a.group === "new");

  return (
    <div className="agent-gallery" data-help="agent-gallery">
      <header className="agent-gallery__intro">
        <p className="agent-gallery__kicker">Ask the directory</p>
        <h2 className="agent-gallery__title">Pick an agent, or type your own question</h2>
        <p className="agent-gallery__sub">
          Each agent routes to a focused directory tool — same search box, clearer intent.
        </p>
      </header>

      <section className="agent-gallery__section" aria-label="Core agents">
        <div className="agent-gallery__grid">
          {core.map((agent, i) => (
            <AgentCard key={agent.id} agent={agent} onPick={onPick} style={{ animationDelay: `${i * 40}ms` }} />
          ))}
        </div>
      </section>

      <section className="agent-gallery__section" aria-label="New agents">
        <p className="agent-gallery__section-label">New</p>
        <div className="agent-gallery__grid agent-gallery__grid--new">
          {newer.map((agent, i) => (
            <AgentCard
              key={agent.id}
              agent={agent}
              onPick={onPick}
              style={{ animationDelay: `${(core.length + i) * 40}ms` }}
            />
          ))}
        </div>
      </section>
    </div>
  );
}

function AgentCard({
  agent,
  onPick,
  style,
}: {
  agent: AgentDef;
  onPick: (example: string) => void;
  style?: CSSProperties;
}) {
  return (
    <button
      type="button"
      className={`agent-card${agent.group === "new" ? " agent-card--new" : ""}`}
      onClick={() => onPick(agent.example)}
      style={style}
    >
      <span className="agent-card__icon" aria-hidden="true">{agent.icon}</span>
      <span className="agent-card__body">
        <span className="agent-card__title">{agent.title}</span>
        <span className="agent-card__blurb">{agent.blurb}</span>
        <span className="agent-card__example">{agent.example}</span>
      </span>
    </button>
  );
}
