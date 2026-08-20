import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Maximize, Minus, Plus } from "../icons";

// Shared zoom/pan/home chrome for every graph view (Department, Team,
// Skills, Community) -- one implementation so the four don't drift into
// four slightly different pan/zoom behaviors.
//
// Split into a hook (useZoomPan, owning zoom/pan state) and a presentational
// wrapper (ZoomPanFrame, owning the toolbar/frame/transform markup) rather
// than one component that both holds the state AND renders children via a
// render-prop: DepartmentGraph and TeamGraph need the current `zoom` value
// themselves, to pass into useTreeConnectors so its DOM measurements stay
// correct at any scale (a CSS transform: scale() on an ancestor makes
// getBoundingClientRect() return post-transform screen pixels, which
// disagree with an untransformed measurement like scrollWidth unless
// divided back down by the same factor -- see treeShared.tsx). That means
// `zoom` has to come from a hook call in THEIR OWN component body, not from
// a value handed back through a child callback -- calling useTreeConnectors
// inside a render-prop closure would attach its hook state to this
// component's fiber instead of theirs, which is exactly the render-prop-hook
// anti-pattern the Rules of Hooks warn about.
//
// FIT-TO-VIEW. Every one of these graphs draws content whose size is decided
// by the data, not by the frame: a 2-report tree is ~620px tall, a 15-person
// team row is wider than it is tall, and a skills graph can be several
// thousand pixels across. The frame is one fixed height. Before fit existed
// the two simply disagreed and the frame won -- `overflow: hidden` quietly
// ate whatever didn't fit, which in practice meant the team hub node and the
// top of the manager card were clipped away and the graph looked like it had
// been designed to sit behind its own box. So the frame now measures its
// content (offsetWidth/offsetHeight -- LAYOUT properties, unaffected by the
// paint-only transform this same component applies, so measuring while
// scaled is safe) and picks the scale that makes the whole thing fit, once
// on mount and again whenever `fitKey` changes. Nothing is ever clipped on
// arrival; panning and zooming are for choice, not for rescue.

const MAX_ZOOM = 2.5;
const ZOOM_STEP = 0.2;
// The floor for the +/- buttons only. Fit is allowed below this (a big
// skills graph legitimately needs ~0.3 to fit at all), and when it lands
// there it becomes the new floor -- see effectiveMin. Zooming out past the
// point where the whole graph is already visible only shrinks it for no
// gain, so the fit scale is the natural stopping point.
const MIN_ZOOM = 0.4;
// Breathing room between the content's bounding box and the frame's edge,
// so a fitted graph doesn't sit flush against the border.
const FIT_PADDING = 28;

