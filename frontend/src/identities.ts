import type { Identity } from "./types";

// Real seeded employees, not placeholder ids — so switching identity
// actually exercises RBAC/ABAC (e.g. Sean Wilson as "manager" has 14 direct
// reports, so he gets direct_reports and the downward org-chart; Joshua Liu
// is a plain IC with none, which is what makes the restricted view visible).
//
// Xiomara Mensah reports to Sean Wilson, who reports to Min-jun Sanchez —
// one chain across three pickable identities, which is what makes the
// certification notifications demoable: a status change on Xiomara puts a
// "you didn't pass X" reminder in her bell and a "did not complete X" report
// in both of theirs. Naomi Lewis (hr) deliberately gets neither — HR can see
// anyone's training status on their profile, but is not a notification
// recipient.
//
// seed.py draws names from a fixed RNG seed but ids from uuid4, so a re-seed
// keeps these names and invalidates every id below. When the picker starts
// 404ing on its own profiles, that's what happened — look the same five
// people up by full_name and paste the new ids in.
export const DEV_IDENTITIES: Identity[] = [
  { role: "hr", id: "647a0d15-9025-4bb4-ba05-495dc5d7937b", name: "Naomi Lewis" },
  { role: "manager", id: "cbf363ff-ce83-40ec-86f9-9db6804e3574", name: "Sean Wilson" },
  { role: "employee", id: "af7f7812-be03-48e9-b81e-ee99a2b183ef", name: "Xiomara Mensah" },
  { role: "employee", id: "24a49b42-6c9d-43c0-abd0-8caca7d04a70", name: "Min-jun Sanchez" },
  { role: "employee", id: "67fbdd87-f4c4-45b0-9224-41e7680d827d", name: "Joshua Liu" },
];
