import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { HELP_TOPICS, TOUR_STEPS, isTargetPresent, topicForElement } from "../help/content";
import type { HelpMode, HelpTopic } from "../help/content";
import { X } from "../icons";

export type HelpState = "off" | "tour" | "inspect";

interface Props {
  state: HelpState;
  onExit: () => void;
  /** Tour steps can live on another tab; the host switches before showing. */
  onRequestMode: (mode: HelpMode) => void;
  /** Capabilities whose targets are conditionally rendered. A topic
   *  declaring one is skipped when it is false — see HelpTopic.capability. */
  capabilities: { build_team: boolean };
  /**
   * Which tabs actually exist for the current identity. Supplied by the
   * host rather than derived here, so the role rules live in exactly one
   * place (App's tab bar) instead of being restated by the help system and
   * drifting from it.
   */
  availableModes: HelpMode[];
}

interface Box { top: number; left: number; width: number; height: number; }

const MARGIN = 10;      // gap between the highlight and the tooltip
const CARD_WIDTH = 340;

function boxOf(el: Element): Box {
  const r = el.getBoundingClientRect();
  return { top: r.top, left: r.left, width: r.width, height: r.height };
}

/**
 * Place the card next to the highlight without letting it leave the
 * viewport. Below the target by default, above when there isn't room —
 * the common case for anything near the bottom of a long results list.
 */
function placeCard(box: Box | null, cardHeight: number): { top: number; left: number } {
  if (!box) {
    return { top: window.innerHeight / 2 - cardHeight / 2, left: window.innerWidth / 2 - CARD_WIDTH / 2 };
  }
  const below = box.top + box.height + MARGIN;
  const fitsBelow = below + cardHeight <= window.innerHeight - MARGIN;
  const top = fitsBelow ? below : Math.max(MARGIN, box.top - cardHeight - MARGIN);
  const left = Math.min(
    Math.max(MARGIN, box.left),
    Math.max(MARGIN, window.innerWidth - CARD_WIDTH - MARGIN),
  );
  return { top, left };
}

