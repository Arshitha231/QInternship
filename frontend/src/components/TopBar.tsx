import { useEffect, useRef, useState } from "react";
import { ChevronDown, Moon, SearchIcon, Sun, X } from "../icons";
import type { ReactNode } from "react";
import type { Identity, ViewMode } from "../types";
import { WORK_MODE_ROLES } from "../types";
import { NotificationBell } from "./NotificationBell";
import { useTheme } from "../hooks";

interface Props {
  query: string;
  onQueryChange: (q: string) => void;
  identity: Identity;
  onSignOut: () => void;
  onOpenPerson: (id: string, name: string) => void;
  viewMode: ViewMode;
  onViewModeChange: (mode: ViewMode) => void;
  /** The "?" control, injected so TopBar stays unaware of help state. */
  helpSlot?: ReactNode;
}

export function TopBar({
  query, onQueryChange, identity, onSignOut, onOpenPerson, viewMode, onViewModeChange, helpSlot,
}: Props) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const [theme, toggleTheme] = useTheme();

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    }
    function onEsc(e: KeyboardEvent) {
      if (e.key === "Escape") setMenuOpen(false);
    }
    document.addEventListener("click", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("click", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, []);

  const initials = identity.name.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();

  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="3" /><path d="M12 2v4M12 18v4M2 12h4M18 12h4" />
          </svg>
        </span>
        <span className="brand-name">OrgHub</span>
      </div>

      <div className="search" data-help="search">
        <SearchIcon className="search-icon" />
        <label className="sr-only" htmlFor="q" style={{ position: "absolute", left: -9999 }}>Search the directory</label>
        <input
          id="q"
          type="search"
          placeholder="Search a name or skill, or ask a question — e.g. &quot;who could mentor me in Terraform?&quot;"
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
        />
        {query && (
          <button className="clear-btn" aria-label="Clear search" onClick={() => onQueryChange("")}>
            <X size={15} />
          </button>
        )}
      </div>

      {/* Only hr and it can act on this, so only they are shown it. That is
          presentation, not enforcement -- an employee who sends
          view_mode=work by hand still gets employee mode back, because the
          server pins it. See resolve_view_mode in app/permissions.py. */}
      {WORK_MODE_ROLES.includes(identity.role) && (
        <div
          data-help="viewmode"
          className="viewmode"
          role="group"
          aria-label={`View mode (available to ${WORK_MODE_ROLES.join(" and ")})`}
        >
          {(["work", "employee"] as ViewMode[]).map((m) => (
            <button
              key={m}
              type="button"
              className={`viewmode-btn ${viewMode === m ? "active" : ""}`}
              aria-pressed={viewMode === m}
              title={m === "work"
                ? "Your full access for this role"
                : "What an ordinary colleague sees — identical for every role"}
              onClick={() => onViewModeChange(m)}
            >
              {m === "work" ? "Work" : "Employee"}
            </button>
          ))}
        </div>
      )}

      {helpSlot}

      <button
        type="button"
        className="theme-toggle"
        aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
        title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
        onClick={toggleTheme}
      >
        {theme === "dark" ? <Sun /> : <Moon />}
      </button>

      <span data-help="notifications" style={{ display: "flex" }}>
        <NotificationBell identity={identity} onOpenPerson={onOpenPerson} />
      </span>

      <div className="account" ref={menuRef} data-help="identity">
        <button
          className="account-btn"
          aria-expanded={menuOpen}
          aria-controls="acct-menu"
          onClick={(e) => {
            e.stopPropagation();
            setMenuOpen((v) => !v);
          }}
        >
          <span className="avatar-sm" aria-hidden="true">{initials}</span>
          <span>
            {identity.name}
            <span className="role-tag" style={{ display: "block", lineHeight: 1 }}>{identity.role}</span>
          </span>
          <ChevronDown />
        </button>
        {menuOpen && (
          <div className="menu" id="acct-menu">
            {/* The role is the access claim the API is being called with,
                not this person's job title -- worth saying out loud here,
                since the two disagree for several of the demo accounts. */}
            <div className="menu-label">
              Signed in as {identity.name} · role: {identity.role}
            </div>
            <button
              className="menu-item"
              onClick={() => {
                setMenuOpen(false);
                onSignOut();
              }}
            >
              <span>Sign out</span>
            </button>
          </div>
        )}
      </div>
    </header>
  );
}
