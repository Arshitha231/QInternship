import { useEffect, useState } from "react";
import {
  addRequirementNotes, ApiError, getRequiredSkills, listProjects, listRequirementNotes,
  setRequiredSkills, uploadPrd,
} from "../api";
import type { SkillLevelName } from "../api";
import { PRDChat } from "./PRDChat";
import { X } from "../icons";
import type {
  Identity, ProjectListItem, ProjectSkillRequirementOut, RequirementNoteOut, ViewMode,
} from "../types";

// PRD chatbot, HR-only, work mode only — same non-visibility guarantee as
// ReviewPage: for any other role/mode this page never renders and every
// call it makes 403s regardless (app/main.py's inline gates on each
// requirements route). Modelled on ReviewPage.tsx's shell (plain
// <section className="card"> blocks, no shared layout wrapper).
//
// Nothing an upload proposes is saved until Confirm — the preview lives
// only in local state, same "review before it reaches a real record"
// discipline ReviewPage's document pipeline already has.

const LEVELS: SkillLevelName[] = ["Learning", "Working", "Expert"];

interface SkillDraft {
  skill: string;
  minimum_level: SkillLevelName;
  fromUpload: boolean;
}

interface NoteDraft {
  note: string;
  fromUpload: boolean;
}

function errorMessage(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback;
}

