import { useState } from "react";
import { Filter, X } from "../icons";
import type { SearchFilters } from "../api";

const LABELS: Record<keyof SearchFilters, string> = {
  name: "Name",
  query: "Query",
  skill: "Skill",
  level: "Level",
  org_unit: "Org unit",
  office: "Office",
  language: "Language",
  available: "Available",
};

export function Filters({
  filters,
  onChange,
}: {
  filters: SearchFilters;
  onChange: (next: SearchFilters) => void;
}) {
  const [open, setOpen] = useState(false);
  const chipKeys: (keyof SearchFilters)[] = ["skill", "level", "org_unit", "office", "language", "available"];
  const activeChips = chipKeys.filter((k) => filters[k] !== undefined && filters[k] !== "");

  function set<K extends keyof SearchFilters>(key: K, value: SearchFilters[K]) {
    onChange({ ...filters, [key]: value });
  }

  return (
    <div>
      <div className="filter-bar">
        <button className={`filter-toggle ${open ? "open" : ""}`} onClick={() => setOpen((v) => !v)}>
          <Filter />
          Filters
        </button>
        {activeChips.map((k) => (
          <span className="filter-chip" key={k}>
            {LABELS[k]}: {String(filters[k])}
            <button aria-label={`Remove ${LABELS[k]} filter`} onClick={() => set(k, undefined)}>
              <X size={12} />
            </button>
          </span>
        ))}
      </div>
      {open && (
        <div className="filter-bar" style={{ marginTop: -8 }}>
          <input
            placeholder="Skill (e.g. Terraform)"
            value={filters.skill ?? ""}
            onChange={(e) => set("skill", e.target.value || undefined)}
          />
          <select value={filters.level ?? ""} onChange={(e) => set("level", (e.target.value || undefined) as SearchFilters["level"])}>
            <option value="">Any level</option>
            <option value="Learning">Learning</option>
            <option value="Working">Working</option>
            <option value="Expert">Expert</option>
          </select>
          <input
            placeholder="Org unit / team"
            value={filters.org_unit ?? ""}
            onChange={(e) => set("org_unit", e.target.value || undefined)}
          />
          <input
            placeholder="Office / city"
            value={filters.office ?? ""}
            onChange={(e) => set("office", e.target.value || undefined)}
          />
          <input
            placeholder="Language"
            value={filters.language ?? ""}
            onChange={(e) => set("language", e.target.value || undefined)}
          />
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, color: "var(--muted)" }}>
            <input
              type="checkbox"
              style={{ width: "auto" }}
              checked={filters.available ?? false}
              onChange={(e) => set("available", e.target.checked || undefined)}
            />
            Available now
          </label>
        </div>
      )}
    </div>
  );
}