export function HelpOverlay({ state, onExit, onRequestMode, availableModes, capabilities }: Props) {
  const [stepIndex, setStepIndex] = useState(0);
  const [inspected, setInspected] = useState<HelpTopic | null>(null);
  const [box, setBox] = useState<Box | null>(null);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const [cardHeight, setCardHeight] = useState(180);

  // Three skip rules, and all of them are load-bearing:
  //
  //   no mode   the target must be on screen NOW (filters and results only
  //             exist after a search).
  //   a mode    the tab must exist for this identity. Keeping these
  //             unconditionally -- on the theory that the target appears
  //             once we switch -- is wrong for a role-gated tab, where it
  //             never appears: an HR user got a "Review (IT)" step, and
  //             requesting that tab blanked the page, because App renders
  //             nothing for a mode the role can't open.
  //   capability the tab exists but the CONTROL inside it does not. Build
  //             Team sits on the Graphs tab, which everyone has, behind a
  //             server-resolved gate -- so the mode rule passes and the
  //             target is still absent. Same failure as the row above,
  //             one level further in, and found the same way: walking the
  //             tour as an ordinary employee and watching a step land on
  //             nothing.
  const visibleSteps = TOUR_STEPS.filter((t) => {
    if (t.capability && !capabilities[t.capability]) return false;
    return t.mode === undefined ? isTargetPresent(t) : availableModes.includes(t.mode);
  });
  const step: HelpTopic | undefined = state === "tour" ? visibleSteps[stepIndex] : undefined;
  const active: HelpTopic | null = state === "tour" ? step ?? null : inspected;

  // Switch tabs before measuring, so the target exists when we look for it.
  useEffect(() => {
    if (state === "tour" && step?.mode) onRequestMode(step.mode);
  }, [state, step, onRequestMode]);

  // The ladder's timers must see the step that is current when they FIRE,
  // not the one captured when they were scheduled -- otherwise clicking
  // Next mid-ladder measures the previous step's target.
  const activeRef = useRef<HelpTopic | null>(null);
  activeRef.current = active;

  const measure = useCallback(() => {
    if (!active) { setBox(null); return; }
    const el = document.querySelector(`[data-help="${active.key}"]`);
    setBox(el ? boxOf(el) : null);
  }, [active]);

  // Layout effect, not effect: measuring after paint makes the spotlight
  // visibly jump on the first frame of every step.
  useLayoutEffect(() => { measure(); }, [measure]);

  useEffect(() => {
    // A tab switch re-renders asynchronously AND THEN FETCHES, so a single
    // deferred measure isn't enough. One 60ms retry was what this had, and
    // it is enough for a tab that renders synchronously and not for one
    // that waits on the network -- Dashboard, Continuity, Review and Admin
    // all landed with no spotlight, the card floating over an unlit page.
    // Found by walking the whole tour as HR and recording which steps drew
    // a ring; the four that missed were exactly the four that fetch.
    //
    // A ladder rather than one longer timeout: a synchronous tab still
    // lights up on the first rung, so nothing gets slower to make the slow
    // case work. Each rung only ever UPGRADES a miss into a hit -- it never
    // clears a box it already found, so a target that legitimately isn't
    // there still ends on "Nothing to show here".
    const rungs = [60, 160, 320, 640, 1100];
    const timers = rungs.map((delay) =>
      window.setTimeout(() => {
        if (!activeRef.current) return;
        const el = document.querySelector(`[data-help="${activeRef.current.key}"]`);
        if (el) setBox(boxOf(el));
      }, delay),
    );
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      timers.forEach(window.clearTimeout);
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [measure]);

  useLayoutEffect(() => {
    if (cardRef.current) setCardHeight(cardRef.current.offsetHeight);
  }, [active]);

  useEffect(() => {
    if (state === "off") { setStepIndex(0); setInspected(null); }
  }, [state]);

  // The `help` cursor is the only affordance telling someone their next
  // click will explain rather than activate. Toggled on <body> because it
  // has to win over every element's own cursor rule.
  useEffect(() => {
    document.body.classList.toggle("help-inspecting", state === "inspect");
    return () => document.body.classList.remove("help-inspecting");
  }, [state]);

  // Click-to-learn: capture phase, so a click lands on help instead of
  // activating the control underneath. Without capture, clicking "Graphs"
  // to ask what it is would also switch tabs.
  useEffect(() => {
    if (state !== "inspect") return;
    function onClick(e: MouseEvent) {
      const target = e.target as Element | null;
      if (target?.closest("[data-help-chrome]")) return; // the help UI itself
      e.preventDefault();
      e.stopPropagation();
      const topic = topicForElement(target);
      setInspected(topic ?? null);
      if (!topic) setBox(null);
    }
    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, [state]);

  useEffect(() => {
    if (state === "off") return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") { e.preventDefault(); onExit(); return; }
      if (state !== "tour") return;
      if (e.key === "ArrowRight") setStepIndex((i) => Math.min(i + 1, visibleSteps.length - 1));
      if (e.key === "ArrowLeft") setStepIndex((i) => Math.max(i - 1, 0));
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [state, onExit, visibleSteps.length]);

  if (state === "off") return null;

  const isLast = stepIndex >= visibleSteps.length - 1;
  const pos = placeCard(box, cardHeight);

  // The shade blocks interaction during the TOUR (a guided walkthrough
  // shouldn't let you click into the app mid-step) but must not block it
  // in INSPECT mode, where clicking the page underneath is the entire
  // interaction. Without this, the full-screen shade swallowed the first
  // click and nothing could ever be selected.
  const shadeEvents: "auto" | "none" = state === "inspect" ? "none" : "auto";

  return (
    <div className="help-layer" data-help-chrome="">
      {/* Four panels around the target rather than one box-shadow ring: the
          hole must not swallow clicks in inspect mode, and a shadow-based
          spotlight is still one element covering the whole viewport. */}
      {box ? (
        <>
          <div className="help-shade" style={{ top: 0, left: 0, width: "100%", height: Math.max(0, box.top), pointerEvents: shadeEvents }} />
          <div className="help-shade" style={{ top: box.top + box.height, left: 0, width: "100%", bottom: 0, pointerEvents: shadeEvents }} />
          <div className="help-shade" style={{ top: box.top, left: 0, width: Math.max(0, box.left), height: box.height, pointerEvents: shadeEvents }} />
          <div className="help-shade" style={{ top: box.top, left: box.left + box.width, right: 0, height: box.height, pointerEvents: shadeEvents }} />
          <div
            className="help-ring"
            style={{ top: box.top - 4, left: box.left - 4, width: box.width + 8, height: box.height + 8 }}
          />
        </>
      ) : (
        <div className="help-shade help-shade-full" style={{ pointerEvents: shadeEvents }} />
      )}

      <div
        ref={cardRef}
        className="help-card"
        style={{ top: pos.top, left: pos.left, width: CARD_WIDTH }}
        role="dialog"
        aria-live="polite"
        aria-label={active ? `Help: ${active.title}` : "Help"}
      >
        <div className="help-card-head">
          <span className="help-kicker">
            {state === "tour" ? `Tour · ${stepIndex + 1} of ${visibleSteps.length}` : "Click to learn"}
          </span>
          <button className="help-close" onClick={onExit} aria-label="Close help">
            <X size={14} />
          </button>
        </div>

        {active ? (
          <>
            <h3 className="help-title">{active.title}</h3>
            <p className="help-body">{active.body}</p>
          </>
        ) : state === "inspect" ? (
          <>
            <h3 className="help-title">Pick anything on the page</h3>
            <p className="help-body">
              Click a part of the app and it explains just that part. Click the <strong>?</strong> button
              again, or press Esc, to leave help.
            </p>
          </>
        ) : (
          <>
            <h3 className="help-title">Nothing to show here</h3>
            <p className="help-body">This step's section isn't available for your current role.</p>
          </>
        )}

        {state === "tour" && (
          <div className="help-actions">
            <button className="help-btn ghost" onClick={() => setStepIndex((i) => Math.max(0, i - 1))} disabled={stepIndex === 0}>
              Back
            </button>
            {isLast ? (
              <button className="help-btn primary" onClick={onExit}>Done</button>
            ) : (
              <button className="help-btn primary" onClick={() => setStepIndex((i) => i + 1)}>Next</button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** Dev-time guard: every topic key should exist in the DOM on some tab. */
export function unmatchedHelpKeys(): string[] {
  return HELP_TOPICS.filter((t) => t.mode === undefined && !isTargetPresent(t)).map((t) => t.key);
}
