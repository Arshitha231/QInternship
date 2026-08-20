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
// exists for hr, Review only for hr-in-work-mode, and the results panel
// only exists after a search. Skipping absent targets means role-gating and
// empty states are handled by the same rule, with no role logic duplicated
// here.

export type HelpMode = "profile" | "graphs" | "dashboard" | "continuity" | "review" | "admin" | "prds";

export interface HelpTopic {
  /** Matches `data-help="<key>"` on the element being described. */
  key: string;
  title: string;
  body: string;
  /** Tab this topic lives on. The tour switches to it before the step. */
  mode?: HelpMode;
  /** Present = part of the guided tour, ordered by this number. */
  tourStep?: number;
  /**
   * A capability the target depends on, beyond its tab existing.
   *
   * `mode` is not enough for everything. Build Team lives on the Graphs
   * tab, which everyone has, but its button is hidden unless the caller
   * can actually use it -- so the tour walked an ordinary employee to a
   * step with nothing under it. Same failure the mode gate was added for
   * ("an HR user got a Review (IT) step"), one level further in.
   */
  capability?: "build_team";
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
    key: "ask-followup",
    title: "Ask a follow-up",
    body:
      "Keep going without retyping the context. The follow-up box remembers the conversation, so " +
      "\"what about Berlin?\" after a question about Terraform means what you'd expect. Each turn shows " +
      "its own trace, and each one re-runs the permission checks from scratch — a conversation never " +
      "accumulates access, so nothing you couldn't ask directly becomes reachable by asking in stages.",
    tourStep: 4,
  },
  {
    key: "results",
    title: "Results",
    body:
      "Each card is one person: role, team, office and availability. Click any card to open their " +
      "full profile. Results respect your permissions — you only ever see what your role is allowed " +
      "to see, and restricted records are absent rather than greyed out.",
    tourStep: 5,
  },
  {
    key: "identity",
    title: "Who you're signed in as",
    body:
      "Your name and the role your requests carry. Sign out from here and back in as someone else to " +
      "see the directory exactly as they would: HR sees salary and continuity data, a manager sees " +
      "their reports, an employee sees neither. The server decides all of it — who you sign in as " +
      "changes who you are, not what you're allowed to do.",
    tourStep: 6,
  },
  {
    key: "viewmode",
    title: "Work / employee mode",
    body:
      "HR can act in two capacities. Work mode exposes the extra fields and edit surfaces the " +
      "job needs; employee mode shows exactly what an ordinary colleague sees. Useful for checking what " +
      "you're about to expose before you share a screen.",
    tourStep: 7,
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
      "Profile is search and people. Graphs draws the org four ways. Continuity and Review are HR's " +
      "and appear only for the role that owns them — which is why the tabs you see change with who " +
      "you sign in as.",
    tourStep: 8,
  },
  {
    key: "profile",
    title: "Profile",
    body:
      "Everything on record for one person: contact details, skills by level, project history, and their " +
      "place in the org. Fields you aren't permitted to see are absent from the response entirely, not " +
      "hidden in the browser. HR can edit from here in work mode.",
    mode: "profile",
    tourStep: 9,
  },
  {
    key: "graphs",
    title: "Graphs",
    body:
      "Four views of the same organisation, each answering a different question. Every one centres on a " +
      "focus person — the purple node — and clicking any other person re-centres the whole graph on them, " +
      "so you navigate by walking the structure rather than searching again. Drag to pan, scroll to zoom. " +
      "The tabs below switch views; the legend under them tells you what each colour means in the current one.",
    mode: "graphs",
    tourStep: 10,
  },
  {
    key: "graph-department",
    title: "Department graph — reporting lines",
    body:
      "Answers \"where does this person sit in the hierarchy?\". A strict tree: the focus person's manager " +
      "directly above them, their direct reports in a row directly below, joined by right-angled connectors. " +
      "It deliberately shows ONE level each way — no grandparents, no grandchildren — so the picture stays " +
      "readable. Anyone in the reports row who manages people of their own gets an expand toggle; expanding " +
      "one branch never expands its siblings, so depth is something you choose rather than something the " +
      "graph decides. Purple is the focus person, pale purple is their reporting chain.",
    mode: "graphs",
    tourStep: 11,
  },
  {
    key: "graph-team",
    title: "Team graph — who they work alongside",
    body:
      "Answers \"who else is on this team?\". Same tree shape as the Department view, but the box on top is " +
      "the TEAM itself (amber) rather than a manager, with its members in a row underneath (pale purple). " +
      "There's no expand control because a roster is already flat — one level, not a chain. Read it as " +
      "peers, not hierarchy: two people side by side here are colleagues, which the reporting view wouldn't " +
      "tell you if they report to different managers.",
    mode: "graphs",
    tourStep: 12,
  },
  {
    key: "graph-skills",
    title: "Skills graph — who else knows this",
    body:
      "Answers \"who do I go to for X?\". Two KINDS of node, not one: green nodes are skills, pale purple " +
      "nodes are people. An edge means that person holds that skill. It starts from the focus person's own " +
      "skills and fans out to whoever else shares each one — so a skill node with many people attached is " +
      "well covered, and one with almost none is a bus-factor risk worth noticing. Use it to find a mentor, " +
      "or someone to pull in right now.",
    mode: "graphs",
    tourStep: 13,
  },
  {
    key: "graph-community",
    title: "Community graph — your own contacts",
    body:
      "Answers \"who do I personally go to for what?\". Unlike the other three, this one is ALWAYS yours: it " +
      "shows the logged-in person's private contact graph whoever is focused above, and nobody else can see " +
      "it — the endpoint takes no id at all, so there is no way to view a colleague's. Treat it as your own " +
      "notes on the org rather than a fact about it.",
    mode: "graphs",
    tourStep: 14,
  },
  {
    key: "graph-build-team",
    title: "Build Team — staff a project that doesn't exist yet",
    body:
      "Describe a project in plain language and it proposes a team for it: a role breakdown, a ranked " +
      "candidate per role, and a coverage figure with the skill gaps and key-person risks behind it. " +
      "The model reads your brief into roles and skills and does nothing else — it never sees an employee " +
      "record, picks nobody, and writes none of the numbers. Candidates come only from people you are " +
      "already allowed to see, so no way of phrasing the brief widens who it can reach. Replace swaps one " +
      "person and recalculates everything. The tab is absent unless you're HR or have people reporting to " +
      "you, because there is no team to build from otherwise.",
    mode: "graphs",
    capability: "build_team",
    tourStep: 15,
  },
  {
    key: "graph-find-team",
    title: "Find a Team — who to go and ask",
    body:
      "The opposite question to Build Team. Instead of assembling people, it points at a team that already " +
      "exists: describe a technical problem and it ranks real teams and departments by the skills they hold, " +
      "with Expert/Working/Learning counts, the projects they're on, and the manager's name and email so you " +
      "can actually make contact. View Team Graph opens that team in the ordinary hierarchy view. Unlike " +
      "Build Team this is open to everyone — finding a colleague to ask is not a privileged act — and unlike " +
      "search it answers \"which team\" rather than \"which person\".",
    mode: "graphs",
    tourStep: 16,
  },
  {
    key: "continuity",
    title: "Continuity (HR)",
    body:
      "Where upcoming HR review dates intersect live client engagements. It classifies the ENGAGEMENT, " +
      "never the person — \"this project has a single-person dependency\", not \"this employee is a risk\" — " +
      "and shows the delivery dependencies and internal backups behind each rating.",
    mode: "continuity",
    tourStep: 19,
  },
  {
    key: "review",
    title: "Review (HR)",
    body:
      "The queue of changes extracted from uploaded documents, waiting on a human. Nothing an automated " +
      "step proposes reaches a record until someone accepts it here, field by field.",
    mode: "review",
    tourStep: 20,
  },
  {
    key: "dashboard",
    title: "Dashboard",
    body:
      "Workforce analytics for the people you're responsible for. HR sees the whole organisation and " +
      "can narrow to any department; a manager sees their own reporting line and only that — the server " +
      "resolves the scope from who you are, so there is no selection that widens it. Every figure is " +
      "counted from the directory's own records, and each verdict states the numbers behind it.",
    mode: "dashboard",
    tourStep: 17,
  },
  {
    key: "workforce-intelligence",
    title: "Ask the workforce a question",
    body:
      "A natural-language question about the people in your scope — \"where are we thin on cloud skills?\" — " +
      "answered as a structured report rather than a chat reply. The model chooses which analyses to run and " +
      "writes the summary; every figure in it is counted from the directory first, and any number the summary " +
      "states that the data doesn't support gets the whole sentence thrown away in favour of a plain computed " +
      "one. The label under the summary tells you which of the two you're reading.",
    mode: "dashboard",
    tourStep: 18,
  },
  {
    key: "dashboard-skills",
    title: "Skill supply vs demand",
    body:
      "Every skill your scope holds, set against the projects that actually need it. Understaffed means fewer " +
      "capable people than live demand; overrepresented means far more. Click any skill to see who holds it at " +
      "which level and which projects depend on it.",
    mode: "dashboard",
  },
  {
    key: "dashboard-training",
    title: "Training & compliance",
    body:
      "Who has finished required courses, who is overdue, and who is due soon. Deadlines come from the course " +
      "requirement plus any grace period the person's hire date earns them — a longer-serving employee is never " +
      "made overdue by a rule added after they joined. You can send reminders straight from the roster.",
    mode: "dashboard",
  },
  {
    key: "dashboard-projects",
    title: "Project coverage & skill risk",
    body:
      "Live projects whose declared skill requirements aren't met by the people currently on them, and skills " +
      "that sit with too few people to be safe. A gap here is a staffing question; a concentration is a " +
      "bus-factor one.",
    mode: "dashboard",
  },
  {
    key: "dashboard-insights",
    title: "Risks & insights",
    body:
      "The dashboard's own read of everything above it, in sentences. Each insight names the figures it came " +
      "from and links back to the card that produced them, so nothing here is a claim you have to take on " +
      "trust.",
    mode: "dashboard",
  },
  {
    key: "graph-history",
    title: "Retracing your steps",
    body:
      "Back and forward walk the people you've centred on, the way browser history walks pages — useful once " +
      "you've clicked three levels into a reporting chain. Home recentres on you, which is the one destination " +
      "that always means something.",
    mode: "graphs",
  },
  {
    key: "admin",
    title: "People admin (HR)",
    body:
      "Add, restrict, deactivate and restore employee records. The three destructive ones are maker-checker: " +
      "one HR user requests, a different one approves, and nothing changes in between. Deactivated people are " +
      "kept rather than deleted, so the audit trail and anything linked to them survives.",
    mode: "admin",
    tourStep: 21,
  },
  {
    key: "prds",
    title: "PRDs (HR)",
    body:
      "Upload a project's requirements document and review what it proposes — skills and qualitative " +
      "notes — before anything is saved. Ask the PRD assistant about a project's requirements directly; " +
      "it only ever answers from what's on record here, never from the general people directory.",
    mode: "prds",
    tourStep: 22,
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
