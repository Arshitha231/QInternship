import { useEffect, useMemo, useState } from "react";
import { ApiError, createCommunityLink, deleteCommunityLink, getPerson, listCommunityLinks } from "../api";
import type { CommunityLinkOut, Identity, PersonDetail, PersonSummary, ViewMode } from "../types";
import { EmployeeSearchPicker } from "./ReviewPage";
import { CANONICAL_ROLE_META, locationNote, roleCaption, roleMeta } from "../community";
import { useFitOnChange, useZoomPan, ZoomPanFrame } from "./ZoomPanFrame";
import { X } from "../icons";
import { avatarStyle } from "../avatarHue";

// Community Graph canvas: a zoomable/pannable node graph (pan/zoom/home
// chrome shared with Department/Team/Skills via ZoomPanFrame -- see that
// file), always centered on the logged-in identity's own node (never the
// Graphs tab's generic "focus person" -- see CommunityPage's note on that
// divergence). Every community_links row (manager included -- see
// app/community_links.py's synthesized manager entry) sits on a ring around
// self, connected by a straight line styled the same as Department/Team's
// .tree-edge connectors (same stroke token) so it reads as the same app's
// line language, even though the layout itself is radial rather than their
// tiered tree.
//
// The ring's radius is computed from the node count, not a fixed constant:
// evenly-spaced points on a circle get CLOSER together as more of them share
// the same radius, so a fixed radius that looked fine for 3 contacts
// visually overlaps once someone has 7 -- radiusForCount below grows the
// radius so adjacent cards' chord distance always clears the card's own
// footprint, at any count.

const STATUS_LABEL: Record<string, string> = {
  available: "Available",
  away: "Away",
  restricted: "Restricted",
};

function initials(name: string): string {
  return name.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
}

function errorMessage(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback;
}

const FRAME_HEIGHT = 560;

// .tree-node is 154px wide; a card can grow to roughly 200px tall in edit
// mode once the source badge (and, for a personal link, the remove button
// overhanging above it) are showing -- the diagonal of that footprint, plus
// a real gap, is the minimum straight-line distance this canvas ever wants
// between two adjacent cards' centers, regardless of which direction
// they're offset from each other.
const CARD_DIAGONAL = Math.hypot(154, 200);
const MIN_ADJACENT_GAP = CARD_DIAGONAL + 40;
const MIN_RADIUS = 170;

function radiusForCount(n: number): number {
  // Fewer than 2 nodes: no adjacent pair to keep apart, so there's nothing
  // for the chord formula to solve for (and it would divide by sin(π)=0 at
  // n=1) -- the floor radius is the only thing that matters.
  if (n < 2) return MIN_RADIUS;
  // Chord length between two points spaced angleStep apart on a circle of
  // radius r is 2r·sin(angleStep/2); solving for r at the minimum required
  // chord gives the radius below which adjacent cards would visually
  // overlap, for this many evenly-spaced points.
  const angleStep = (2 * Math.PI) / n;
  const required = MIN_ADJACENT_GAP / (2 * Math.sin(angleStep / 2));
  return Math.max(MIN_RADIUS, required);
}

interface ContactNode {
  link: CommunityLinkOut;
  x: number;
  y: number;
  // undefined = still loading. Never null: a link whose contact resolved to
  // "not visible to you" is dropped before nodes are built, so no node can
  // exist without a person to name it. That used to be a `| null` case, and
  // the card fell back to rendering the raw contact id.
  person: PersonDetail | undefined;
}

// ---------------------------------------------------------------------------
// One connected person's mini card. Every edit-mode affordance lives here:
// the source badge (always exact, never a shared/generic one) and the
// remove control, which is rendered at all -- not just disabled -- only for
// a personal link. An official link never reaches the branch that renders
// a remove button, in or out of edit mode.
// ---------------------------------------------------------------------------

