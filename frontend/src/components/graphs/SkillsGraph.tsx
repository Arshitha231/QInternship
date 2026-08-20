import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, getSkillRoutes, getSkillSuggestions } from "../../api";
import type { Identity, SkillRoute, SkillRouteResult, SuggestedSkill, ViewMode } from "../../types";
import { ArrowRight, Award, Briefcase, SearchIcon, Users } from "../../icons";
import { avatarStyle } from "../../avatarHue";
import { initials } from "./treeShared";
import { useDebouncedValue } from "../../hooks";

// ---------------------------------------------------------------------------
// Skill bridges: the shortest introduction chain to a skill you don't have.
//
// This replaced a bipartite force-directed graph -- you in the middle, your
// skills around you, their holders hanging off those. It was replaced because
// its TOPOLOGY WAS FIXED BY CONSTRUCTION: always a star, whatever the data, so
// the layout could never reveal anything the summary line did not already say
// in words ("4 skills, shared with 24 colleagues"). A force-directed layout
// earns its cost when the structure is unknown and the picture discovers it.
// It also cost one sequential request per skill, and the dashboard's skill
// popup now answers "who holds this" far better than a hairball could.
//
// What is drawn here is a PATH, because the answer is a path. Cards left to
// right, each connector labelled with what the two people have in common, so
// the picture doubles as the opening line of the message you are about to
// send. That is the thing no list, filter or search box in this app can do.
//
// Always computed from the SIGNED-IN person, not the graph's focus person --
// "how do I reach X" is a question about you. Same deliberate divergence the
// Community tab makes, and the caption says so.
// ---------------------------------------------------------------------------

const VIA_PREFIX: Record<string, string> = {
  project: "both on",
  team: "both in",
  past_project: "both worked on",
  skill: "both know",
};

function ViaIcon({ kind }: { kind: string }) {
  if (kind === "skill") return <Award size={12} />;
  if (kind === "team") return <Users size={12} />;
  return <Briefcase size={12} />;
}

function PersonPip({
  name, role, tone, onClick,
}: {
  name: string;
  role?: string;
  tone: "you" | "hop" | "target";
  onClick?: () => void;
}) {
  const body = (
    <>
      <span className="avatar" style={avatarStyle(name)} aria-hidden="true">{initials(name)}</span>
      <span className="route-pip-text">
        <span className="route-pip-name">{name}</span>
        {role && <span className="route-pip-role">{role}</span>}
      </span>
    </>
  );
  return onClick ? (
    <button type="button" className={`route-pip route-pip-${tone}`} onClick={onClick}>{body}</button>
  ) : (
    <span className={`route-pip route-pip-${tone}`}>{body}</span>
  );
}

function RouteChain({
  route, youLabel, onOpenProfile,
}: {
  route: SkillRoute;
  youLabel: string;
  onOpenProfile: (id: string, name: string) => void;
}) {
  return (
    <li className="route-chain">
      <div className="route-chain-scroll">
        <PersonPip name={youLabel} tone="you" />
        {route.hops.map((hop, i) => {
          const last = i === route.hops.length - 1;
          return (
            <span className="route-step" key={`${hop.person.id}-${i}`}>
              {/* The label is the point of the whole feature: it is the
                  reason this step exists and the line you open with. */}
              <span className="route-via" title={`${VIA_PREFIX[hop.via_kind] ?? "via"} ${hop.via}`}>
                <ViaIcon kind={hop.via_kind} />
                <span className="route-via-text">
                  {VIA_PREFIX[hop.via_kind] ?? "via"} <strong>{hop.via}</strong>
                </span>
                <ArrowRight size={13} />
              </span>
              <PersonPip
                name={hop.person.full_name}
                role={hop.job_title}
                tone={last ? "target" : "hop"}
                onClick={() => onOpenProfile(hop.person.id, hop.person.full_name)}
              />
            </span>
          );
        })}
      </div>
      <p className="route-chain-foot">
        <span className={`pill ${route.level === "Expert" ? "pill-expert" : "pill-working"}`}>{route.level}</span>
        <span className="muted">
          {route.hops.length === 1
            ? "You already have a direct connection."
            : `${route.hops.length} introductions away.`}
        </span>
      </p>
    </li>
  );
}

