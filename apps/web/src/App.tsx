import { useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, FormEvent } from "react";
import { useGSAP } from "@gsap/react";
import { gsap } from "gsap";
import {
  Activity,
  AlertCircle,
  Archive,
  Binary,
  Braces,
  CheckCircle2,
  ChevronRight,
  Download,
  Eye,
  FileCode2,
  FileText,
  Filter,
  GitBranch,
  Link2,
  Network,
  Radar,
  RefreshCw,
  RotateCcw,
  SearchCode,
  ShieldCheck,
  Sparkles,
  Upload,
  Workflow,
  XCircle
} from "lucide-react";
import type {
  Artifact,
  CloudMode,
  EvidenceRef,
  InferenceRecord,
  Job,
  JobStatus,
  ReviewRun,
  RuntimeComparisonReport,
  RuntimeValidationRun,
  ToolCall
} from "@ai-jsunpack/shared";
import { CLOUD_MODES, JOB_STATUSES } from "@ai-jsunpack/shared";
import {
  API_BASE_URL,
  createJob,
  fetchArtifactText,
  fetchInferenceRecords,
  fetchJobSummary,
  fetchReviewRuns,
  fetchRuntimeValidations,
  fetchToolCalls,
  rerunJob,
  uploadSource
} from "./api";
import type { JobSummary } from "./api";

gsap.registerPlugin(useGSAP);

type StageState = "done" | "active" | "pending" | "warning" | "fail";
type MetricStatus = "pass" | "warn" | "fail";

interface StageDefinition {
  status: JobStatus;
  label: string;
}

interface StageItem extends StageDefinition {
  state: StageState;
}

interface RuntimeMetric {
  label: string;
  value: string;
  status: MetricStatus;
}

interface WorkbenchData {
  stages: StageItem[];
  latestRuntime: RuntimeValidationRun | null;
  reportArtifacts: Artifact[];
  runtimeMetrics: RuntimeMetric[];
}

interface JobEvidence {
  runtimeValidations: RuntimeValidationRun[];
  inferenceRecords: InferenceRecord[];
  reviewRuns: ReviewRun[];
  toolCalls: ToolCall[];
}

interface EvidenceAttachmentEntry {
  artifactId: string;
  contentType: string;
  hash: string;
  included: boolean;
  kind: Artifact["kind"] | string;
  packagePath: string | null;
  reason: string;
  size: number;
  sourceFilename: string;
  stage: JobStatus | string;
}

interface EvidenceIndexPayload {
  attachments: EvidenceAttachmentEntry[];
  includedCount: number;
  jobId: string;
  kind: "evidence_index";
  omittedCount: number;
  schemaVersion: string;
}

interface JobWorkspace {
  summary: JobSummary;
  evidence: JobEvidence;
}

type ArtifactPreviewStatus = "idle" | "loading" | "ready" | "unsupported" | "error";
type AuditCategory = "all" | "inference" | "review" | "tool";
type AuditStatusFilter = "all" | "attention" | "pass" | "fail";

interface ArtifactPreview {
  artifactId: string | null;
  error: string | null;
  reason: string | null;
  status: ArtifactPreviewStatus;
  text: string | null;
}

interface ArtifactPreviewSupport {
  reason: string | null;
  supported: boolean;
}

interface AuditFilterState {
  category: AuditCategory;
  query: string;
  status: AuditStatusFilter;
}

interface NormalizedAuditRecord {
  artifactIds: string[];
  category: Exclude<AuditCategory, "all">;
  detail: string;
  evidenceRefs: EvidenceRef[];
  failureClass: string;
  id: string;
  label: string;
  secondary: string;
  status: string;
}

interface RuntimeComparisonState {
  artifactId: string | null;
  error: string | null;
  report: RuntimeComparisonReport | null;
  status: "idle" | "loading" | "ready" | "error";
}

const previewMaxBytes = 256 * 1024;

const stageDefinitions: StageDefinition[] = [
  { status: "queued", label: "Job created" },
  { status: "intake", label: "Input inventory" },
  { status: "indexing", label: "AST and resource index" },
  { status: "agent_pass", label: "Agent inference" },
  { status: "reconstructing", label: "Project writer" },
  { status: "runtime_smoke", label: "Browser validation" },
  { status: "runtime_compare", label: "Runtime compare" },
  { status: "reviewing", label: "Review and repair" },
  { status: "completed", label: "Package ready" }
];

const reportArtifactKinds = new Set<Artifact["kind"]>([
  "audit_report",
  "html_report",
  "evidence_index",
  "result_package",
  "runtime_validation",
  "runtime_trace",
  "runtime_screenshot",
  "runtime_scenario",
  "runtime_comparison",
  "review_run",
  "build_artifact",
  "tool_call",
  "inference_record",
  "build_log"
]);

const textualArtifactKinds = new Set<Artifact["kind"]>([
  "input_inventory",
  "source_index",
  "ast_index",
  "agent_plan",
  "inference_record",
  "reconstruction_plan",
  "build_log",
  "build_artifact",
  "runtime_validation",
  "runtime_trace",
  "runtime_scenario",
  "runtime_comparison",
  "review_run",
  "tool_call",
  "memory_record",
  "knowledge_evidence",
  "repair_instruction",
  "evidence_index",
  "audit_report"
]);

const statusOrder = new Map<JobStatus, number>(JOB_STATUSES.map((status, index) => [status, index]));

function emptyEvidence(): JobEvidence {
  return {
    runtimeValidations: [],
    inferenceRecords: [],
    reviewRuns: [],
    toolCalls: []
  };
}

function emptyArtifactPreview(): ArtifactPreview {
  return {
    artifactId: null,
    error: null,
    reason: null,
    status: "idle",
    text: null
  };
}

