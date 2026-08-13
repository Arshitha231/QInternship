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
  { role: "hr", id: "1bd22d46-069c-4ac1-9607-288b9f55c5d4", name: "Naomi Lewis" },
  { role: "manager", id: "2d984195-dc5b-49c8-9d20-63846dc72235", name: "Sean Wilson" },
  { role: "employee", id: "92fbf228-de0d-4851-9e68-ad6123bc6b54", name: "Min-jun Sanchez" },
  { role: "employee", id: "64d23996-e737-4017-b44d-08fdb7e12f92", name: "Joshua Liu" },
];
