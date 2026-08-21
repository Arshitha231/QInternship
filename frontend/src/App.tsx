import { useEffect, useState } from "react";
import { TopBar } from "./components/TopBar";
import { Filters } from "./components/Filters";
import { UnifiedResults } from "./components/UnifiedResults";
import { AskChat } from "./components/AskChat";
import { ProfilePage, type ProfileStackEntry } from "./components/ProfilePage";
import { GraphPage } from "./components/GraphPage";
import { DashboardPage } from "./components/DashboardPage";
import { ContinuityPage } from "./components/ContinuityPage";
import { PendingApprovals } from "./components/PendingApprovals";
import { PeopleAdminPage } from "./components/PeopleAdminPage";
import { ReviewPage } from "./components/ReviewPage";
import { HelpMenu } from "./components/HelpMenu";
import { LoginPage } from "./components/LoginPage";
import { HelpOverlay } from "./components/HelpOverlay";
import type { HelpState } from "./components/HelpOverlay";
import { Sidebar, type SidebarItem } from "./components/mel/Sidebar";
import { HomePage } from "./components/mel/HomePage";
import { useDebouncedValue, useFocusHistory } from "./hooks";
import { ApiError, UNAUTHORIZED_EVENT, unifiedSearch, type SearchFilters } from "./api";
import { clearSession, loadSession, saveSession } from "./session";
import { WORK_MODE_ROLES } from "./types";
import type { Identity, UnifiedSearchResponse, ViewMode } from "./types";
import { Briefcase, Building, Check, Clock, Home as HomeIcon, Network, SearchIcon, UserReports } from "./icons";

type Mode = "home" | "profile" | "graphs" | "dashboard" | "continuity" | "review" | "admin";

function initialQuery(): string {
  return new URLSearchParams(window.location.search).get("q") ?? "";
}

function profileIdFromPath(): string | null {
  const m = window.location.pathname.match(/^\/profile\/([^/]+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}

function profileUrl(id: string): string {
  return `/profile/${encodeURIComponent(id)}${window.location.search}`;
}

// Nothing below the gate ever sees a null identity, and <Directory> is
// keyed by id: signing in as someone else remounts the whole app rather
// than trying to reconcile one person's open profile stack, graph focus and
// view mode with another person's permissions.
export default function App() {
  const [identity, setIdentity] = useState<Identity | null>(loadSession);

  function signOut() {
    clearSession();
    setIdentity(null);
    // Back to a bare URL, so the next person to sign in doesn't land on the
    // previous one's profile before their own is loaded.
    window.history.replaceState(null, "", "/");
  }

  // A 401 from any call means these headers are no longer accepted (the
  // backend restarted into entra mode, say). Drop the session rather than
  // leaving every panel rendering its own error.
  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, signOut);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, signOut);
  }, []);

  if (!identity) {
    return (
      <LoginPage
        onLogin={(next) => {
          saveSession(next);
          // Point the URL at their own profile BEFORE mounting Directory --
          // its initial state reads the path, so this is what makes signing
          // in land you on your own page rather than on whatever the last
          // session left in the address bar. Also drops a stale ?q=.
          window.history.replaceState(null, "", `/profile/${encodeURIComponent(next.id)}`);
          setIdentity(next);
        }}
      />
    );
  }
  return <Directory key={identity.id} identity={identity} onSignOut={signOut} />;
}

interface DirectoryProps {
  identity: Identity;
  onSignOut: () => void;
}

