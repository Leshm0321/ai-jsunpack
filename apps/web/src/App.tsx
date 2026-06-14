import { useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, FormEvent } from "react";
import { useGSAP } from "@gsap/react";
import { gsap } from "gsap";
import {
  Activity,
  Archive,
  Binary,
  Braces,
  CheckCircle2,
  ChevronRight,
  Download,
  FileCode2,
  GitBranch,
  Network,
  Radar,
  RefreshCw,
  SearchCode,
  ShieldCheck,
  Sparkles,
  Upload,
  Workflow,
  XCircle
} from "lucide-react";
import type { Artifact, CloudMode, Job, JobStatus } from "@ai-jsunpack/shared";
import { CLOUD_MODES, JOB_STATUSES } from "@ai-jsunpack/shared";
import { API_BASE_URL, createJob, fetchJobSummary, uploadSource } from "./api";
import type { JobSummary } from "./api";

gsap.registerPlugin(useGSAP);

type StageState = "done" | "active" | "pending" | "warning" | "fail";

interface StageDefinition {
  status: JobStatus;
  label: string;
}

interface StageItem extends StageDefinition {
  state: StageState;
}

interface AuditRow {
  type: string;
  subject: string;
  confidence: number;
  evidence: string;
  status: "accepted" | "needs_review";
}

interface RuntimeMetric {
  label: string;
  value: string;
  status: "pass" | "warn" | "fail";
}

interface WorkbenchData {
  stages: StageItem[];
  files: string[];
  auditRows: AuditRow[];
  runtimeMetrics: RuntimeMetric[];
}

const stageDefinitions: StageDefinition[] = [
  { status: "queued", label: "Job created" },
  { status: "intake", label: "Input inventory" },
  { status: "indexing", label: "AST and resource index" },
  { status: "agent_pass", label: "Agent inference" },
  { status: "reconstructing", label: "Project writer" },
  { status: "runtime_smoke", label: "Browser validation" },
  { status: "reviewing", label: "Review and repair" },
  { status: "completed", label: "Package ready" }
];

const statusOrder = new Map<JobStatus, number>(JOB_STATUSES.map((status, index) => [status, index]));

const projectFiles = [
  "src/main.tsx",
  "src/routes/AppShell.tsx",
  "src/modules/auth/session.ts",
  "src/modules/cart/pricing.ts",
  "src/types/generated.d.ts",
  "public/assets/runtime-shim.js"
];

const auditRows: AuditRow[] = [
  {
    type: "Naming",
    subject: "n -> calculateDiscount",
    confidence: 0.84,
    evidence: "Symbol called before price total branch",
    status: "accepted"
  },
  {
    type: "Framework",
    subject: "React route tree",
    confidence: 0.79,
    evidence: "JSX factory and root hydrate call",
    status: "accepted"
  },
  {
    type: "Runtime",
    subject: "public path shim",
    confidence: 0.72,
    evidence: "Chunk loader reads asset base at startup",
    status: "accepted"
  },
  {
    type: "Dead code",
    subject: "debug branch retained",
    confidence: 0.58,
    evidence: "No direct reference, behavior risk marked",
    status: "needs_review"
  }
];

const runtimeMetrics: RuntimeMetric[] = [
  { label: "Entry load", value: "Pending", status: "warn" },
  { label: "Console errors", value: "Pending", status: "warn" },
  { label: "Failed requests", value: "Pending", status: "warn" },
  { label: "Screenshot diff", value: "Pending", status: "warn" }
];

const codePreview = `export function calculateDiscount(cart: CartState) {
  const eligibleItems = cart.items.filter(item => item.price > 0);
  const subtotal = eligibleItems.reduce((sum, item) => sum + item.price, 0);

  if (cart.flags.includes("enterprise")) {
    return subtotal * 0.12;
  }

  return subtotal > 500 ? subtotal * 0.08 : 0;
}`;

