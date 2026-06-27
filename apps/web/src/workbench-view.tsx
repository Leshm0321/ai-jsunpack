import { useRef } from "react";
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
  Languages,
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
import type { Artifact, CloudMode, Job } from "@ai-jsunpack/shared";
import { CLOUD_MODES } from "@ai-jsunpack/shared";
import { useLocalization } from "./i18n";
import type { Language } from "./i18n";
import type { ArtifactPreview, JobEvidence, StageState, WorkbenchData } from "./workbench-types";
import { ArtifactDetail, ArtifactList } from "./workbench-artifacts";
import { AuditPanel } from "./workbench-audit";
import { StatusBanner } from "./workbench-common";
import { EvidenceGraphPanel } from "./workbench-graph";
import { downloadJsonFile, formatBytes, formatTimestamp } from "./workbench-logic";
import { ReportArtifactList } from "./workbench-report";
import { RuntimePanel } from "./workbench-runtime";
import { JobSummaryPanel, LanguageToggle, ModePill, PipelineMap, WorkspaceActions } from "./workbench-shell";
gsap.registerPlugin(useGSAP);

function stageIcon(state: StageState) {
  if (state === "done") {
    return <CheckCircle2 size={16} aria-hidden="true" />;
  }
  if (state === "fail") {
    return <XCircle size={16} aria-hidden="true" />;
  }
  return <Activity size={16} aria-hidden="true" />;
}

export function emptyEvidence(): JobEvidence {
  return {
    runtimeValidations: [],
    inferenceRecords: [],
    reviewRuns: [],
    toolCalls: []
  };
}

export function emptyArtifactPreview(): ArtifactPreview {
  return {
    artifactId: null,
    error: null,
    reason: null,
    status: "idle",
    text: null
  };
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

export function AppView({
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
  const { language, setLanguage, t } = useLocalization();
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
            gsap.set(".motion-item, .topbar, .pipeline-node, .pipeline-status, .stage-step", {
              autoAlpha: 1,
              x: 0,
              y: 0,
              scale: 1
            });
            return;
          }

          const timeline = gsap.timeline({ defaults: { duration: 0.46, ease: "power3.out" } });
          timeline
            .from(".topbar", { y: -16, autoAlpha: 0, duration: 0.36 })
            .from(".entry-copy", { y: 24, autoAlpha: 0 }, "<0.08")
            .from(".entry-visual", { y: 24, autoAlpha: 0, scale: 0.985 }, "<0.1")
            .from(".pipeline-node, .pipeline-status", { x: 18, autoAlpha: 0, stagger: 0.045 }, "<0.08")
            .from(".workbench-panel", { y: 24, autoAlpha: 0, stagger: 0.055 }, "<0.08")
            .from(".stage-step", { x: -18, autoAlpha: 0, stagger: 0.045 }, "<0.05");

          return () => timeline.kill();
        }
      );

      return () => mm.revert();
    },
    { dependencies: [language], revertOnUpdate: true, scope: rootRef }
  );

  return (
    <div className="app-shell" ref={rootRef}>
      <header className="topbar">
        <a className="brand" href="#overview" aria-label={t("app.aria.overview")}>
          <span className="brand-mark">
            <Binary size={18} aria-hidden="true" />
          </span>
          <span>AI JS Unpack</span>
        </a>
        <nav className="topnav" aria-label={t("app.aria.primaryNav")}>
          <a href="#workflow">{t("nav.workflow")}</a>
          <a href="#audit">{t("nav.audit")}</a>
          <a href="#runtime">{t("nav.runtime")}</a>
        </nav>
        <LanguageToggle language={language} onLanguageChange={setLanguage} />
      </header>

      <main>
        <section className="entry-band" id="overview">
          <div className="entry-copy motion-item">
            <p className="eyebrow">{t("hero.eyebrow")}</p>
            <h1>{t("hero.title")}</h1>
            <p className="entry-text">{t("hero.text")}</p>
            <div className="entry-actions">
              <button className="primary-action" type="button" onClick={() => fileInputRef.current?.click()}>
                <Upload size={18} aria-hidden="true" />
                {t("action.uploadBuild")}
              </button>
              <button className="secondary-action" type="button" onClick={onRefreshJob} disabled={!currentJob || isRefreshing}>
                <RefreshCw size={18} aria-hidden="true" />
                {t("action.refreshJob")}
              </button>
            </div>
          </div>

          <div className="entry-visual motion-item" aria-label={t("app.aria.pipelineOverview")}>
            <PipelineMap currentJob={currentJob} artifacts={artifacts} evidence={evidence} />
          </div>
        </section>

        <section className="workbench" id="workflow" aria-label={t("app.aria.workbench")}>
          <section className="workbench-panel upload-panel motion-item">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">{t("panel.sourceKicker")}</p>
                <h2>{t("panel.inputPackage")}</h2>
              </div>
              <ShieldCheck size={22} aria-hidden="true" />
            </div>
            <form className="upload-form" onSubmit={onSubmitJob}>
              <label className="dropzone" htmlFor="source-upload">
                <Archive size={24} aria-hidden="true" />
                <div>
                  <strong>{selectedUploadFile?.name ?? t("upload.selectArtifact")}</strong>
                  <span>{selectedUploadFile ? formatBytes(selectedUploadFile.size) : t("upload.fileTypes")}</span>
                </div>
              </label>
              <input
                ref={fileInputRef}
                className="visually-hidden"
                id="source-upload"
                type="file"
                onChange={(event: ChangeEvent<HTMLInputElement>) => onFileChange(event.currentTarget.files?.[0] ?? null)}
              />
              <div className="mode-grid" aria-label={t("app.aria.processingModes")}>
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
                  {isSubmitting ? t("action.uploading") : t("action.createJob")}
                </button>
                <button
                  className="secondary-action compact"
                  type="button"
                  onClick={onRefreshJob}
                  disabled={!currentJob || isRefreshing}
                >
                  <RefreshCw size={16} aria-hidden="true" />
                  {isRefreshing ? t("action.refreshing") : t("action.refresh")}
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
                <p className="panel-kicker">{t("panel.jobKicker")}</p>
                <h2>{currentJob ? currentJob.status : t("panel.noJob")}</h2>
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
                <p className="panel-kicker">{t("panel.artifactKicker")}</p>
                <h2>{artifacts.length} {t("panel.records")}</h2>
              </div>
              <FileCode2 size={22} aria-hidden="true" />
            </div>
            <ArtifactList artifacts={artifacts} selectedArtifact={selectedArtifact} onArtifactSelect={onArtifactSelect} />
          </section>

          <section className="workbench-panel graph-panel motion-item" aria-label={t("app.aria.graph")}>
            <div className="panel-heading padded-heading">
              <div>
                <p className="panel-kicker">{t("panel.graphKicker")}</p>
                <h2>{t("panel.graphTitle")}</h2>
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
                <p className="panel-kicker">{t("panel.detailKicker")}</p>
                <h2>{selectedArtifact ? selectedArtifact.kind : t("panel.noArtifactSelected")}</h2>
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
                <p className="panel-kicker">{t("panel.reportKicker")}</p>
                <h2>{data.reportArtifacts.length} {t("panel.outputs")}</h2>
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
                <p className="panel-kicker">{t("panel.auditKicker")}</p>
                <h2>{t("panel.auditTitle")}</h2>
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
                <p className="panel-kicker">{t("panel.runtimeKicker")}</p>
                <h2>{t("panel.runtimeTitle")}</h2>
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
