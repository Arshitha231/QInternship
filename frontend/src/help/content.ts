// The single source of help content, consumed by BOTH help modes.
//
// Every topic is keyed to a `data-help="<key>"` attribute somewhere in the
// UI. Guided tour walks the topics that have a `tourStep`; click-to-learn
// looks up whichever topic the user clicked. One registry, two consumers --
// so a feature can never be explained in the tour but silent when clicked,
// or vice versa, which is what happens as soon as the two modes keep
// separate copy.
//
// A topic whose target element is not in the DOM is SKIPPED, not an error.
// That is load-bearing rather than defensive: the Continuity tab only
// exists for hr, Review only for it-in-work-mode, and the results panel
// only exists after a search. Skipping absent targets means role-gating and
// empty states are handled by the same rule, with no role logic duplicated
// here.

export type HelpMode = "profile" | "graphs" | "continuity" | "review";

export interface HelpTopic {
  /** Matches `data-help="<key>"` on the element being described. */
  key: string;
  title: string;
  body: string;
  /** Tab this topic lives on. The tour switches to it before the step. */
  mode?: HelpMode;
  /** Present = part of the guided tour, ordered by this number. */
  tourStep?: number;
}

export const HELP_TOPICS: HelpTopic[] = [
  {
    key: "search",
    title: "Search & ask",
    body:
      "One box for both. Type a name, a skill, or a filter phrase and results appear instantly. " +
      "Ask a full question — \"who could mentor me in Terraform?\" — and it routes to the right " +
      "lookup and writes an answer above the results. Describe a problem you're stuck on and it " +
      "finds people who worked on projects that hit it.",
    tourStep: 1,
  },
  {
    key: "filters",
    title: "Filters",
    body:
      "Narrow by skill, level, org unit, office, language, or availability. Filters combine with " +
      "whatever is in the search box, so you can ask a question and constrain it at the same time. " +
      "Every active filter shows as a chip you can remove.",
    tourStep: 2,
  },
  {
    key: "ai-overview",
    title: "Answer & trace",
    body:
      "When you ask a question, this panel gives the answer in a sentence and cites the people it " +
      "came from. The trace underneath shows which function ran, with what arguments, and how long " +
      "it took — so an answer is always checkable rather than something you take on trust.",
    tourStep: 3,
  },
  {
    key: "results",
    title: "Results",
    body:
      "Each card is one person: role, team, office and availability. Click any card to open their " +
      "full profile. Results respect your permissions — you only ever see what your role is allowed " +
      "to see, and restricted records are absent rather than greyed out.",
    tourStep: 4,
  },
  {
    key: "identity",
    title: "Identity switcher",
    body:
      "A demo affordance: switch between people and roles to see the directory exactly as they would. " +
      "HR sees salary and continuity data, a manager sees their reports, an employee sees neither. " +
      "The server decides all of it — switching here changes who you are, not what you're allowed to do.",
    tourStep: 5,
  },
  {
    key: "viewmode",
    title: "Work / employee mode",
    body:
      "HR and IT can act in two capacities. Work mode exposes the extra fields and edit surfaces their " +
      "job needs; employee mode shows exactly what an ordinary colleague sees. Useful for checking what " +
      "you're about to expose before you share a screen.",
    tourStep: 6,
  },
  {
    key: "notifications",
    title: "Notifications",
    body: "Reminders that concern you — upcoming date milestones and training you still owe.",
  },
  {
    key: "tabs",
    title: "Sections",
    body:
      "Profile is search and people. Graphs draws the org three ways. Continuity (HR) and Review (IT) " +
      "appear only for the roles that own them — which is why the tabs you see change when you switch " +
      "identity.",
    tourStep: 7,
  },
  {
    key: "profile",
    title: "Profile",
    body:
      "Everything on record for one person: contact details, skills by level, project history, and their " +
      "place in the org. Fields you aren't permitted to see are absent from the response entirely, not " +
      "hidden in the browser. HR can edit from here in work mode.",
    mode: "profile",
    tourStep: 8,
  },
  {
    key: "graphs",
    title: "Graphs",
    body:
      "Three views of the same organisation: reporting lines, team structure, and who shares which skills. " +
      "Drag to pan, scroll to zoom, click any node to open that person. Good for finding structure that a " +
      "list of names can't show you.",
    mode: "graphs",
    tourStep: 9,
  },
  {
    key: "continuity",
    title: "Continuity (HR)",
    body:
      "Where upcoming HR review dates intersect live client engagements. It classifies the ENGAGEMENT, " +
      "never the person — \"this project has a single-person dependency\", not \"this employee is a risk\" — " +
      "and shows the delivery dependencies and internal backups behind each rating.",
    mode: "continuity",
    tourStep: 10,
  },
  {
    key: "review",
    title: "Review (IT)",
    body:
      "The queue of changes extracted from uploaded documents, waiting on a human. Nothing an automated " +
      "step proposes reaches a record until someone accepts it here, field by field.",
    mode: "review",
    tourStep: 11,
  },
];

export const TOUR_STEPS: HelpTopic[] = HELP_TOPICS
  .filter((t) => t.tourStep !== undefined)
  .sort((a, b) => a.tourStep! - b.tourStep!);

const BY_KEY = new Map(HELP_TOPICS.map((t) => [t.key, t]));

export function topicFor(key: string): HelpTopic | undefined {
  return BY_KEY.get(key);
}

/**
 * The topic for whatever was clicked: the nearest ancestor carrying a
 * `data-help` key. Walking UP means an element can be described without
 * every child needing its own annotation — clicking the job title inside a
 * person card still resolves to the card.
 */
export function topicForElement(el: Element | null): HelpTopic | undefined {
  const holder = el?.closest("[data-help]");
  const key = holder?.getAttribute("data-help");
  return key ? topicFor(key) : undefined;
}

export function isTargetPresent(topic: HelpTopic): boolean {
  return document.querySelector(`[data-help="${topic.key}"]`) !== null;
}
