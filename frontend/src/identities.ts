import type { Identity } from "./types";

// Real seeded employees, not placeholder ids — so switching identity
// actually exercises RBAC/ABAC (e.g. Sean Wilson as "manager" can see
// direct_reports and downward org-chart; Min-jun Sanchez is a Project
// Nightingale member, so their own project history includes it).
//
// seed.py generates fresh random ids/names on every run, so these are
// only valid for whatever the database was last seeded with — re-run
// seed.py and these will need refreshing again (query for a manager
// with reports, a Project Nightingale member, etc., same as originally).
export const DEV_IDENTITIES: Identity[] = [
  { role: "hr", id: "035ee29c-6632-43f6-936a-202e95a67a25", name: "Ashley Clark" },
  { role: "manager", id: "3a26d2e6-c554-4a95-b130-e9a8affbd1fd", name: "Sean Wilson" },
  { role: "employee", id: "546443e5-1d31-40d3-a2a0-f68aae7cbfa5", name: "Min-jun Sanchez" },
  { role: "employee", id: "42ea4bee-e407-4c03-a324-54ae1e42cc69", name: "Joshua Liu" },
];
