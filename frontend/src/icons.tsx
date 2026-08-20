type IconProps = { size?: number; className?: string };

const base = (size: number) => ({
  width: size,
  height: size,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
});

export const SearchIcon = ({ size = 17, className }: IconProps) => (
  <svg {...base(size)} className={className}><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
);
export const ChevronDown = ({ size = 15, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="m6 9 6 6 6-6" /></svg>
);
export const ChevronLeft = ({ size = 15, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="m15 18-6-6 6-6" /></svg>
);
export const Building = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M3 10h18M8 3v4M16 3v4" /></svg>
);
export const MapPin = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 1 1 16 0Z" /><circle cx="12" cy="10" r="3" /></svg>
);
export const Clock = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>
);
export const UserReports = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><rect x="9" y="3" width="6" height="5" rx="1" /><path d="M12 8v4M6 20v-4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v4" /><path d="M12 12v2" /></svg>
);
export const Mail = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></svg>
);
export const Slack = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><rect x="10" y="3" width="4" height="10" rx="2" /><rect x="3" y="10" width="10" height="4" rx="2" /><rect x="10" y="11" width="4" height="10" rx="2" /><rect x="11" y="10" width="10" height="4" rx="2" /></svg>
);
export const LinkIcon = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" /><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" /></svg>
);
export const HelpCircle = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><circle cx="12" cy="12" r="10" /><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3" /><path d="M12 17h.01" /></svg>
);
export const Phone = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.362 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.338 1.85.573 2.81.7A2 2 0 0 1 22 16.92Z" /></svg>
);
export const Briefcase = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><rect x="2" y="7" width="20" height="14" rx="2" /><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" /></svg>
);
export const Users = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" /></svg>
);
export const Sparkles = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8" /></svg>
);
export const Send = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="m22 2-7 20-4-9-9-4Z" /><path d="M22 2 11 13" /></svg>
);
export const ArrowRight = ({ size = 14, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M5 12h14M13 6l6 6-6 6" /></svg>
);
export const X = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M18 6 6 18M6 6l12 12" /></svg>
);
export const Filter = ({ size = 15, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3Z" /></svg>
);
export const Loader = ({ size = 18, className }: IconProps) => (
  <svg {...base(size)} className={`spin ${className ?? ""}`}><path d="M21 12a9 9 0 1 1-6.219-8.56" /></svg>
);
export const Cake = ({ size = 14, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M3 21h18M4 21v-6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v6" /><path d="M12 13V9M12 6.5V5" /><path d="M8 13V9M16 13V9" /></svg>
);
export const Award = ({ size = 14, className }: IconProps) => (
  <svg {...base(size)} className={className}><circle cx="12" cy="9" r="5" /><path d="m8.5 13.5-1 7.5 4.5-2.5 4.5 2.5-1-7.5" /></svg>
);
export const Bell = ({ size = 17, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M18 8a6 6 0 1 0-12 0c0 6-3 7-3 7h18s-3-1-3-7" /><path d="M13.7 21a2 2 0 0 1-3.4 0" /></svg>
);
export const GraduationCap = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M22 9 12 4 2 9l10 5 10-5Z" /><path d="M6 11.5V16c0 1.5 2.7 3 6 3s6-1.5 6-3v-4.5" /></svg>
);
export const Check = ({ size = 14, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="m20 6-11 11-5-5" /></svg>
);
export const AlertCircle = ({ size = 14, className }: IconProps) => (
  <svg {...base(size)} className={className}><circle cx="12" cy="12" r="9" /><path d="M12 8v5M12 16.5v.01" /></svg>
);
export const Network = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><circle cx="12" cy="5" r="2.5" /><circle cx="5" cy="19" r="2.5" /><circle cx="19" cy="19" r="2.5" /><path d="M12 7.5v4M12 11.5 6.5 17M12 11.5 17.5 17" /></svg>
);
export const Sun = ({ size = 17, className }: IconProps) => (
  <svg {...base(size)} className={className}><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>
);
export const Moon = ({ size = 17, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8Z" /></svg>
);
export const Volume = ({ size = 15, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M11 5 6 9H3v6h3l5 4V5Z" /><path d="M16 8.5a5 5 0 0 1 0 7M19 5.5a9 9 0 0 1 0 13" /></svg>
);
export const Home = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="m3 11 9-8 9 8" /><path d="M5 10v10a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V10" /></svg>
);
export const Minus = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M5 12h14" /></svg>
);
export const Plus = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M12 5v14M5 12h14" /></svg>
);
// Fit-to-view (four corner brackets pointing outward) -- the graph frames'
// "show me the whole thing" control, distinct from Home's "go back to start".
export const Maximize = ({ size = 16, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3" /></svg>
);
// Chevron pair used by the department tree's expand/collapse control.
export const ChevronsDown = ({ size = 14, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="m7 6 5 5 5-5M7 13l5 5 5-5" /></svg>
);
export const ChevronsUp = ({ size = 14, className }: IconProps) => (
  <svg {...base(size)} className={className}><path d="m7 11 5-5 5 5M7 18l5-5 5 5" /></svg>
);
