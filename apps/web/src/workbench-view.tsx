import { useRef } from "react";
import type { ChangeEvent, FormEvent } from "react";
import {
  Activity, Archive, Binary, Bot, Braces, CheckCircle2, ChevronRight, CircleAlert, FileCode2,
  GitBranch, LayoutDashboard, Network, Plus, Radar, RefreshCw, SearchCode, Settings2, ShieldCheck,
  Sparkles, Upload, Workflow, XCircle
} from "lucide-react";
import type { Artifact, CloudMode, Job } from "@ai-jsunpack/shared";
import { CLOUD_MODES } from "@ai-jsunpack/shared";
import { useLocalization } from "./i18n";
import type { TranslationKey } from "./i18n";
import {
  useActiveNavMotion,
  useApplicationMotion,
  useApplicationScrollMotion,
  useMetricMotion
} from "./app-motion";
import type { ArtifactPreview, JobEvidence, StageState, WorkbenchData } from "./workbench-types";
import { ArtifactDetail, ArtifactList } from "./workbench-artifacts";
import { AuditPanel } from "./workbench-audit";
import { EmptyState, StatusBanner, StatusToken } from "./workbench-common";
import { EvidenceGraphPanel } from "./workbench-graph";
import { formatBytes, formatPercent } from "./workbench-logic";
import { RuntimePanel } from "./workbench-runtime";
import { JobSummaryPanel, LanguageToggle, ModePill, PipelineMap, WorkspaceActions } from "./workbench-shell";
import { workbenchPath } from "./routes";
import type { AppRoute, WorkbenchSection } from "./routes";

type WorkbenchView = WorkbenchSection | "new";

export function emptyEvidence(): JobEvidence {
  return { runtimeValidations: [], inferenceRecords: [], reviewRuns: [], toolCalls: [] };
}

export function emptyArtifactPreview(): ArtifactPreview {
  return { artifactId: null, error: null, reason: null, status: "idle", text: null };
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
  onNavigate: (route: AppRoute) => void;
  onRefreshJob: () => void;
  onRerunJob: () => void;
  onSelectCloudMode: (mode: CloudMode) => void;
  onSubmitJob: (event: FormEvent<HTMLFormElement>) => void;
  pollError: string | null;
  selectedArtifact: Artifact | null;
  selectedCloudMode: CloudMode;
  selectedUploadFile: File | null;
  uploadError: string | null;
  view: WorkbenchView;
}

const viewMeta = {
  overview: { labelKey: "workbench.view.overview.label", summaryKey: "workbench.view.overview.summary", icon: LayoutDashboard },
  artifacts: { labelKey: "workbench.view.artifacts.label", summaryKey: "workbench.view.artifacts.summary", icon: FileCode2 },
  evidence: { labelKey: "workbench.view.evidence.label", summaryKey: "workbench.view.evidence.summary", icon: GitBranch },
  agents: { labelKey: "workbench.view.agents.label", summaryKey: "workbench.view.agents.summary", icon: Bot },
  runtime: { labelKey: "workbench.view.runtime.label", summaryKey: "workbench.view.runtime.summary", icon: Radar },
  audit: { labelKey: "workbench.view.audit.label", summaryKey: "workbench.view.audit.summary", icon: SearchCode }
} satisfies Record<WorkbenchSection, { labelKey: TranslationKey; summaryKey: TranslationKey; icon: typeof Activity }>;

