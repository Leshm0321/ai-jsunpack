import { useEffect, useMemo, useState } from "react";
import { AlertCircle, Eye, FileText, Link2 } from "lucide-react";
import type { Artifact, Job, RuntimeComparisonReport } from "@ai-jsunpack/shared";
import { fetchArtifactText } from "./api";
import { useLocalization } from "./i18n";
import type { RuntimeComparisonFilters, RuntimeComparisonState } from "./workbench-types";
import { runtimeComparisonListHeight, runtimeComparisonRowHeight } from "./workbench-types";
import {
  artifactDownloadUrl,
  formatPixelDiff,
  formatRuntimeGroups,
  formatScreenshotDiff,
  formatTimestamp,
  formatUnknownValue,
  parseRuntimeComparisonReport,
  runtimeComparisonMatchesFilters,
  runtimeComparisonScopeLabel,
  runtimeEvidenceLabel,
  runtimeScreenshotPreviewItems,
  uniqueRuntimeComparisonScenarios,
  uniqueRuntimeComparisonViewports,
  virtualListRange
} from "./workbench-logic";
import { EmptyState, StatusToken } from "./workbench-common";

export function RuntimeCompareStatus({
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
  const { t } = useLocalization();
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
          <span>{t("runtime.diff")}</span>
          <strong>
            {comparisonArtifacts.length > 0
              ? `${comparisonArtifacts.length} ${t("runtime.comparisonRecorded")}`
              : t("runtime.waitingCompare")}
          </strong>
        </div>
        {report ? <StatusToken status={report.status} /> : null}
      </div>

      {comparison.status === "loading" ? (
        <div className="preview-message">
          <FileText size={18} aria-hidden="true" />
          {t("runtime.loadingComparison")}
        </div>
      ) : null}
      {comparison.status === "error" ? (
        <div className="preview-message preview-error">
          <AlertCircle size={18} aria-hidden="true" />
          {comparison.error ?? t("runtime.comparisonLoadFailed")}
        </div>
      ) : null}
      {comparisonArtifacts.length === 0 ? (
        <span className="runtime-compare-note">{t("runtime.noLinkedCompare")}</span>
      ) : null}
      {comparison.reports.length > 1 ? (
        <>
          <div className="runtime-filter-grid" aria-label={t("app.aria.runtimeFilters")}>
            <label htmlFor="runtime-scenario-filter">
              <span>{t("runtime.scenario")}</span>
              <select
                id="runtime-scenario-filter"
                value={filters.scenario}
                onChange={(event) => {
                  setListScrollTop(0);
                  setFilters((current) => ({ ...current, scenario: event.currentTarget.value }));
                }}
              >
                <option value="all">{t("runtime.allScenarios")}</option>
                {scenarioOptions.map((scenario) => (
                  <option key={scenario} value={scenario}>
                    {scenario}
                  </option>
                ))}
              </select>
            </label>
            <label htmlFor="runtime-viewport-filter">
              <span>{t("runtime.viewport")}</span>
              <select
                id="runtime-viewport-filter"
                value={filters.viewport}
                onChange={(event) => {
                  setListScrollTop(0);
                  setFilters((current) => ({ ...current, viewport: event.currentTarget.value }));
                }}
              >
                <option value="all">{t("runtime.allViewports")}</option>
                {viewportOptions.map((viewport) => (
                  <option key={viewport.value} value={viewport.value}>
                    {viewport.label}
                  </option>
                ))}
              </select>
            </label>
            <label htmlFor="runtime-status-filter">
              <span>{t("audit.status")}</span>
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
                <option value="all">{t("audit.allStatuses")}</option>
                <option value="pass">{t("runtime.pass")}</option>
                <option value="best_effort">{t("runtime.bestEffort")}</option>
                <option value="retry">{t("runtime.retry")}</option>
                <option value="fail">{t("runtime.fail")}</option>
              </select>
            </label>
          </div>
          <div className="runtime-compare-note">
            {t("runtime.showing")} {filteredReports.length} {t("runtime.of")} {comparison.reports.length} {t("runtime.comparisonRows")}
          </div>
          {filteredReports.length > 0 ? (
            <div
              className="runtime-comparison-viewport"
              onScroll={(event) => setListScrollTop(event.currentTarget.scrollTop)}
              style={{ maxHeight: runtimeComparisonListHeight }}
            >
              <div className="runtime-comparison-spacer" style={{ height: filteredReports.length * runtimeComparisonRowHeight }}>
                <div className="runtime-comparison-list" style={{ transform: `translateY(${virtualRange.start * runtimeComparisonRowHeight}px)` }} aria-label={t("app.aria.runtimeMatrix")}>
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
                      <span>{runtimeComparisonScopeLabel(item.report, t)}</span>
                      <small>{formatScreenshotDiff(item.report.differences)}</small>
                      <StatusToken status={item.report.status} />
                    </button>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <EmptyState title={t("empty.noComparisonRows.title")} detail={t("empty.noComparisonRows.detail")} />
          )}
        </>
      ) : null}
      {selectedComparison && report && differences ? (
        <>
          <div className="runtime-diff-grid" aria-label={t("app.aria.runtimeDiff")}>
            <RuntimeDiffMetric label={t("runtime.scope")} value={runtimeComparisonScopeLabel(report, t)} />
            <RuntimeDiffMetric label={t("runtime.screenshot")} value={formatScreenshotDiff(differences)} />
            <RuntimeDiffMetric label={t("runtime.pixels")} value={formatPixelDiff(differences, t)} />
            <RuntimeDiffMetric label={t("runtime.domPaths")} value={String(differences.domDifferences.length)} />
            <RuntimeDiffMetric label={t("runtime.networkGroups")} value={formatRuntimeGroups(differences.networkDiff.groups, t)} />
            <RuntimeDiffMetric label={t("runtime.consoleGroups")} value={formatRuntimeGroups(differences.consoleDiff.groups, t)} />
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
            title={t("runtime.networkDifferences")}
            originalOnly={differences.networkDiff.originalOnly}
            reconstructedOnly={differences.networkDiff.reconstructedOnly}
          />
          <RuntimeCollectionDifferenceList
            title={t("runtime.consoleDifferences")}
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

export function RuntimeDiffMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="runtime-diff-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function RuntimeComparisonEvidenceButtons({
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
  const { t } = useLocalization();
  const linkedIds = [
    comparisonArtifactId,
    report.scenarioArtifactId,
    ...report.traceArtifactIds,
    ...report.screenshotArtifactIds,
    report.differences.screenshotDiff.diffArtifactId
  ].filter((artifactId, index, ids): artifactId is string => Boolean(artifactId) && ids.indexOf(artifactId) === index);
  const knownArtifactIds = new Set(artifacts.map((artifact) => artifact.id));

  return (
    <div className="runtime-evidence-buttons" aria-label={t("app.aria.runtimeEvidence")}>
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
          <span>{runtimeEvidenceLabel(artifactId, report, t)}</span>
          <small>{artifactId}</small>
        </button>
      ))}
    </div>
  );
}