export function useZoomPan() {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  // The scale at which the content exactly fits. Doubles as the zoom-out
  // floor once it drops below MIN_ZOOM.
  const [fitScale, setFitScale] = useState(1);
  const frameRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const dragState = useRef<{ startX: number; startY: number; panX: number; panY: number } | null>(null);

  const effectiveMin = Math.min(MIN_ZOOM, fitScale);

  const clampZoom = useCallback(
    (z: number): number => Math.min(MAX_ZOOM, Math.max(effectiveMin, z)),
    [effectiveMin],
  );

  // Measures content against frame and returns the scale that fits both
  // axes, capped at 1 so a small graph (a two-person team) is shown at its
  // designed size rather than blown up to fill the frame -- upscaling past
  // 1:1 makes a sparse graph look like a zoom-in artifact, and the + button
  // is right there for anyone who wants that.
  const measureFit = useCallback((): number | null => {
    const frame = frameRef.current;
    const content = contentRef.current;
    if (!frame || !content) return null;
    const cw = content.offsetWidth;
    const ch = content.offsetHeight;
    if (cw === 0 || ch === 0) return null;
    const availW = frame.clientWidth - FIT_PADDING * 2;
    const availH = frame.clientHeight - FIT_PADDING * 2;
    if (availW <= 0 || availH <= 0) return null;
    return Math.min(1, availW / cw, availH / ch);
  }, []);

  // Reset to "whole graph visible, centered". Pan goes to zero rather than
  // to a computed offset because the viewport is already flex-centered in
  // the frame and scales about its own center (see .zoom-pan-frame /
  // .zoom-pan-viewport in index.css) -- so at the fit scale, zero pan IS
  // centered, on both axes, whether the content overflows or underfills.
  const fit = useCallback(() => {
    const next = measureFit();
    if (next === null) return;
    setFitScale(next);
    setZoom(next);
    setPan({ x: 0, y: 0 });
  }, [measureFit]);

  function zoomIn() {
    setZoom((z) => clampZoom(z + ZOOM_STEP));
  }
  function zoomOut() {
    setZoom((z) => clampZoom(z - ZOOM_STEP));
  }
  // Trackpad pinch arrives as a wheel event with a smaller, continuous
  // delta rather than a button click's fixed step -- ZOOM_STEP/2 per notch
  // keeps it from feeling twitchy relative to +/- .
  function onZoomDelta(direction: 1 | -1) {
    setZoom((z) => clampZoom(z + direction * (ZOOM_STEP / 2)));
  }

  function onPointerDown(e: React.PointerEvent) {
    // Ignore drags that start on a node or a control inside it -- those
    // have their own click handlers (open profile, expand, remove), and
    // starting a pan underneath them would turn a click into an accidental
    // drag. Checked generically (role="button", <button>, <a>) rather than
    // by a graph-specific card class, since this frame wraps four
    // different node shapes (SVG circles, tree cards, ring cards).
    if ((e.target as HTMLElement).closest('[role="button"], button, a')) return;
    dragState.current = { startX: e.clientX, startY: e.clientY, panX: pan.x, panY: pan.y };
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  }
  function onPointerMove(e: React.PointerEvent) {
    if (!dragState.current) return;
    const { startX, startY, panX, panY } = dragState.current;
    setPan({ x: panX + (e.clientX - startX), y: panY + (e.clientY - startY) });
  }
  function onPointerUp() {
    dragState.current = null;
  }

  return {
    zoom, pan, fit, zoomIn, zoomOut, onZoomDelta,
    onPointerDown, onPointerMove, onPointerUp,
    frameRef, contentRef,
  };
}

// Refits when the drawn content changes size, when the frame changes size,
// and when `key` says this is different content now (a new focus person --
// which should reset a pan even if the new tree happens to be the same
// size as the old one).
//
// A ResizeObserver on the CONTENT is the primary trigger, not a timer or an
// animation frame. Two reasons. It is the direct signal -- the thing that
// actually invalidates a fit is the content's measured size, so observing
// that is neither early nor late, and it covers every cause at once (data
// arriving, a branch expanding, a font finishing loading). And it keeps
// working in a background tab: requestAnimationFrame does not fire in one,
// so an rAF-scheduled fit silently never runs and the graph is left at the
// wrong scale until something else nudges it.
//
// No feedback loop, despite this observer's own callback setting the zoom
// that scales the observed element: offsetWidth/offsetHeight and
// ResizeObserver's box sizes are LAYOUT measurements, and `scale()` is a
// paint-only transform. Scaling the content does not change what either one
// reports, so fit -> resize -> fit cannot cycle.
export function useFitOnChange(
  fit: () => void,
  frameRef: React.RefObject<HTMLDivElement | null>,
  contentRef: React.RefObject<HTMLDivElement | null>,
  key: unknown,
) {
  // Synchronous, in a layout effect, so the first paint after new data is
  // already at the right scale rather than showing one clipped frame first.
  // Safe to measure here: the tree graphs' connector <svg> is absolutely
  // positioned and so contributes nothing to the content's layout size,
  // which means their own layout effect can't change what this one measures.
  useLayoutEffect(() => {
    fit();
  }, [fit, key]);

  useEffect(() => {
    const frame = frameRef.current;
    const content = contentRef.current;
    if (!frame || !content) return;
    const ro = new ResizeObserver(() => fit());
    ro.observe(frame);
    ro.observe(content);
    return () => ro.disconnect();
  }, [fit, frameRef, contentRef]);
}