export function AppContainer() {
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);
  const [artifactPreview, setArtifactPreview] = useState<ArtifactPreview>(() => emptyArtifactPreview());
  const [selectedCloudMode, setSelectedCloudMode] = useState<CloudMode>("local_only");
  const [selectedUploadFile, setSelectedUploadFile] = useState<File | null>(null);
  const [jobSummary, setJobSummary] = useState<JobSummary | null>(null);
  const [evidence, setEvidence] = useState<JobEvidence>(() => emptyEvidence());
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isRerunning, setIsRerunning] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);

  const currentJob = jobSummary?.job ?? null;
  const artifacts = jobSummary?.artifacts ?? [];
  const latestRuntime = evidence.runtimeValidations.at(-1) ?? null;
  const selectedArtifact = useMemo(
    () => artifacts.find((artifact) => artifact.id === selectedArtifactId) ?? artifacts[0] ?? null,
    [artifacts, selectedArtifactId]
  );
  const data = useMemo<WorkbenchData>(
    () => ({
      stages: buildStageItems(currentJob?.status),
      latestRuntime,
      reportArtifacts: buildReportArtifacts(artifacts),
      runtimeMetrics: buildRuntimeMetrics(latestRuntime, evidence.runtimeValidations.length)
    }),
    [artifacts, currentJob?.status, evidence.runtimeValidations.length, latestRuntime]
  );

  useEffect(() => {
    if (!currentJob?.id || !selectedArtifact) {
      setArtifactPreview(emptyArtifactPreview());
      return;
    }

    const previewSupport = artifactPreviewSupport(selectedArtifact);
    if (!previewSupport.supported) {
      setArtifactPreview({
        artifactId: selectedArtifact.id,
        error: null,
        reason: previewSupport.reason,
        status: "unsupported",
        text: null
      });
      return;
    }

    const controller = new AbortController();
    setArtifactPreview({
      artifactId: selectedArtifact.id,
      error: null,
      reason: null,
      status: "loading",
      text: null
    });

    fetchArtifactText(currentJob.id, selectedArtifact.id, controller.signal)
      .then((text) => {
        setArtifactPreview({
          artifactId: selectedArtifact.id,
          error: null,
          reason: null,
          status: "ready",
          text: formatArtifactPreviewText(selectedArtifact, text)
        });
      })
      .catch((error) => {
        if (error instanceof Error && error.name === "AbortError") {
          return;
        }
        setArtifactPreview({
          artifactId: selectedArtifact.id,
          error: errorMessage(error),
          reason: null,
          status: "error",
          text: null
        });
      });

    return () => controller.abort();
  }, [currentJob?.id, selectedArtifact]);

  useEffect(() => {
    if (!currentJob?.id) {
      return;
    }

    let cancelled = false;
    const pollJob = async () => {
      try {
        const workspace = await fetchJobWorkspace(currentJob.id);
        if (!cancelled) {
          setJobSummary(workspace.summary);
          setEvidence(workspace.evidence);
          setPollError(null);
        }
      } catch (error) {
        if (!cancelled) {
          setPollError(errorMessage(error));
        }
      }
    };

    const intervalId = window.setInterval(pollJob, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [currentJob?.id]);

  const handleSubmitJob = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isSubmitting) {
      return;
    }
    if (!selectedUploadFile) {
      setUploadError("Select an input artifact before creating a job.");
      return;
    }

    setIsSubmitting(true);
    setUploadError(null);
    setPollError(null);
    setEvidence(emptyEvidence());
    setSelectedArtifactId(null);
    try {
      const created = await createJob(selectedCloudMode);
      setJobSummary(created);
      const uploaded = await uploadSource(created.job.id, selectedUploadFile);
      setJobSummary(uploaded);
      setEvidence(await fetchJobEvidence(created.job.id));
    } catch (error) {
      setUploadError(errorMessage(error));
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleRefreshJob = async () => {
    if (!currentJob?.id || isRefreshing) {
      return;
    }
    setIsRefreshing(true);
    setPollError(null);
    try {
      const workspace = await fetchJobWorkspace(currentJob.id);
      setJobSummary(workspace.summary);
      setEvidence(workspace.evidence);
    } catch (error) {
      setPollError(errorMessage(error));
    } finally {
      setIsRefreshing(false);
    }
  };

  const handleRerunJob = async () => {
    if (!currentJob?.id || isRerunning) {
      return;
    }
    setIsRerunning(true);
    setUploadError(null);
    setPollError(null);
    setSelectedArtifactId(null);
    setEvidence(emptyEvidence());
    try {
      const rerun = await rerunJob(currentJob.id);
      setJobSummary(rerun);
      setEvidence(await fetchJobEvidence(rerun.job.id));
    } catch (error) {
      setPollError(errorMessage(error));
    } finally {
      setIsRerunning(false);
    }
  };

  const handleArtifactEvidenceSelect = (artifactId: string) => {
    setSelectedArtifactId(artifactId);
    window.requestAnimationFrame(() => {
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      document
        .getElementById("artifact-detail")
        ?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    });
  };

  return (
    <AppView
      apiBaseUrl={API_BASE_URL}
      artifactPreview={artifactPreview}
      artifacts={artifacts}
      currentJob={currentJob}
      data={data}
      evidence={evidence}
      isRefreshing={isRefreshing}
      isRerunning={isRerunning}
      isSubmitting={isSubmitting}
      onArtifactSelect={setSelectedArtifactId}
      onEvidenceArtifactSelect={handleArtifactEvidenceSelect}
      onFileChange={setSelectedUploadFile}
      onRefreshJob={handleRefreshJob}
      onRerunJob={handleRerunJob}
      onSelectCloudMode={setSelectedCloudMode}
      onSubmitJob={handleSubmitJob}
      pollError={pollError}
      selectedArtifact={selectedArtifact}
      selectedCloudMode={selectedCloudMode}
      selectedUploadFile={selectedUploadFile}
      uploadError={uploadError}
    />
  );
}

interface AppViewProps {
  apiBaseUrl: string;
  artifactPreview: ArtifactPreview;
  artifacts: Artifact[];
  currentJob: Job | null;
  data: WorkbenchData;
  evidence: JobEvidence;
  isRefreshing: boolean;
  isRerunning: boolean;
  isSubmitting: boolean;
  onArtifactSelect: (artifactId: string) => void;
  onEvidenceArtifactSelect: (artifactId: string) => void;
  onFileChange: (file: File | null) => void;
  onRefreshJob: () => void;
  onRerunJob: () => void;
  onSelectCloudMode: (mode: CloudMode) => void;
  onSubmitJob: (event: FormEvent<HTMLFormElement>) => void;
  pollError: string | null;
  selectedArtifact: Artifact | null;
  selectedCloudMode: CloudMode;
  selectedUploadFile: File | null;
  uploadError: string | null;
}

function AppView({
  apiBaseUrl,
  artifactPreview,
  artifacts,
  currentJob,
  data,
  evidence,
  isRefreshing,
  isRerunning,
  isSubmitting,
  onArtifactSelect,
  onEvidenceArtifactSelect,
  onFileChange,
  onRefreshJob,
  onRerunJob,
  onSelectCloudMode,
  onSubmitJob,
  pollError,
  selectedArtifact,
  selectedCloudMode,
  selectedUploadFile,
  uploadError
}: AppViewProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useGSAP(
    () => {
      const mm = gsap.matchMedia();
      mm.add(
        {
          reduceMotion: "(prefers-reduced-motion: reduce)"
        },
        (context) => {
          const reduceMotion = Boolean(context.conditions?.reduceMotion);
          if (reduceMotion) {
            gsap.set(".motion-item", { autoAlpha: 1, x: 0, y: 0, scale: 1 });
            return;
          }

          const timeline = gsap.timeline({ defaults: { duration: 0.45, ease: "power2.out" } });
          timeline
            .from(".entry-copy", { y: 18, autoAlpha: 0 })
            .from(".entry-visual", { y: 18, autoAlpha: 0 }, "<0.08")
            .from(".workbench-panel", { y: 18, autoAlpha: 0, stagger: 0.06 }, "<0.12")
            .from(".stage-step", { x: -16, autoAlpha: 0, stagger: 0.05 }, "<0.1");

          return () => timeline.kill();
        }
      );

      return () => mm.revert();
    },
    { scope: rootRef }
  );

  return (
    <div className="app-shell" ref={rootRef}>
      <header className="topbar">
        <a className="brand" href="#overview" aria-label="AI JS Unpack overview">
          <span className="brand-mark">
            <Binary size={18} aria-hidden="true" />
          </span>
          <span>AI JS Unpack</span>
        </a>
        <nav className="topnav" aria-label="Primary">
          <a href="#workflow">Workflow</a>
          <a href="#audit">Audit</a>
          <a href="#runtime">Runtime</a>
        </nav>
      </header>

      <main>
        <section className="entry-band" id="overview">
          <div className="entry-copy motion-item">
            <p className="eyebrow">Authorized JavaScript restoration</p>
            <h1>AI-assisted deobfuscation with browser runtime evidence.</h1>
            <p className="entry-text">
              Upload a production build, inspect recovered artifacts, trace every inference, and validate behavior in a
              controlled browser runtime.
            </p>
            <div className="entry-actions">
              <button className="primary-action" type="button" onClick={() => fileInputRef.current?.click()}>
                <Upload size={18} aria-hidden="true" />
                Upload build
              </button>
              <button className="secondary-action" type="button" onClick={onRefreshJob} disabled={!currentJob || isRefreshing}>
                <RefreshCw size={18} aria-hidden="true" />
                Refresh job
              </button>
            </div>
          </div>

          <div className="entry-visual motion-item" aria-label="Pipeline overview">
            <PipelineMap currentJob={currentJob} artifacts={artifacts} evidence={evidence} />
          </div>
        </section>

        <section className="workbench" id="workflow" aria-label="Analysis workbench">
          <section className="workbench-panel upload-panel motion-item">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Source intake</p>
                <h2>Input package</h2>
              </div>
              <ShieldCheck size={22} aria-hidden="true" />
            </div>
            <form className="upload-form" onSubmit={onSubmitJob}>
              <label className="dropzone" htmlFor="source-upload">
                <Archive size={24} aria-hidden="true" />
                <div>
                  <strong>{selectedUploadFile?.name ?? "Select build artifact"}</strong>
                  <span>{selectedUploadFile ? formatBytes(selectedUploadFile.size) : "HTML, chunks, CSS, assets, sourcemaps"}</span>
                </div>
              </label>
              <input
                ref={fileInputRef}
                className="visually-hidden"
                id="source-upload"
                type="file"
                onChange={(event: ChangeEvent<HTMLInputElement>) => onFileChange(event.currentTarget.files?.[0] ?? null)}
              />
              <div className="mode-grid" aria-label="Processing modes">
                {CLOUD_MODES.map((mode) => (
                  <ModePill
                    active={mode === selectedCloudMode}
                    key={mode}
                    label={mode}
                    onClick={() => onSelectCloudMode(mode)}
                  />
                ))}
              </div>
              <div className="upload-actions">
                <button className="primary-action compact" type="submit" disabled={isSubmitting}>
                  <Upload size={16} aria-hidden="true" />
                  {isSubmitting ? "Uploading" : "Create job"}
                </button>
                <button
                  className="secondary-action compact"
                  type="button"
                  onClick={onRefreshJob}
                  disabled={!currentJob || isRefreshing}
                >
                  <RefreshCw size={16} aria-hidden="true" />
                  {isRefreshing ? "Refreshing" : "Refresh"}
                </button>
              </div>
            </form>
            <JobSummaryPanel apiBaseUrl={apiBaseUrl} artifacts={artifacts} currentJob={currentJob} evidence={evidence} />
            <WorkspaceActions
              artifacts={artifacts}
              currentJob={currentJob}
              evidence={evidence}
              isRerunning={isRerunning}
              onRerunJob={onRerunJob}
            />
            {uploadError ? <StatusBanner tone="error" message={uploadError} /> : null}
            {pollError ? <StatusBanner tone="warning" message={pollError} /> : null}
          </section>

          <section className="workbench-panel timeline-panel motion-item">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Job state</p>
                <h2>{currentJob ? currentJob.status : "No job"}</h2>
              </div>
              <Workflow size={22} aria-hidden="true" />
            </div>
            <ol className="stage-list">
              {data.stages.map((stage) => (
                <li className={`stage-step stage-${stage.state}`} key={stage.status}>
                  <span className="stage-icon">{stageIcon(stage.state)}</span>
                  <span>{stage.label}</span>
                  <small>{stage.status}</small>
                </li>
              ))}
            </ol>
          </section>

          <section className="workbench-panel tree-panel motion-item">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Artifact index</p>
                <h2>{artifacts.length} records</h2>
              </div>
              <FileCode2 size={22} aria-hidden="true" />
            </div>
            <ArtifactList artifacts={artifacts} selectedArtifact={selectedArtifact} onArtifactSelect={onArtifactSelect} />
          </section>

          <section className="workbench-panel code-panel motion-item" id="artifact-detail">
            <div className="panel-heading padded-heading">
              <div>
                <p className="panel-kicker">Artifact detail</p>
                <h2>{selectedArtifact ? selectedArtifact.kind : "No artifact selected"}</h2>
              </div>
              <Braces size={22} aria-hidden="true" />
            </div>
            <ArtifactDetail
              apiBaseUrl={apiBaseUrl}
              artifact={selectedArtifact}
              artifactPreview={artifactPreview}
              artifacts={artifacts}
              currentJob={currentJob}
              onArtifactSelect={onArtifactSelect}
            />
          </section>

          <section className="workbench-panel report-panel motion-item">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Reports and evidence</p>
                <h2>{data.reportArtifacts.length} outputs</h2>
              </div>
              <Archive size={22} aria-hidden="true" />
            </div>
            <ReportArtifactList
              apiBaseUrl={apiBaseUrl}
              artifacts={data.reportArtifacts}
              currentJob={currentJob}
              evidence={evidence}
              onArtifactSelect={onEvidenceArtifactSelect}
            />
          </section>

          <section className="workbench-panel audit-panel motion-item" id="audit">
            <div className="panel-heading padded-heading">
              <div>
                <p className="panel-kicker">Evidence ledger</p>
                <h2>Agent audit</h2>
              </div>
              <SearchCode size={22} aria-hidden="true" />
            </div>
            <AuditPanel
              artifacts={artifacts}
              currentJob={currentJob}
              evidence={evidence}
              onArtifactSelect={onEvidenceArtifactSelect}
            />
          </section>

          <section className="workbench-panel runtime-panel motion-item" id="runtime">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Browser evidence</p>
                <h2>Runtime validation</h2>
              </div>
              <Radar size={22} aria-hidden="true" />
            </div>
            <RuntimePanel
              apiBaseUrl={apiBaseUrl}
              artifacts={artifacts}
              currentJob={currentJob}
              latestRuntime={data.latestRuntime}
              onArtifactSelect={onEvidenceArtifactSelect}
              runtimeMetrics={data.runtimeMetrics}
              runtimeValidations={evidence.runtimeValidations}
            />
          </section>
        </section>
      </main>
    </div>
  );
}

function ModePill({
  active = false,
  label,
  onClick
}: {
  active?: boolean;
  label: CloudMode;
  onClick: () => void;
}) {
  return (
    <button
      aria-pressed={active}
      className={active ? "mode-pill mode-pill-active" : "mode-pill"}
      type="button"
      onClick={onClick}
    >
      {label}
    </button>
  );
}

function JobSummaryPanel({
  apiBaseUrl,
  artifacts,
  currentJob,
  evidence
}: {
  apiBaseUrl: string;
  artifacts: Artifact[];
  currentJob: Job | null;
  evidence: JobEvidence;
}) {
  if (!currentJob) {
    return (
      <div className="job-summary">
        <span>API</span>
        <strong>{apiBaseUrl}</strong>
      </div>
    );
  }

  return (
    <div className="job-summary">
      <span>Job</span>
      <strong>{currentJob.id}</strong>
      <span>Updated</span>
      <strong>{formatTimestamp(currentJob.updatedAt)}</strong>
      <span>Artifacts</span>
      <strong>{artifacts.length}</strong>
      <span>Audit</span>
      <strong>{evidence.inferenceRecords.length + evidence.reviewRuns.length + evidence.toolCalls.length}</strong>
      <span>Runtime</span>
      <strong>{evidence.runtimeValidations.length}</strong>
      {currentJob.failureReason ? (
        <>
          <span>Failure</span>
          <strong>{currentJob.failureReason}</strong>
        </>
      ) : null}
    </div>
  );
}

function WorkspaceActions({
  artifacts,
  currentJob,
  evidence,
  isRerunning,
  onRerunJob
}: {
  artifacts: Artifact[];
  currentJob: Job | null;
  evidence: JobEvidence;
  isRerunning: boolean;
  onRerunJob: () => void;
}) {
  const canRerun = Boolean(currentJob && artifacts.some((artifact) => artifact.kind === "source_input"));
  return (
    <div className="workspace-actions" aria-label="Workspace actions">
      <button
        className="secondary-action compact"
        type="button"
        disabled={!currentJob}
        onClick={() =>
          currentJob
            ? downloadJsonFile(`ai-jsunpack-${currentJob.id}-workspace.json`, {
                exportedAt: new Date().toISOString(),
                job: currentJob,
                artifacts,
                evidence
              })
            : undefined
        }
      >
        <Download size={16} aria-hidden="true" />
        Export JSON
      </button>
      <button
        className="secondary-action compact"
        type="button"
        disabled={!canRerun || isRerunning}
        onClick={onRerunJob}
        title={canRerun ? "Create a new job from the current source input." : "Rerun requires a source input artifact."}
      >
        <RotateCcw size={16} aria-hidden="true" />
        {isRerunning ? "Rerunning" : "Rerun"}
      </button>
    </div>
  );
}

function ArtifactList({
  artifacts,
  onArtifactSelect,
  selectedArtifact
}: {
  artifacts: Artifact[];
  onArtifactSelect: (artifactId: string) => void;
  selectedArtifact: Artifact | null;
}) {
  if (artifacts.length === 0) {
    return <EmptyState title="No artifacts yet" detail="Create a job and run the worker to populate artifact metadata." />;
  }

  return (
    <div className="file-list artifact-picker" role="listbox" aria-label="Artifacts">
      {artifacts.map((artifact) => (
        <button
          className={artifact.id === selectedArtifact?.id ? "file-row file-row-active" : "file-row"}
          key={artifact.id}
          type="button"
          onClick={() => onArtifactSelect(artifact.id)}
        >
          <FileCode2 size={15} aria-hidden="true" />
          <span>{artifact.kind}</span>
          <small>{formatBytes(artifact.size)}</small>
        </button>
      ))}
    </div>
  );
}

function ArtifactDetail({
  apiBaseUrl,
  artifact,
  artifactPreview,
  artifacts,
  onArtifactSelect,
  currentJob
}: {
  apiBaseUrl: string;
  artifact: Artifact | null;
  artifactPreview: ArtifactPreview;
  artifacts: Artifact[];
  currentJob: Job | null;
  onArtifactSelect: (artifactId: string) => void;
}) {
  if (!artifact) {
    return (
      <div className="detail-surface">
        <EmptyState title="Select an artifact" detail="Artifact metadata appears after upload or worker pipeline output." />
      </div>
    );
  }

  return (
    <div className="detail-surface">
      <div className="detail-grid">
        <DetailItem label="Artifact ID" value={artifact.id} />
        <DetailItem label="Kind" value={artifact.kind} />
        <DetailItem label="Stage" value={artifact.stage} />
        <DetailItem label="Attempt" value={String(artifact.attempt)} />
        <DetailItem label="Producer" value={artifact.producer} />
        <DetailItem label="Content type" value={artifact.contentType} />
        <DetailItem label="Size" value={formatBytes(artifact.size)} />
        <DetailItem label="Hash" value={artifact.hash} />
        <DetailItem label="Sensitivity" value={artifact.sensitivityClass} />
        <DetailItem label="Retention" value={artifact.retentionClass} />
        <DetailItem label="Created" value={formatTimestamp(artifact.createdAt)} />
        <DetailItem label="Schema" value={artifact.schemaVersion} />
      </div>
      <div className="detail-block">
        <span>Storage URI</span>
        <code>{artifact.storageUri}</code>
      </div>
      <div className="detail-block">
        <span>Parent artifacts</span>
        <code>{formatIdList(artifact.parentArtifactIds)}</code>
      </div>
      <ArtifactLineage artifact={artifact} artifacts={artifacts} onArtifactSelect={onArtifactSelect} />
      <ArtifactPreviewPane artifact={artifact} preview={artifactPreview} />
      <div className="detail-actions">
        <ArtifactDownloadLink apiBaseUrl={apiBaseUrl} artifact={artifact} currentJob={currentJob} label="Download artifact" />
      </div>
    </div>
  );
}

function ArtifactLineage({
  artifact,
  artifacts,
  onArtifactSelect
}: {
  artifact: Artifact;
  artifacts: Artifact[];
  onArtifactSelect: (artifactId: string) => void;
}) {
  const parents = artifact.parentArtifactIds
    .map((artifactId) => artifacts.find((candidate) => candidate.id === artifactId))
    .filter((candidate): candidate is Artifact => Boolean(candidate));
  const children = artifacts.filter((candidate) => candidate.parentArtifactIds.includes(artifact.id));

  if (parents.length === 0 && children.length === 0) {
    return (
      <div className="lineage-panel">
        <div className="lineage-heading">
          <Link2 size={16} aria-hidden="true" />
          <strong>Lineage</strong>
        </div>
        <EmptyState title="No linked artifacts" detail="Parent and child artifact relationships appear when lineage is recorded." />
      </div>
    );
  }

  return (
    <div className="lineage-panel">
      <div className="lineage-heading">
        <Link2 size={16} aria-hidden="true" />
        <strong>Lineage</strong>
      </div>
      <ArtifactLineageGroup label="Parents" artifacts={parents} onArtifactSelect={onArtifactSelect} />
      <ArtifactLineageGroup label="Children" artifacts={children} onArtifactSelect={onArtifactSelect} />
    </div>
  );
}

function ArtifactLineageGroup({
  artifacts,
  label,
  onArtifactSelect
}: {
  artifacts: Artifact[];
  label: string;
  onArtifactSelect: (artifactId: string) => void;
}) {
  return (
    <div className="lineage-group">
      <span>{label}</span>
      {artifacts.length > 0 ? (
        <div className="lineage-links">
          {artifacts.map((artifact) => (
            <button className="lineage-chip" key={artifact.id} type="button" onClick={() => onArtifactSelect(artifact.id)}>
              <FileCode2 size={14} aria-hidden="true" />
              {artifact.kind}
              <small>{artifact.id}</small>
            </button>
          ))}
        </div>
      ) : (
        <strong>None</strong>
      )}
    </div>
  );
}

function ArtifactPreviewPane({ artifact, preview }: { artifact: Artifact; preview: ArtifactPreview }) {
  const isCurrentPreview = preview.artifactId === artifact.id;
  const status = isCurrentPreview ? preview.status : "idle";

  return (
    <div className="preview-panel">
      <div className="preview-heading">
        <div>
          <span>Content preview</span>
          <strong>{artifact.contentType}</strong>
        </div>
        <Eye size={18} aria-hidden="true" />
      </div>
      {status === "loading" ? (
        <div className="preview-message">
          <FileText size={18} aria-hidden="true" />
          Loading artifact content
        </div>
      ) : null}
      {status === "ready" && preview.text ? <pre className="artifact-preview-code">{preview.text}</pre> : null}
      {status === "unsupported" ? (
        <div className="preview-message preview-muted">
          <AlertCircle size={18} aria-hidden="true" />
          {preview.reason ?? "This artifact is not previewable in the browser."}
        </div>
      ) : null}
      {status === "error" ? (
        <div className="preview-message preview-error">
          <AlertCircle size={18} aria-hidden="true" />
          {preview.error ?? "Artifact preview failed."}
        </div>
      ) : null}
      {status === "idle" ? (
        <div className="preview-message preview-muted">
          <FileText size={18} aria-hidden="true" />
          Select a text or JSON artifact to load a preview.
        </div>
      ) : null}
    </div>
  );
}

function ReportArtifactList({
  apiBaseUrl,
  artifacts,
  currentJob,
  evidence,
  onArtifactSelect
}: {
  apiBaseUrl: string;
  artifacts: Artifact[];
  currentJob: Job | null;
  evidence: JobEvidence;
  onArtifactSelect: (artifactId: string) => void;
}) {
  const evidenceIndexArtifact = artifacts.find((artifact) => artifact.kind === "evidence_index") ?? null;
  const artifactsById = useMemo(() => new Map(artifacts.map((artifact) => [artifact.id, artifact])), [artifacts]);
  const [evidenceIndex, setEvidenceIndex] = useState<{
    artifactId: string | null;
    error: string | null;
    payload: EvidenceIndexPayload | null;
    status: "idle" | "loading" | "ready" | "error";
  }>({ artifactId: null, error: null, payload: null, status: "idle" });

  useEffect(() => {
    if (!currentJob || !evidenceIndexArtifact) {
      setEvidenceIndex({ artifactId: null, error: null, payload: null, status: "idle" });
      return;
    }

    const controller = new AbortController();
    let active = true;
    setEvidenceIndex({ artifactId: evidenceIndexArtifact.id, error: null, payload: null, status: "loading" });
    fetchArtifactText(currentJob.id, evidenceIndexArtifact.id, controller.signal)
      .then((text) => parseEvidenceIndexPayload(text))
      .then((payload) => {
        if (active) {
          setEvidenceIndex({ artifactId: evidenceIndexArtifact.id, error: null, payload, status: "ready" });
        }
      })
      .catch((error: Error) => {
        if (active) {
          setEvidenceIndex({
            artifactId: evidenceIndexArtifact.id,
            error: error.message,
            payload: null,
            status: "error"
          });
        }
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [currentJob?.id, evidenceIndexArtifact?.id]);

  if (artifacts.length === 0) {
    return <EmptyState title="No report outputs" detail="Runtime, audit, review, and package artifacts appear here when produced." />;
  }

  const markdownReports = artifacts.filter((artifact) => artifact.kind === "audit_report").length;
  const htmlReports = artifacts.filter((artifact) => artifact.kind === "html_report").length;
  const packages = artifacts.filter((artifact) => artifact.kind === "result_package").length;
  const reviewAttention = evidence.reviewRuns.filter(
    (run) => run.failureClass !== "none" || ["best_effort", "fail", "retry"].includes(run.status)
  ).length;
  const attachments = evidenceIndex.payload?.attachments ?? [];

  return (
    <div className="report-list">
      <div className="report-summary-grid" aria-label="Report output summary">
        <ReportMetric label="Markdown" value={String(markdownReports)} />
        <ReportMetric label="HTML" value={String(htmlReports)} />
        <ReportMetric label="Packages" value={String(packages)} />
        <ReportMetric label="Evidence files" value={String(evidenceIndex.payload?.includedCount ?? 0)} />
      </div>

      <div className={reviewAttention > 0 ? "report-risk-strip warning" : "report-risk-strip"}>
        <AlertCircle size={17} aria-hidden="true" />
        <div>
          <strong>{currentJob?.failureClass === "none" ? "No job failure class" : currentJob?.failureClass ?? "Awaiting job"}</strong>
          <span>
            {reviewAttention > 0
              ? `${reviewAttention} review record${reviewAttention === 1 ? "" : "s"} need attention.`
              : currentJob?.failureReason ?? "Build, review, and runtime evidence decide final package confidence."}
          </span>
        </div>
      </div>

      <section className="report-detail-block" aria-label="Report downloads">
        <div className="section-heading">
          <h3>Report artifacts</h3>
          <span>{artifacts.length}</span>
        </div>
        {artifacts.map((artifact) => (
          <div className="report-row" key={artifact.id}>
            <div>
              <strong>{artifact.kind}</strong>
              <span>
                {artifact.stage} / {artifact.producer}
              </span>
            </div>
            <div className="report-actions">
              <button className="secondary-action compact" type="button" onClick={() => onArtifactSelect(artifact.id)}>
                <Eye size={16} aria-hidden="true" />
                Inspect
              </button>
              <ArtifactDownloadLink apiBaseUrl={apiBaseUrl} artifact={artifact} currentJob={currentJob} label="Download" />
            </div>
          </div>
        ))}
      </section>

      <section className="report-detail-block" aria-label="Evidence attachment index">
        <div className="section-heading">
          <h3>Evidence attachments</h3>
          <span>{evidenceIndex.status === "ready" ? attachments.length : evidenceIndex.status}</span>
        </div>
        {evidenceIndex.status === "loading" ? (
          <div className="preview-message">
            <FileText size={18} aria-hidden="true" />
            Loading evidence index
          </div>
        ) : null}
        {evidenceIndex.status === "error" ? (
          <div className="preview-message preview-error">
            <AlertCircle size={18} aria-hidden="true" />
            {evidenceIndex.error ?? "Evidence index could not be loaded."}
          </div>
        ) : null}
        {evidenceIndex.status === "idle" ? (
          <EmptyState title="No evidence index" detail="Packaging will add screenshot, trace, and log attachment indexes." />
        ) : null}
        {evidenceIndex.status === "ready" && attachments.length === 0 ? (
          <EmptyState title="No indexed attachments" detail="No screenshot, trace, or log artifacts were available at packaging time." />
        ) : null}
        {evidenceIndex.status === "ready" && attachments.length > 0 ? (
          <div className="evidence-index-list">
            {attachments.map((attachment) => {
              const linkedArtifact = artifactsById.get(attachment.artifactId);
              return (
                <div className="evidence-index-row" key={attachment.artifactId}>
                  <div>
                    <strong>{attachment.kind}</strong>
                    <span>{attachment.packagePath ?? attachment.reason}</span>
                    <small>{attachment.artifactId}</small>
                  </div>
                  <StatusToken status={attachment.included ? "pass" : "best_effort"} />
                  <div className="report-actions">
                    <button
                      className="secondary-action compact"
                      type="button"
                      disabled={!linkedArtifact}
                      onClick={() => (linkedArtifact ? onArtifactSelect(linkedArtifact.id) : undefined)}
                    >
                      <Link2 size={16} aria-hidden="true" />
                      Locate
                    </button>
                    {linkedArtifact ? (
                      <ArtifactDownloadLink apiBaseUrl={apiBaseUrl} artifact={linkedArtifact} currentJob={currentJob} label="Download" />
                    ) : (
                      <span className="download-disabled">Missing</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}
      </section>
    </div>
  );
}

function ReportMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="report-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function AuditPanel({
  artifacts,
  currentJob,
  evidence,
  onArtifactSelect
}: {
  artifacts: Artifact[];
  currentJob: Job | null;
  evidence: JobEvidence;
  onArtifactSelect: (artifactId: string) => void;
}) {
  const [filters, setFilters] = useState<AuditFilterState>({ category: "all", query: "", status: "all" });
  const auditRecords = useMemo(() => buildAuditRecords(evidence), [evidence]);
  const filteredRecords = useMemo(
    () => auditRecords.filter((record) => auditRecordMatches(record, filters)),
    [auditRecords, filters]
  );
  const attentionCount = auditRecords.filter((record) => auditRecordNeedsAttention(record)).length;

  return (
    <div className="audit-sections">
      <section className="audit-section" aria-label="Audit filters">
        <div className="audit-toolbar">
          <div className="audit-filter-icon">
            <Filter size={18} aria-hidden="true" />
          </div>
          <label htmlFor="audit-search">
            <span>Search</span>
            <input
              id="audit-search"
              name="audit-search"
              value={filters.query}
              onChange={(event) => setFilters((current) => ({ ...current, query: event.currentTarget.value }))}
              placeholder="Agent, decision, artifact, evidence"
            />
          </label>
          <label htmlFor="audit-category">
            <span>Record type</span>
            <select
              id="audit-category"
              name="audit-category"
              value={filters.category}
              onChange={(event) =>
                setFilters((current) => ({ ...current, category: event.currentTarget.value as AuditCategory }))
              }
            >
              <option value="all">All records</option>
              <option value="inference">Inference</option>
              <option value="review">Review</option>
              <option value="tool">Tool calls</option>
            </select>
          </label>
          <label htmlFor="audit-status">
            <span>Status</span>
            <select
              id="audit-status"
              name="audit-status"
              value={filters.status}
              onChange={(event) =>
                setFilters((current) => ({ ...current, status: event.currentTarget.value as AuditStatusFilter }))
              }
            >
              <option value="all">All statuses</option>
              <option value="attention">Needs attention</option>
              <option value="pass">Passing</option>
              <option value="fail">Failing</option>
            </select>
          </label>
          <button
            className="secondary-action compact"
            type="button"
            disabled={!currentJob || filteredRecords.length === 0}
            onClick={() =>
              currentJob
                ? downloadJsonFile(`ai-jsunpack-${currentJob.id}-audit-filtered.json`, {
                    exportedAt: new Date().toISOString(),
                    jobId: currentJob.id,
                    filters,
                    artifactCount: artifacts.length,
                    records: filteredRecords
                  })
                : undefined
            }
          >
            <Download size={16} aria-hidden="true" />
            Export view
          </button>
        </div>
        <div className="audit-summary-grid">
          <div>
            <span>Total</span>
            <strong>{auditRecords.length}</strong>
          </div>
          <div>
            <span>Attention</span>
            <strong>{attentionCount}</strong>
          </div>
          <div>
            <span>Visible</span>
            <strong>{filteredRecords.length}</strong>
          </div>
        </div>
      </section>

      <section className="audit-section" aria-label="Filtered audit records">
        <div className="section-heading">
          <h3>Audit records</h3>
          <span>{filteredRecords.length}</span>
        </div>
        {filteredRecords.length > 0 ? (
          <div className="table-shell">
            <table className="data-table audit-ledger-table">
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Subject</th>
                  <th>Status</th>
                  <th>Evidence</th>
                  <th>Artifacts</th>
                </tr>
              </thead>
              <tbody>
                {filteredRecords.map((record) => (
                  <tr key={record.id}>
                    <td>
                      <strong>{record.category}</strong>
                      <span>{record.label}</span>
                    </td>
                    <td>
                      <strong>{record.secondary}</strong>
                      <span>{record.detail}</span>
                    </td>
                    <td>
                      <StatusToken status={record.status} />
                      {record.failureClass !== "none" ? <small>{record.failureClass}</small> : null}
                    </td>
                    <td>
                      <EvidenceRefButtons refs={record.evidenceRefs} onArtifactSelect={onArtifactSelect} />
                    </td>
                    <td>{record.artifactIds.length > 0 ? formatIdList(record.artifactIds) : "None"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="No matching audit records" detail="Adjust filters or wait for worker audit artifacts." />
        )}
      </section>
    </div>
  );
}

function StatusToken({ status }: { status: string }) {
  const tone = statusTokenTone(status);
  return <span className={`status-token status-token-${tone}`}>{status}</span>;
}

function EvidenceRefButtons({
  onArtifactSelect,
  refs
}: {
  onArtifactSelect: (artifactId: string) => void;
  refs: EvidenceRef[];
}) {
  if (refs.length === 0) {
    return <span className="muted-inline">None</span>;
  }

  return (
    <div className="evidence-ref-list">
      {refs.map((ref) => (
        <button
          className="evidence-ref-chip"
          key={`${ref.artifactId}-${ref.label}-${ref.locator ?? ""}`}
          type="button"
          onClick={() => onArtifactSelect(ref.artifactId)}
          title={[ref.label, ref.locator, ref.excerpt].filter(Boolean).join(" / ")}
        >
          <Link2 size={14} aria-hidden="true" />
          <span>{ref.label}</span>
          {ref.locator ? <small>{ref.locator}</small> : null}
        </button>
      ))}
    </div>
  );
}

function RuntimePanel({
  apiBaseUrl,
  artifacts,
  currentJob,
  latestRuntime,
  onArtifactSelect,
  runtimeMetrics,
  runtimeValidations
}: {
  apiBaseUrl: string;
  artifacts: Artifact[];
  currentJob: Job | null;
  latestRuntime: RuntimeValidationRun | null;
  onArtifactSelect: (artifactId: string) => void;
  runtimeMetrics: RuntimeMetric[];
  runtimeValidations: RuntimeValidationRun[];
}) {
  return (
    <>
      <div className="runtime-grid">
        {runtimeMetrics.map((metric) => (
          <div className={`runtime-metric runtime-${metric.status}`} key={metric.label}>
            <span>{metric.label}</span>
            <strong>{metric.value}</strong>
          </div>
        ))}
      </div>
      {latestRuntime ? (
        <div className="runtime-detail">
          <DetailItem label="Latest status" value={latestRuntime.status} />
          <DetailItem label="Target" value={latestRuntime.target} />
          <DetailItem label="Entry URL" value={latestRuntime.entryUrl} />
          <DetailItem label="Attempt" value={String(latestRuntime.attempt)} />
          <EvidenceArtifactLinks
            apiBaseUrl={apiBaseUrl}
            artifactIds={[
              ...latestRuntime.screenshotArtifactIds,
              latestRuntime.traceArtifactId,
              latestRuntime.comparisonArtifactId
            ]}
            artifacts={artifacts}
            currentJob={currentJob}
            onArtifactSelect={onArtifactSelect}
          />
          <RuntimeCompareStatus
            artifacts={artifacts}
            currentJob={currentJob}
            latestRuntime={latestRuntime}
            onArtifactSelect={onArtifactSelect}
          />
          <RuntimeIssueList title="Console errors" items={latestRuntime.consoleErrors} />
          <RuntimeIssueList title="Page errors" items={latestRuntime.pageErrors} />
          <RuntimeIssueList title="Failed requests" items={latestRuntime.failedRequests} />
        </div>
      ) : (
        <EmptyState title="No runtime evidence" detail="Runtime validation reports appear after the runtime_smoke stage." />
      )}
      {runtimeValidations.length > 1 ? (
        <div className="runtime-history">
          <div className="section-heading">
            <h3>Runtime history</h3>
            <span>{runtimeValidations.length}</span>
          </div>
          {runtimeValidations.map((run) => (
            <RuntimeRunDetail
              apiBaseUrl={apiBaseUrl}
              artifacts={artifacts}
              currentJob={currentJob}
              key={run.id}
              onArtifactSelect={onArtifactSelect}
              run={run}
            />
          ))}
        </div>
      ) : null}
    </>
  );
}

function RuntimeCompareStatus({
  artifacts,
  currentJob,
  latestRuntime,
  onArtifactSelect
}: {
  artifacts: Artifact[];
  currentJob: Job | null;
  latestRuntime: RuntimeValidationRun;
  onArtifactSelect: (artifactId: string) => void;
}) {
  const comparisonArtifactId = latestRuntime.comparisonArtifactId ?? null;
  const [comparison, setComparison] = useState<RuntimeComparisonState>({
    artifactId: null,
    error: null,
    report: null,
    status: "idle"
  });

  useEffect(() => {
    if (!currentJob || !comparisonArtifactId) {
      setComparison({ artifactId: comparisonArtifactId, error: null, report: null, status: "idle" });
      return;
    }

    const controller = new AbortController();
    let active = true;
    setComparison({ artifactId: comparisonArtifactId, error: null, report: null, status: "loading" });
    fetchArtifactText(currentJob.id, comparisonArtifactId, controller.signal)
      .then((text) => parseRuntimeComparisonReport(text))
      .then((report) => {
        if (active) {
          setComparison({ artifactId: comparisonArtifactId, error: null, report, status: "ready" });
        }
      })
      .catch((error: Error) => {
        if (active) {
          setComparison({ artifactId: comparisonArtifactId, error: error.message, report: null, status: "error" });
        }
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [currentJob?.id, comparisonArtifactId]);

  const report = comparison.report;
  const differences = report?.differences ?? null;

  return (
    <div className="runtime-compare-status runtime-compare-detail">
      <div className="runtime-compare-heading">
        <div>
          <span>Runtime diff</span>
          <strong>{comparisonArtifactId ? "Comparison artifact recorded" : "Waiting for runtime_compare evidence"}</strong>
        </div>
        {report ? <StatusToken status={report.status} /> : null}
      </div>

      {comparison.status === "loading" ? (
        <div className="preview-message">
          <FileText size={18} aria-hidden="true" />
          Loading comparison detail
        </div>
      ) : null}
      {comparison.status === "error" ? (
        <div className="preview-message preview-error">
          <AlertCircle size={18} aria-hidden="true" />
          {comparison.error ?? "Runtime comparison could not be loaded."}
        </div>
      ) : null}
      {!comparisonArtifactId ? (
        <span className="runtime-compare-note">Runtime compare has not produced a linked artifact yet.</span>
      ) : null}
      {report && differences ? (
        <>
          <div className="runtime-diff-grid" aria-label="Runtime comparison difference summary">
            <RuntimeDiffMetric label="Screenshot" value={formatScreenshotDiff(differences)} />
            <RuntimeDiffMetric label="DOM paths" value={String(differences.domDifferences.length)} />
            <RuntimeDiffMetric label="Network groups" value={formatRuntimeGroups(differences.networkDiff.groups)} />
            <RuntimeDiffMetric label="Console groups" value={formatRuntimeGroups(differences.consoleDiff.groups)} />
          </div>
          <RuntimeComparisonEvidenceButtons
            artifacts={artifacts}
            comparisonArtifactId={comparisonArtifactId ?? report.id}
            onArtifactSelect={onArtifactSelect}
            report={report}
          />
          <RuntimeDomDifferenceList differences={differences.domDifferences} />
          <RuntimeCollectionDifferenceList
            title="Network differences"
            originalOnly={differences.networkDiff.originalOnly}
            reconstructedOnly={differences.networkDiff.reconstructedOnly}
          />
          <RuntimeCollectionDifferenceList
            title="Console differences"
            originalOnly={differences.consoleDiff.originalOnly}
            reconstructedOnly={differences.consoleDiff.reconstructedOnly}
          />
          {differences.screenshotDiff.reason ? (
            <span className="runtime-compare-note">{differences.screenshotDiff.reason}</span>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function RuntimeDiffMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="runtime-diff-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function RuntimeComparisonEvidenceButtons({
  artifacts,
  comparisonArtifactId,
  onArtifactSelect,
  report
}: {
  artifacts: Artifact[];
  comparisonArtifactId: string;
  onArtifactSelect: (artifactId: string) => void;
  report: RuntimeComparisonReport;
}) {
  const linkedIds = [
    comparisonArtifactId,
    report.scenarioArtifactId,
    ...report.traceArtifactIds,
    ...report.screenshotArtifactIds
  ].filter((artifactId, index, ids): artifactId is string => Boolean(artifactId) && ids.indexOf(artifactId) === index);
  const knownArtifactIds = new Set(artifacts.map((artifact) => artifact.id));

  return (
    <div className="runtime-evidence-buttons" aria-label="Runtime comparison evidence links">
      {linkedIds.map((artifactId) => (
        <button
          className="evidence-ref-chip"
          disabled={!knownArtifactIds.has(artifactId)}
          key={artifactId}
          type="button"
          onClick={() => onArtifactSelect(artifactId)}
          title={artifactId}
        >
          <Link2 size={14} aria-hidden="true" />
          <span>{runtimeEvidenceLabel(artifactId, report)}</span>
          <small>{artifactId}</small>
        </button>
      ))}
    </div>
  );
}

function RuntimeDomDifferenceList({ differences }: { differences: RuntimeComparisonReport["differences"]["domDifferences"] }) {
  if (differences.length === 0) {
    return <RuntimeDiffSection title="DOM differences" items={["No DOM summary paths changed."]} />;
  }

  return (
    <div className="runtime-diff-section">
      <span>DOM differences</span>
      {differences.slice(0, 8).map((difference) => (
        <code key={difference.path}>
          {difference.path}: {formatUnknownValue(difference.original)}
          {" -> "}
          {formatUnknownValue(difference.reconstructed)}
        </code>
      ))}
      {differences.length > 8 ? <small>{differences.length - 8} more DOM path changes in the comparison artifact.</small> : null}
    </div>
  );
}

function RuntimeCollectionDifferenceList({
  originalOnly,
  reconstructedOnly,
  title
}: {
  originalOnly: string[];
  reconstructedOnly: string[];
  title: string;
}) {
  const items = [
    ...originalOnly.slice(0, 4).map((item) => `original only: ${item}`),
    ...reconstructedOnly.slice(0, 4).map((item) => `reconstructed only: ${item}`)
  ];
  if (items.length === 0) {
    return <RuntimeDiffSection title={title} items={["No unique entries."]} />;
  }
  return <RuntimeDiffSection title={title} items={items} />;
}

function RuntimeDiffSection({ items, title }: { items: string[]; title: string }) {
  return (
    <div className="runtime-diff-section">
      <span>{title}</span>
      {items.map((item) => (
        <code key={item}>{item}</code>
      ))}
    </div>
  );
}

function RuntimeRunDetail({
  apiBaseUrl,
  artifacts,
  currentJob,
  onArtifactSelect,
  run
}: {
  apiBaseUrl: string;
  artifacts: Artifact[];
  currentJob: Job | null;
  onArtifactSelect: (artifactId: string) => void;
  run: RuntimeValidationRun;
}) {
  return (
    <div className="runtime-run-card">
      <div className="history-row">
        <StatusToken status={run.status} />
        <strong>{run.target}</strong>
        <small>{run.entryUrl}</small>
      </div>
      <div className="runtime-run-issues">
        <RuntimeIssueList title="Console" items={run.consoleErrors} />
        <RuntimeIssueList title="Page" items={run.pageErrors} />
        <RuntimeIssueList title="Requests" items={run.failedRequests} />
      </div>
      <EvidenceArtifactLinks
        apiBaseUrl={apiBaseUrl}
        artifactIds={[...run.screenshotArtifactIds, run.traceArtifactId, run.comparisonArtifactId]}
        artifacts={artifacts}
        currentJob={currentJob}
        onArtifactSelect={onArtifactSelect}
      />
    </div>
  );
}

function EvidenceArtifactLinks({
  apiBaseUrl,
  artifactIds,
  artifacts,
  currentJob,
  onArtifactSelect
}: {
  apiBaseUrl: string;
  artifactIds: Array<string | null | undefined>;
  artifacts: Artifact[];
  currentJob: Job | null;
  onArtifactSelect: (artifactId: string) => void;
}) {
  const linkedArtifacts = artifactIds
    .filter((artifactId): artifactId is string => Boolean(artifactId))
    .map((artifactId) => artifacts.find((artifact) => artifact.id === artifactId))
    .filter((artifact): artifact is Artifact => Boolean(artifact));

  if (linkedArtifacts.length === 0) {
    return null;
  }

  return (
    <div className="evidence-links" aria-label="Runtime evidence artifact downloads">
      {linkedArtifacts.map((artifact) => (
        <div className="evidence-link-group" key={artifact.id}>
          <button className="download-link" type="button" onClick={() => onArtifactSelect(artifact.id)}>
            <Link2 size={15} aria-hidden="true" />
            {artifact.kind}
          </button>
          <ArtifactDownloadLink apiBaseUrl={apiBaseUrl} artifact={artifact} currentJob={currentJob} label="Download" />
        </div>
      ))}
    </div>
  );
}

function RuntimeIssueList({ items, title }: { items: string[]; title: string }) {
  if (items.length === 0) {
    return (
      <div className="issue-list">
        <span>{title}</span>
        <strong>None</strong>
      </div>
    );
  }

  return (
    <div className="issue-list">
      <span>{title}</span>
      {items.map((item) => (
        <code key={item}>{item}</code>
      ))}
    </div>
  );
}

function ArtifactDownloadLink({
  apiBaseUrl,
  artifact,
  currentJob,
  label
}: {
  apiBaseUrl: string;
  artifact: Artifact;
  currentJob: Job | null;
  label: string;
}) {
  if (!currentJob) {
    return null;
  }

  if (!canDownloadArtifact(artifact)) {
    return (
      <span className="download-disabled">
        <AlertCircle size={15} aria-hidden="true" />
        Package required
      </span>
    );
  }

  return (
    <a className="download-link" href={artifactDownloadUrl(apiBaseUrl, currentJob.id, artifact.id)}>
      <Download size={15} aria-hidden="true" />
      {label}
    </a>
  );
}

function DetailItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="detail-item">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function EmptyState({ detail, title }: { detail: string; title: string }) {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      <span>{detail}</span>
    </div>
  );
}

function StatusBanner({ message, tone }: { message: string; tone: "error" | "warning" }) {
  return <div className={`status-banner status-${tone}`}>{message}</div>;
}

function PipelineMap({
  artifacts,
  currentJob,
  evidence
}: {
  artifacts: Artifact[];
  currentJob: Job | null;
  evidence: JobEvidence;
}) {
  const nodes = [
    { label: "Input", icon: Upload },
    { label: "AST", icon: GitBranch },
    { label: "Agents", icon: Sparkles },
    { label: "Runtime", icon: Radar },
    { label: "Review", icon: ShieldCheck }
  ];

  return (
    <div className="pipeline-map">
      {nodes.map((node, index) => {
        const Icon = node.icon;
        return (
          <div className="pipeline-node" key={node.label}>
            <span>
              <Icon size={18} aria-hidden="true" />
            </span>
            <strong>{node.label}</strong>
            {index < nodes.length - 1 ? <ChevronRight className="pipeline-arrow" size={18} aria-hidden="true" /> : null}
          </div>
        );
      })}
      <div className="pipeline-status">
        <CheckCircle2 size={18} aria-hidden="true" />
        <span>{currentJob ? currentJob.status : "Awaiting job"}</span>
      </div>
      <div className={artifacts.length > 0 ? "pipeline-status" : "pipeline-status warning"}>
        <Archive size={18} aria-hidden="true" />
        <span>{artifacts.length} artifacts</span>
      </div>
      <div className={evidence.runtimeValidations.length > 0 ? "pipeline-status" : "pipeline-status warning"}>
        <Network size={18} aria-hidden="true" />
        <span>{evidence.runtimeValidations.length} runtime runs</span>
      </div>
    </div>
  );
}

async function fetchJobWorkspace(jobId: string): Promise<JobWorkspace> {
  const [summary, evidence] = await Promise.all([fetchJobSummary(jobId), fetchJobEvidence(jobId)]);
  return { summary, evidence };
}

async function fetchJobEvidence(jobId: string): Promise<JobEvidence> {
  const [runtimeValidations, inferenceRecords, reviewRuns, toolCalls] = await Promise.all([
    fetchRuntimeValidations(jobId),
    fetchInferenceRecords(jobId),
    fetchReviewRuns(jobId),
    fetchToolCalls(jobId)
  ]);
  return { runtimeValidations, inferenceRecords, reviewRuns, toolCalls };
}

function parseEvidenceIndexPayload(text: string): EvidenceIndexPayload {
  const payload = JSON.parse(text) as Partial<EvidenceIndexPayload>;
  if (payload.kind !== "evidence_index" || !Array.isArray(payload.attachments)) {
    throw new Error("Artifact is not a valid evidence index.");
  }
  return {
    attachments: payload.attachments,
    includedCount: payload.includedCount ?? payload.attachments.filter((item) => item.included).length,
    jobId: payload.jobId ?? "",
    kind: "evidence_index",
    omittedCount: payload.omittedCount ?? payload.attachments.filter((item) => !item.included).length,
    schemaVersion: payload.schemaVersion ?? "unknown"
  };
}

function parseRuntimeComparisonReport(text: string): RuntimeComparisonReport {
  const payload = JSON.parse(text) as Partial<RuntimeComparisonReport>;
  if (!payload.id || !payload.differences || !payload.original || !payload.reconstructed) {
    throw new Error("Artifact is not a valid runtime comparison report.");
  }
  return payload as RuntimeComparisonReport;
}

function artifactPreviewSupport(artifact: Artifact): ArtifactPreviewSupport {
  if (artifact.kind === "generated_project") {
    return { supported: false, reason: "Directory artifacts are available through the packaged result download." };
  }
  if (artifact.kind === "result_package") {
    return { supported: false, reason: "Result packages are binary downloads and are not rendered inline." };
  }
  if (artifact.kind === "html_report") {
    return { supported: false, reason: "HTML reports are download-only so report markup is not executed inside the workbench." };
  }
  if (artifact.kind === "runtime_screenshot") {
    return { supported: false, reason: "Screenshots are image evidence. Use the download action to inspect the capture." };
  }
  if (artifact.size > previewMaxBytes) {
    return { supported: false, reason: `Preview is limited to ${formatBytes(previewMaxBytes)} artifacts.` };
  }
  if (textualArtifactKinds.has(artifact.kind) || isTextContentType(artifact.contentType)) {
    return { supported: true, reason: null };
  }
  return { supported: false, reason: `${artifact.contentType} is not treated as browser-previewable text.` };
}

function canDownloadArtifact(artifact: Artifact): boolean {
  return artifact.kind !== "generated_project";
}

function isTextContentType(contentType: string): boolean {
  const normalized = contentType.toLowerCase();
  return (
    normalized.startsWith("text/") ||
    normalized.includes("json") ||
    normalized.includes("javascript") ||
    normalized.includes("xml")
  );
}

function formatArtifactPreviewText(artifact: Artifact, text: string): string {
  if (artifact.contentType.toLowerCase().includes("json") || text.trimStart().startsWith("{") || text.trimStart().startsWith("[")) {
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      return text;
    }
  }
  return text;
}

function formatScreenshotDiff(differences: RuntimeComparisonReport["differences"]): string {
  const changed = differences.screenshotDiff.changed ?? differences.screenshotChanged;
  const status = differences.screenshotDiff.pixelDiffStatus;
  return `changed ${String(changed)} / ${status}`;
}

function formatRuntimeGroups(groups: Record<string, string[]>): string {
  const keys = Object.keys(groups);
  if (keys.length === 0) {
    return "none";
  }
  return keys.slice(0, 3).join(", ");
}

function runtimeEvidenceLabel(artifactId: string, report: RuntimeComparisonReport): string {
  if (artifactId === report.scenarioArtifactId) {
    return "scenario";
  }
  if (report.traceArtifactIds.includes(artifactId)) {
    return "trace";
  }
  if (report.screenshotArtifactIds.includes(artifactId)) {
    return "screenshot";
  }
  return "comparison";
}

function formatUnknownValue(value: unknown): string {
  if (value === undefined) {
    return "undefined";
  }
  if (value === null) {
    return "null";
  }
  if (typeof value === "string") {
    return value.length > 90 ? `${value.slice(0, 87)}...` : value;
  }
  try {
    const text = JSON.stringify(value);
    return text.length > 90 ? `${text.slice(0, 87)}...` : text;
  } catch {
    return String(value);
  }
}

function buildAuditRecords(evidence: JobEvidence): NormalizedAuditRecord[] {
  return [
    ...evidence.inferenceRecords.map((record) => ({
      artifactIds: [...record.inputArtifactIds, ...record.outputArtifactIds],
      category: "inference" as const,
      detail: `confidence ${formatPercent(record.confidence)} / ${record.validationStatus}`,
      evidenceRefs: record.evidenceRefs,
      failureClass: "none",
      id: record.id,
      label: record.type,
      secondary: record.agentName,
      status: record.validationStatus
    })),
    ...evidence.reviewRuns.map((run) => ({
      artifactIds: [run.logsArtifactId, ...run.repairInstructionIds].filter((artifactId): artifactId is string =>
        Boolean(artifactId)
      ),
      category: "review" as const,
      detail: run.decision,
      evidenceRefs: run.evidenceRefs,
      failureClass: run.failureClass,
      id: run.id,
      label: run.reviewType,
      secondary: `attempt ${run.attempt}`,
      status: run.status
    })),
    ...evidence.toolCalls.map((call) => ({
      artifactIds: [...call.inputArtifactIds, ...call.outputArtifactIds],
      category: "tool" as const,
      detail: `${call.caller} / ${formatDuration(call.duration)}`,
      evidenceRefs: [],
      failureClass: call.failureClass,
      id: call.id,
      label: call.toolName,
      secondary: call.toolVersion,
      status: call.status
    }))
  ];
}

function auditRecordMatches(record: NormalizedAuditRecord, filters: AuditFilterState): boolean {
  if (filters.category !== "all" && record.category !== filters.category) {
    return false;
  }
  if (filters.status === "attention" && !auditRecordNeedsAttention(record)) {
    return false;
  }
  if (filters.status === "pass" && statusTokenTone(record.status) !== "pass") {
    return false;
  }
  if (filters.status === "fail" && statusTokenTone(record.status) !== "fail") {
    return false;
  }
  const query = filters.query.trim().toLowerCase();
  if (!query) {
    return true;
  }
  const haystack = [
    record.category,
    record.detail,
    record.failureClass,
    record.id,
    record.label,
    record.secondary,
    record.status,
    ...record.artifactIds,
    ...record.evidenceRefs.flatMap((ref) => [ref.artifactId, ref.label, ref.locator ?? "", ref.excerpt ?? ""])
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query);
}

function auditRecordNeedsAttention(record: NormalizedAuditRecord): boolean {
  return record.failureClass !== "none" || ["best_effort", "fail", "needs_review", "rejected", "retry", "unverified"].includes(record.status);
}

function statusTokenTone(status: string): "pass" | "warn" | "fail" {
  if (status === "pass" || status === "accepted") {
    return "pass";
  }
  if (status === "fail" || status === "rejected") {
    return "fail";
  }
  return "warn";
}

function buildStageItems(currentStatus: JobStatus | undefined): StageItem[] {
  const activeStatus = visibleStageFor(currentStatus);
  const activeIndex = activeStatus ? statusIndex(activeStatus) : -1;
  const failed = currentStatus === "failed" || currentStatus === "cancelled";

  return stageDefinitions.map((stage) => {
    const stageIndex = statusIndex(stage.status);
    let state: StageState = "pending";
    if (failed && stage.status === activeStatus) {
      state = "fail";
    } else if (currentStatus === "completed_best_effort" && stage.status === "completed") {
      state = "warning";
    } else if (stage.status === activeStatus) {
      state = "active";
    } else if (activeIndex >= 0 && stageIndex < activeIndex) {
      state = "done";
    }
    return { ...stage, state };
  });
}

function buildReportArtifacts(artifacts: Artifact[]): Artifact[] {
  return [...artifacts]
    .filter((artifact) => reportArtifactKinds.has(artifact.kind))
    .sort((left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt));
}

function buildRuntimeMetrics(latestRuntime: RuntimeValidationRun | null, runtimeCount: number): RuntimeMetric[] {
  if (!latestRuntime) {
    return [
      { label: "Runs", value: String(runtimeCount), status: "warn" },
      { label: "Entry load", value: "Pending", status: "warn" },
      { label: "Console errors", value: "Pending", status: "warn" },
      { label: "Failed requests", value: "Pending", status: "warn" }
    ];
  }

  const status = runStatusToMetricStatus(latestRuntime.status);
  return [
    { label: "Runs", value: String(runtimeCount), status },
    { label: "Entry load", value: latestRuntime.status, status },
    {
      label: "Console errors",
      value: String(latestRuntime.consoleErrors.length + latestRuntime.pageErrors.length),
      status: latestRuntime.consoleErrors.length + latestRuntime.pageErrors.length > 0 ? "fail" : "pass"
    },
    {
      label: "Failed requests",
      value: String(latestRuntime.failedRequests.length),
      status: latestRuntime.failedRequests.length > 0 ? "fail" : "pass"
    }
  ];
}

function visibleStageFor(status: JobStatus | undefined): JobStatus | undefined {
  if (!status) {
    return undefined;
  }
  if (status === "failed" || status === "cancelled") {
    return "reviewing";
  }
  if (status === "completed_best_effort") {
    return "completed";
  }
  const currentIndex = statusIndex(status);
  return stageDefinitions.find((stage) => statusIndex(stage.status) >= currentIndex)?.status ?? "completed";
}

function statusIndex(status: JobStatus): number {
  return statusOrder.get(status) ?? Number.MAX_SAFE_INTEGER;
}

function stageIcon(state: StageState) {
  if (state === "done") {
    return <CheckCircle2 size={16} aria-hidden="true" />;
  }
  if (state === "fail") {
    return <XCircle size={16} aria-hidden="true" />;
  }
  return <Activity size={16} aria-hidden="true" />;
}

function runStatusToMetricStatus(status: RuntimeValidationRun["status"]): MetricStatus {
  if (status === "pass") {
    return "pass";
  }
  if (status === "fail") {
    return "fail";
  }
  return "warn";
}

function formatBytes(size: number): string {
  if (size < 1024) {
    return `${size} B`;
  }
  const kib = size / 1024;
  if (kib < 1024) {
    return `${kib.toFixed(1)} KiB`;
  }
  return `${(kib / 1024).toFixed(1)} MiB`;
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function formatDuration(seconds: number): string {
  if (seconds < 1) {
    return `${Math.round(seconds * 1000)} ms`;
  }
  return `${seconds.toFixed(2)} s`;
}

function formatEvidenceRefs(refs: EvidenceRef[]): string {
  if (refs.length === 0) {
    return "None";
  }
  return refs.map((ref) => [ref.label, ref.locator].filter(Boolean).join(" / ")).join("; ");
}

function formatIdList(ids: string[]): string {
  return ids.length > 0 ? ids.join(", ") : "None";
}

function artifactDownloadUrl(apiBaseUrl: string, jobId: string, artifactId: string): string {
  return `${apiBaseUrl}/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(artifactId)}/download`;
}

function downloadJsonFile(filename: string, payload: unknown): void {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return "Request failed.";
}
