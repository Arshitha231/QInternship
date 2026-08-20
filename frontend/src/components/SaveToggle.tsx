import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Loader } from "../icons";

export type SaveStatus = "idle" | "saving" | "saved" | "error";

interface Props {
  /** The real write. Must REJECT on failure — see the note below. */
  onSave: () => Promise<void>;
  idleText?: string;
  savingText?: string;
  savedText?: string;
  /** How long the confirmation holds before returning to idle. */
  successDuration?: number;
  size?: "sm" | "md";
  disabled?: boolean;
  onStatusChange?: (status: SaveStatus) => void;
  /** Fires once the confirmation has been SHOWN and the button has settled
      back to idle. This is where a form closes itself: closing inside
      onSave unmounts the button mid-animation, so the user does the work,
      the panel vanishes, and nothing ever confirms it landed. */
  onConfirmed?: () => void;
}

// ---------------------------------------------------------------------------
// Save button that confirms itself: idle -> saving -> saved -> idle.
//
// Adapted from the SaveToggle demo, with one deliberate change. The demo
// takes a `loadingDuration` and shows a spinner for exactly that long — fine
// for a component gallery, wrong here: it would show "Saved" after a fixed
// 1200ms whether or not the PATCH had landed, and would say it even when the
// request had failed. This version is driven by the promise instead, so the
// confirmation means the write actually happened. `successDuration` is kept,
// because how long a confirmation LINGERS is a presentational choice.
//
// A rejected promise returns the button to idle without ever showing
// "Saved", and reports "error" through onStatusChange. Rendering the message
// stays with the caller — every form here already has its own error line,
// and two competing error displays is worse than one.
//
// Call sites therefore have to let failures propagate. Handlers that catch
// internally must re-throw after recording their own error state, or this
// button will report success for a write that never happened.
// ---------------------------------------------------------------------------

export function SaveToggle({
  onSave, idleText = "Save", savingText, savedText = "Saved",
  successDuration = 1400, size = "md", disabled = false, onStatusChange, onConfirmed,
}: Props) {
  const [status, setStatus] = useState<SaveStatus>("idle");
  // Timers and post-unmount writes: the confirmation outlives the request, so
  // a form that closes itself on success (several here do) would otherwise
  // set state on an unmounted component.
  const timer = useRef<number | undefined>(undefined);
  const alive = useRef(true);

  // Re-armed on mount, not just cleared on unmount. StrictMode runs effects
  // mount -> cleanup -> mount in development, so a guard that is only ever
  // set false in the cleanup is already false by the time the first save
  // resolves: the write lands, the promise resolves, and the button silently
  // returns early and sits on "Saving…" forever. Setting it true in the body
  // makes the flag mean "currently mounted" rather than "never unmounted".
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      window.clearTimeout(timer.current);
    };
  }, []);

  const report = useCallback((next: SaveStatus) => {
    setStatus(next);
    onStatusChange?.(next);
  }, [onStatusChange]);

  async function run() {
    if (status === "saving" || disabled) return;
    window.clearTimeout(timer.current);
    report("saving");
    try {
      await onSave();
      if (!alive.current) return;
      report("saved");
      timer.current = window.setTimeout(() => {
        if (!alive.current) return;
        report("idle");
        onConfirmed?.();
      }, successDuration);
    } catch {
      if (!alive.current) return;
      // Straight back to idle: the caller is showing why, and a button stuck
      // in a red state on top of that message is noise.
      report("error");
      report("idle");
    }
  }

  const busy = status === "saving";
  const done = status === "saved";

  return (
    <button
      type="button"
      className={`btn btn-primary save-toggle save-toggle-${size}${done ? " is-saved" : ""}`}
      onClick={run}
      disabled={disabled || busy}
      // Announced rather than just animated — the icon swap is the only
      // signal a sighted user gets, and it needs a text equivalent.
      aria-live="polite"
    >
      <span className="save-toggle-icon" aria-hidden="true">
        {busy ? <Loader size={14} className="spin" /> : done ? <Check size={14} /> : null}
      </span>
      <span>{busy ? (savingText ?? `${idleText}…`) : done ? savedText : idleText}</span>
    </button>
  );
}
