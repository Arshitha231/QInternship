import { useEffect, useRef, useState } from "react";
import { getMyNotifications } from "../api";
import { AlertCircle, Award, Bell, Cake, Check } from "../icons";
import type { Identity, NotificationOut } from "../types";

interface Props {
  identity: Identity;
  onOpenPerson: (id: string, name: string) => void;
}

// Read state is per-identity and lives in the browser: there is no read/unread
// column on the server, and inventing one would mean a write endpoint whose
// only purpose is a badge. Switching identity in the dev picker therefore
// keeps each persona's own count, which is what you want when demoing the
// employee view and the manager view side by side.
const SEEN_KEY = "orghub.notifications.seen";

function loadSeen(identityId: string): number {
  try {
    const all = JSON.parse(window.localStorage.getItem(SEEN_KEY) ?? "{}") as Record<string, number>;
    return all[identityId] ?? 0;
  } catch {
    return 0;
  }
}

function saveSeen(identityId: string, topId: number): void {
  try {
    const all = JSON.parse(window.localStorage.getItem(SEEN_KEY) ?? "{}") as Record<string, number>;
    all[identityId] = topId;
    window.localStorage.setItem(SEEN_KEY, JSON.stringify(all));
  } catch {
    /* storage unavailable (private mode) — the badge just stays lit */
  }
}

function presentation(n: NotificationOut): { tone: string; icon: React.ReactNode } {
  if (n.kind === "birthday_reminder") return { tone: "celebrate", icon: <Cake /> };
  if (n.kind === "work_anniversary_reminder") return { tone: "celebrate", icon: <Award /> };
  if (n.display_status === "completed") return { tone: "ok", icon: <Check /> };
  return { tone: "warn", icon: <AlertCircle /> };
}

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const minutes = Math.round((Date.now() - then) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function NotificationBell({ identity, onOpenPerson }: Props) {
  const [items, setItems] = useState<NotificationOut[]>([]);
  const [open, setOpen] = useState(false);
  const [seenId, setSeenId] = useState(() => loadSeen(identity.id));
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    setOpen(false);
    setSeenId(loadSeen(identity.id));
    getMyNotifications(identity, controller.signal)
      .then(setItems)
      .catch(() => setItems([]));  // a bell that can't load is a quiet bell, not an error banner
    return () => controller.abort();
  }, [identity]);

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) setOpen(false);
    }
    function onEsc(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("click", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("click", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, []);

  const unread = items.filter((n) => n.id > seenId).length;

  function toggle(e: React.MouseEvent) {
    e.stopPropagation();
    const next = !open;
    setOpen(next);
    if (next && items.length > 0) {
      const topId = Math.max(...items.map((n) => n.id));
      setSeenId(topId);
      saveSeen(identity.id, topId);
    }
  }

  return (
    <div className="notif" ref={panelRef}>
      <button
        className="notif-btn"
        aria-expanded={open}
        aria-controls="notif-panel"
        aria-label={unread > 0 ? `Notifications, ${unread} unread` : "Notifications"}
        onClick={toggle}
      >
        <Bell />
        {unread > 0 && <span className="notif-badge" aria-hidden="true">{unread > 9 ? "9+" : unread}</span>}
      </button>

      {open && (
        <div className="menu notif-panel" id="notif-panel">
          <div className="menu-label">Notifications</div>
          {items.length === 0 ? (
            <p className="notif-empty">Nothing yet. Course reminders and completion reports land here.</p>
          ) : (
            <ul className="notif-list">
              {items.map((n) => {
                const aboutSomeoneElse = n.subject_person.id !== identity.id;
                // Tone comes from the kind, not just display_status — the
                // date-driven kinds carry no status at all, and a birthday
                // rendered with the amber "something needs attention" icon
                // would read as a problem.
                const { tone, icon } = presentation(n);
                return (
                  <li key={n.id} className={n.id > seenId ? "unseen" : ""}>
                    <span className={`notif-dot ${tone}`} aria-hidden="true">{icon}</span>
                    <div className="notif-text">
                      <p className="notif-body">{n.body}</p>
                      <p className="notif-meta">
                        {/* course_name is empty for the date-driven kinds,
                            which aren't about a course — don't render a
                            dangling separator in front of the timestamp. */}
                        {n.course_name && <>{n.course_name} &middot; </>}
                        {relativeTime(n.created_at)}
                        {aboutSomeoneElse && (
                          <>
                            {" "}&middot;{" "}
                            <button
                              className="link-btn"
                              onClick={() => {
                                setOpen(false);
                                onOpenPerson(n.subject_person.id, n.subject_person.full_name);
                              }}
                            >
                              View profile
                            </button>
                          </>
                        )}
                      </p>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
