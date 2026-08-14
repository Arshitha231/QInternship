import { API_BASE } from "./api";
import type { Identity } from "./types";

// Real seeded employees, not placeholder ids -- so switching identity
// actually exercises RBAC/ABAC (a manager with real direct reports gets
// direct_reports and the downward org-chart; a plain IC with none is what
// makes the restricted view visible).
//
// There are two lists because there are two databases. seed.py draws names
// from a fixed RNG seed but ids from uuid4, so every run produces the same
// PEOPLE with different IDS -- and the local sqlite file and the deployed
// Azure SQL database were seeded by separate runs. A single hardcoded list
// is therefore always wrong in one environment: pointing it at local ids
//404s every profile on tempest34, and pointing it at production ids 404s
// every profile in `npm run dev`. Both failure modes have now happened.
//
// Keyed off API_BASE rather than a build flag, because which backend the
// app talks to is exactly what determines which ids resolve. `npm run dev`
// leaves VITE_API_BASE unset (localhost default -> local ids); the CI build
// sets it empty for same-origin production, and `npm run dev:live` points it
// at the Azure host -- both non-localhost, both production ids.
//
// When either database is re-seeded, its list needs refreshing: look the
// same people up by full_name and paste the new ids in. If the picker starts
// 404ing on its own profiles, that's what happened.

// Local sqlite (directory.db). Xiomara Mensah reports to Sean Wilson, who
// reports to Min-jun Sanchez -- one chain across three pickable identities,
// which is what makes the certification notifications demoable from the UI:
// a status change on Xiomara puts a reminder in her bell and a report in
// both of theirs. Naomi Lewis (hr) deliberately gets neither -- HR can see
// anyone's training status on their profile but is not a recipient.
const LOCAL_IDENTITIES: Identity[] = [
  { role: "hr", id: "647a0d15-9025-4bb4-ba05-495dc5d7937b", name: "Naomi Lewis" },
  { role: "manager", id: "cbf363ff-ce83-40ec-86f9-9db6804e3574", name: "Sean Wilson" },
  { role: "employee", id: "af7f7812-be03-48e9-b81e-ee99a2b183ef", name: "Xiomara Mensah" },
  { role: "employee", id: "24a49b42-6c9d-43c0-abd0-8caca7d04a70", name: "Min-jun Sanchez" },
  { role: "employee", id: "67fbdd87-f4c4-45b0-9224-41e7680d827d", name: "Joshua Liu" },
];

// Deployed Azure SQL (tempest-database1). Verified live against
// tempest34.azurewebsites.net -- same four people the picker has always had.
const PRODUCTION_IDENTITIES: Identity[] = [
  { role: "hr", id: "1bd22d46-069c-4ac1-9607-288b9f55c5d4", name: "Naomi Lewis" },
  { role: "manager", id: "2d984195-dc5b-49c8-9d20-63846dc72235", name: "Sean Wilson" },
  { role: "employee", id: "92fbf228-de0d-4851-9e68-ad6123bc6b54", name: "Min-jun Sanchez" },
  { role: "employee", id: "64d23996-e737-4017-b44d-08fdb7e12f92", name: "Joshua Liu" },
];

const TALKS_TO_LOCAL_API = /^https?:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/.test(API_BASE);

export const DEV_IDENTITIES: Identity[] = TALKS_TO_LOCAL_API
  ? LOCAL_IDENTITIES
  : PRODUCTION_IDENTITIES;
