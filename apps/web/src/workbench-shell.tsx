import { Archive, CheckCircle2, ChevronRight, Download, GitBranch, Languages, Network, Radar, RotateCcw, ShieldCheck, Sparkles, Upload } from "lucide-react";
import type { Artifact, CloudMode, Job } from "@ai-jsunpack/shared";
import { useLocalization } from "./i18n";
import type { Language } from "./i18n";
import type { JobEvidence } from "./workbench-types";
import { downloadJsonFile, formatTimestamp } from "./workbench-logic";

export function ModePill({
  active = false,
  label,
  onClick
}: {
  active?: boolean;
  label: CloudMode;
  onClick: () => void;
}) {
  const { t } = useLocalization();
  return (
    <button
      aria-pressed={active}
      className={active ? "mode-pill mode-pill-active" : "mode-pill"}
      type="button"
      onClick={onClick}
      title={t(`cloud.${label}`)}
    >
      {t(`cloud.${label}`)}
    </button>
  );
}

export function LanguageToggle({
  language,
  onLanguageChange
}: {
  language: Language;
  onLanguageChange: (language: Language) => void;
}) {
  const { t } = useLocalization();
  return (
    <div className="language-toggle" aria-label={t("app.aria.toggleLanguage")}>
      <Languages size={16} aria-hidden="true" />
      <span className="visually-hidden">{t("language.current")}</span>
      {(["en", "zh"] as const).map((option) => (
        <button
          aria-pressed={language === option}
          className={language === option ? "language-option language-option-active" : "language-option"}
          key={option}
          type="button"
          onClick={() => onLanguageChange(option)}
        >
          {t(`language.${option}`)}
        </button>
      ))}
    </div>
  );
}

export function JobSummaryPanel({
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
  const { t } = useLocalization();
  if (!currentJob) {
    return (
      <div className="job-summary">
        <span>{t("job.api")}</span>
        <strong>{apiBaseUrl}</strong>
      </div>
    );
  }

  return (
    <div className="job-summary">
      <span>{t("job.job")}</span>
      <strong>{currentJob.id}</strong>
      <span>{t("job.updated")}</span>
      <strong>{formatTimestamp(currentJob.updatedAt)}</strong>
      <span>{t("job.artifacts")}</span>
      <strong>{artifacts.length}</strong>
      <span>{t("job.audit")}</span>
      <strong>{evidence.inferenceRecords.length + evidence.reviewRuns.length + evidence.toolCalls.length}</strong>
      <span>{t("job.runtime")}</span>
      <strong>{evidence.runtimeValidations.length}</strong>
      {currentJob.failureReason ? (
        <>
          <span>{t("job.failure")}</span>
          <strong>{currentJob.failureReason}</strong>
        </>
      ) : null}
    </div>
  );
}

export function WorkspaceActions({
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
  const { t } = useLocalization();
  const canRerun = Boolean(currentJob && artifacts.some((artifact) => artifact.kind === "source_input"));
  return (
    <div className="workspace-actions" aria-label={t("app.aria.workspaceActions")}>
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
        {t("action.exportJson")}
      </button>
      <button
        className="secondary-action compact"
        type="button"
        disabled={!canRerun || isRerunning}
        onClick={onRerunJob}
        title={canRerun ? t("workspace.rerunTitle.ready") : t("workspace.rerunTitle.blocked")}
      >
        <RotateCcw size={16} aria-hidden="true" />
        {isRerunning ? t("action.rerunning") : t("action.rerun")}
      </button>
    </div>
  );
}

export function PipelineMap({
  artifacts,
  currentJob,
  evidence
}: {
  artifacts: Artifact[];
  currentJob: Job | null;
  evidence: JobEvidence;
}) {
  const { t } = useLocalization();
  const nodes = [
    { label: t("pipeline.input"), icon: Upload },
    { label: t("pipeline.ast"), icon: GitBranch },
    { label: t("pipeline.agents"), icon: Sparkles },
    { label: t("pipeline.runtime"), icon: Radar },
    { label: t("pipeline.review"), icon: ShieldCheck }
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
        <span>{currentJob ? t(`stage.${currentJob.status}`) : t("pipeline.awaiting")}</span>
      </div>
      <div className={artifacts.length > 0 ? "pipeline-status" : "pipeline-status warning"}>
        <Archive size={18} aria-hidden="true" />
        <span>{artifacts.length} {t("pipeline.artifacts")}</span>
      </div>
      <div className={evidence.runtimeValidations.length > 0 ? "pipeline-status" : "pipeline-status warning"}>
        <Network size={18} aria-hidden="true" />
        <span>{evidence.runtimeValidations.length} {t("pipeline.runtimeRuns")}</span>
      </div>
    </div>
  );
}
