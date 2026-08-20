import { useCallback, useRef, useState } from "react";

// Curved "groove" lines for the lower sphere, generated rather than
// hand-written -- 16 near-vertical strokes fanning out from the sphere's
// centre, clipped to its circle in the SVG below.
const GROOVES = Array.from({ length: 16 }, (_, i) => {
  const t = i / 15;
  const x = 56 + t * 208;
  const bow = (t - 0.5) * 70;
  return `M${x.toFixed(1)} 118 Q${(x - bow).toFixed(1)} 230 ${x.toFixed(1)} 342`;
});

/**
 * Decorative hero graphic for the login page. Drawn as layered SVG groups
 * rather than a flat image: each layer sits at a different notional depth
 * and gets its own translate multiplier off the pointer position (set as
 * --px/--py below), so the graphic visibly separates into moving parts
 * instead of tilting as one flat card. See the "Login hero" block in
 * index.css for the per-layer transforms.
 */
export function LoginHeart() {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [pointer, setPointer] = useState({ x: 0, y: 0 });

  const handleMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect) return;
    setPointer({
      x: Math.max(-1, Math.min(1, ((e.clientX - rect.left) / rect.width) * 2 - 1)),
      y: Math.max(-1, Math.min(1, ((e.clientY - rect.top) / rect.height) * 2 - 1)),
    });
  }, []);

  const handleLeave = useCallback(() => setPointer({ x: 0, y: 0 }), []);

  const style = {
    "--px": pointer.x.toFixed(3),
    "--py": pointer.y.toFixed(3),
  } as React.CSSProperties;

  return (
    <div
      ref={wrapRef}
      className="login-heart"
      style={style}
      onMouseMove={handleMove}
      onMouseLeave={handleLeave}
      aria-hidden="true"
    >
      <svg className="login-heart-svg" viewBox="0 0 320 340">
        <defs>
          <radialGradient id="heart-glow" cx="50%" cy="40%" r="65%">
            <stop offset="0%" stopColor="#c86bf2" stopOpacity=".55" />
            <stop offset="100%" stopColor="#c86bf2" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="heart-sphere" cx="36%" cy="32%" r="75%">
            <stop offset="0%" stopColor="#4c3a86" />
            <stop offset="55%" stopColor="#2c1f57" />
            <stop offset="100%" stopColor="#150e2f" />
          </radialGradient>
          <linearGradient id="heart-swirl-a" x1="8%" y1="4%" x2="88%" y2="96%">
            <stop offset="0%" stopColor="#7de6d8" />
            <stop offset="35%" stopColor="#7c8cf0" />
            <stop offset="70%" stopColor="#a45fe0" />
            <stop offset="100%" stopColor="#e0589f" />
          </linearGradient>
          <linearGradient id="heart-swirl-b" x1="0%" y1="100%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="#5f3fb0" />
            <stop offset="60%" stopColor="#9b4fd6" />
            <stop offset="100%" stopColor="#ef7fc0" />
          </linearGradient>
          <linearGradient id="heart-groove" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stopColor="#f077bd" />
            <stop offset="100%" stopColor="#7b2e6b" />
          </linearGradient>
          <clipPath id="heart-sphere-clip">
            <circle cx="160" cy="226" r="108" />
          </clipPath>
          <filter id="heart-blur" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="16" />
          </filter>
        </defs>

        <g className="heart-layer heart-layer-glow">
          <circle cx="160" cy="168" r="150" fill="url(#heart-glow)" />
        </g>

        <g className="heart-layer heart-layer-sphere">
          <circle cx="160" cy="226" r="108" fill="url(#heart-sphere)" />
          <g clipPath="url(#heart-sphere-clip)">
            {GROOVES.map((d, i) => (
              <path
                key={i}
                d={d}
                stroke="url(#heart-groove)"
                strokeWidth="2.2"
                fill="none"
                opacity={0.3 + (i % 4) * 0.12}
              />
            ))}
          </g>
          <circle cx="160" cy="226" r="108" fill="none" stroke="#f077bd" strokeOpacity=".25" />
        </g>

        <g className="heart-layer heart-layer-swirl">
          <path
            d="M92 150C74 108 104 58 154 54c46-4 82 26 88 66 5 32-14 58-46 70-10 4-16 12-14 22 3 15-8 27-24 26-32-2-56-24-66-56-6-19-6-30 0-32z"
            fill="url(#heart-swirl-a)"
          />
          <path
            d="M130 70c34-10 66 4 78 34 10 26-2 50-28 60-8 3-18 2-24-6-10-13-8-30 4-42 8-8 8-18-2-24-10-6-20-16-28-22z"
            fill="url(#heart-swirl-b)"
            opacity=".9"
          />
        </g>

        <g className="heart-layer heart-layer-ribbon">
          <path
            d="M96 132c14-40 52-64 92-58"
            fill="none"
            stroke="#eafcff"
            strokeWidth="3"
            strokeLinecap="round"
            opacity=".55"
          />
        </g>

        <g className="heart-layer heart-layer-shine">
          <ellipse cx="132" cy="90" rx="20" ry="11" fill="#ffffff" opacity=".55" filter="url(#heart-blur)" />
        </g>
      </svg>
    </div>
  );
}