export function AppView(props: AppViewProps) {
  const { language, setLanguage, t } = useLocalization();
  const rootRef = useRef<HTMLDivElement>(null);
  const motionKey = `${props.view}:${props.currentJob?.id ?? "new"}:${language}`;
  const metricKey = [
    props.artifacts.length,
    props.evidence.inferenceRecords.length,
    props.evidence.runtimeValidations.length,
    props.evidence.reviewRuns.length,
    props.evidence.toolCalls.length
  ].join(":");
  useApplicationMotion(rootRef, [motionKey]);
  useApplicationScrollMotion(rootRef, [motionKey]);
  useActiveNavMotion(rootRef, props.view);
  useMetricMotion(rootRef, metricKey);
  const activeMeta = props.view === "new"
    ? { labelKey: "workbench.new.label" as const, summaryKey: "workbench.new.summary" as const }
    : viewMeta[props.view];
  return (
    <div className="application-frame" ref={rootRef}>
      <header className="application-topbar">
        <button className="brand brand-button" type="button" onClick={() => props.onNavigate("/")} aria-label={t("site.aria.home")}>
          <span className="brand-mark"><Binary size={18} aria-hidden="true" /></span><span>AI JS Unpack</span>
        </button>
        <div className="application-breadcrumb" aria-label={t("app.aria.currentLocation")}>
          <span>{t("workbench.label")}</span><ChevronRight size={14} aria-hidden="true" /><strong>{t(activeMeta.labelKey)}</strong>
          {props.currentJob ? <code>{props.currentJob.id}</code> : null}
        </div>
        <div className="application-topbar-actions">
          <button className="icon-action" type="button" title={t("workbench.settings")} aria-label={t("workbench.settings")} onClick={() => props.onNavigate("/settings/ai")}><Settings2 size={18} aria-hidden="true" /></button>
          <LanguageToggle language={language} onLanguageChange={setLanguage} />
        </div>
      </header>
      <div className="application-body">
        <WorkbenchSidebar currentJob={props.currentJob} activeView={props.view} onNavigate={props.onNavigate} />
        <main className="application-content workbench-content">
          <div className="page-heading compact-page-heading">
            <div><p className="panel-kicker">{t("workbench.kicker")}</p><h1>{t(activeMeta.labelKey)}</h1><p>{t(activeMeta.summaryKey)}</p></div>
            {props.currentJob && props.view !== "new" ? <button className="secondary-action compact" type="button" disabled={props.isRefreshing} onClick={props.onRefreshJob}><RefreshCw size={16} aria-hidden="true" />{props.isRefreshing ? t("action.refreshing") : t("action.refresh")}</button> : null}
          </div>
          {props.pollError ? <StatusBanner tone="warning" message={props.pollError} /> : null}
          {props.uploadError ? <StatusBanner tone="error" message={props.uploadError} /> : null}
          <WorkbenchRouteContent {...props} />
        </main>
      </div>
    </div>
  );
}

function WorkbenchSidebar({ currentJob, activeView, onNavigate }: { currentJob: Job | null; activeView: WorkbenchView; onNavigate: (route: AppRoute) => void }) {
  const { t } = useLocalization();
  return (
    <aside className="application-sidebar workbench-sidebar" aria-label={t("app.aria.workbenchNav")}>
      <span className="sidebar-active-indicator" aria-hidden="true" />
      <button className={activeView === "new" ? "sidebar-link active" : "sidebar-link"} type="button" onClick={() => onNavigate("/workbench/new")}>
        <Plus size={17} aria-hidden="true" /><span><strong>{t("workbench.new.label")}</strong><small>{t("workbench.new.sidebarSummary")}</small></span><ChevronRight size={15} aria-hidden="true" />
      </button>
      <div className="sidebar-context"><span>{t("workbench.currentJob")}</span><strong>{currentJob?.id ?? t("workbench.noJobSelected")}</strong>{currentJob ? <StatusToken status={currentJob.status} /> : null}</div>
      <nav className="sidebar-nav">
        {(Object.entries(viewMeta) as Array<[WorkbenchSection, (typeof viewMeta)[WorkbenchSection]]>).map(([section, meta]) => {
          const Icon = meta.icon;
          return <button aria-current={activeView === section ? "page" : undefined} className={activeView === section ? "sidebar-link active" : "sidebar-link"} disabled={!currentJob} key={section} type="button" onClick={() => currentJob ? onNavigate(workbenchPath(currentJob.id, section)) : undefined}><Icon size={17} aria-hidden="true" /><span><strong>{t(meta.labelKey)}</strong><small>{t(meta.summaryKey)}</small></span><ChevronRight size={15} aria-hidden="true" /></button>;
        })}
      </nav>
      <div className="sidebar-note"><ShieldCheck size={17} aria-hidden="true" /><span>{t("workbench.authorizedOnly")}</span></div>
    </aside>
  );
}

