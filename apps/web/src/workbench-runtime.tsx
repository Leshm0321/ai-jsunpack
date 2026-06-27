import { Link2 } from "lucide-react";
import type { Artifact, Job, RuntimeValidationRun } from "@ai-jsunpack/shared";
import { useLocalization } from "./i18n";
import type { RuntimeMetric } from "./workbench-types";
import { formatTimestamp } from "./workbench-logic";
import { RuntimeCompareStatus } from "./workbench-runtime-compare";
import { ArtifactDownloadLink, DetailItem, EmptyState, StatusToken } from "./workbench-common";

export function RuntimePanel({
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
  const { t } = useLocalization();
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
          <DetailItem label={t("runtime.latestStatus")} value={latestRuntime.status} />
          <DetailItem label={t("runtime.target")} value={latestRuntime.target} />
          <DetailItem label={t("runtime.entryUrl")} value={latestRuntime.entryUrl} />
          <DetailItem label={t("runtime.attempt")} value={String(latestRuntime.attempt)} />
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
          <RuntimeIssueList title={t("runtime.consoleErrors")} items={latestRuntime.consoleErrors} />
          <RuntimeIssueList title={t("runtime.pageErrors")} items={latestRuntime.pageErrors} />
          <RuntimeIssueList title={t("runtime.failedRequests")} items={latestRuntime.failedRequests} />
        </div>
      ) : (
        <EmptyState title={t("empty.noRuntime.title")} detail={t("empty.noRuntime.detail")} />
      )}
      {runtimeValidations.length > 1 ? (
        <div className="runtime-history">
          <div className="section-heading">
            <h3>{t("runtime.history")}</h3>
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

export function RuntimeRunDetail({
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
  const { t } = useLocalization();
  return (
    <div className="runtime-run-card">
      <div className="history-row">
        <StatusToken status={run.status} />
        <strong>{run.target}</strong>
        <small>{run.entryUrl}</small>
      </div>
      <div className="runtime-run-issues">
        <RuntimeIssueList title={t("runtime.console")} items={run.consoleErrors} />
        <RuntimeIssueList title={t("runtime.page")} items={run.pageErrors} />
        <RuntimeIssueList title={t("runtime.requests")} items={run.failedRequests} />
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

export function EvidenceArtifactLinks({
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
  const { t } = useLocalization();
  const linkedArtifacts = artifactIds
    .filter((artifactId): artifactId is string => Boolean(artifactId))
    .map((artifactId) => artifacts.find((artifact) => artifact.id === artifactId))
    .filter((artifact): artifact is Artifact => Boolean(artifact));

  if (linkedArtifacts.length === 0) {
    return null;
  }

  return (
    <div className="evidence-links" aria-label={t("app.aria.runtimeLinks")}>
      {linkedArtifacts.map((artifact) => (
        <div className="evidence-link-group" key={artifact.id}>
          <button className="download-link" type="button" onClick={() => onArtifactSelect(artifact.id)}>
            <Link2 size={15} aria-hidden="true" />
            {artifact.kind}
          </button>
          <ArtifactDownloadLink apiBaseUrl={apiBaseUrl} artifact={artifact} currentJob={currentJob} label={t("action.download")} />
        </div>
      ))}
    </div>
  );
}

export function RuntimeIssueList({ items, title }: { items: string[]; title: string }) {
  const { t } = useLocalization();
  if (items.length === 0) {
    return (
      <div className="issue-list">
        <span>{title}</span>
        <strong>{t("common.none")}</strong>
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