export function RuntimeScreenshotPreview({
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
  const { t } = useLocalization();
  const screenshotItems = runtimeScreenshotPreviewItems(report, t)
    .map((item) => ({
      ...item,
      artifact: artifacts.find((artifact) => artifact.id === item.artifactId) ?? null
    }))
    .filter((item) => item.artifact);

  if (!currentJob || screenshotItems.length === 0) {
    return <RuntimeDiffSection title={t("runtime.screenshotPreviews")} items={[t("runtime.noScreenshots")]} />;
  }

  return (
    <div className="runtime-screenshot-grid" aria-label={t("app.aria.runtimeScreenshots")}>
      {screenshotItems.map((item) => (
        <figure className="runtime-screenshot-card" key={`${item.label}-${item.artifactId}`}>
          <img
            alt={`${item.label} ${t("runtime.screenshotAlt")}`}
            loading="lazy"
            src={artifactDownloadUrl(apiBaseUrl, currentJob.id, item.artifactId)}
          />
          <figcaption>
            <strong>{item.label}</strong>
            <span>{item.detail}</span>
            <button className="download-link" type="button" onClick={() => onArtifactSelect(item.artifactId)}>
              <Link2 size={14} aria-hidden="true" />
              {t("action.locateArtifact")}
            </button>
          </figcaption>
        </figure>
      ))}
    </div>
  );
}

export function RuntimeDomDifferenceList({ differences }: { differences: RuntimeComparisonReport["differences"]["domDifferences"] }) {
  const { t } = useLocalization();
  if (differences.length === 0) {
    return <RuntimeDiffSection title={t("runtime.domDifferences")} items={[t("runtime.noDomChanges")]} />;
  }

  return (
    <div className="runtime-diff-section">
      <span>{t("runtime.domDifferences")}</span>
      {differences.slice(0, 8).map((difference) => (
        <code key={difference.path}>
          {difference.path}: {formatUnknownValue(difference.original)}
          {" -> "}
          {formatUnknownValue(difference.reconstructed)}
        </code>
      ))}
      {differences.length > 8 ? <small>{differences.length - 8} {t("runtime.moreDomChanges")}</small> : null}
    </div>
  );
}

export function RuntimeCollectionDifferenceList({
  originalOnly,
  reconstructedOnly,
  title
}: {
  originalOnly: string[];
  reconstructedOnly: string[];
  title: string;
}) {
  const { t } = useLocalization();
  const items = [
    ...originalOnly.slice(0, 4).map((item) => `${t("runtime.originalOnly")}: ${item}`),
    ...reconstructedOnly.slice(0, 4).map((item) => `${t("runtime.reconstructedOnly")}: ${item}`)
  ];
  if (items.length === 0) {
    return <RuntimeDiffSection title={title} items={[t("runtime.noUniqueEntries")]} />;
  }
  return <RuntimeDiffSection title={title} items={items} />;
}

export function RuntimeDiffSection({ items, title }: { items: string[]; title: string }) {
  return (
    <div className="runtime-diff-section">
      <span>{title}</span>
      {items.map((item) => (
        <code key={item}>{item}</code>
      ))}
    </div>
  );
}