function WorkbenchRouteContent(props: AppViewProps) {
  const { t } = useLocalization();
  if (props.view === "new") return <NewAnalysisView {...props} />;
  if (!props.currentJob) return <section className="route-panel"><EmptyState title={t("workbench.error.jobUnavailable.title")} detail={t("workbench.error.jobUnavailable.detail")} /></section>;
  if (props.view === "overview") return <OverviewView {...props} />;
  if (props.view === "artifacts") return <ArtifactsView {...props} />;
  if (props.view === "evidence") return <EvidenceView {...props} />;
  if (props.view === "agents") return <AgentsView {...props} />;
  if (props.view === "runtime") return <RuntimeView {...props} />;
  return <AuditView {...props} />;
}

function NewAnalysisView(props: AppViewProps) {
  const { t } = useLocalization();
  return (
    <div className="route-grid new-analysis-grid">
      <section className="route-panel upload-panel">
        <PanelHeading kicker={t("workbench.new.sourceKicker")} title={t("workbench.new.inputTitle")} icon={Archive} />
        <form className="upload-form" onSubmit={props.onSubmitJob}>
          <label className="dropzone" htmlFor="source-upload"><Archive size={24} aria-hidden="true" /><div><strong>{props.selectedUploadFile?.name ?? t("upload.selectArtifact")}</strong><span>{props.selectedUploadFile ? formatBytes(props.selectedUploadFile.size) : t("upload.fileTypes")}</span></div></label>
          <input className="visually-hidden" id="source-upload" type="file" onChange={(event: ChangeEvent<HTMLInputElement>) => props.onFileChange(event.currentTarget.files?.[0] ?? null)} />
          <div className="mode-grid" aria-label={t("app.aria.processingModes")}>{CLOUD_MODES.map((mode) => <ModePill active={mode === props.selectedCloudMode} key={mode} label={mode} onClick={() => props.onSelectCloudMode(mode)} />)}</div>
          <button className="primary-action" type="submit" disabled={props.isSubmitting}><Upload size={17} aria-hidden="true" />{props.isSubmitting ? t("workbench.new.creating") : t("workbench.new.create")}</button>
        </form>
      </section>
      <section className="route-panel intake-guidance">
        <PanelHeading kicker={t("workbench.new.policyKicker")} title={t("workbench.new.beforeStart")} icon={ShieldCheck} />
        <div className="guidance-list">
          <Guidance icon={CheckCircle2} title={t("workbench.new.boundary.title")} detail={t("workbench.new.boundary.detail")} />
          <Guidance icon={Network} title={t("workbench.new.readiness.title")} detail={t("workbench.new.readiness.detail")} />
          <Guidance icon={Workflow} title={t("workbench.new.stages.title")} detail={t("workbench.new.stages.detail")} />
        </div>
        <button className="secondary-action" type="button" onClick={() => props.onNavigate("/settings/ai")}><Settings2 size={17} aria-hidden="true" />{t("workbench.new.reviewSettings")}</button>
      </section>
    </div>
  );
}

