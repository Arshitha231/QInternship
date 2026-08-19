import type { CommunityLinkOut } from "./types";

// Presentation for the seven canonical roles the backend resolves
// (app/community_roles.py's CANONICAL_ROLES). The backend decides WHO fills
// each role and never sends a caption; this file is the only place the
// wording and icons live, so changing what a role is called is a one-file
// change that can't drift from what the resolver actually answered.
//
// Order matches CANONICAL_ROLES, which is also the order they claim people
// in — keeping the two the same means the graph reads top-to-bottom in the
// same order the backend reasoned in.

export interface RoleMeta {
  key: string;
  icon: string;
  label: string;
  /** What this role answers, in the employee's own words. */
  question: string;
}

export const CANONICAL_ROLE_META: RoleMeta[] = [
  { key: "manager", icon: "👔", label: "Manager", question: "Who I report to" },
  { key: "mentor", icon: "🧑‍🏫", label: "Mentor", question: "Who helps me grow and get settled" },
  {
    key: "hr_rep", icon: "🧑‍💼", label: "HR Representative",
    question: "Leave, benefits, payroll, visas, policies, facilities",
  },
  {
    key: "security_rep", icon: "🔐", label: "Security Representative",
    question: "Security, phishing, incidents",
  },
  {
    key: "it_rep", icon: "💻", label: "IT Representative",
    question: "Computer, accounts, software, VPN, access",
  },
  {
    key: "technical_expert", icon: "🧑‍💻", label: "Technical Expert",
    question: "Who can help me solve a technical problem",
  },
  {
    key: "project_contact", icon: "📋", label: "Project Contact",
    question: "Who knows about a project I'm on",
  },
];

const BY_KEY = new Map(CANONICAL_ROLE_META.map((r) => [r.key, r]));

export function roleMeta(link: CommunityLinkOut): RoleMeta | null {
  return link.role_key ? BY_KEY.get(link.role_key) ?? null : null;
}

/** The caption under a node: the canonical role's name, or — for a personal
 *  link — whatever the owner typed when they added it. */
export function roleCaption(link: CommunityLinkOut): string {
  return roleMeta(link)?.label ?? link.role_label;
}

/** Where this contact is, and whether that's because the owner's own office
 *  had nobody to fill the role. Null when there's nothing worth saying. */
export function locationNote(link: CommunityLinkOut): string | null {
  if (link.is_remote_fallback && link.contact_office_city) {
    const distance = link.distance_km ? ` · ${link.distance_km.toLocaleString()} km` : "";
    return `Nearest: ${link.contact_office_city}${distance}`;
  }
  return link.contact_office_city;
}
