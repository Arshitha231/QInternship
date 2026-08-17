import { useCallback, useRef, useState, type ReactNode } from "react";
import { Home, Minus, Plus } from "../icons";

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

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2;
const ZOOM_STEP = 0.2;

export function useZoomPan() {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const dragState = useRef<{ startX: number; startY: number; panX: number; panY: number } | null>(null);

  function clampZoom(z: number): number {
    return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
  }
  function zoomIn() {
    setZoom((z) => clampZoom(z + ZOOM_STEP));
  }
  function zoomOut() {
    setZoom((z) => clampZoom(z - ZOOM_STEP));
  }
  function home() {
    setZoom(1);
    setPan({ x: 0, y: 0 });
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

  return { zoom, pan, zoomIn, zoomOut, home, onZoomDelta, onPointerDown, onPointerMove, onPointerUp };
}

// Props here deliberately match useZoomPan()'s return shape name-for-name
// (zoom, pan, zoomIn, zoomOut, home, onZoomDelta, onPointerDown/Move/Up) so
// every caller can just spread the hook's result straight in --
// `<ZoomPanFrame height={480} {...zoomPan}>`.
export function ZoomPanFrame({
  height, zoom, pan, zoomIn, zoomOut, home, onZoomDelta,
  onPointerDown, onPointerMove, onPointerUp, extra, children,
}: {
  height: number;
  zoom: number;
  pan: { x: number; y: number };
  zoomIn: () => void;
  zoomOut: () => void;
  home: () => void;
  onZoomDelta: (direction: 1 | -1) => void;
  onPointerDown: (e: React.PointerEvent) => void;
  onPointerMove: (e: React.PointerEvent) => void;
  onPointerUp: () => void;
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
        <button className="zoom-pan-btn" onClick={home} aria-label="Re-center the view"><Home size={16} /></button>
        <button className="zoom-pan-btn" onClick={zoomIn} aria-label="Zoom in"><Plus size={16} /></button>
      </div>
      {extra && <div className="zoom-pan-extra">{extra}</div>}
      <div className="zoom-pan-viewport" style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})` }}>
        {children}
      </div>
    </div>
  );
}
