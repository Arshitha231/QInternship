import type { ReactNode } from "react";

export interface SidebarItem {
  key: string;
  label: string;
  icon: ReactNode;
  active: boolean;
  onClick: () => void;
}

interface Props {
  items: SidebarItem[];
}

// One row height, in px, shared with the CSS below -- the accent bar's
// position is computed from this rather than measured off the DOM, which is
// fine as long as every row stays the same height (they do: same padding,
// same icon size, one line of label text).
const ROW_HEIGHT = 40;
const ROW_GAP = 4;

export function Sidebar({ items }: Props) {
  const activeIndex = Math.max(0, items.findIndex((i) => i.active));

  return (
    <nav className="mel-sidebar" data-help="tabs" aria-label="Section">
      <ul className="mel-sidebar-list">
        <span
          className="mel-sidebar-accent-bar"
          aria-hidden="true"
          style={{ transform: `translateY(${activeIndex * (ROW_HEIGHT + ROW_GAP)}px)` }}
        />
        {items.map((item) => (
          <li key={item.key}>
            <button
              type="button"
              className={`mel-sidebar-item ${item.active ? "active" : ""}`}
              aria-current={item.active ? "page" : undefined}
              onClick={item.onClick}
            >
              {item.icon}
              <span>{item.label}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
