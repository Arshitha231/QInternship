import { useEffect, useState } from "react";
import { ApiError, approveActionRequest, listPendingApprovals, rejectActionRequest } from "../api";
import type { ActionRequestResult } from "../api";
import type { Identity, ViewMode } from "../types";

// Restrict/deactivate/create are maker-checker (see app.writes' three
// request_* functions) — whoever the REQUESTER's reporting chain names as
// approver sees their pending requests here, regardless of which role
// header they're currently using. Deliberately not gated to "hr" at all:
// the approver is resolved by identity, not role (see
// app.writes.list_my_pending_approvals), so an "employee"-role identity
// who happens to manage an HR person still needs to see this.

// What each action does to the person named, in the approver's words rather
// than the enum's. "create" reads as an addition, not a mutation, because
// approving it is the thing that brings the profile into existence.
const ACTION_VERB: Record<string, string> = {
  restrict: "hide the profile of",
  deactivate: "deactivate",
  create: "add",
};

// The consequence of approving, spelled out. An approver seeing one of
// these for the first time shouldn't have to know what the directory means
// by "restricted" to decide.
const ACTION_EFFECT: Record<string, string> = {
  restrict: "Their profile becomes invisible to everyone except HR.",
  deactivate: "They're removed from the directory, search, and every graph.",
  create: "A new profile is created and becomes visible to everyone.",
};

export function PendingApprovals({ identity, viewMode }: { identity: Identity; viewMode: ViewMode }) {
  const [requests, setRequests] = useState<ActionRequestResult[]>([]);
  const [open, setOpen] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    listPendingApprovals(identity)
      .then((r) => {
        if (!cancelled) setRequests(r.requests);
      })
      .catch(() => {
        // Quiet failure — this is a passive, always-on check, not a
        // navigated-to page; surfacing a fetch error here would be noise
        // on every screen for something the user didn't ask to see.
      });
    return () => {
      cancelled = true;
    };
  }, [identity, refreshToken]);

  if (requests.length === 0) return null;

  async function handleApprove(requestId: number) {
    setBusyId(requestId);
    setError(null);
    try {
      await approveActionRequest(identity, requestId, viewMode);
      setRefreshToken((t) => t + 1);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't approve — try again.");
    } finally {
      setBusyId(null);
    }
  }

  async function handleReject(requestId: number) {
    const reason = window.prompt("Reason for rejecting (optional):") ?? undefined;
    setBusyId(requestId);
    setError(null);
    try {
      await rejectActionRequest(identity, requestId, viewMode, reason || undefined);
      setRefreshToken((t) => t + 1);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't reject — try again.");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="pending-approvals">
      <button type="button" className="pending-approvals-toggle" onClick={() => setOpen((v) => !v)}>
        {requests.length} pending approval{requests.length === 1 ? "" : "s"}
      </button>
      {open && (
        <ul className="pending-approvals-list">
          {error && <li className="bio-error">{error}</li>}
          {requests.map((r) => (
            <li key={r.request_id} className="pending-approvals-item">
              <span>
                {/* Names throughout, never ids — this is read by a person
                    deciding whether to approve, and target_id is null
                    anyway for a create (nobody exists to have an id yet). */}
                <strong>{r.requested_by_name}</strong> requested to{" "}
                {ACTION_VERB[r.action_type] ?? r.action_type}{" "}
                <strong>{r.target_name}</strong>
                {ACTION_EFFECT[r.action_type] && (
                  <span className="pending-approvals-effect">{ACTION_EFFECT[r.action_type]}</span>
                )}
              </span>
              <div className="pending-approvals-actions">
                <button
                  className="btn btn-primary" disabled={busyId === r.request_id}
                  onClick={() => handleApprove(r.request_id)}
                >
                  Approve
                </button>
                <button
                  className="btn btn-danger-outline" disabled={busyId === r.request_id}
                  onClick={() => handleReject(r.request_id)}
                >
                  Reject
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