function OverviewView(props: AppViewProps) {
  const { t } = useLocalization();
  return (
    <div className="route-grid overview-grid">
      <section className="route-panel overview-summary-panel"><PanelHeading kicker={t("workbench.overview.jobState")} title={props.currentJob ? localizeJobStatus(props.currentJob.status, t) : t("panel.noJob")} icon={Activity} /><JobSummaryPanel apiBaseUrl={props.apiBaseUrl} artifacts={props.artifacts} currentJob={props.currentJob} evidence={props.evidence} /><WorkspaceActions artifacts={props.artifacts} currentJob={props.currentJob} evidence={props.evidence} isRerunning={props.isRerunning} onRerunJob={props.onRerunJob} /></section>
      <section className="route-panel pipeline-overview-panel"><PipelineMap currentJob={props.currentJob} artifacts={props.artifacts} evidence={props.evidence} /></section>
      <section className="route-panel stage-panel"><PanelHeading kicker={t("workbench.overview.pipeline")} title={t("workbench.overview.executionStages")} icon={Workflow} /><ol className="stage-list compact-stage-list">{props.data.stages.map((stage) => <li className={`stage-step stage-${stage.state}`} key={stage.status}><span className="stage-icon">{stageIcon(stage.state)}</span><span>{stage.label}</span><small>{localizeJobStatus(stage.status, t)}</small></li>)}</ol></section>
      <section className="route-panel overview-metrics-panel"><div className="overview-metric-grid"><OverviewMetric label={t("workbench.metric.artifacts")} value={String(props.artifacts.length)} icon={FileCode2} /><OverviewMetric label={t("workbench.metric.agentDecisions")} value={String(props.evidence.inferenceRecords.length)} icon={Sparkles} /><OverviewMetric label={t("workbench.metric.runtimeRuns")} value={String(props.evidence.runtimeValidations.length)} icon={Radar} /><OverviewMetric label={t("workbench.metric.reviewRuns")} value={String(props.evidence.reviewRuns.length)} icon={ShieldCheck} /></div></section>
    </div>
  );
}

function ArtifactsView(props: AppViewProps) {
  const { t } = useLocalization();
  return <div className="artifact-workspace"><section className="route-panel artifact-index-panel"><PanelHeading kicker={t("workbench.artifacts.index")} title={`${props.artifacts.length} ${t("workbench.artifacts.records")}`} icon={FileCode2} /><ArtifactList artifacts={props.artifacts} selectedArtifact={props.selectedArtifact} onArtifactSelect={props.onArtifactSelect} /></section><section className="route-panel artifact-detail-panel" id="artifact-detail"><PanelHeading kicker={t("workbench.artifacts.detail")} title={props.selectedArtifact?.kind ?? t("panel.noArtifactSelected")} icon={Braces} /><ArtifactDetail apiBaseUrl={props.apiBaseUrl} artifact={props.selectedArtifact} artifactPreview={props.artifactPreview} artifacts={props.artifacts} currentJob={props.currentJob} onArtifactSelect={props.onArtifactSelect} /></section></div>;
}

function EvidenceView(props: AppViewProps) {
  return <section className="route-panel evidence-route-panel"><EvidenceGraphPanel artifacts={props.artifacts} currentJob={props.currentJob} evidence={props.evidence} onArtifactSelect={props.onEvidenceArtifactSelect} selectedArtifactId={props.selectedArtifact?.id ?? null} /></section>;
}

