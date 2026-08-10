import type { Identity } from "./types";

// Real seeded employees, not placeholder ids — so switching identity
// actually exercises RBAC/ABAC (e.g. Sean Wilson as "manager" can see
// direct_reports and downward org-chart; Priya Brown is a Project
// Nightingale member, so her own project history includes it).
export const DEV_IDENTITIES: Identity[] = [
  { role: "hr", id: "647a0d15-9025-4bb4-ba05-495dc5d7937b", name: "Naomi Lewis" },
  { role: "manager", id: "cbf363ff-ce83-40ec-86f9-9db6804e3574", name: "Sean Wilson" },
  { role: "employee", id: "5d11bd84-bc15-4fa3-bab4-00a753f6e7ca", name: "Priya Brown" },
  { role: "employee", id: "74e72796-f56b-42f7-9119-52dc36f02af0", name: "Shaun Anderson" },
];
