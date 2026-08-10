import type { PersonSummary } from "../types";
import { Building, MapPin } from "../icons";

function initials(name: string): string {
  return name.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
}

const STATUS_LABEL: Record<string, string> = {
  available: "Available",
  away: "Away",
  restricted: "Restricted",
};

export function PersonCard({ person, onClick }: { person: PersonSummary; onClick: () => void }) {
  const status = person.availability_status;
  return (
    <button className="result-card" onClick={onClick}>
      <span className="avatar" style={{ width: 42, height: 42, fontSize: 15 }} aria-hidden="true">
        {initials(person.full_name)}
      </span>
      <span style={{ minWidth: 0, flex: 1 }}>
        <p className="result-name">{person.preferred_name || person.full_name}</p>
        <p className="result-role">{person.job_title}</p>
        <span className="result-meta">
          <span><Building size={13} />{person.org_unit}</span>
          {person.office && <span><MapPin size={13} />{person.office.city}</span>}
          <span>
            <span className={`status-dot status-${status}`} />
            {STATUS_LABEL[status] ?? status}
          </span>
        </span>
      </span>
    </button>
  );
}
