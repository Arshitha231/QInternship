import { useEffect, useRef, useState } from "react";
import { HelpCircle } from "../icons";
import type { HelpState } from "./HelpOverlay";

interface Props {
  state: HelpState;
  onStart: (state: Exclude<HelpState, "off">) => void;
  onExit: () => void;
}

/**
 * The "?" control. Closed, it opens a two-option menu; while help is
 * running it becomes the way out, which is the behaviour asked for —
 * "click back on the help button to leave the help view" — and also what
 * anyone would try first.
 */
export function HelpMenu({ state, onStart, onExit }: Props) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
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
  }, [open]);

  const running = state !== "off";

  return (
    // data-help-chrome marks this as help UI, so inspect mode's capturing
    // click handler lets it through instead of describing it.
    <div className="help-menu" ref={rootRef} data-help-chrome="">
      <button
        className={`help-trigger ${running ? "active" : ""}`}
        aria-label={running ? "Leave help" : "Help"}
        aria-expanded={open}
        title={running ? "Leave help" : "Help"}
        onClick={() => {
          if (running) { onExit(); setOpen(false); return; }
          setOpen((v) => !v);
        }}
      >
        <HelpCircle size={17} />
      </button>

      {open && !running && (
        <div className="help-pop" role="menu">
          <button
            role="menuitem"
            className="help-pop-item"
            onClick={() => { setOpen(false); onStart("tour"); }}
          >
            <span className="help-pop-title">Take the tour</span>
            <span className="help-pop-sub">A guided walkthrough of every part of the app.</span>
          </button>
          <button
            role="menuitem"
            className="help-pop-item"
            onClick={() => { setOpen(false); onStart("inspect"); }}
          >
            <span className="help-pop-title">Explain one thing</span>
            <span className="help-pop-sub">Click any part of the page to learn just about that.</span>
          </button>
        </div>
      )}
    </div>
  );
}
