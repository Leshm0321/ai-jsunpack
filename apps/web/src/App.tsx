import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
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
  FileJson2,
  FileText,
  Filter,
  GitBranch,
  Link2,
  ListTree,
  Network,
  Radar,
  RefreshCw,
  RotateCcw,
  Save,
  SearchCode,
  ShieldCheck,
  Sparkles,
  Trash2,
  Upload,
  Workflow,
  XCircle
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type {
  Artifact,
  AstIndex,
  CloudMode,
  EvidenceRef,
  InferenceRecord,
  InputInventory,
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
const ArtifactTextEditor = lazy(() => import("./ArtifactTextEditor"));

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
  retentionClass?: Artifact["retentionClass"] | string;
  sensitivityClass?: Artifact["sensitivityClass"] | string;
  size: number;
  sourceFilename: string;
  stage: JobStatus | string;
}

interface PackageContentEntry {
  artifactId: string | null;
  contentType: string;
  description: string;
  included: boolean;
  path: string;
  reason: string;
  size: number | null;
  source: string;
}

interface ReportSectionDetailEntry {
  details: Record<string, unknown>;
  label: string;
  status?: string;
  value: string;
}

interface ReportSectionEntry {
  anchor: string;
  artifactIds: string[];
  artifactKinds: string[];
  details: ReportSectionDetailEntry[];
  evidenceLinks: string[];
  summary: string;
  title: string;
}

interface FailureSummaryEntry {
  decision: string;
  failureClass: string;
  group: string;
  status: string;
}

interface EvidenceIndexPayload {
  attachments: EvidenceAttachmentEntry[];
  failureSummary: FailureSummaryEntry[];
  includedCount: number;
  jobId: string;
  kind: "evidence_index";
  omittedCount: number;
  packageContents: PackageContentEntry[];
  reportSections: ReportSectionEntry[];
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

type AuditRiskGroupId = "blocking" | "review" | "passing";

interface SavedAuditFilter {
  createdAt: string;
  filters: AuditFilterState;
  id: string;
  name: string;
}

interface AuditRiskGroup {
  detail: string;
  id: AuditRiskGroupId;
  records: NormalizedAuditRecord[];
  title: string;
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
  error: string | null;
  reports: RuntimeComparisonLoaded[];
  status: "idle" | "loading" | "ready" | "error";
}

interface RuntimeComparisonLoaded {
  artifactId: string;
  report: RuntimeComparisonReport;
}

interface RuntimeComparisonFilters {
  scenario: string;
  status: "all" | RuntimeComparisonReport["status"];
  viewport: string;
}

type EvidenceGraphMode = "lineage" | "chunks" | "agents";
type EvidenceGraphNodeKind = "artifact" | "resource" | "analysis" | "agent" | "review" | "tool";
type EvidenceGraphTone = "neutral" | "pass" | "warn" | "fail" | "active";

interface EvidenceGraphNode {
  artifactId?: string;
  column: number;
  detail: string;
  id: string;
  kind: EvidenceGraphNodeKind;
  title: string;
  tone?: EvidenceGraphTone;
}

interface EvidenceGraphEdge {
  from: string;
  id: string;
  label: string;
  to: string;
}

interface EvidenceGraph {
  edges: EvidenceGraphEdge[];
  emptyDetail: string;
  nodes: EvidenceGraphNode[];
  summary: string;
  title: string;
}

interface EvidenceGraphSourceState {
  astIndexes: AstIndex[] | null;
  error: string | null;
  inventory: InputInventory | null;
  status: "idle" | "loading" | "ready" | "error";
}

const previewMaxBytes = 256 * 1024;
const auditFilterStorageKey = "ai-jsunpack.auditFilters.v1";
const defaultAuditFilters: AuditFilterState = { category: "all", query: "", status: "all" };
const runtimeComparisonRowHeight = 50;
const runtimeComparisonListHeight = 280;

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