export function PRDsPage({ identity, viewMode }: { identity: Identity; viewMode: ViewMode }) {
  const [projects, setProjects] = useState<ProjectListItem[] | null>(null);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const [existingSkills, setExistingSkills] = useState<ProjectSkillRequirementOut[] | null>(null);
  const [existingNotes, setExistingNotes] = useState<RequirementNoteOut[] | null>(null);
  const [loadingRequirements, setLoadingRequirements] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);

  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [previewDocId, setPreviewDocId] = useState<number | null>(null);
  const [previewSkills, setPreviewSkills] = useState<SkillDraft[]>([]);
  const [previewNotes, setPreviewNotes] = useState<NoteDraft[]>([]);

  const [confirming, setConfirming] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [justConfirmed, setJustConfirmed] = useState(false);

  useEffect(() => {
    listProjects(identity, viewMode)
      .then(setProjects)
      .catch((e) => setProjectsError(errorMessage(e, "Couldn't load projects.")));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity, viewMode]);

  useEffect(() => {
    if (selectedId === null) {
      setExistingSkills(null);
      setExistingNotes(null);
      return;
    }
    let cancelled = false;
    setLoadingRequirements(true);
    Promise.all([
      getRequiredSkills(identity, selectedId),
      listRequirementNotes(identity, selectedId, viewMode),
    ]).then(([skills, notes]) => {
      if (cancelled) return;
      setExistingSkills(skills);
      setExistingNotes(notes);
      setLoadingRequirements(false);
    }).catch((e) => {
      if (cancelled) return;
      setProjectsError(errorMessage(e, "Couldn't load this project's requirements."));
      setLoadingRequirements(false);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity, viewMode, selectedId, refreshToken]);

  function selectProject(id: number | null) {
    setSelectedId(id);
    // A different project's upload preview has no business surviving the
    // switch -- it was never confirmed, and it names a different project.
    setPreviewDocId(null);
    setPreviewSkills([]);
    setPreviewNotes([]);
    setUploadError(null);
    setConfirmError(null);
    setJustConfirmed(false);
  }

  async function handleUpload(file: File) {
    if (selectedId === null) return;
    setUploading(true);
    setUploadError(null);
    setJustConfirmed(false);
    try {
      const result = await uploadPrd(identity, selectedId, file, viewMode);
      setPreviewDocId(result.doc_id);
      // Seed the editable set from what's already on record, so Confirm's
      // PUT (a full REPLACE) can't silently drop a requirement an earlier
      // upload or a hand-entry recorded — then layer the newly extracted
      // skills on top, extracted values winning on a name collision.
      const merged = new Map<string, SkillDraft>();
      for (const s of existingSkills ?? []) {
        merged.set(s.skill.toLowerCase(), { skill: s.skill, minimum_level: s.minimum_level as SkillLevelName, fromUpload: false });
      }
      for (const s of result.skills) {
        merged.set(s.skill.toLowerCase(), { skill: s.skill, minimum_level: s.minimum_level as SkillLevelName, fromUpload: true });
      }
      setPreviewSkills([...merged.values()]);
      // Notes are append-only server-side (never a replace), so the
      // preview only needs the newly extracted ones -- existing notes
      // stay exactly as they are without needing to be re-sent here.
      setPreviewNotes(result.notes.map((n) => ({ note: n.note, fromUpload: true })));
    } catch (e) {
      setUploadError(errorMessage(e, "Upload failed — try again."));
    } finally {
      setUploading(false);
    }
  }

  async function confirm() {
    if (selectedId === null) return;
    setConfirming(true);
    setConfirmError(null);
    try {
      await setRequiredSkills(
        identity, selectedId,
        previewSkills.map((s) => ({ skill: s.skill, minimum_level: s.minimum_level })),
      );
      if (previewNotes.length > 0) {
        await addRequirementNotes(
          identity, selectedId,
          previewNotes.map((n) => ({ note: n.note, source_doc_id: previewDocId })),
          viewMode,
        );
      }
      setPreviewDocId(null);
      setPreviewSkills([]);
      setPreviewNotes([]);
      setJustConfirmed(true);
      setRefreshToken((t) => t + 1);
    } catch (e) {
      setConfirmError(errorMessage(e, "Couldn't save these requirements — try again."));
    } finally {
      setConfirming(false);
    }
  }

  const selectedProject = projects?.find((p) => p.id === selectedId) ?? null;
  const hasPreview = previewDocId !== null;

  return (
    <div className="review-page">
      <section className="card">
        <h2>Project</h2>
        {projectsError && <p className="bio-error">{projectsError}</p>}
        {projects === null ? (
          <div className="skel skel-card" style={{ height: 40 }} />
        ) : (
          <select
            value={selectedId ?? ""}
            aria-label="Select a project"
            onChange={(e) => selectProject(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">Choose a project…</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}{p.has_requirements ? " (has requirements)" : ""}
              </option>
            ))}
          </select>
        )}
      </section>

      {selectedProject && (
        <section className="card">
          <h2>Upload a requirements document</h2>
          <p className="continuity-meta">
            A project requirements document (.docx or .pdf) for {selectedProject.name}. Nothing it proposes is
            saved until you review and confirm it below.
          </p>
          <input
            type="file" accept=".docx,.pdf" disabled={uploading}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) handleUpload(file);
              e.target.value = "";
            }}
          />
          {uploading && <p className="continuity-meta">Uploading and extracting…</p>}
          {uploadError && <p className="bio-error">{uploadError}</p>}
          {justConfirmed && <p className="continuity-meta">Saved.</p>}

          {hasPreview && (
            <div className="skill-edit">
              <h3>Required skills</h3>
              {previewSkills.length === 0 ? (
                <p className="continuity-meta">No skills proposed — add one below if needed.</p>
              ) : (
                <ul className="skill-edit-list">
                  {previewSkills.map((s, i) => (
                    <li className="skill-edit-row" key={i}>
                      <input
                        className="edit-input skill-edit-name" value={s.skill}
                        aria-label={`Skill ${i + 1} name`}
                        onChange={(e) => setPreviewSkills((rows) =>
                          rows.map((r, j) => (j === i ? { ...r, skill: e.target.value } : r)))}
                      />
                      <select
                        className="skill-edit-level" value={s.minimum_level}
                        aria-label={`Skill ${i + 1} level`}
                        onChange={(e) => setPreviewSkills((rows) =>
                          rows.map((r, j) => (j === i ? { ...r, minimum_level: e.target.value as SkillLevelName } : r)))}
                      >
                        {LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
                      </select>
                      {s.fromUpload && <span className="pill">from upload</span>}
                      <button
                        type="button" className="icon-btn"
                        aria-label={`Remove ${s.skill || "this skill"}`}
                        onClick={() => setPreviewSkills((rows) => rows.filter((_, j) => j !== i))}
                      >
                        <X size={14} />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <button
                type="button" className="link-btn"
                onClick={() => setPreviewSkills((rows) => [...rows, { skill: "", minimum_level: "Working", fromUpload: false }])}
              >
                + Add a skill
              </button>

              <h3>Notes</h3>
              {previewNotes.length === 0 ? (
                <p className="continuity-meta">No notes proposed — add one below if needed.</p>
              ) : (
                <ul className="skill-edit-list">
                  {previewNotes.map((n, i) => (
                    <li className="skill-edit-row" key={i}>
                      <textarea
                        className="edit-input" value={n.note} rows={2}
                        aria-label={`Note ${i + 1}`}
                        onChange={(e) => setPreviewNotes((rows) =>
                          rows.map((r, j) => (j === i ? { ...r, note: e.target.value } : r)))}
                      />
                      <button
                        type="button" className="icon-btn"
                        aria-label={`Remove note ${i + 1}`}
                        onClick={() => setPreviewNotes((rows) => rows.filter((_, j) => j !== i))}
                      >
                        <X size={14} />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <button
                type="button" className="link-btn"
                onClick={() => setPreviewNotes((rows) => [...rows, { note: "", fromUpload: false }])}
              >
                + Add a note
              </button>

              {confirmError && <p className="bio-error">{confirmError}</p>}
              <div className="review-document-head-actions">
                <button
                  type="button" className="btn btn-primary" disabled={confirming}
                  onClick={() => void confirm()}
                >
                  {confirming ? "Saving…" : "Confirm"}
                </button>
                <button
                  type="button" className="link-btn" disabled={confirming}
                  onClick={() => {
                    setPreviewDocId(null);
                    setPreviewSkills([]);
                    setPreviewNotes([]);
                  }}
                >
                  Discard
                </button>
              </div>
            </div>
          )}
        </section>
      )}

      {selectedProject && (
        <section className="card">
          <h2>Current requirements — {selectedProject.name}</h2>
          {loadingRequirements ? (
            <div className="skel skel-card" style={{ height: 100 }} />
          ) : (
            <>
              {(existingSkills?.length ?? 0) === 0 && (existingNotes?.length ?? 0) === 0 ? (
                <p className="continuity-meta">Nothing recorded for this project yet.</p>
              ) : (
                <>
                  {existingSkills && existingSkills.length > 0 && (
                    <ul className="skill-edit-list">
                      {existingSkills.map((s) => (
                        <li className="skill-edit-row" key={s.skill}>
                          <span className="skill-edit-name">{s.skill}</span>
                          <span className={`pill pill-${s.minimum_level.toLowerCase()}`}>{s.minimum_level}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                  {existingNotes && existingNotes.length > 0 && (
                    <ul className="skill-edit-list">
                      {existingNotes.map((n, i) => (
                        <li className="skill-edit-row" key={i}>
                          <span>{n.note}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              )}
            </>
          )}
        </section>
      )}

      {selectedProject && (
        <section className="card">
          <PRDChat
            key={selectedProject.id}
            identity={identity} viewMode={viewMode}
            projectId={selectedProject.id} projectName={selectedProject.name}
          />
        </section>
      )}
    </div>
  );
}