function AgentsView(props: AppViewProps) {
  const { t } = useLocalization();
  const records = props.evidence.inferenceRecords;
  const models = new Set(records.map((record) => `${record.modelProvider}/${record.modelName}`));
  const accepted = records.filter((record) => record.validationStatus === "accepted").length;
  return <div className="agents-layout">
    <section className="agent-summary-grid"><OverviewMetric label={t("workbench.metric.agentDecisions")} value={String(records.length)} icon={Sparkles} /><OverviewMetric label={t("workbench.metric.models")} value={String(models.size)} icon={Bot} /><OverviewMetric label={t("workbench.metric.accepted")} value={String(accepted)} icon={CheckCircle2} /><OverviewMetric label={t("workbench.metric.toolCalls")} value={String(props.evidence.toolCalls.length)} icon={Workflow} /></section>
    <section className="route-panel agent-flow-panel"><PanelHeading kicker={t("workbench.agents.dependencyFlow")} title={t("workbench.agents.analysisToReview")} icon={Network} /><div className="agent-dag" aria-label={t("app.aria.agentFlow")}><span>{t("workbench.agents.planner")}</span><ChevronRight size={16} /><span>{t("workbench.agents.analysis")}</span><ChevronRight size={16} /><div><span>{t("workbench.agents.naming")}</span><span>{t("workbench.agents.types")}</span><span>{t("workbench.agents.framework")}</span><span>{t("workbench.agents.deadCode")}</span><span>{t("workbench.agents.runtime")}</span></div><ChevronRight size={16} /><span>{t("workbench.agents.repair")}</span><ChevronRight size={16} /><span>{t("workbench.agents.review")}</span></div></section>
    <section className="route-panel agent-records-panel"><PanelHeading kicker={t("workbench.agents.structuredOutputs")} title={t("workbench.agents.decisions")} icon={Bot} />{records.length ? <div className="agent-record-list">{records.map((record) => <article className="agent-record" key={record.id}><div className="agent-record-heading"><div><strong>{record.agentName}</strong><span>{record.type}</span></div><StatusToken status={record.validationStatus} /></div><dl><dt>{t("workbench.agents.model")}</dt><dd>{record.modelProvider} / {record.modelName}</dd><dt>{t("workbench.agents.confidence")}</dt><dd>{formatPercent(record.confidence)}</dd><dt>{t("workbench.agents.outputs")}</dt><dd>{record.outputArtifactIds.length}</dd><dt>{t("workbench.agents.evidence")}</dt><dd>{record.evidenceRefs.length}</dd></dl>{record.uncertaintyReasons.length ? <p><CircleAlert size={15} aria-hidden="true" />{record.uncertaintyReasons.join("; ")}</p> : null}{record.outputArtifactIds.length ? <div className="agent-artifact-links">{record.outputArtifactIds.map((artifactId) => <button type="button" key={artifactId} onClick={() => props.onEvidenceArtifactSelect(artifactId)}>{artifactId}</button>)}</div> : null}</article>)}</div> : <EmptyState title={t("workbench.agents.empty.title")} detail={t("workbench.agents.empty.detail")} />}</section>
  </div>;
}

function RuntimeView(props: AppViewProps) {
  return <section className="route-panel runtime-route-panel"><RuntimePanel apiBaseUrl={props.apiBaseUrl} artifacts={props.artifacts} currentJob={props.currentJob} latestRuntime={props.data.latestRuntime} onArtifactSelect={props.onEvidenceArtifactSelect} runtimeMetrics={props.data.runtimeMetrics} runtimeValidations={props.evidence.runtimeValidations} /></section>;
}

function AuditView(props: AppViewProps) {
  return <section className="route-panel audit-route-panel"><AuditPanel artifacts={props.artifacts} currentJob={props.currentJob} evidence={props.evidence} onArtifactSelect={props.onEvidenceArtifactSelect} /></section>;
}

function PanelHeading({ icon: Icon, kicker, title }: { icon: typeof Activity; kicker: string; title: string }) {
  return <div className="panel-heading"><div><p className="panel-kicker">{kicker}</p><h2>{title}</h2></div><Icon size={22} aria-hidden="true" /></div>;
}

function Guidance({ icon: Icon, title, detail }: { icon: typeof Activity; title: string; detail: string }) {
  return <div><Icon size={17} aria-hidden="true" /><span><strong>{title}</strong><small>{detail}</small></span></div>;
}

function OverviewMetric({ icon: Icon, label, value }: { icon: typeof Activity; label: string; value: string }) {
  return <div className="overview-metric"><Icon size={18} aria-hidden="true" /><span>{label}</span><strong className="motion-metric-value">{value}</strong></div>;
}

function stageIcon(state: StageState) {
  if (state === "done") return <CheckCircle2 size={16} aria-hidden="true" />;
  if (state === "fail") return <XCircle size={16} aria-hidden="true" />;
  return <Activity size={16} aria-hidden="true" />;
}

function localizeJobStatus(status: Job["status"], t: (key: TranslationKey) => string): string {
  const statusKeys: Partial<Record<Job["status"], TranslationKey>> = {
    queued: "stage.queued",
    intake: "stage.intake",
    indexing: "stage.indexing",
    agent_pass: "stage.agent_pass",
    reconstructing: "stage.reconstructing",
    runtime_smoke: "stage.runtime_smoke",
    runtime_compare: "stage.runtime_compare",
    reviewing: "stage.reviewing",
    completed: "stage.completed"
  };
  const key = statusKeys[status];
  return key ? t(key) : status;
}