          <section className="workbench-panel graph-panel motion-item" aria-label="Evidence graph">
            <div className="panel-heading padded-heading">
              <div>
                <p className="panel-kicker">Evidence graph</p>
                <h2>Lineage, chunks, agents</h2>
              </div>
              <GitBranch size={22} aria-hidden="true" />
            </div>
            <EvidenceGraphPanel
              artifacts={artifacts}
              currentJob={currentJob}
              evidence={evidence}
              onArtifactSelect={onEvidenceArtifactSelect}
              selectedArtifactId={selectedArtifact?.id ?? null}
            />
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

function EvidenceGraphPanel({
  artifacts,
  currentJob,
  evidence,
  onArtifactSelect,
  selectedArtifactId
}: {
  artifacts: Artifact[];
  currentJob: Job | null;
  evidence: JobEvidence;
  onArtifactSelect: (artifactId: string) => void;
  selectedArtifactId: string | null;
}) {
  const [mode, setMode] = useState<EvidenceGraphMode>("lineage");
  const inventoryArtifact = useMemo(() => latestArtifactOfKind(artifacts, "input_inventory"), [artifacts]);
  const astIndexArtifact = useMemo(() => latestArtifactOfKind(artifacts, "ast_index"), [artifacts]);
  const [sources, setSources] = useState<EvidenceGraphSourceState>({
    astIndexes: null,
    error: null,
    inventory: null,
    status: "idle"
  });

  useEffect(() => {
    if (!currentJob || (!inventoryArtifact && !astIndexArtifact)) {
      setSources({ astIndexes: null, error: null, inventory: null, status: "idle" });
      return;
    }

    const controller = new AbortController();
    let active = true;
    setSources((current) => ({ ...current, error: null, status: "loading" }));

    Promise.all([
      inventoryArtifact
        ? fetchArtifactText(currentJob.id, inventoryArtifact.id, controller.signal).then(parseInputInventoryArtifact)
        : Promise.resolve<InputInventory | null>(null),
      astIndexArtifact
        ? fetchArtifactText(currentJob.id, astIndexArtifact.id, controller.signal).then(parseAstIndexArtifact)
        : Promise.resolve<AstIndex[] | null>(null)
    ])
      .then(([inventory, astIndexes]) => {
        if (active) {
          setSources({ astIndexes, error: null, inventory, status: "ready" });
        }
      })
      .catch((error: Error) => {
        if (active) {
          setSources({ astIndexes: null, error: error.message, inventory: null, status: "error" });
        }
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [currentJob?.id, inventoryArtifact?.id, astIndexArtifact?.id]);

  const graph = useMemo(() => {
    if (mode === "chunks") {
      return buildChunkEvidenceGraph(artifacts, sources.inventory, sources.astIndexes, sources.status);
    }
    if (mode === "agents") {
      return buildAgentFlowGraph(artifacts, evidence);
    }
    return buildArtifactLineageGraph(artifacts, selectedArtifactId);
  }, [artifacts, evidence, mode, selectedArtifactId, sources.astIndexes, sources.inventory, sources.status]);

  return (
    <div className="evidence-graph">
      <div className="graph-toolbar" role="tablist" aria-label="Evidence graph views">
        <GraphModeButton active={mode === "lineage"} icon={Link2} label="Lineage" onClick={() => setMode("lineage")} />
        <GraphModeButton active={mode === "chunks"} icon={GitBranch} label="Chunks" onClick={() => setMode("chunks")} />
        <GraphModeButton active={mode === "agents"} icon={Sparkles} label="Agents" onClick={() => setMode("agents")} />
      </div>

      <div className="graph-summary-grid" aria-label="Graph summary">
        <GraphMetric label="Nodes" value={String(graph.nodes.length)} />
        <GraphMetric label="Edges" value={String(graph.edges.length)} />
        <GraphMetric label="Mode" value={graph.title} />
      </div>

      {mode === "chunks" && sources.status === "loading" ? (
        <div className="preview-message">
          <FileText size={18} aria-hidden="true" />
          Loading inventory and AST index artifacts
        </div>
      ) : null}
      {mode === "chunks" && sources.status === "error" ? (
        <div className="preview-message preview-error">
          <AlertCircle size={18} aria-hidden="true" />
          {sources.error ?? "Chunk evidence could not be loaded; showing artifact-stage fallback."}
        </div>
      ) : null}

      <EvidenceGraphCanvas graph={graph} onArtifactSelect={onArtifactSelect} selectedArtifactId={selectedArtifactId} />
    </div>
  );
}

function GraphModeButton({
  active,
  icon: Icon,
  label,
  onClick
}: {
  active: boolean;
  icon: LucideIcon;
  label: string;
  onClick: () => void;
}) {
  return (
    <button aria-pressed={active} className={active ? "graph-mode graph-mode-active" : "graph-mode"} type="button" onClick={onClick}>
      <Icon size={16} aria-hidden="true" />
      {label}
    </button>
  );
}

function GraphMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="graph-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function EvidenceGraphCanvas({
  graph,
  onArtifactSelect,
  selectedArtifactId
}: {
  graph: EvidenceGraph;
  onArtifactSelect: (artifactId: string) => void;
  selectedArtifactId: string | null;
}) {
  const layout = useMemo(() => layoutEvidenceGraph(graph), [graph]);

  if (graph.nodes.length === 0) {
    return <EmptyState title={graph.title} detail={graph.emptyDetail} />;
  }

  return (
    <div className="graph-viewport" aria-label={graph.summary}>
      <div className="graph-canvas" style={{ height: `${layout.height}px`, width: `${layout.width}px` }}>
        <svg className="graph-edges" height={layout.height} width={layout.width} aria-hidden="true">
          <defs>
            <marker id="graph-arrow" markerHeight="8" markerWidth="8" orient="auto" refX="7" refY="4">
              <path d="M0,0 L8,4 L0,8 Z" />
            </marker>
          </defs>
          {layout.edges.map((edge) => (
            <path className="graph-edge-path" d={edge.path} key={edge.id} markerEnd="url(#graph-arrow)" />
          ))}
        </svg>
        {layout.nodes.map((node) => {
          const isActive = node.artifactId === selectedArtifactId;
          const className = [
            "graph-node",
            `graph-node-${node.kind}`,
            `graph-node-${node.tone ?? "neutral"}`,
            isActive ? "graph-node-selected" : ""
          ]
            .filter(Boolean)
            .join(" ");
          const style = { left: `${node.x}px`, top: `${node.y}px` };
          const content = (
            <>
              <span>{node.title}</span>
              <small>{node.detail}</small>
            </>
          );

          return node.artifactId ? (
            <button className={className} key={node.id} style={style} type="button" onClick={() => onArtifactSelect(node.artifactId!)}>
              {content}
            </button>
          ) : (
            <div className={className} key={node.id} style={style}>
              {content}
            </div>
          );
        })}
      </div>
      <div className="graph-edge-list" aria-label="Graph edge list">
        {graph.edges.slice(0, 12).map((edge) => (
          <span key={edge.id}>{edge.label}</span>
        ))}
        {graph.edges.length > 12 ? <span>{graph.edges.length - 12} more edges</span> : null}
      </div>
    </div>
  );
}

function ArtifactPreviewPane({ artifact, preview }: { artifact: Artifact; preview: ArtifactPreview }) {
  const isCurrentPreview = preview.artifactId === artifact.id;
  const status = isCurrentPreview ? preview.status : "idle";
  const language = preview.text ? artifactPreviewLanguage(artifact, preview.text) : "plaintext";

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
      {status === "ready" && preview.text ? (
        <Suspense
          fallback={
            <div className="preview-message">
              <FileText size={18} aria-hidden="true" />
              Loading editor
            </div>
          }
        >
          <ArtifactTextEditor ariaLabel={`${artifact.kind} artifact content preview`} language={language} text={preview.text} />
        </Suspense>
      ) : null}
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
  const [reportDetailQuery, setReportDetailQuery] = useState("");

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
  const packageContents = evidenceIndex.payload?.packageContents ?? [];
  const reportSections = evidenceIndex.payload?.reportSections ?? [];
  const failureSummary =
    evidenceIndex.payload?.failureSummary.length ? evidenceIndex.payload.failureSummary : buildFailureSummary(evidence, currentJob);
  const indexedPackageContents = packageContents.length > 0 ? packageContents : buildFallbackPackageContents(attachments);
  const indexedReportSections = reportSections.length > 0 ? reportSections : buildFallbackReportSections(attachments, artifacts);
  const filteredReportSections = filterReportSections(indexedReportSections, reportDetailQuery);
  const reportDetailFilterActive = reportDetailQuery.trim().length > 0;
  const riskCount = failureSummary.length || reviewAttention;

  return (
    <div className="report-list">
      <div className="report-summary-grid" aria-label="Report output summary">
        <ReportMetric label="Markdown" value={String(markdownReports)} />
        <ReportMetric label="HTML" value={String(htmlReports)} />
        <ReportMetric label="Packages" value={String(packages)} />
        <ReportMetric label="Evidence files" value={String(evidenceIndex.payload?.includedCount ?? 0)} />
      </div>

      <div className={riskCount > 0 ? "report-risk-strip warning" : "report-risk-strip"}>
        <AlertCircle size={17} aria-hidden="true" />
        <div>
          <strong>{currentJob?.failureClass === "none" ? "No job failure class" : currentJob?.failureClass ?? "Awaiting job"}</strong>
          <span>
            {riskCount > 0
              ? `${riskCount} packaged failure or review item${riskCount === 1 ? "" : "s"} need attention.`
              : currentJob?.failureReason ?? "Build, review, and runtime evidence decide final package confidence."}
          </span>
        </div>
      </div>

      <section className="report-detail-block" aria-label="Failure summary">
        <div className="section-heading">
          <h3>Failure summary</h3>
          <span>{failureSummary.length}</span>
        </div>
        {failureSummary.length > 0 ? (
          <div className="report-issue-list">
            {failureSummary.map((item, index) => (
              <div className="report-issue-row" key={`${item.group}-${item.status}-${item.failureClass}-${index}`}>
                <div>
                  <strong>{item.group}</strong>
                  <span>{item.decision}</span>
                </div>
                <StatusToken status={item.status} />
                <small>{item.failureClass}</small>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState title="No packaged failures" detail="The latest packaged build, runtime, and review evidence did not add failure observations." />
        )}
      </section>

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

      <section className="report-detail-block" aria-label="Package contents">
        <div className="section-heading">
          <h3>Package contents</h3>
          <span>{evidenceIndex.status === "ready" ? indexedPackageContents.length : evidenceIndex.status}</span>
        </div>
        {evidenceIndex.status === "ready" && indexedPackageContents.length > 0 ? (
          <div className="package-content-list">
            {indexedPackageContents.map((item) => {
              const linkedArtifact = item.artifactId ? artifactsById.get(item.artifactId) : null;
              return (
                <div className="package-content-row" key={`${item.path}-${item.artifactId ?? item.source}`}>
                  <FileJson2 size={17} aria-hidden="true" />
                  <div>
                    <strong>{item.path}</strong>
                    <span>{item.description || item.reason}</span>
                    <small>
                      {item.source}
                      {item.size !== null ? ` / ${formatBytes(item.size)}` : ""}
                    </small>
                  </div>
                  <StatusToken status={item.included ? "pass" : "best_effort"} />
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
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}
        {evidenceIndex.status === "ready" && indexedPackageContents.length === 0 ? (
          <EmptyState title="No package index" detail="Older evidence indexes do not expose structured package contents." />
        ) : null}
      </section>

      <section className="report-detail-block" aria-label="Report evidence map">
        <div className="section-heading">
          <h3>Report evidence map</h3>
          <span>
            {evidenceIndex.status === "ready"
              ? reportDetailFilterActive
                ? `${filteredReportSections.length}/${indexedReportSections.length}`
                : indexedReportSections.length
              : evidenceIndex.status}
          </span>
        </div>
        {evidenceIndex.status === "ready" && indexedReportSections.length > 0 ? (
          <>
            <div className="report-detail-filter">
              <Filter size={16} aria-hidden="true" />
              <span className="visually-hidden">Filter report details</span>
              <input
                aria-label="Filter report details"
                placeholder="Filter details, status, artifacts"
                type="search"
                value={reportDetailQuery}
                onChange={(event) => setReportDetailQuery(event.target.value)}
              />
              {reportDetailFilterActive ? (
                <button className="secondary-action compact" type="button" onClick={() => setReportDetailQuery("")}>Clear</button>
              ) : null}
            </div>
            {filteredReportSections.length > 0 ? (
              <div className="report-section-list">
                {filteredReportSections.map((section) => (
                  <div className="report-section-row" key={section.anchor}>
                    <ListTree size={17} aria-hidden="true" />
                    <div>
                      <strong>{section.title}</strong>
                      <span>#{section.anchor}</span>
                      <small>{section.summary}</small>
                      {section.details.length > 0 ? (
                        <div className="report-section-detail-list">
                          {section.details.map((detail) => (
                            <div className="report-section-detail-row" key={`${section.anchor}-${detail.label}-${detail.value}`}>
                              <div>
                                <span>{detail.label}</span>
                                <strong>{detail.value}</strong>
                                <small>{reportSectionDetailSummary(detail)}</small>
                              </div>
                              {detail.status ? <StatusToken status={detail.status} /> : null}
                            </div>
                          ))}
                        </div>
                      ) : null}
                    </div>
                    <div className="evidence-link-group">
                      {section.artifactIds.slice(0, 5).map((artifactId) => {
                        const linkedArtifact = artifactsById.get(artifactId);
                        return (
                          <button
                            className="evidence-ref-chip"
                            disabled={!linkedArtifact}
                            key={artifactId}
                            type="button"
                            onClick={() => (linkedArtifact ? onArtifactSelect(linkedArtifact.id) : undefined)}
                          >
                            <Link2 size={14} aria-hidden="true" />
                            {linkedArtifact?.kind ?? "artifact"}
                            <small>{artifactId}</small>
                          </button>
                        );
                      })}
                      {section.artifactIds.length > 5 ? <span className="report-overflow-note">{section.artifactIds.length - 5} more</span> : null}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState title="No matching report details" detail="Adjust the report evidence map filter to show packaged section details." />
            )}
          </>
        ) : null}
        {evidenceIndex.status === "ready" && indexedReportSections.length === 0 ? (
          <EmptyState title="No report map" detail="Older evidence indexes do not expose report section mappings." />
        ) : null}
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
  const [filters, setFilters] = useState<AuditFilterState>(defaultAuditFilters);
  const [savedFilters, setSavedFilters] = useState<SavedAuditFilter[]>(() => readSavedAuditFilters());
  const [selectedSavedFilterId, setSelectedSavedFilterId] = useState("");
  const [filterName, setFilterName] = useState("");
  const [selectedRecordIds, setSelectedRecordIds] = useState<Set<string>>(() => new Set());
  const auditRecords = useMemo(() => buildAuditRecords(evidence), [evidence]);
  const filteredRecords = useMemo(
    () => auditRecords.filter((record) => auditRecordMatches(record, filters)),
    [auditRecords, filters]
  );
  const groupedRecords = useMemo(() => groupAuditRecordsByRisk(filteredRecords), [filteredRecords]);
  const selectedRecords = useMemo(
    () => auditRecords.filter((record) => selectedRecordIds.has(record.id)),
    [auditRecords, selectedRecordIds]
  );
  const visibleSelectedCount = filteredRecords.filter((record) => selectedRecordIds.has(record.id)).length;
  const allVisibleSelected = filteredRecords.length > 0 && visibleSelectedCount === filteredRecords.length;
  const attentionCount = auditRecords.filter((record) => auditRecordNeedsAttention(record)).length;

  useEffect(() => {
    const knownIds = new Set(auditRecords.map((record) => record.id));
    setSelectedRecordIds((current) => {
      const next = new Set([...current].filter((recordId) => knownIds.has(recordId)));
      return next.size === current.size ? current : next;
    });
  }, [auditRecords]);

  const handleFilterSave = () => {
    const name = filterName.trim() || auditFilterLabel(filters);
    const existingId = selectedSavedFilterId && savedFilters.some((saved) => saved.id === selectedSavedFilterId)
      ? selectedSavedFilterId
      : "";
    const savedFilter: SavedAuditFilter = {
      createdAt: new Date().toISOString(),
      filters,
      id: existingId || `audit-filter-${Date.now()}`,
      name
    };
    const nextSavedFilters = existingId
      ? savedFilters.map((saved) => (saved.id === existingId ? savedFilter : saved))
      : [...savedFilters, savedFilter];
    setSavedFilters(nextSavedFilters);
    setSelectedSavedFilterId(savedFilter.id);
    setFilterName(name);
    persistSavedAuditFilters(nextSavedFilters);
  };

  const handleSavedFilterLoad = (filterId: string) => {
    setSelectedSavedFilterId(filterId);
    const saved = savedFilters.find((item) => item.id === filterId);
    if (!saved) {
      return;
    }
    setFilters(saved.filters);
    setFilterName(saved.name);
  };

  const handleSavedFilterDelete = () => {
    if (!selectedSavedFilterId) {
      return;
    }
    const nextSavedFilters = savedFilters.filter((saved) => saved.id !== selectedSavedFilterId);
    setSavedFilters(nextSavedFilters);
    setSelectedSavedFilterId("");
    setFilterName("");
    persistSavedAuditFilters(nextSavedFilters);
  };

  const handleVisibleSelectionToggle = () => {
    const visibleIds = filteredRecords.map((record) => record.id);
    setSelectedRecordIds((current) => {
      const next = new Set(current);
      if (allVisibleSelected) {
        visibleIds.forEach((recordId) => next.delete(recordId));
      } else {
        visibleIds.forEach((recordId) => next.add(recordId));
      }
      return next;
    });
  };

  const handleRecordSelectionToggle = (recordId: string) => {
    setSelectedRecordIds((current) => {
      const next = new Set(current);
      if (next.has(recordId)) {
        next.delete(recordId);
      } else {
        next.add(recordId);
      }
      return next;
    });
  };

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
        <div className="audit-saved-toolbar" aria-label="Saved audit filters">
          <label htmlFor="audit-saved-filter">
            <span>Saved filter</span>
            <select
              id="audit-saved-filter"
              name="audit-saved-filter"
              value={selectedSavedFilterId}
              onChange={(event) => handleSavedFilterLoad(event.currentTarget.value)}
            >
              <option value="">Manual filters</option>
              {savedFilters.map((saved) => (
                <option key={saved.id} value={saved.id}>
                  {saved.name}
                </option>
              ))}
            </select>
          </label>
          <label htmlFor="audit-filter-name">
            <span>Filter name</span>
            <input
              id="audit-filter-name"
              name="audit-filter-name"
              value={filterName}
              onChange={(event) => setFilterName(event.currentTarget.value)}
              placeholder={auditFilterLabel(filters)}
            />
          </label>
          <button className="secondary-action compact" type="button" onClick={handleFilterSave}>
            <Save size={16} aria-hidden="true" />
            Save filter
          </button>
          <button
            className="secondary-action compact"
            type="button"
            disabled={!selectedSavedFilterId}
            onClick={handleSavedFilterDelete}
          >
            <Trash2 size={16} aria-hidden="true" />
            Delete
          </button>
        </div>
        <div className="audit-bulk-toolbar" aria-label="Audit bulk actions">
          <button className="secondary-action compact" type="button" disabled={filteredRecords.length === 0} onClick={handleVisibleSelectionToggle}>
            <Filter size={16} aria-hidden="true" />
            {allVisibleSelected ? "Clear visible" : "Select visible"}
          </button>
          <button className="secondary-action compact" type="button" disabled={selectedRecordIds.size === 0} onClick={() => setSelectedRecordIds(new Set())}>
            <XCircle size={16} aria-hidden="true" />
            Clear selection
          </button>
          <button
            className="secondary-action compact"
            type="button"
            disabled={!currentJob || selectedRecords.length === 0}
            onClick={() =>
              currentJob
                ? downloadJsonFile(`ai-jsunpack-${currentJob.id}-audit-selected.json`, {
                    exportedAt: new Date().toISOString(),
                    jobId: currentJob.id,
                    filters,
                    selectedCount: selectedRecords.length,
                    records: selectedRecords
                  })
                : undefined
            }
          >
            <Download size={16} aria-hidden="true" />
            Export selected
          </button>
          <span>
            {visibleSelectedCount} visible selected / {selectedRecords.length} total selected
          </span>
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
          <div>
            <span>Selected</span>
            <strong>{selectedRecords.length}</strong>
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
                  <th>
                    <input
                      aria-label={allVisibleSelected ? "Clear all visible audit records" : "Select all visible audit records"}
                      checked={allVisibleSelected}
                      disabled={filteredRecords.length === 0}
                      onChange={handleVisibleSelectionToggle}
                      type="checkbox"
                    />
                  </th>
                  <th>Type</th>
                  <th>Subject</th>
                  <th>Status</th>
                  <th>Evidence</th>
                  <th>Artifacts</th>
                </tr>
              </thead>
              <tbody>
                {groupedRecords.map((group) =>
                  group.records.length > 0 ? (
                    <AuditRiskGroupRows
                      group={group}
                      key={group.id}
                      onArtifactSelect={onArtifactSelect}
                      onRecordSelectionToggle={handleRecordSelectionToggle}
                      selectedRecordIds={selectedRecordIds}
                    />
                  ) : null
                )}
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

function AuditRiskGroupRows({
  group,
  onArtifactSelect,
  onRecordSelectionToggle,
  selectedRecordIds
}: {
  group: AuditRiskGroup;
  onArtifactSelect: (artifactId: string) => void;
  onRecordSelectionToggle: (recordId: string) => void;
  selectedRecordIds: Set<string>;
}) {
  return (
    <>
      <tr className={`audit-group-row audit-group-${group.id}`}>
        <td colSpan={6}>
          <strong>{group.title}</strong>
          <span>
            {group.records.length} record{group.records.length === 1 ? "" : "s"} / {group.detail}
          </span>
        </td>
      </tr>
      {group.records.map((record) => (
        <tr className={selectedRecordIds.has(record.id) ? "audit-selected-row" : undefined} key={record.id}>
          <td>
            <input
              aria-label={`Select audit record ${record.label}`}
              checked={selectedRecordIds.has(record.id)}
              onChange={() => onRecordSelectionToggle(record.id)}
              type="checkbox"
            />
          </td>
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
    </>
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
            apiBaseUrl={apiBaseUrl}
            artifacts={artifacts}
            currentJob={currentJob}
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
  apiBaseUrl,
  artifacts,
  currentJob,
  onArtifactSelect
}: {
  apiBaseUrl: string;
  artifacts: Artifact[];
  currentJob: Job | null;
  onArtifactSelect: (artifactId: string) => void;
}) {
  const comparisonArtifacts = useMemo(
    () => artifacts.filter((artifact) => artifact.kind === "runtime_comparison"),
    [artifacts]
  );
  const [selectedComparisonId, setSelectedComparisonId] = useState<string | null>(null);
  const [comparison, setComparison] = useState<RuntimeComparisonState>({
    error: null,
    reports: [],
    status: "idle"
  });
  const [filters, setFilters] = useState<RuntimeComparisonFilters>({ scenario: "all", status: "all", viewport: "all" });
  const [listScrollTop, setListScrollTop] = useState(0);

  useEffect(() => {
    if (!currentJob || comparisonArtifacts.length === 0) {
      setComparison({ error: null, reports: [], status: "idle" });
      return;
    }

    const controller = new AbortController();
    let active = true;
    setComparison({ error: null, reports: [], status: "loading" });
    Promise.all(
      comparisonArtifacts.map((artifact) =>
        fetchArtifactText(currentJob.id, artifact.id, controller.signal).then((text) => ({
          artifactId: artifact.id,
          report: parseRuntimeComparisonReport(text)
        }))
      )
    )
      .then((reports) => {
        if (active) {
          setComparison({ error: null, reports, status: "ready" });
        }
      })
      .catch((error: Error) => {
        if (active) {
          setComparison({ error: error.message, reports: [], status: "error" });
        }
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [currentJob?.id, comparisonArtifacts]);

  const scenarioOptions = useMemo(() => uniqueRuntimeComparisonScenarios(comparison.reports), [comparison.reports]);
  const viewportOptions = useMemo(() => uniqueRuntimeComparisonViewports(comparison.reports), [comparison.reports]);
  const filteredReports = useMemo(
    () => comparison.reports.filter((item) => runtimeComparisonMatchesFilters(item.report, filters)),
    [comparison.reports, filters]
  );
  const virtualRange = useMemo(
    () => virtualListRange(filteredReports.length, listScrollTop, runtimeComparisonRowHeight, runtimeComparisonListHeight),
    [filteredReports.length, listScrollTop]
  );
  const visibleReports = filteredReports.slice(virtualRange.start, virtualRange.end);
  const selectedComparison =
    filteredReports.find((item) => item.artifactId === selectedComparisonId) ?? filteredReports[0] ?? null;
  const report = selectedComparison?.report ?? null;
  const differences = report?.differences ?? null;

  return (
    <div className="runtime-compare-status runtime-compare-detail">
      <div className="runtime-compare-heading">
        <div>
          <span>Runtime diff</span>
          <strong>
            {comparisonArtifacts.length > 0
              ? `${comparisonArtifacts.length} comparison artifact${comparisonArtifacts.length === 1 ? "" : "s"} recorded`
              : "Waiting for runtime_compare evidence"}
          </strong>
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
      {comparisonArtifacts.length === 0 ? (
        <span className="runtime-compare-note">Runtime compare has not produced a linked artifact yet.</span>
      ) : null}
      {comparison.reports.length > 1 ? (
        <>
          <div className="runtime-filter-grid" aria-label="Runtime comparison filters">
            <label htmlFor="runtime-scenario-filter">
              <span>Scenario</span>
              <select
                id="runtime-scenario-filter"
                value={filters.scenario}
                onChange={(event) => {
                  setListScrollTop(0);
                  setFilters((current) => ({ ...current, scenario: event.currentTarget.value }));
                }}
              >
                <option value="all">All scenarios</option>
                {scenarioOptions.map((scenario) => (
                  <option key={scenario} value={scenario}>
                    {scenario}
                  </option>
                ))}
              </select>
            </label>
            <label htmlFor="runtime-viewport-filter">
              <span>Viewport</span>
              <select
                id="runtime-viewport-filter"
                value={filters.viewport}
                onChange={(event) => {
                  setListScrollTop(0);
                  setFilters((current) => ({ ...current, viewport: event.currentTarget.value }));
                }}
              >
                <option value="all">All viewports</option>
                {viewportOptions.map((viewport) => (
                  <option key={viewport.value} value={viewport.value}>
                    {viewport.label}
                  </option>
                ))}
              </select>
            </label>
            <label htmlFor="runtime-status-filter">
              <span>Status</span>
              <select
                id="runtime-status-filter"
                value={filters.status}
                onChange={(event) => {
                  setListScrollTop(0);
                  setFilters((current) => ({
                    ...current,
                    status: event.currentTarget.value as RuntimeComparisonFilters["status"]
                  }));
                }}
              >
                <option value="all">All statuses</option>
                <option value="pass">Pass</option>
                <option value="best_effort">Best effort</option>
                <option value="retry">Retry</option>
                <option value="fail">Fail</option>
              </select>
            </label>
          </div>
          <div className="runtime-compare-note">
            Showing {filteredReports.length} of {comparison.reports.length} comparison rows.
          </div>
          {filteredReports.length > 0 ? (
            <div
              className="runtime-comparison-viewport"
              onScroll={(event) => setListScrollTop(event.currentTarget.scrollTop)}
              style={{ maxHeight: runtimeComparisonListHeight }}
            >
              <div className="runtime-comparison-spacer" style={{ height: filteredReports.length * runtimeComparisonRowHeight }}>
                <div className="runtime-comparison-list" style={{ transform: `translateY(${virtualRange.start * runtimeComparisonRowHeight}px)` }} aria-label="Runtime comparison matrix">
                  {visibleReports.map((item) => (
                    <button
                      className={
                        item.artifactId === selectedComparison?.artifactId ? "runtime-comparison-row row-active" : "runtime-comparison-row"
                      }
                      key={item.artifactId}
                      style={{ minHeight: runtimeComparisonRowHeight }}
                      type="button"
                      onClick={() => setSelectedComparisonId(item.artifactId)}
                    >
                      <Eye size={15} aria-hidden="true" />
                      <span>{runtimeComparisonScopeLabel(item.report)}</span>
                      <small>{formatScreenshotDiff(item.report.differences)}</small>
                      <StatusToken status={item.report.status} />
                    </button>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <EmptyState title="No comparison rows" detail="Adjust the runtime comparison filters." />
          )}
        </>
      ) : null}
      {selectedComparison && report && differences ? (
        <>
          <div className="runtime-diff-grid" aria-label="Runtime comparison difference summary">
            <RuntimeDiffMetric label="Scope" value={runtimeComparisonScopeLabel(report)} />
            <RuntimeDiffMetric label="Screenshot" value={formatScreenshotDiff(differences)} />
            <RuntimeDiffMetric label="Pixels" value={formatPixelDiff(differences)} />
            <RuntimeDiffMetric label="DOM paths" value={String(differences.domDifferences.length)} />
            <RuntimeDiffMetric label="Network groups" value={formatRuntimeGroups(differences.networkDiff.groups)} />
            <RuntimeDiffMetric label="Console groups" value={formatRuntimeGroups(differences.consoleDiff.groups)} />
          </div>
          <RuntimeComparisonEvidenceButtons
            artifacts={artifacts}
            comparisonArtifactId={selectedComparison.artifactId}
            onArtifactSelect={onArtifactSelect}
            report={report}
          />
          <RuntimeScreenshotPreview
            apiBaseUrl={apiBaseUrl}
            artifacts={artifacts}
            currentJob={currentJob}
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
    ...report.screenshotArtifactIds,
    report.differences.screenshotDiff.diffArtifactId
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

function RuntimeScreenshotPreview({
  apiBaseUrl,
  artifacts,
  currentJob,
  onArtifactSelect,
  report
}: {
  apiBaseUrl: string;
  artifacts: Artifact[];
  currentJob: Job | null;
  onArtifactSelect: (artifactId: string) => void;
  report: RuntimeComparisonReport;
}) {
  const screenshotItems = runtimeScreenshotPreviewItems(report)
    .map((item) => ({
      ...item,
      artifact: artifacts.find((artifact) => artifact.id === item.artifactId) ?? null
    }))
    .filter((item) => item.artifact);

  if (!currentJob || screenshotItems.length === 0) {
    return <RuntimeDiffSection title="Screenshot previews" items={["No linked screenshot artifacts are available for inline preview."]} />;
  }

  return (
    <div className="runtime-screenshot-grid" aria-label="Runtime screenshot previews">
      {screenshotItems.map((item) => (
        <figure className="runtime-screenshot-card" key={`${item.label}-${item.artifactId}`}>
          <img
            alt={`${item.label} screenshot evidence`}
            loading="lazy"
            src={artifactDownloadUrl(apiBaseUrl, currentJob.id, item.artifactId)}
          />
          <figcaption>
            <strong>{item.label}</strong>
            <span>{item.detail}</span>
            <button className="download-link" type="button" onClick={() => onArtifactSelect(item.artifactId)}>
              <Link2 size={14} aria-hidden="true" />
              Locate artifact
            </button>
          </figcaption>
        </figure>
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
  const attachments = payload.attachments.map(normalizeEvidenceAttachment);
  return {
    attachments,
    failureSummary: Array.isArray(payload.failureSummary) ? payload.failureSummary.map(normalizeFailureSummary) : [],
    includedCount: payload.includedCount ?? attachments.filter((item) => item.included).length,
    jobId: payload.jobId ?? "",
    kind: "evidence_index",
    omittedCount: payload.omittedCount ?? attachments.filter((item) => !item.included).length,
    packageContents: Array.isArray(payload.packageContents) ? payload.packageContents.map(normalizePackageContent) : [],
    reportSections: Array.isArray(payload.reportSections) ? payload.reportSections.map(normalizeReportSection) : [],
    schemaVersion: payload.schemaVersion ?? "unknown"
  };
}

function normalizeEvidenceAttachment(value: EvidenceAttachmentEntry): EvidenceAttachmentEntry {
  return {
    artifactId: String(value.artifactId ?? ""),
    contentType: String(value.contentType ?? "application/octet-stream"),
    hash: String(value.hash ?? ""),
    included: Boolean(value.included),
    kind: String(value.kind ?? "unknown"),
    packagePath: typeof value.packagePath === "string" ? value.packagePath : null,
    reason: String(value.reason ?? ""),
    retentionClass: typeof value.retentionClass === "string" ? value.retentionClass : undefined,
    sensitivityClass: typeof value.sensitivityClass === "string" ? value.sensitivityClass : undefined,
    size: typeof value.size === "number" ? value.size : 0,
    sourceFilename: String(value.sourceFilename ?? ""),
    stage: String(value.stage ?? "packaging")
  };
}

function normalizePackageContent(value: PackageContentEntry): PackageContentEntry {
  return {
    artifactId: typeof value.artifactId === "string" ? value.artifactId : null,
    contentType: String(value.contentType ?? "application/octet-stream"),
    description: String(value.description ?? ""),
    included: value.included !== false,
    path: String(value.path ?? ""),
    reason: String(value.reason ?? ""),
    size: typeof value.size === "number" ? value.size : null,
    source: String(value.source ?? "package")
  };
}

function normalizeReportSection(value: ReportSectionEntry): ReportSectionEntry {
  const rawDetails: unknown[] = Array.isArray(value.details) ? value.details : [];
  return {
    anchor: String(value.anchor ?? ""),
    artifactIds: Array.isArray(value.artifactIds) ? value.artifactIds.map(String) : [],
    artifactKinds: Array.isArray(value.artifactKinds) ? value.artifactKinds.map(String) : [],
    details: rawDetails.filter(isRecord).map(normalizeReportSectionDetail),
    evidenceLinks: Array.isArray(value.evidenceLinks) ? value.evidenceLinks.map(String) : [],
    summary: String(value.summary ?? ""),
    title: String(value.title ?? "Report section")
  };
}

function normalizeReportSectionDetail(value: Record<string, unknown>): ReportSectionDetailEntry {
  return {
    details: isRecord(value.details) ? value.details : {},
    label: String(value.label ?? "Detail"),
    status: typeof value.status === "string" ? value.status : undefined,
    value: String(value.value ?? "")
  };
}

function normalizeFailureSummary(value: FailureSummaryEntry): FailureSummaryEntry {
  return {
    decision: String(value.decision ?? "Validation did not fully pass."),
    failureClass: String(value.failureClass ?? "unknown"),
    group: String(value.group ?? "reports"),
    status: String(value.status ?? "best_effort")
  };
}

function buildFailureSummary(evidence: JobEvidence, currentJob: Job | null): FailureSummaryEntry[] {
  const reviewItems = evidence.reviewRuns
    .filter((run) => run.failureClass !== "none" || ["best_effort", "fail", "retry"].includes(run.status))
    .map((run): FailureSummaryEntry => ({
      decision: run.decision,
      failureClass: run.failureClass,
      group: run.reviewType,
      status: run.status
    }));
  if (reviewItems.length > 0) {
    return reviewItems;
  }
  if (currentJob?.failureClass && currentJob.failureClass !== "none") {
    return [
      {
        decision: currentJob.failureReason ?? "Job completed with a non-none failure class.",
        failureClass: currentJob.failureClass,
        group: "job",
        status: currentJob.status
      }
    ];
  }
  return [];
}

function buildFallbackPackageContents(attachments: EvidenceAttachmentEntry[]): PackageContentEntry[] {
  const fixedEntries: PackageContentEntry[] = [
    fallbackPackageContent("audit-report.md", "audit_report", "Human-readable Markdown audit report."),
    fallbackPackageContent("audit-report.html", "html_report", "Offline HTML audit report."),
    fallbackPackageContent("audit.json", "audit_payload", "Structured audit payload."),
    fallbackPackageContent("evidence-index.json", "evidence_index", "Evidence attachment index."),
    fallbackPackageContent("artifact-manifest.json", "artifact_manifest", "Artifact manifest."),
    fallbackPackageContent("runtime-report.json", "runtime_validation", "Runtime validation records."),
    fallbackPackageContent("review-runs.json", "review_run", "Review records.")
  ];
  return [
    ...fixedEntries,
    ...attachments.map((attachment): PackageContentEntry => ({
      artifactId: attachment.artifactId,
      contentType: attachment.contentType,
      description: "Evidence attachment collected into the result package.",
      included: attachment.included,
      path: attachment.packagePath ?? `evidence/${attachment.kind}/${attachment.artifactId}`,
      reason: attachment.reason,
      size: attachment.size,
      source: String(attachment.kind)
    }))
  ];
}

function fallbackPackageContent(path: string, source: string, description: string): PackageContentEntry {
  return {
    artifactId: null,
    contentType: path.endsWith(".html") ? "text/html" : "application/json",
    description,
    included: true,
    path,
    reason: "included",
    size: null,
    source
  };
}

function buildFallbackReportSections(attachments: EvidenceAttachmentEntry[], artifacts: Artifact[]): ReportSectionEntry[] {
  const artifactIdsByKind = new Map<string, string[]>();
  artifacts.forEach((artifact) => {
    artifactIdsByKind.set(artifact.kind, [...(artifactIdsByKind.get(artifact.kind) ?? []), artifact.id]);
  });
  const attachmentIds = attachments.filter((item) => item.included).map((item) => item.artifactId);
  return [
    fallbackReportSection("Completion Decision", "completion-decision", "Final packaging decision.", ["audit_report", "html_report"], artifactIdsByKind),
    fallbackReportSection(
      "Risk And Failure Groups",
      "risk-and-failure-groups",
      "Failing or best-effort observations.",
      ["build_artifact", "runtime_validation", "review_run"],
      artifactIdsByKind
    ),
    fallbackReportSection(
      "Runtime Compare Difference Summary",
      "runtime-compare-difference-summary",
      "Runtime comparison differences and related evidence.",
      ["runtime_comparison", "runtime_scenario", "runtime_trace", "runtime_screenshot"],
      artifactIdsByKind
    ),
    {
      anchor: "evidence-attachment-index",
      artifactIds: attachmentIds,
      artifactKinds: [],
      details: [],
      evidenceLinks: attachmentIds.map((artifactId) => `artifact://${artifactId}`),
      summary: "Evidence files included in or omitted from the result package.",
      title: "Evidence Attachment Index"
    },
    fallbackReportSection("Reproduction", "reproduction", "Offline inspection commands.", ["result_package", "evidence_index"], artifactIdsByKind)
  ];
}

function fallbackReportSection(
  title: string,
  anchor: string,
  summary: string,
  kinds: string[],
  artifactIdsByKind: Map<string, string[]>
): ReportSectionEntry {
  const artifactIds = kinds.flatMap((kind) => artifactIdsByKind.get(kind) ?? []);
  return {
    anchor,
    artifactIds,
    artifactKinds: kinds,
    details: [],
    evidenceLinks: artifactIds.map((artifactId) => `artifact://${artifactId}`),
    summary,
    title
  };
}

function parseRuntimeComparisonReport(text: string): RuntimeComparisonReport {
  const payload = JSON.parse(text) as Partial<RuntimeComparisonReport>;
  if (!payload.id || !payload.differences || !payload.original || !payload.reconstructed) {
    throw new Error("Artifact is not a valid runtime comparison report.");
  }
  return payload as RuntimeComparisonReport;
}

function readSavedAuditFilters(): SavedAuditFilter[] {
  if (typeof window === "undefined") {
    return [];
  }
  try {
    const raw = window.localStorage.getItem(auditFilterStorageKey);
    if (!raw) {
      return [];
    }
    const payload = JSON.parse(raw);
    if (!Array.isArray(payload)) {
      return [];
    }
    return payload
      .filter(isRecord)
      .map((item): SavedAuditFilter | null => {
        if (typeof item.id !== "string" || typeof item.name !== "string" || !isRecord(item.filters)) {
          return null;
        }
        return {
          createdAt: typeof item.createdAt === "string" ? item.createdAt : new Date().toISOString(),
          filters: sanitizeAuditFilters(item.filters),
          id: item.id,
          name: item.name
        };
      })
      .filter((item): item is SavedAuditFilter => Boolean(item));
  } catch {
    return [];
  }
}

function persistSavedAuditFilters(filters: SavedAuditFilter[]): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(auditFilterStorageKey, JSON.stringify(filters));
}

function sanitizeAuditFilters(value: Record<string, unknown>): AuditFilterState {
  const category = value.category === "inference" || value.category === "review" || value.category === "tool" ? value.category : "all";
  const status = value.status === "attention" || value.status === "pass" || value.status === "fail" ? value.status : "all";
  return {
    category,
    query: typeof value.query === "string" ? value.query : "",
    status
  };
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

function artifactPreviewLanguage(artifact: Artifact, text: string): string {
  const contentType = artifact.contentType.toLowerCase();
  const hint = `${artifact.kind} ${artifact.storageUri} ${artifact.id}`.toLowerCase();
  const trimmed = text.trimStart();

  if (contentType.includes("json") || trimmed.startsWith("{") || trimmed.startsWith("[")) {
    return "json";
  }
  if (contentType.includes("markdown") || hasArtifactExtension(hint, [".md", ".markdown"]) || artifact.kind === "audit_report") {
    return "markdown";
  }
  if (contentType.includes("typescript") || hasArtifactExtension(hint, [".ts", ".tsx"])) {
    return "typescript";
  }
  if (contentType.includes("javascript") || hasArtifactExtension(hint, [".js", ".jsx", ".mjs", ".cjs"])) {
    return "javascript";
  }
  if (contentType.includes("html") || hasArtifactExtension(hint, [".html", ".htm"])) {
    return "html";
  }
  if (contentType.includes("css") || hasArtifactExtension(hint, [".css", ".scss", ".less"])) {
    return "css";
  }
  if (contentType.includes("xml") || hasArtifactExtension(hint, [".xml", ".svg"])) {
    return "xml";
  }
  return "plaintext";
}

function hasArtifactExtension(value: string, extensions: string[]): boolean {
  return extensions.some((extension) => value.includes(extension));
}

function formatScreenshotDiff(differences: RuntimeComparisonReport["differences"]): string {
  const changed = differences.screenshotDiff.changed ?? differences.screenshotChanged;
  const status = differences.screenshotDiff.pixelDiffStatus;
  return `changed ${String(changed)} / ${status}`;
}

function formatPixelDiff(differences: RuntimeComparisonReport["differences"]): string {
  const changed = differences.screenshotDiff.changedPixelCount;
  const total = differences.screenshotDiff.pixelCount;
  if (changed === undefined || changed === null || total === undefined || total === null) {
    return "unavailable";
  }
  const ratio = differences.screenshotDiff.changedPixelRatio;
  const percent = typeof ratio === "number" ? ` (${formatPercent(ratio)})` : "";
  return `${changed}/${total}${percent}`;
}

function runtimeComparisonScopeLabel(report: RuntimeComparisonReport): string {
  const scope = report.differences.comparisonScope;
  const viewport = scope.viewport;
  const viewportLabel = viewport
    ? `${viewport.name ? `${viewport.name} ` : ""}${viewport.width}x${viewport.height}`
    : "default viewport";
  return `${scope.scenarioName} / ${viewportLabel}`;
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
    return artifactId === report.differences.screenshotDiff.diffArtifactId ? "pixel diff" : "screenshot";
  }
  if (artifactId === report.differences.screenshotDiff.diffArtifactId) {
    return "pixel diff";
  }
  return "comparison";
}

function buildArtifactLineageGraph(artifacts: Artifact[], selectedArtifactId: string | null): EvidenceGraph {
  const sortedArtifacts = [...artifacts].sort(compareArtifactsForGraph);
  const nodes = sortedArtifacts.map((artifact) => ({
    artifactId: artifact.id,
    column: artifactGraphColumn(artifact),
    detail: `${artifact.stage} / ${shortId(artifact.id)}`,
    id: artifactNodeId(artifact.id),
    kind: "artifact" as const,
    title: artifact.kind,
    tone: artifact.id === selectedArtifactId ? ("active" as const) : ("neutral" as const)
  }));
  const knownArtifacts = new Set(sortedArtifacts.map((artifact) => artifact.id));
  const edges = sortedArtifacts.flatMap((artifact) =>
    artifact.parentArtifactIds
      .filter((parentId) => knownArtifacts.has(parentId))
      .map((parentId) => ({
        from: artifactNodeId(parentId),
        id: `artifact-edge:${parentId}:${artifact.id}`,
        label: `${shortId(parentId)} -> ${shortId(artifact.id)}`,
        to: artifactNodeId(artifact.id)
      }))
  );

  return {
    edges,
    emptyDetail: "Create a job and run the worker to populate parent and child artifact evidence.",
    nodes,
    summary: `${nodes.length} artifacts with ${edges.length} lineage links`,
    title: "Artifact lineage"
  };
}

function buildChunkEvidenceGraph(
  artifacts: Artifact[],
  inventory: InputInventory | null,
  astIndexes: AstIndex[] | null,
  sourceStatus: EvidenceGraphSourceState["status"]
): EvidenceGraph {
  if (!inventory && !astIndexes) {
    const fallbackArtifacts = artifacts.filter((artifact) =>
      ["source_input", "input_inventory", "source_index", "ast_index", "runtime_scenario"].includes(artifact.kind)
    );
    return {
      edges: fallbackArtifacts.flatMap((artifact) =>
        artifact.parentArtifactIds.map((parentId) => ({
          from: artifactNodeId(parentId),
          id: `chunk-fallback:${parentId}:${artifact.id}`,
          label: `${shortId(parentId)} -> ${artifact.kind}`,
          to: artifactNodeId(artifact.id)
        }))
      ),
      emptyDetail:
        sourceStatus === "loading"
          ? "Inventory and AST index artifacts are loading."
          : "No input inventory or AST index artifact is available yet.",
      nodes: fallbackArtifacts.map((artifact) => ({
        artifactId: artifact.id,
        column: artifactGraphColumn(artifact),
        detail: `${artifact.stage} / ${shortId(artifact.id)}`,
        id: artifactNodeId(artifact.id),
        kind: "artifact" as const,
        title: artifact.kind,
        tone: "warn" as const
      })),
      summary: "Fallback graph from artifact metadata",
      title: "Chunk graph"
    };
  }

  const nodes: EvidenceGraphNode[] = [
    {
      column: 0,
      detail: inventory?.isSingleBundle ? "single bundle" : "input package",
      id: "chunk:root",
      kind: "resource",
      title: "Input package",
      tone: inventory?.warnings.length ? "warn" : "neutral"
    }
  ];
  const edges: EvidenceGraphEdge[] = [];

  if (inventory) {
    addResourceNodes(nodes, edges, "entry", inventory.entries, 1, "HTML entry");
    addResourceNodes(nodes, edges, "script", inventory.scripts, 2, "Script chunk");
    addResourceNodes(nodes, edges, "style", inventory.styles, 2, "Stylesheet");
    addResourceNodes(nodes, edges, "asset", inventory.assets, 3, "Asset");
    addResourceNodes(nodes, edges, "sourcemap", inventory.sourceMaps, 3, "Source map");
    addResourceNodes(nodes, edges, "manifest", inventory.manifests, 3, "Manifest");
  }

  if (astIndexes) {
    for (const astIndex of astIndexes.slice(0, 10)) {
      const astNodeId = `chunk:ast:${astIndex.filePath}`;
      nodes.push({
        column: 3,
        detail: `${astIndex.symbols.length} symbols / ${astIndex.imports.length} imports`,
        id: astNodeId,
        kind: "analysis",
        title: trimMiddle(astIndex.filePath, 38),
        tone: astIndex.warnings.length ? "warn" : "pass"
      });
      const scriptNodeId = `chunk:script:${astIndex.filePath}`;
      edges.push({
        from: nodes.some((node) => node.id === scriptNodeId) ? scriptNodeId : "chunk:root",
        id: `chunk-edge:ast:${astIndex.filePath}`,
        label: `AST index for ${trimMiddle(astIndex.filePath, 34)}`,
        to: astNodeId
      });
    }
    if (astIndexes.length > 10) {
      nodes.push({
        column: 3,
        detail: `${astIndexes.length - 10} additional AST indexes`,
        id: "chunk:ast:more",
        kind: "analysis",
        title: "More AST files",
        tone: "warn"
      });
      edges.push({ from: "chunk:root", id: "chunk-edge:ast:more", label: "additional AST indexes", to: "chunk:ast:more" });
    }
  }

  return {
    edges,
    emptyDetail: "No chunk, resource, or AST evidence is available.",
    nodes,
    summary: `${nodes.length} chunk/resource nodes built from inventory and AST artifacts`,
    title: "Chunk graph"
  };
}

function buildAgentFlowGraph(artifacts: Artifact[], evidence: JobEvidence): EvidenceGraph {
  const nodes = new Map<string, EvidenceGraphNode>();
  const edges: EvidenceGraphEdge[] = [];
  const artifactsById = new Map(artifacts.map((artifact) => [artifact.id, artifact]));
  const ensureArtifactNode = (artifactId: string, column: number) => {
    if (nodes.has(artifactNodeId(artifactId))) {
      return;
    }
    const artifact = artifactsById.get(artifactId);
    nodes.set(artifactNodeId(artifactId), {
      artifactId,
      column,
      detail: artifact ? `${artifact.stage} / ${shortId(artifact.id)}` : shortId(artifactId),
      id: artifactNodeId(artifactId),
      kind: "artifact",
      title: artifact?.kind ?? "artifact",
      tone: artifact ? "neutral" : "warn"
    });
  };

  for (const record of evidence.inferenceRecords.slice(0, 18)) {
    const nodeId = `agent:inference:${record.id}`;
    nodes.set(nodeId, {
      column: 1,
      detail: `${record.agentName} / ${formatPercent(record.confidence)}`,
      id: nodeId,
      kind: "agent",
      title: record.type,
      tone: statusTokenTone(record.validationStatus)
    });
    for (const artifactId of record.inputArtifactIds) {
      ensureArtifactNode(artifactId, 0);
      edges.push({ from: artifactNodeId(artifactId), id: `agent-edge:${artifactId}:${record.id}:in`, label: "agent input", to: nodeId });
    }
    for (const artifactId of record.outputArtifactIds) {
      ensureArtifactNode(artifactId, 2);
      edges.push({ from: nodeId, id: `agent-edge:${record.id}:${artifactId}:out`, label: "agent output", to: artifactNodeId(artifactId) });
    }
    for (const ref of record.evidenceRefs) {
      ensureArtifactNode(ref.artifactId, 3);
      edges.push({ from: nodeId, id: `agent-edge:${record.id}:${ref.artifactId}:evidence`, label: ref.label, to: artifactNodeId(ref.artifactId) });
    }
  }

  for (const call of evidence.toolCalls.slice(0, 18)) {
    const nodeId = `agent:tool:${call.id}`;
    nodes.set(nodeId, {
      column: 1,
      detail: `${call.caller} / ${formatDuration(call.duration)}`,
      id: nodeId,
      kind: "tool",
      title: call.toolName,
      tone: call.failureClass === "none" ? statusTokenTone(call.status) : "fail"
    });
    for (const artifactId of call.inputArtifactIds) {
      ensureArtifactNode(artifactId, 0);
      edges.push({ from: artifactNodeId(artifactId), id: `tool-edge:${artifactId}:${call.id}:in`, label: "tool input", to: nodeId });
    }
    for (const artifactId of call.outputArtifactIds) {
      ensureArtifactNode(artifactId, 2);
      edges.push({ from: nodeId, id: `tool-edge:${call.id}:${artifactId}:out`, label: "tool output", to: artifactNodeId(artifactId) });
    }
  }

  for (const run of evidence.reviewRuns.slice(0, 18)) {
    const nodeId = `agent:review:${run.id}`;
    nodes.set(nodeId, {
      column: 3,
      detail: `${run.reviewType} / attempt ${run.attempt}`,
      id: nodeId,
      kind: "review",
      title: run.decision,
      tone: run.failureClass === "none" ? statusTokenTone(run.status) : "fail"
    });
    for (const artifactId of [run.logsArtifactId, ...run.repairInstructionIds].filter((value): value is string => Boolean(value))) {
      ensureArtifactNode(artifactId, 2);
      edges.push({ from: artifactNodeId(artifactId), id: `review-edge:${artifactId}:${run.id}`, label: "review evidence", to: nodeId });
    }
    for (const ref of run.evidenceRefs) {
      ensureArtifactNode(ref.artifactId, 2);
      edges.push({ from: artifactNodeId(ref.artifactId), id: `review-edge:${ref.artifactId}:${run.id}:ref`, label: ref.label, to: nodeId });
    }
  }

  return {
    edges,
    emptyDetail: "Agent, review, and tool-call evidence appears after the worker records audit artifacts.",
    nodes: [...nodes.values()],
    summary: `${nodes.size} agent evidence nodes with ${edges.length} links`,
    title: "Agent flow"
  };
}

function addResourceNodes(
  nodes: EvidenceGraphNode[],
  edges: EvidenceGraphEdge[],
  group: string,
  paths: string[],
  column: number,
  title: string
): void {
  for (const filePath of paths.slice(0, 8)) {
    const nodeId = `chunk:${group}:${filePath}`;
    nodes.push({
      column,
      detail: title,
      id: nodeId,
      kind: "resource",
      title: trimMiddle(filePath, 38)
    });
    edges.push({ from: "chunk:root", id: `chunk-edge:${group}:${filePath}`, label: title, to: nodeId });
  }
  if (paths.length > 8) {
    const moreNodeId = `chunk:${group}:more`;
    nodes.push({
      column,
      detail: `${paths.length - 8} additional ${group} records`,
      id: moreNodeId,
      kind: "resource",
      title: `More ${group}`
    });
    edges.push({ from: "chunk:root", id: `chunk-edge:${group}:more`, label: `${group} overflow`, to: moreNodeId });
  }
}

function layoutEvidenceGraph(graph: EvidenceGraph): {
  edges: Array<EvidenceGraphEdge & { path: string }>;
  height: number;
  nodes: Array<EvidenceGraphNode & { x: number; y: number }>;
  width: number;
} {
  const columnWidth = 246;
  const nodeHeight = 72;
  const nodeWidth = 210;
  const rowGap = 18;
  const origin = 24;
  const rowByColumn = new Map<number, number>();
  const laidOutNodes = graph.nodes.map((node) => {
    const row = rowByColumn.get(node.column) ?? 0;
    rowByColumn.set(node.column, row + 1);
    return {
      ...node,
      x: origin + node.column * columnWidth,
      y: origin + row * (nodeHeight + rowGap)
    };
  });
  const maxColumn = Math.max(0, ...laidOutNodes.map((node) => node.column));
  const maxRows = Math.max(1, ...rowByColumn.values());
  const nodesById = new Map(laidOutNodes.map((node) => [node.id, node]));
  const laidOutEdges = graph.edges
    .map((edge) => {
      const from = nodesById.get(edge.from);
      const to = nodesById.get(edge.to);
      if (!from || !to) {
        return null;
      }
      const startX = from.x + nodeWidth;
      const startY = from.y + nodeHeight / 2;
      const endX = to.x;
      const endY = to.y + nodeHeight / 2;
      const controlOffset = Math.max(54, Math.abs(endX - startX) * 0.42);
      return {
        ...edge,
        path: `M ${startX} ${startY} C ${startX + controlOffset} ${startY}, ${endX - controlOffset} ${endY}, ${endX} ${endY}`
      };
    })
    .filter((edge): edge is EvidenceGraphEdge & { path: string } => Boolean(edge));

  return {
    edges: laidOutEdges,
    height: origin * 2 + maxRows * nodeHeight + Math.max(0, maxRows - 1) * rowGap,
    nodes: laidOutNodes,
    width: origin * 2 + (maxColumn + 1) * nodeWidth + maxColumn * (columnWidth - nodeWidth)
  };
}

function parseInputInventoryArtifact(text: string): InputInventory {
  const payload = JSON.parse(text) as { inventory?: unknown; kind?: unknown };
  if (payload.kind !== "input_inventory" || !isRecord(payload.inventory)) {
    throw new Error("Artifact is not a valid input inventory.");
  }
  const inventory = payload.inventory;
  return {
    assets: readStringArray(inventory.assets),
    entries: readStringArray(inventory.entries),
    files: Array.isArray(inventory.files) ? (inventory.files as InputInventory["files"]) : [],
    isSingleBundle: Boolean(inventory.isSingleBundle),
    manifests: readStringArray(inventory.manifests),
    scripts: readStringArray(inventory.scripts),
    sourceMaps: readStringArray(inventory.sourceMaps),
    styles: readStringArray(inventory.styles),
    warnings: readStringArray(inventory.warnings)
  };
}

function parseAstIndexArtifact(text: string): AstIndex[] {
  const payload = JSON.parse(text) as { astIndexes?: unknown; kind?: unknown };
  if (payload.kind !== "ast_index" || !Array.isArray(payload.astIndexes)) {
    throw new Error("Artifact is not a valid AST index.");
  }
  return payload.astIndexes.filter(isRecord).map((index) => ({
    exports: readStringArray(index.exports),
    filePath: typeof index.filePath === "string" ? index.filePath : "unknown",
    imports: readStringArray(index.imports),
    sourceHash: typeof index.sourceHash === "string" ? index.sourceHash : "",
    symbols: Array.isArray(index.symbols) ? (index.symbols as AstIndex["symbols"]) : [],
    warnings: readStringArray(index.warnings)
  }));
}

function latestArtifactOfKind(artifacts: Artifact[], kind: Artifact["kind"]): Artifact | null {
  return (
    [...artifacts]
      .filter((artifact) => artifact.kind === kind)
      .sort((left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt))[0] ?? null
  );
}

function compareArtifactsForGraph(left: Artifact, right: Artifact): number {
  const columnDiff = artifactGraphColumn(left) - artifactGraphColumn(right);
  if (columnDiff !== 0) {
    return columnDiff;
  }
  return Date.parse(left.createdAt) - Date.parse(right.createdAt);
}

function artifactGraphColumn(artifact: Artifact): number {
  if (["source_input", "input_inventory", "source_index"].includes(artifact.kind)) {
    return 0;
  }
  if (["ast_index", "agent_plan", "inference_record", "memory_record", "knowledge_evidence", "tool_call"].includes(artifact.kind)) {
    return 1;
  }
  if (
    [
      "reconstruction_plan",
      "generated_project",
      "build_log",
      "build_artifact",
      "runtime_validation",
      "runtime_trace",
      "runtime_screenshot",
      "runtime_scenario",
      "runtime_comparison",
      "repair_instruction"
    ].includes(artifact.kind)
  ) {
    return 2;
  }
  return 3;
}

function artifactNodeId(artifactId: string): string {
  return `artifact:${artifactId}`;
}

function readStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function shortId(value: string): string {
  return value.length > 10 ? `${value.slice(0, 6)}...${value.slice(-4)}` : value;
}

function trimMiddle(value: string, maxLength: number): string {
  if (value.length <= maxLength) {
    return value;
  }
  const side = Math.max(4, Math.floor((maxLength - 3) / 2));
  return `${value.slice(0, side)}...${value.slice(-side)}`;
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

function filterReportSections(sections: ReportSectionEntry[], query: string): ReportSectionEntry[] {
  const normalizedQuery = query.trim().toLowerCase();
  if (!normalizedQuery) {
    return sections;
  }
  return sections.flatMap((section) => {
    const sectionMatches = reportSectionSearchText(section).includes(normalizedQuery);
    const matchingDetails = section.details.filter((detail) => reportSectionDetailSearchText(detail).includes(normalizedQuery));
    if (sectionMatches) {
      return [{ ...section, details: matchingDetails.length > 0 ? matchingDetails : section.details }];
    }
    if (matchingDetails.length > 0) {
      return [{ ...section, details: matchingDetails }];
    }
    return [];
  });
}

function reportSectionSearchText(section: ReportSectionEntry): string {
  return [
    section.anchor,
    section.summary,
    section.title,
    ...section.artifactIds,
    ...section.artifactKinds,
    ...section.evidenceLinks
  ]
    .join(" ")
    .toLowerCase();
}

function reportSectionDetailSearchText(detail: ReportSectionDetailEntry): string {
  return [detail.label, detail.value, detail.status ?? "", reportSectionDetailSummary(detail), safeJsonText(detail.details)].join(" ").toLowerCase();
}

function reportSectionDetailSummary(detail: ReportSectionDetailEntry): string {
  const payload = detail.details;
  const parts: string[] = [];
  appendPayloadPart(parts, "type", payload.reviewType ?? payload.phase);
  appendPayloadPart(parts, "attempt", payload.attempt);
  appendPayloadPart(parts, "failure", payload.failureClass && payload.failureClass !== "none" ? payload.failureClass : null);
  appendPayloadPart(parts, "command", payload.commandSource);
  appendPayloadPart(parts, "script", payload.scriptName);
  if (typeof payload.diagnosticCount === "number") {
    parts.push(`${payload.diagnosticCount} diagnostics`);
  }
  if (isRecord(payload.resourcePolicy)) {
    const runner = [payload.resourcePolicy.runnerKind, payload.resourcePolicy.enforcement].filter(Boolean).join("/");
    appendPayloadPart(parts, "runner", runner || null);
  }
  if (Array.isArray(payload.repairInstructionIds) && payload.repairInstructionIds.length > 0) {
    parts.push(`${payload.repairInstructionIds.length} repairs`);
  }
  if (Array.isArray(payload.domDifferences)) {
    parts.push(`DOM ${payload.domDifferences.length}`);
  }
  const networkSummary = collectionDiffSummary(payload.networkDiff);
  if (networkSummary) {
    parts.push(`network ${networkSummary}`);
  }
  const consoleSummary = collectionDiffSummary(payload.consoleDiff);
  if (consoleSummary) {
    parts.push(`console ${consoleSummary}`);
  }
  appendPayloadPart(parts, "decision", payload.decision);
  if (Array.isArray(payload.evidenceLinks)) {
    parts.push(`${payload.evidenceLinks.length} evidence`);
  }
  return parts.length > 0 ? parts.join(" / ") : "No structured breakdown";
}

function appendPayloadPart(parts: string[], label: string, value: unknown): void {
  if (value === undefined || value === null || value === "") {
    return;
  }
  parts.push(`${label} ${formatUnknownValue(value)}`);
}

function safeJsonText(value: unknown): string {
  try {
    return JSON.stringify(value) ?? "";
  } catch {
    return String(value);
  }
}

function collectionDiffSummary(value: unknown): string | null {
  if (!isRecord(value)) {
    return null;
  }
  if (value.changed === false) {
    return "unchanged";
  }
  const originalCount = typeof value.originalCount === "number" ? value.originalCount : null;
  const reconstructedCount = typeof value.reconstructedCount === "number" ? value.reconstructedCount : null;
  const groups = Array.isArray(value.groups) ? value.groups.map(String).slice(0, 3) : [];
  const countSummary = originalCount !== null && reconstructedCount !== null ? `${originalCount}->${reconstructedCount}` : "changed";
  return groups.length > 0 ? `${countSummary} ${groups.join(", ")}` : countSummary;
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

function auditFilterLabel(filters: AuditFilterState): string {
  const parts = [
    filters.category === "all" ? "all records" : filters.category,
    filters.status === "all" ? "all statuses" : filters.status,
    filters.query.trim() ? `"${filters.query.trim()}"` : "no search"
  ];
  return parts.join(" / ");
}

function groupAuditRecordsByRisk(records: NormalizedAuditRecord[]): AuditRiskGroup[] {
  const groups: AuditRiskGroup[] = [
    {
      detail: "non-none failure classes or failing decisions",
      id: "blocking",
      records: [],
      title: "Blocking risk"
    },
    {
      detail: "best-effort, retry, unverified, or needs-review records",
      id: "review",
      records: [],
      title: "Needs review"
    },
    {
      detail: "accepted or passing records",
      id: "passing",
      records: [],
      title: "Passing evidence"
    }
  ];
  const groupsById = new Map(groups.map((group) => [group.id, group]));
  for (const record of records) {
    groupsById.get(auditRecordRiskGroup(record))?.records.push(record);
  }
  return groups;
}

function auditRecordRiskGroup(record: NormalizedAuditRecord): AuditRiskGroupId {
  if (record.failureClass !== "none" || statusTokenTone(record.status) === "fail") {
    return "blocking";
  }
  if (auditRecordNeedsAttention(record)) {
    return "review";
  }
  return "passing";
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

function uniqueRuntimeComparisonScenarios(reports: RuntimeComparisonLoaded[]): string[] {
  return [...new Set(reports.map((item) => item.report.differences.comparisonScope.scenarioName))].sort();
}

function uniqueRuntimeComparisonViewports(reports: RuntimeComparisonLoaded[]): Array<{ label: string; value: string }> {
  const viewports = new Map<string, string>();
  for (const item of reports) {
    const value = runtimeComparisonViewportValue(item.report);
    viewports.set(value, runtimeComparisonViewportLabel(item.report));
  }
  return [...viewports.entries()].map(([value, label]) => ({ label, value })).sort((left, right) => left.label.localeCompare(right.label));
}

function runtimeComparisonMatchesFilters(report: RuntimeComparisonReport, filters: RuntimeComparisonFilters): boolean {
  if (filters.scenario !== "all" && report.differences.comparisonScope.scenarioName !== filters.scenario) {
    return false;
  }
  if (filters.viewport !== "all" && runtimeComparisonViewportValue(report) !== filters.viewport) {
    return false;
  }
  if (filters.status !== "all" && report.status !== filters.status) {
    return false;
  }
  return true;
}

function runtimeComparisonViewportLabel(report: RuntimeComparisonReport): string {
  const viewport = report.differences.comparisonScope.viewport;
  if (!viewport) {
    return "default viewport";
  }
  const name = viewport.name ? `${viewport.name} ` : "";
  return `${name}${viewport.width}x${viewport.height}`;
}

function runtimeComparisonViewportValue(report: RuntimeComparisonReport): string {
  const viewport = report.differences.comparisonScope.viewport;
  if (!viewport) {
    return "default";
  }
  return `${viewport.name ?? "viewport"}:${viewport.width}x${viewport.height}`;
}

function virtualListRange(totalItems: number, scrollTop: number, rowHeight: number, viewportHeight: number): { end: number; start: number } {
  const overscan = 4;
  const start = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const visibleCount = Math.ceil(viewportHeight / rowHeight) + overscan * 2;
  return {
    end: Math.min(totalItems, start + visibleCount),
    start
  };
}

function runtimeScreenshotPreviewItems(report: RuntimeComparisonReport): Array<{ artifactId: string; detail: string; label: string }> {
  const items = [
    {
      artifactId: report.original.screenshotArtifactId ?? null,
      detail: "Original runtime capture",
      label: "Original"
    },
    {
      artifactId: report.reconstructed.screenshotArtifactId ?? null,
      detail: "Reconstructed runtime capture",
      label: "Reconstructed"
    },
    {
      artifactId: report.differences.screenshotDiff.diffArtifactId ?? null,
      detail: "Pixel difference capture",
      label: "Pixel diff"
    }
  ];
  return items.filter((item): item is { artifactId: string; detail: string; label: string } => Boolean(item.artifactId));
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