function ContactCard({
  node, person, editMode, busy, onOpenProfile, onRemove,
}: {
  node: ContactNode;
  // Required, and separate from `node`, so this card cannot be rendered for
  // a contact nobody could name. Both the name and the status below used to
  // fall back to a placeholder when this was missing — the id, and a green
  // "available" dot for someone who was neither available nor visible.
  person: PersonDetail;
  editMode: boolean;
  busy: boolean;
  onOpenProfile: (id: string, name: string) => void;
  onRemove: (linkId: number) => void;
}) {
  const { link } = node;
  const name = person.full_name;
  const status = person.availability_status;
  const isPersonal = link.source === "personal";
  const meta = roleMeta(link);
  const location = locationNote(link);

  return (
    <div
      className="community-canvas-node tree-node"
      style={{ left: node.x, top: node.y }}
      onClick={() => onOpenProfile(link.contact_employee_id, name)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") onOpenProfile(link.contact_employee_id, name);
      }}
    >
      {editMode && isPersonal && (
        <button
          className="community-node-remove"
          aria-label={`Remove connection to ${name}`}
          disabled={busy}
          onClick={(e) => {
            e.stopPropagation();
            onRemove(link.id);
          }}
        >
          <X size={12} />
        </button>
      )}
      <span className="avatar" style={avatarStyle(name)} aria-hidden="true">{initials(name)}</span>
      <p className="tree-node-name">{name}</p>
      <p className="tree-node-role">{person.job_title ?? link.role_label}</p>
      {/* No status line at all when the field isn't on the payload, rather
          than a default. This used to fall back to "available", which
          reported a cheerful green dot for people who were restricted or
          gone — the status was the part of that bug most likely to mislead,
          since a missing name at least looks broken. */}
      {status && (
        <p className="tree-node-status">
          <span className={`status-dot status-${status}`} />
          {STATUS_LABEL[status] ?? status}
        </p>
      )}
      <p className="community-node-what">
        {meta && <span aria-hidden="true">{meta.icon} </span>}
        {roleCaption(link)}
      </p>
      {/* Where this contact sits, and — when the owner's own office had
          nobody for a place-based role — that this is the nearest office
          that did. Silent when there's nothing to say. */}
      {location && (
        <p className={`community-node-where ${link.is_remote_fallback ? "is-fallback" : ""}`}>
          {location}
        </p>
      )}
      {editMode && (
        // Exact, unambiguous text per source -- never a shared badge an
        // employee could mistake for the other kind. Three kinds now, not
        // two: a resolved canonical role is worked out from the directory
        // on every read, so calling it "HR added" would name the wrong
        // author and imply someone could be asked to change it.
        <span className={`pill community-source-pill community-source-${meta ? "role" : link.source}`}>
          {isPersonal
            ? "You added — can edit"
            : meta
              ? "From the directory — can't edit"
              : "HR added — can't edit"}
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add-connection flow: search (the same directory search-as-you-type used
// elsewhere) -> pick a person -> a short reason -> POST. Only ever reachable
// in edit mode, and always submits source=personal, owner=self -- there is
// no field anywhere in this flow that could produce an official link.
// ---------------------------------------------------------------------------

function AddConnectionForm({
  identity, viewMode, onAdded, onCancel,
}: { identity: Identity; viewMode: ViewMode; onAdded: () => void; onCancel: () => void }) {
  const [contact, setContact] = useState<PersonSummary | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (!contact || !reason.trim()) {
      setError("Pick a person and a short reason.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      // The single short text doubles as both fields the backend requires:
      // role_label (what for) and reason (why this person) -- exactly what
      // examples like "helps with CI" already read as.
      await createCommunityLink(identity, {
        contact_employee_id: contact.id, role_label: reason.trim(), reason: reason.trim(),
      });
      onAdded();
    } catch (e) {
      setError(errorMessage(e, "Couldn't add — try again."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card community-add-form">
      <div className="card-head">
        <h3>Add a connection</h3>
        <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
      </div>
      {!contact ? (
        <EmployeeSearchPicker
          identity={identity} viewMode={viewMode} placeholder="Search the directory…"
          onSelect={setContact}
        />
      ) : (
        <div className="bio-edit">
          <label className="edit-field">
            <span className="edit-label">Connecting to</span>
            <span>
              {contact.full_name}{" "}
              <button className="link-btn" onClick={() => setContact(null)} disabled={busy}>change</button>
            </span>
          </label>
          <label className="edit-field">
            <span className="edit-label">Reason / what for</span>
            <input
              className="edit-input" placeholder='e.g. "helps with CI", "design go-to"'
              value={reason} onChange={(e) => setReason(e.target.value)}
              autoFocus
            />
          </label>
          {error && <p className="bio-error">{error}</p>}
          <div className="bio-actions">
            <button className="btn btn-primary" disabled={busy} onClick={submit}>
              {busy ? "Adding…" : "Add connection"}
            </button>
          </div>
        </div>
      )}
      {error && !contact && <p className="bio-error">{error}</p>}
    </div>
  );
}

export function CommunityGraphCanvas({
  identity, viewMode, onOpenProfile,
}: { identity: Identity; viewMode: ViewMode; onOpenProfile: (id: string, name: string) => void }) {
  const [selfPerson, setSelfPerson] = useState<PersonDetail | null | undefined>(undefined);
  const [links, setLinks] = useState<CommunityLinkOut[] | null>(null);
  const [peopleById, setPeopleById] = useState<Map<string, PersonDetail | null>>(new Map());
  const [error, setError] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);

  const [editMode, setEditMode] = useState(false);
  const [adding, setAdding] = useState(false);
  const [removingId, setRemovingId] = useState<number | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);

  const zoomPan = useZoomPan();

  useEffect(() => {
    let cancelled = false;
    getPerson(identity, identity.id, viewMode).then((p) => {
      if (!cancelled) setSelfPerson(p);
    });
    return () => {
      cancelled = true;
    };
  }, [identity, viewMode]);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    listCommunityLinks(identity, viewMode)
      .then((rows) => {
        if (cancelled) return;
        setLinks(rows);
        Promise.all(rows.map((l) => getPerson(identity, l.contact_employee_id, viewMode)))
          .then((people) => {
            if (cancelled) return;
            setPeopleById(new Map(rows.map((l, i) => [l.contact_employee_id, people[i]])));
          });
      })
      .catch((e) => {
        if (!cancelled) setError(errorMessage(e, "Couldn't load your community graph."));
      });
    return () => {
      cancelled = true;
    };
  }, [identity, viewMode, refreshToken]);

  function refresh() {
    setRefreshToken((t) => t + 1);
  }

  async function removeLink(linkId: number) {
    setRemovingId(linkId);
    setRemoveError(null);
    try {
      await deleteCommunityLink(identity, linkId);
      refresh();
    } catch (e) {
      setRemoveError(errorMessage(e, "Couldn't remove — try again."));
    } finally {
      setRemovingId(null);
    }
  }

  // Links we can actually draw. The server already drops contacts you can't
  // see, so this should be every link — it stays as a second line of defence
  // because the alternative failure mode is silent and ugly: a node with a
  // raw id where a name goes. A `null` here means the profile lookup refused
  // someone the link list still returned, which is a real disagreement
  // between two endpoints and not something to paper over on the canvas.
  const visibleLinks = useMemo(() => {
    const drawable = (links ?? []).filter((l) => peopleById.get(l.contact_employee_id) !== null);
    // Canonical roles first, in CANONICAL_ROLES order, then the owner's own
    // additions. Without this the ring's clockwise order is whatever the
    // response happened to list, so the same graph reshuffles as roles
    // resolve differently — and the seven read as an arbitrary pile rather
    // than the fixed set they are.
    const rank = new Map(CANONICAL_ROLE_META.map((r, i) => [r.key, i]));
    return drawable.sort((a, b) => {
      const ra = a.role_key ? rank.get(a.role_key) ?? 99 : 99;
      const rb = b.role_key ? rank.get(b.role_key) ?? 99 : 99;
      return ra - rb || a.id - b.id;
    });
  }, [links, peopleById]);

  const radius = useMemo(() => radiusForCount(visibleLinks.length), [visibleLinks]);
  // The canvas's own coordinate space grows with the radius, so a large
  // ring never clips against a fixed-size drawing surface -- the outer
  // frame (fixed FRAME_HEIGHT, overflow:hidden) is what actually bounds
  // what's visible; pan/zoom explore the rest.
  const size = radius * 2 + 154 + 80;
  const center = { x: size / 2, y: size / 2 };
  // Refit whenever the ring's size changes -- adding or removing a
  // connection changes the radius, and a ring that grew past the frame used
  // to just get its top and bottom cards clipped off.
  useFitOnChange(zoomPan.fit, zoomPan.frameRef, zoomPan.contentRef, `${size}:${visibleLinks.length}`);

  const nodes: ContactNode[] = useMemo(() => {
    return visibleLinks.map((link, i) => {
      const angle = (2 * Math.PI * i) / Math.max(visibleLinks.length, 1) - Math.PI / 2;
      return {
        link,
        x: center.x + radius * Math.cos(angle),
        y: center.y + radius * Math.sin(angle),
        // `?? undefined` collapses "not fetched yet" and "fetched as null"
        // into the loading state — but a null can no longer reach here,
        // since visibleLinks filtered it out above.
        person: peopleById.get(link.contact_employee_id) ?? undefined,
      };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visibleLinks, peopleById, radius]);

  if (error) {
    return (
      <div className="state-block error" style={{ padding: "50px 20px" }}>
        <strong>Couldn't load your community graph</strong>
        <p>{error}</p>
      </div>
    );
  }

  if (links === null || selfPerson === undefined) {
    return <div className="skel skel-card" style={{ height: FRAME_HEIGHT }} />;
  }

  const selfName = selfPerson?.full_name ?? identity.name;
  const selfStatus = selfPerson?.availability_status ?? "available";

  const editToggle = (
    <button
      className={`btn ${editMode ? "btn-primary" : ""}`}
      onClick={() => {
        setEditMode((v) => !v);
        setAdding(false);
      }}
    >
      {editMode ? "Done editing" : "Edit"}
    </button>
  );

  return (
    <div className="community-canvas-wrap">
      {removeError && <p className="bio-error">{removeError}</p>}

      <ZoomPanFrame height="var(--graph-height)" {...zoomPan} extra={editToggle}>
        <div className="community-canvas-ring" style={{ width: size, height: size }}>
          <svg width={size} height={size} className="community-canvas-edges">
            {nodes.map((n) => (
              <line key={n.link.id} x1={center.x} y1={center.y} x2={n.x} y2={n.y} className="tree-edge" />
            ))}
          </svg>

          <div
            className="community-canvas-node tree-node tree-node-focus"
            style={{ left: center.x, top: center.y }}
          >
            <span className="avatar" style={avatarStyle(selfName)} aria-hidden="true">{initials(selfName)}</span>
            <p className="tree-node-name">{selfName}</p>
            <p className="tree-node-role">{selfPerson?.job_title ?? ""}</p>
            <p className="tree-node-status">
              <span className={`status-dot status-${selfStatus}`} />
              {STATUS_LABEL[selfStatus] ?? selfStatus}
            </p>
          </div>

          {nodes.map((n) =>
            n.person === undefined ? (
              <div key={n.link.id} className="community-canvas-node skel skel-card" style={{ left: n.x, top: n.y, width: 154, height: 90 }} />
            ) : (
              <ContactCard
                key={n.link.id} node={n} person={n.person}
                editMode={editMode} busy={removingId === n.link.id}
                onOpenProfile={onOpenProfile} onRemove={removeLink}
              />
            ),
          )}
        </div>
      </ZoomPanFrame>

      {/* The fixed seven, as a key to the ring — and the honest part: a
          role this directory can't answer for you is shown as a gap, not
          left out. */}
      <div className="community-roles-key">
        {CANONICAL_ROLE_META.map((role) => {
          const filled = visibleLinks.find((l) => l.role_key === role.key);
          const person = filled ? peopleById.get(filled.contact_employee_id) : null;
          return (
            <div key={role.key} className={`community-role-row ${filled ? "" : "is-missing"}`}>
              <span className="community-role-text">
                <strong>{role.label}</strong>
                <span className="sub">{role.question}</span>
              </span>
              {filled && person ? (
                <button
                  className="community-role-who"
                  onClick={() => onOpenProfile(person.id, person.full_name)}
                >
                  {person.full_name}
                  {filled.is_remote_fallback && filled.contact_office_city && (
                    <span className="sub">Nearest office: {filled.contact_office_city}</span>
                  )}
                </button>
              ) : (
                <span className="community-role-who is-missing">Nobody assigned</span>
              )}
            </div>
          );
        })}
      </div>

      {editMode && !adding && (
        nodes.length === 0 ? (
          <button className="btn btn-primary community-add-empty" onClick={() => setAdding(true)}>
            + Add your first connection
          </button>
        ) : (
          <button className="btn btn-primary community-add-fab" onClick={() => setAdding(true)}>
            + Add connection
          </button>
        )
      )}

      {editMode && adding && (
        <AddConnectionForm
          identity={identity} viewMode={viewMode}
          onCancel={() => setAdding(false)}
          onAdded={() => {
            setAdding(false);
            refresh();
          }}
        />
      )}
    </div>
  );
}