function Directory({ identity, onSignOut }: DirectoryProps) {
  // Work mode for the two roles that have one (matching the server's own
  // default), employee mode for everyone else -- so the UI never opens
  // claiming a mode the server isn't honouring.
  const [viewMode, setViewMode] = useState<ViewMode>(
    WORK_MODE_ROLES.includes(identity.role) ? "work" : "employee",
  );
  const [mode, setMode] = useState<Mode>("home");
  const [help, setHelp] = useState<HelpState>("off");
  const [query, setQuery] = useState(initialQuery);
  const [filters, setFilters] = useState<SearchFilters>({});
  const [profileStack, setProfileStack] = useState<ProfileStackEntry[]>(() => {
    // The path wins on a refresh or a shared deep link; signing in has
    // already rewritten it to this person's own profile (see App above).
    const urlId = profileIdFromPath();
    return urlId ? [{ id: urlId, name: "" }] : [{ id: identity.id, name: identity.name }];
  });
  const profileId = profileStack[profileStack.length - 1].id;
  // The graph's focus person, with a back/forward trail behind it. Home is
  // the signed-in identity: "recentre on me" is the one destination that is
  // always meaningful, whoever you have wandered off to.
  const graphFocus = useFocusHistory(identity.id);
  // Whether to OFFER the Dashboard tab. HR gets it in work mode, the same
  // gate Continuity/Review/Admin carry; a manager gets it in either mode,
  // because their dashboard is their own reporting line and that is not a
  // privileged lens -- app/permissions.py already grants a manager their
  // reports' training status, which is the only non-BASE_FIELDS data on it.
  //
  // This is a decision about what to SHOW. The backend resolves the scope
  // from the caller regardless (app/analytics.py's resolve_scope) and 403s
  // anyone with no dashboard of their own, including a "manager" role claim
  // with nobody reporting to them -- so a wrong guess here can only ever
  // render a tab that then says so, never open one that should be shut.
  const canSeeDashboard =
    (identity.role === "hr" && viewMode === "work") || identity.role === "manager";
  const [flashId, setFlashId] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);
  const [savedSearch, setSavedSearch] = useState<{ query: string; filters: SearchFilters } | null>(null);

  // Normalize the URL to reflect the initial profile on first mount, without
  // creating a spurious history entry.
  useEffect(() => {
    window.history.replaceState({ stack: profileStack }, "", profileUrl(profileStack[profileStack.length - 1].id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Browser back/forward restores whichever stack was pushed at that point
  // in history; a bare URL with no app state (e.g. a fresh navigation from
  // outside) falls back to a single-entry stack for that id.
  useEffect(() => {
    function onPopState(e: PopStateEvent) {
      const state = e.state as { stack?: ProfileStackEntry[] } | null;
      if (state?.stack && state.stack.length > 0) {
        setProfileStack(state.stack);
        setMode("profile");
      } else {
        const urlId = profileIdFromPath();
        if (urlId) {
          setProfileStack([{ id: urlId, name: "" }]);
          setMode("profile");
        }
      }
    }
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  function goToStack(nextStack: ProfileStackEntry[]) {
    setProfileStack(nextStack);
    window.history.pushState({ stack: nextStack }, "", profileUrl(nextStack[nextStack.length - 1].id));
  }

  function pushProfile(id: string, name: string) {
    goToStack([...profileStack, { id, name }]);
  }

  function resetProfile(id: string, name: string) {
    goToStack([{ id, name }]);
  }

  function backOneProfile() {
    if (profileStack.length > 1) goToStack(profileStack.slice(0, -1));
  }

  function jumpToProfileIndex(index: number) {
    if (index < profileStack.length - 1) goToStack(profileStack.slice(0, index + 1));
  }

  function backToSearch() {
    if (!savedSearch) return;
    setQuery(savedSearch.query);
    setFilters(savedSearch.filters);
    setSavedSearch(null);
  }

  const debouncedQuery = useDebouncedValue(query, 300);
  const debouncedFilters = useDebouncedValue(filters, 300);

  const [response, setResponse] = useState<UnifiedSearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hasQuery = debouncedQuery.trim() !== "" || Object.keys(debouncedFilters).length > 0;

  // Keeps the URL shareable/bookmarkable (?q=...) without spamming browser
  // history on every keystroke -- replaceState, not pushState. A copied
  // link at any point in time reproduces the same search.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (debouncedQuery.trim()) params.set("q", debouncedQuery.trim());
    else params.delete("q");
    const qs = params.toString();
    window.history.replaceState(null, "", qs ? `${window.location.pathname}?${qs}` : window.location.pathname);
  }, [debouncedQuery]);

  useEffect(() => {
    if (!hasQuery) {
      setResponse(null);
      setLoading(false);
      setError(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    unifiedSearch(identity, { q: debouncedQuery.trim() || undefined, ...debouncedFilters }, viewMode, controller.signal)
      .then((res) => {
        setResponse(res);
        setLoading(false);
      })
      .catch((e) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof ApiError ? e.message : "Unknown error");
        setLoading(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedQuery, debouncedFilters, identity, viewMode, retryToken]);

  function jumpToCard(id: string) {
    const el = document.getElementById(`person-card-${id}`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    setFlashId(id);
    window.setTimeout(() => setFlashId((cur) => (cur === id ? null : cur)), 1200);
  }

  function openProfile(id: string, name: string) {
    resetProfile(id, name);
    setMode("profile");
  }

  // Recorded as a navigation, not a reset: arriving in the graph from a
  // profile drill-down should still be reversible with Back -- the same
  // reasoning DashboardPage's identical onOpenGraph call already carries.
  function openOrgChart(id: string) {
    graphFocus.go(id);
    setMode("graphs");
  }

  // "People" has no Mode of its own -- typing into either search box shows
  // results regardless of `mode` (see the ternary below), so its sidebar
  // entry is a shortcut into that experience rather than a page swap.
  const activeSidebarKey = hasQuery ? "people" : mode;

  const sidebarItems: SidebarItem[] = [
    {
      key: "home", label: "Home", icon: <HomeIcon size={17} />, active: activeSidebarKey === "home",
      onClick: () => { setMode("home"); setQuery(""); },
    },
    {
      key: "people", label: "People", icon: <SearchIcon size={17} />, active: activeSidebarKey === "people",
      onClick: () => document.getElementById("q")?.focus(),
    },
    {
      key: "profile", label: "My Profile", icon: <UserReports size={17} />, active: activeSidebarKey === "profile",
      onClick: () => {
        setMode("profile");
        setQuery("");
        setSavedSearch(null);
        resetProfile(identity.id, identity.name);
      },
    },
    {
      key: "graphs", label: "Organization", icon: <Network size={17} />, active: activeSidebarKey === "graphs",
      onClick: () => { setMode("graphs"); setQuery(""); },
    },
    ...(canSeeDashboard ? [{
      key: "dashboard", label: "Dashboard", icon: <Briefcase size={17} />, active: activeSidebarKey === "dashboard",
      onClick: () => { setMode("dashboard"); setQuery(""); },
    }] : []),
    ...(identity.role === "hr" && viewMode === "work" ? [{
      key: "continuity", label: "Continuity", icon: <Clock size={17} />, active: activeSidebarKey === "continuity",
      onClick: () => { setMode("continuity"); setQuery(""); },
    }] : []),
    ...(identity.role === "hr" && viewMode === "work" ? [{
      key: "review", label: "Review", icon: <Check size={17} />, active: activeSidebarKey === "review",
      onClick: () => { setMode("review"); setQuery(""); },
    }] : []),
    ...(identity.role === "hr" && viewMode === "work" ? [{
      key: "admin", label: "Admin", icon: <Building size={17} />, active: activeSidebarKey === "admin",
      onClick: () => { setMode("admin"); setQuery(""); },
    }] : []),
  ];

  return (
    <div className="app">
      <TopBar
        query={query}
        onQueryChange={setQuery}
        identity={identity}
        viewMode={viewMode}
        onViewModeChange={(next) => {
          setViewMode(next);
          // Review is one of HR's WORK-mode surfaces (it edits real
          // records) -- it doesn't exist in employee mode. If it was open
          // when the toggle switched away from work mode, there'd be no tab
          // left to click back out of it.
          if (next !== "work" && mode === "review") setMode("profile");
          // Admin is HR's work-mode surface for the same reason.
          if (next !== "work" && mode === "admin") setMode("profile");
          // HR's dashboard is an org-wide view, so it belongs to work mode
          // for the same reason Continuity does -- and the server refuses
          // it in employee mode regardless. A manager keeps theirs: their
          // scope is their own team either way.
          if (next !== "work" && mode === "dashboard" && identity.role === "hr") setMode("profile");
          // So is Continuity: employee mode is "what an ordinary colleague
          // sees", and work-authorization review dates are precisely what an
          // ordinary colleague does not see. The server refuses these calls
          // in employee mode (app.continuity._require_hr) -- this only stops
          // the UI from asking.
          if (next !== "work" && mode === "continuity") setMode("profile");
        }}
        onSignOut={onSignOut}
        onOpenPerson={(id, name) => {
          resetProfile(id, name);
          setMode("profile");
          setQuery("");
        }}
        helpSlot={
          <HelpMenu
            state={help}
            onStart={(next) => setHelp(next)}
            onExit={() => setHelp("off")}
          />
        }
      />

      <div className="mel-shell">
        <Sidebar items={sidebarItems} />
        <div className="mel-shell-main">
      <PendingApprovals identity={identity} viewMode={viewMode} />

      <main className="content">
        {hasQuery ? (
          <>
            <div data-help="filters">
              <Filters filters={filters} onChange={setFilters} />
            </div>
            <div data-help="results">
            <UnifiedResults
              loading={loading}
              error={error}
              response={response}
              hasQuery={hasQuery}
              flashId={flashId}
              onSelect={(id, name) => {
                setSavedSearch({ query: debouncedQuery, filters: debouncedFilters });
                resetProfile(id, name);
                setMode("profile");
                setQuery("");
              }}
              onJumpToCard={jumpToCard}
              onExampleClick={(text) => setQuery(text)}
              onRetry={() => setRetryToken((t) => t + 1)}
            />
            {/* Available under every search, direct or assisted -- a bare
                name/skill match still deserves a way to ask a follow-up
                ("which of those are in Bangalore?") without retyping the
                whole thing as a question. This costs nothing extra for a
                direct search: the model is only ever called once the box
                is actually used, so a plain lookup stays exactly as free
                as it is today (see unified_search.py's
                test_direct_mode_never_calls_the_model — untouched by
                this). Remounts (key=debouncedQuery) on a brand new
                question, so an old conversation never appears to answer
                a different one. */}
            {!loading && !error && response !== null && (
              <AskChat key={debouncedQuery} identity={identity} viewMode={viewMode} onSelect={(id, name) => {
                setSavedSearch({ query: debouncedQuery, filters: debouncedFilters });
                resetProfile(id, name);
                setMode("profile");
                setQuery("");
              }} />
            )}
            </div>
          </>
        ) : mode === "home" ? (
          <HomePage
            identity={identity}
            viewMode={viewMode}
            canSeeDashboard={canSeeDashboard}
            query={query}
            onQueryChange={setQuery}
            stack={profileStack}
            onOpenPerson={openProfile}
          />
        ) : mode === "profile" ? (
          <div data-help="profile">
          <ProfilePage
            personId={profileId}
            identity={identity}
            viewMode={viewMode}
            stack={profileStack}
            onNavigate={pushProfile}
            onBack={backOneProfile}
            onBreadcrumb={jumpToProfileIndex}
            onBackToSearch={savedSearch ? backToSearch : undefined}
            onOpenOrgChart={openOrgChart}
          />
          </div>
        ) : mode === "graphs" ? (
          <div data-help="graphs">
          <GraphPage
            identity={identity}
            viewMode={viewMode}
            focus={graphFocus}
            onOpenProfile={(id, name) => {
              resetProfile(id, name);
              setMode("profile");
            }}
          />
          </div>
        ) : mode === "dashboard" && canSeeDashboard ? (
          <div data-help="dashboard">
            <DashboardPage
              identity={identity}
              viewMode={viewMode}
              onOpenProfile={(id, name) => {
                resetProfile(id, name);
                setMode("profile");
              }}
              onOpenGraph={(employeeId) => {
                // Recorded as a navigation, not a reset: arriving in the
                // graph from a dashboard drill-down should still be
                // reversible with Back.
                graphFocus.go(employeeId);
                setMode("graphs");
              }}
            />
          </div>
        ) : mode === "continuity" && identity.role === "hr" && viewMode === "work" ? (
          <div data-help="continuity"><ContinuityPage identity={identity} viewMode={viewMode} /></div>
        ) : mode === "review" && identity.role === "hr" && viewMode === "work" ? (
          <div data-help="review"><ReviewPage identity={identity} viewMode={viewMode} /></div>
        ) : mode === "admin" && identity.role === "hr" && viewMode === "work" ? (
          <div data-help="admin">
            <PeopleAdminPage
              identity={identity} viewMode={viewMode}
              onOpenPerson={(id, name) => {
                resetProfile(id, name);
                setMode("profile");
              }}
            />
          </div>
        ) : null}
      </main>
        </div>
      </div>

      <HelpOverlay
        state={help}
        // Mirrors the tab bar's own render conditions above -- if a tab
        // isn't there, the tour must not walk to it.
        availableModes={[
          "profile",
          "graphs",
          ...(canSeeDashboard ? (["dashboard"] as const) : []),
          ...(identity.role === "hr" && viewMode === "work" ? (["continuity"] as const) : []),
          ...(identity.role === "hr" && viewMode === "work" ? (["review"] as const) : []),
        ]}
        onExit={() => setHelp("off")}
        onRequestMode={(m) => {
          // Clearing the query matters as much as setting the mode: the
          // content area renders SEARCH RESULTS whenever hasQuery is true,
          // whatever `mode` says (see the ternary above). Setting mode
          // alone left the tour narrating the Graphs tabs while the page
          // still showed a result list, and the step's target never
          // existed. The real tab buttons clear the query for the same
          // reason -- this just does what clicking them does.
          setMode(m as Mode);
          setQuery("");
        }}
      />
    </div>
  );
}