// Props here deliberately match useZoomPan()'s return shape name-for-name
// so every caller can just spread the hook's result straight in --
// `<ZoomPanFrame height={480} {...zoomPan}>`.
export function ZoomPanFrame({
  height, zoom, pan, fit, zoomIn, zoomOut, onZoomDelta,
  onPointerDown, onPointerMove, onPointerUp, frameRef, contentRef, extra, children,
}: {
  height: number | string;
  zoom: number;
  pan: { x: number; y: number };
  fit: () => void;
  zoomIn: () => void;
  zoomOut: () => void;
  onZoomDelta: (direction: 1 | -1) => void;
  onPointerDown: (e: React.PointerEvent) => void;
  onPointerMove: (e: React.PointerEvent) => void;
  onPointerUp: () => void;
  frameRef: React.RefObject<HTMLDivElement | null>;
  contentRef: React.RefObject<HTMLDivElement | null>;
  // An additional control overlaid in the frame's top-right corner
  // (Community's Edit toggle; absent for the other three graphs).
  extra?: ReactNode;
  children: ReactNode;
}) {
  const wheelCleanup = useRef<(() => void) | null>(null);

  // A callback ref, not useEffect(..., []): several of this frame's callers
  // render a loading skeleton (no frame element at all) before their data
  // resolves, so a mount-time effect with an empty dependency array would
  // run while the ref is still null and never re-attach once the real frame
  // appears. A callback ref fires exactly when the node itself mounts or
  // unmounts, regardless of which render that happens on.
  //
  // Also why this is a native listener rather than React's onWheel: React
  // attaches wheel/touch listeners at the root as passive by default (since
  // v17), and calling preventDefault() inside a passive listener is a
  // silent no-op in every browser (Chrome/Firefox/Safari all log "Unable to
  // preventDefault inside passive event listener invocation" and still let
  // the page's own pinch-zoom fire alongside this frame's). { passive:
  // false } here is what actually lets a pinch stop the browser from also
  // zooming the whole page.
  const setFrameRef = useCallback((el: HTMLDivElement | null) => {
    wheelCleanup.current?.();
    wheelCleanup.current = null;
    frameRef.current = el;
    if (!el) return;
    function onWheel(e: WheelEvent) {
      // Trackpad pinch is reported to the browser as a wheel event with
      // ctrlKey set (every browser does this, on every platform, since
      // there is no separate DOM "pinch" event) -- an ordinary two-finger
      // scroll has no ctrlKey. Checking it is what lets the frame zoom on a
      // pinch while a plain scroll does nothing (no accidental
      // zoom-while-scrolling-the-page).
      if (!e.ctrlKey) return;
      e.preventDefault();
      onZoomDelta(e.deltaY < 0 ? 1 : -1);
    }
    el.addEventListener("wheel", onWheel, { passive: false });
    wheelCleanup.current = () => el.removeEventListener("wheel", onWheel);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      ref={setFrameRef}
      className="graph-canvas zoom-pan-frame"
      style={{ height }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={onPointerUp}
    >
      {/* Overlaid on the canvas itself, not a separate row above it -- the
          frame's own height is exactly the graph's visible area, the same
          size it was before pan/zoom existed at all. */}
      <div className="zoom-pan-controls">
        <button className="zoom-pan-btn" onClick={zoomOut} aria-label="Zoom out"><Minus size={16} /></button>
        <button className="zoom-pan-btn" onClick={fit} aria-label="Fit the whole graph in view"><Maximize size={16} /></button>
        <button className="zoom-pan-btn" onClick={zoomIn} aria-label="Zoom in"><Plus size={16} /></button>
      </div>
      <span className="zoom-pan-level" aria-hidden="true">{Math.round(zoom * 100)}%</span>
      {extra && <div className="zoom-pan-extra">{extra}</div>}
      <div className="zoom-pan-viewport" style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})` }}>
        <div ref={contentRef} className="zoom-pan-content">{children}</div>
      </div>
    </div>
  );
}