export function SkillsGraph({
  identity, viewMode, onNavigate,
}: {
  identity: Identity;
  viewMode: ViewMode;
  onNavigate: (id: string, name: string) => void;
}) {
  const [query, setQuery] = useState("");
  const debounced = useDebouncedValue(query.trim(), 350);
  const [result, setResult] = useState<SkillRouteResult | null>(null);
  const [suggestions, setSuggestions] = useState<SuggestedSkill[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    getSkillSuggestions(identity, identity.id, viewMode, controller.signal)
      .then(setSuggestions)
      .catch(() => setSuggestions([]));
    return () => controller.abort();
  }, [identity, viewMode]);

  useEffect(() => {
    if (!debounced) {
      setResult(null);
      setError(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    getSkillRoutes(identity, identity.id, debounced, viewMode, controller.signal)
      .then((r) => {
        setResult(r);
        setLoading(false);
      })
      .catch((e) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof ApiError ? e.message : "Couldn't work out a route.");
        setLoading(false);
      });
    return () => controller.abort();
  }, [identity, viewMode, debounced]);

  const youLabel = useMemo(() => identity.name || "You", [identity.name]);

  return (
    <div className="skill-routes">
      <div className="skill-routes-search">
        <SearchIcon size={16} />
        <input
          ref={inputRef}
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Name a skill you need — e.g. Kubernetes, GDPR, FHIR Interoperability"
          aria-label="Skill to find a route to"
        />
      </div>

      {/* Empty state does real work: the suggestions come from this person's
          OWN current projects and from what the directory is thin on, each
          carrying the reason it was suggested. A generic "popular skills"
          list would be decoration. */}
      {!debounced && (
        <div className="skill-routes-empty">
          <p className="skill-routes-lead">
            Find the shortest way to reach someone who has a skill you don't — through people you
            already share work with.
          </p>
          {suggestions === null ? (
            <div className="skel skel-card" style={{ height: 90 }} />
          ) : suggestions.length === 0 ? (
            <p className="muted">Nothing obvious to suggest — type any skill above.</p>
          ) : (
            <>
              <p className="skill-label">Worth knowing who to ask</p>
              <ul className="suggest-list">
                {suggestions.map((s) => (
                  <li key={s.skill_id}>
                    <button type="button" className="suggest-chip" onClick={() => setQuery(s.skill)}>
                      <span className="suggest-chip-name">{s.skill}</span>
                      <span className="suggest-chip-why">{s.reason}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {loading && <div className="skel skel-card" style={{ height: 160 }} />}
      {error && <p className="state-block error" style={{ padding: 20 }}>{error}</p>}

      {result && !loading && (
        <div className="skill-routes-result">
          {result.skill === null ? (
            <p className="muted">
              No skill in the directory matches “{result.requested}”. Try another name — synonyms
              resolve, so “SRE” finds Site Reliability Engineering.
            </p>
          ) : (
            <>
              <div className="skill-routes-head">
                <div>
                  <h3 className="skill-routes-title">{result.skill.skill}</h3>
                  <p className="muted">
                    {result.skill.capable_count}{" "}
                    {result.skill.capable_count === 1 ? "person" : "people"} in the directory can do
                    it at Working or above.
                  </p>
                </div>
              </div>

              {result.already_capable ? (
                <p className="state-block" style={{ padding: 18 }}>
                  You already have {result.skill.skill} at Working or above — no introduction needed.
                </p>
              ) : result.routes.length === 0 ? (
                <p className="state-block" style={{ padding: 18 }}>
                  {result.skill.capable_count === 0
                    ? "Nobody in the directory has this skill yet."
                    : `No chain of shared work reaches ${
                        result.skill.capable_count === 1 ? "the one person" : "any of the people"
                      } who ${result.skill.capable_count === 1 ? "has" : "have"} it within three introductions.`}
                </p>
              ) : (
                <ul className="route-list">
                  {result.routes.map((r) => (
                    <RouteChain
                      key={r.target.id}
                      route={r}
                      youLabel={youLabel}
                      onOpenProfile={onNavigate}
                    />
                  ))}
                </ul>
              )}

              {result.unreachable_holder_count > 0 && result.routes.length > 0 && (
                <p className="dashboard-note dashboard-note-quiet">
                  {result.unreachable_holder_count} other{" "}
                  {result.unreachable_holder_count === 1 ? "person has" : "people have"} this skill
                  with no shared-work path to you inside three introductions.
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
