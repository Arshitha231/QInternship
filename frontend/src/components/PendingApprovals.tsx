import { useEffect, useState } from "react";
import { ApiError, approveActionRequest, listPendingApprovals, rejectActionRequest } from "../api";
import type { ActionRequestResult } from "../api";
import type { Identity, ViewMode } from "../types";

// Restrict/deactivate are maker-checker now (see app.writes.request_
// restriction/request_deactivation) — whoever the REQUESTER's reporting
// chain names as approver sees their pending requests here, regardless of
// which role header they're currently using. Deliberately not gated to
// "hr" at all: the approver is resolved by identity, not role (see
// app.writes.list_my_pending_approvals), so an "employee"-role identity
// who happens to manage an HR person still needs to see this.

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
                <strong>{r.requested_by}</strong> requested to <strong>{r.action_type}</strong>{" "}
                <strong>{r.target_name}</strong>
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
