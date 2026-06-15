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
import type {
  Artifact,
  CloudMode,
  EvidenceRef,
  InferenceRecord,
  Job,
  JobStatus,
  ReviewRun,
  RuntimeValidationRun,
  ToolCall
} from "@ai-jsunpack/shared";
import { CLOUD_MODES, JOB_STATUSES } from "@ai-jsunpack/shared";
import {
  API_BASE_URL,
  createJob,
  fetchInferenceRecords,
  fetchJobSummary,
  fetchReviewRuns,
  fetchRuntimeValidations,
  fetchToolCalls,
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

interface JobWorkspace {
  summary: JobSummary;
  evidence: JobEvidence;
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

const reportArtifactKinds = new Set<Artifact["kind"]>([
  "audit_report",
  "result_package",
  "runtime_validation",
  "runtime_trace",
  "runtime_screenshot",
  "review_run",
  "tool_call",
  "inference_record",
  "build_log"
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

export function AppContainer() {
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);
  const [selectedCloudMode, setSelectedCloudMode] = useState<CloudMode>("local_only");
  const [selectedUploadFile, setSelectedUploadFile] = useState<File | null>(null);
  const [jobSummary, setJobSummary] = useState<JobSummary | null>(null);
  const [evidence, setEvidence] = useState<JobEvidence>(() => emptyEvidence());
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
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

  return (
    <AppView
      apiBaseUrl={API_BASE_URL}
      artifacts={artifacts}
      currentJob={currentJob}
      data={data}
      evidence={evidence}
      isRefreshing={isRefreshing}
      isSubmitting={isSubmitting}
      onArtifactSelect={setSelectedArtifactId}
      onFileChange={setSelectedUploadFile}
      onRefreshJob={handleRefreshJob}
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
  artifacts: Artifact[];
  currentJob: Job | null;
  data: WorkbenchData;
  evidence: JobEvidence;
  isRefreshing: boolean;
  isSubmitting: boolean;
  onArtifactSelect: (artifactId: string) => void;
  onFileChange: (file: File | null) => void;
  onRefreshJob: () => void;
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
  artifacts,
  currentJob,
  data,
  evidence,
  isRefreshing,
  isSubmitting,
  onArtifactSelect,
  onFileChange,
  onRefreshJob,
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

          <section className="workbench-panel code-panel motion-item">
            <div className="panel-heading padded-heading">
              <div>
                <p className="panel-kicker">Artifact detail</p>
                <h2>{selectedArtifact ? selectedArtifact.kind : "No artifact selected"}</h2>
              </div>
              <Braces size={22} aria-hidden="true" />
            </div>
            <ArtifactDetail apiBaseUrl={apiBaseUrl} artifact={selectedArtifact} currentJob={currentJob} />
          </section>

          <section className="workbench-panel report-panel motion-item">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Reports and evidence</p>
                <h2>{data.reportArtifacts.length} outputs</h2>
              </div>
              <Archive size={22} aria-hidden="true" />
            </div>
            <ReportArtifactList apiBaseUrl={apiBaseUrl} artifacts={data.reportArtifacts} currentJob={currentJob} />
          </section>

          <section className="workbench-panel audit-panel motion-item" id="audit">
            <div className="panel-heading padded-heading">
              <div>
                <p className="panel-kicker">Evidence ledger</p>
                <h2>Agent audit</h2>
              </div>
              <SearchCode size={22} aria-hidden="true" />
            </div>
            <AuditPanel evidence={evidence} />
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
  currentJob
}: {
  apiBaseUrl: string;
  artifact: Artifact | null;
  currentJob: Job | null;
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
      <div className="detail-actions">
        <ArtifactDownloadLink apiBaseUrl={apiBaseUrl} artifact={artifact} currentJob={currentJob} label="Download artifact" />
      </div>
    </div>
  );
}

function ReportArtifactList({
  apiBaseUrl,
  artifacts,
  currentJob
}: {
  apiBaseUrl: string;
  artifacts: Artifact[];
  currentJob: Job | null;
}) {
  if (artifacts.length === 0) {
    return <EmptyState title="No report outputs" detail="Runtime, audit, review, and package artifacts appear here when produced." />;
  }

  return (
    <div className="report-list">
      {artifacts.map((artifact) => (
        <div className="report-row" key={artifact.id}>
          <div>
            <strong>{artifact.kind}</strong>
            <span>
              {artifact.stage} / {artifact.producer}
            </span>
          </div>
          <ArtifactDownloadLink apiBaseUrl={apiBaseUrl} artifact={artifact} currentJob={currentJob} label="Download" />
        </div>
      ))}
    </div>
  );
}

function AuditPanel({ evidence }: { evidence: JobEvidence }) {
  return (
    <div className="audit-sections">
      <section className="audit-section" aria-label="Inference records">
        <div className="section-heading">
          <h3>Inference records</h3>
          <span>{evidence.inferenceRecords.length}</span>
        </div>
        {evidence.inferenceRecords.length > 0 ? (
          <div className="table-shell">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Agent</th>
                  <th>Confidence</th>
                  <th>Validation</th>
                  <th>Evidence</th>
                </tr>
              </thead>
              <tbody>
                {evidence.inferenceRecords.map((record) => (
                  <tr key={record.id}>
                    <td>{record.type}</td>
                    <td>{record.agentName}</td>
                    <td>{formatPercent(record.confidence)}</td>
                    <td>{record.validationStatus}</td>
                    <td>{formatEvidenceRefs(record.evidenceRefs)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="No inference records" detail="Agent output records will appear after the agent_pass stage." />
        )}
      </section>

      <section className="audit-section" aria-label="Review runs">
        <div className="section-heading">
          <h3>Review runs</h3>
          <span>{evidence.reviewRuns.length}</span>
        </div>
        {evidence.reviewRuns.length > 0 ? (
          <div className="table-shell">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Status</th>
                  <th>Decision</th>
                  <th>Failure</th>
                  <th>Evidence</th>
                </tr>
              </thead>
              <tbody>
                {evidence.reviewRuns.map((run) => (
                  <tr key={run.id}>
                    <td>{run.reviewType}</td>
                    <td>{run.status}</td>
                    <td>{run.decision}</td>
                    <td>{run.failureClass}</td>
                    <td>{formatEvidenceRefs(run.evidenceRefs)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="No review runs" detail="Review and repair decisions will appear when the worker emits review artifacts." />
        )}
      </section>

      <section className="audit-section" aria-label="Tool calls">
        <div className="section-heading">
          <h3>Tool calls</h3>
          <span>{evidence.toolCalls.length}</span>
        </div>
        {evidence.toolCalls.length > 0 ? (
          <div className="table-shell">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Tool</th>
                  <th>Caller</th>
                  <th>Status</th>
                  <th>Duration</th>
                  <th>Outputs</th>
                </tr>
              </thead>
              <tbody>
                {evidence.toolCalls.map((call) => (
                  <tr key={call.id}>
                    <td>{call.toolName}</td>
                    <td>{call.caller}</td>
                    <td>{call.status}</td>
                    <td>{formatDuration(call.duration)}</td>
                    <td>{formatIdList(call.outputArtifactIds)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="No tool calls" detail="Tool registry calls are listed once the agent runtime records them." />
        )}
      </section>
    </div>
  );
}

function RuntimePanel({
  apiBaseUrl,
  artifacts,
  currentJob,
  latestRuntime,
  runtimeMetrics,
  runtimeValidations
}: {
  apiBaseUrl: string;
  artifacts: Artifact[];
  currentJob: Job | null;
  latestRuntime: RuntimeValidationRun | null;
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
            <div className="history-row" key={run.id}>
              <span>{run.status}</span>
              <strong>{run.target}</strong>
              <small>{run.entryUrl}</small>
            </div>
          ))}
        </div>
      ) : null}
    </>
  );
}

function EvidenceArtifactLinks({
  apiBaseUrl,
  artifactIds,
  artifacts,
  currentJob
}: {
  apiBaseUrl: string;
  artifactIds: Array<string | null | undefined>;
  artifacts: Artifact[];
  currentJob: Job | null;
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
        <ArtifactDownloadLink
          apiBaseUrl={apiBaseUrl}
          artifact={artifact}
          currentJob={currentJob}
          key={artifact.id}
          label={artifact.kind}
        />
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

function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return "Request failed.";
}