export function AppContainer() {
  const [selectedFile, setSelectedFile] = useState(projectFiles[0]);
  const [selectedCloudMode, setSelectedCloudMode] = useState<CloudMode>("local_only");
  const [selectedUploadFile, setSelectedUploadFile] = useState<File | null>(null);
  const [jobSummary, setJobSummary] = useState<JobSummary | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);

  const currentJob = jobSummary?.job ?? null;
  const artifacts = jobSummary?.artifacts ?? [];
  const data = useMemo<WorkbenchData>(
    () => ({
      stages: buildStageItems(currentJob?.status),
      files: projectFiles,
      auditRows,
      runtimeMetrics
    }),
    [currentJob?.status]
  );

  useEffect(() => {
    if (!currentJob?.id) {
      return;
    }

    let cancelled = false;
    const pollJob = async () => {
      try {
        const summary = await fetchJobSummary(currentJob.id);
        if (!cancelled) {
          setJobSummary(summary);
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
    try {
      const created = await createJob(selectedCloudMode);
      setJobSummary(created);
      const uploaded = await uploadSource(created.job.id, selectedUploadFile);
      setJobSummary(uploaded);
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
      setJobSummary(await fetchJobSummary(currentJob.id));
    } catch (error) {
      setPollError(errorMessage(error));
    } finally {
      setIsRefreshing(false);
    }
  };

  return (
    <AppView
      apiBaseUrl={API_BASE_URL}
      artifacts={artifacts}
      currentJob={currentJob}
      data={data}
      isRefreshing={isRefreshing}
      isSubmitting={isSubmitting}
      onFileChange={setSelectedUploadFile}
      onRefreshJob={handleRefreshJob}
      onSelectCloudMode={setSelectedCloudMode}
      onSelectFile={setSelectedFile}
      onSubmitJob={handleSubmitJob}
      pollError={pollError}
      selectedCloudMode={selectedCloudMode}
      selectedFile={selectedFile}
      selectedUploadFile={selectedUploadFile}
      uploadError={uploadError}
    />
  );
}

interface AppViewProps {
  apiBaseUrl: string;
  artifacts: Artifact[];
  currentJob: Job | null;
  data: WorkbenchData;
  isRefreshing: boolean;
  isSubmitting: boolean;
  onFileChange: (file: File | null) => void;
  onRefreshJob: () => void;
  onSelectCloudMode: (mode: CloudMode) => void;
  onSelectFile: (file: string) => void;
  onSubmitJob: (event: FormEvent<HTMLFormElement>) => void;
  pollError: string | null;
  selectedCloudMode: CloudMode;
  selectedFile: string;
  selectedUploadFile: File | null;
  uploadError: string | null;
}

function AppView({
  apiBaseUrl,
  artifacts,
  currentJob,
  data,
  isRefreshing,
  isSubmitting,
  onFileChange,
  onRefreshJob,
  onSelectCloudMode,
  onSelectFile,
  onSubmitJob,
  pollError,
  selectedCloudMode,
  selectedFile,
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
              Upload a production build, inspect the recovered project, trace every inference, and validate behavior in
              a controlled browser runtime.
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
            <PipelineMap currentJob={currentJob} artifacts={artifacts} />
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
            <JobSummaryPanel apiBaseUrl={apiBaseUrl} artifacts={artifacts} currentJob={currentJob} />
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
                <p className="panel-kicker">Generated project</p>
                <h2>File tree</h2>
              </div>
              <FileCode2 size={22} aria-hidden="true" />
            </div>
            <div className="file-list" role="listbox" aria-label="Generated files">
              {data.files.map((file) => (
                <button
                  className={file === selectedFile ? "file-row file-row-active" : "file-row"}
                  key={file}
                  type="button"
                  onClick={() => onSelectFile(file)}
                >
                  <FileCode2 size={15} aria-hidden="true" />
                  <span>{file}</span>
                </button>
              ))}
            </div>
          </section>

          <section className="workbench-panel code-panel motion-item">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Code preview</p>
                <h2>{selectedFile}</h2>
              </div>
              <Braces size={22} aria-hidden="true" />
            </div>
            <pre className="code-preview" aria-label="Recovered TypeScript preview">
              <code>{codePreview}</code>
            </pre>
          </section>

          <section className="workbench-panel audit-panel motion-item" id="audit">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Evidence ledger</p>
                <h2>Inference audit</h2>
              </div>
              <SearchCode size={22} aria-hidden="true" />
            </div>
            <div className="audit-table" role="table" aria-label="Inference audit records">
              <div className="audit-row audit-head" role="row">
                <span>Type</span>
                <span>Subject</span>
                <span>Confidence</span>
                <span>Evidence</span>
              </div>
              {data.auditRows.map((row) => (
                <div className="audit-row" role="row" key={`${row.type}-${row.subject}`}>
                  <span>{row.type}</span>
                  <span>{row.subject}</span>
                  <span>{Math.round(row.confidence * 100)}%</span>
                  <span>{row.evidence}</span>
                </div>
              ))}
            </div>
          </section>

          <section className="workbench-panel runtime-panel motion-item" id="runtime">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Browser evidence</p>
                <h2>Runtime validation</h2>
              </div>
              <Radar size={22} aria-hidden="true" />
            </div>
            <div className="runtime-grid">
              {data.runtimeMetrics.map((metric) => (
                <div className={`runtime-metric runtime-${metric.status}`} key={metric.label}>
                  <span>{metric.label}</span>
                  <strong>{metric.value}</strong>
                </div>
              ))}
            </div>
            <div className="runtime-actions">
              <button className="secondary-action compact" type="button" disabled>
                <Network size={16} aria-hidden="true" />
                Network log
              </button>
              <button className="primary-action compact" type="button" disabled>
                <Download size={16} aria-hidden="true" />
                Download package
              </button>
            </div>
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
  currentJob
}: {
  apiBaseUrl: string;
  artifacts: Artifact[];
  currentJob: Job | null;
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
      {artifacts.length > 0 ? (
        <div className="artifact-list" aria-label="Artifact summary">
          {artifacts.slice(0, 3).map((artifact) => (
            <div className="artifact-row" key={artifact.id}>
              <span>{artifact.kind}</span>
              <small>{formatBytes(artifact.size)}</small>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function StatusBanner({ message, tone }: { message: string; tone: "error" | "warning" }) {
  return <div className={`status-banner status-${tone}`}>{message}</div>;
}

function PipelineMap({ artifacts, currentJob }: { artifacts: Artifact[]; currentJob: Job | null }) {
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
    </div>
  );
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

function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return "Request failed.";
}
