import { useEffect, useMemo, useState } from "react";
import { AlertCircle, Eye, FileJson2, FileText, Filter, Link2, ListTree } from "lucide-react";
import type { Artifact, Job } from "@ai-jsunpack/shared";
import { fetchArtifactText } from "./api";
import { useLocalization } from "./i18n";
import type { EvidenceIndexPayload, JobEvidence } from "./workbench-types";
import {
  buildFailureSummary,
  buildFallbackPackageContents,
  buildFallbackReportSections,
  filterReportSections,
  formatBytes,
  parseEvidenceIndexPayload,
  reportSectionDetailSummary
} from "./workbench-logic";
import { ArtifactDownloadLink, EmptyState, StatusToken } from "./workbench-common";

export function ReportArtifactList({
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
  const { t } = useLocalization();
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
      .then((text) => parseEvidenceIndexPayload(text, t))
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
  }, [currentJob?.id, evidenceIndexArtifact?.id, t]);

  if (artifacts.length === 0) {
    return <EmptyState title={t("empty.noReportOutputs.title")} detail={t("empty.noReportOutputs.detail")} />;
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
    evidenceIndex.payload?.failureSummary.length ? evidenceIndex.payload.failureSummary : buildFailureSummary(evidence, currentJob, t);
  const indexedPackageContents = packageContents.length > 0 ? packageContents : buildFallbackPackageContents(attachments, t);
  const indexedReportSections = reportSections.length > 0 ? reportSections : buildFallbackReportSections(attachments, artifacts, t);
  const filteredReportSections = filterReportSections(indexedReportSections, reportDetailQuery, t);
  const reportDetailFilterActive = reportDetailQuery.trim().length > 0;
  const riskCount = failureSummary.length || reviewAttention;

  return (
    <div className="report-list">
      <div className="report-summary-grid" aria-label={t("app.aria.reportSummary")}>
        <ReportMetric label={t("report.metric.markdown")} value={String(markdownReports)} />
        <ReportMetric label={t("report.metric.html")} value={String(htmlReports)} />
        <ReportMetric label={t("report.metric.packages")} value={String(packages)} />
        <ReportMetric label={t("report.metric.evidenceFiles")} value={String(evidenceIndex.payload?.includedCount ?? 0)} />
      </div>

      <div className={riskCount > 0 ? "report-risk-strip warning" : "report-risk-strip"}>
        <AlertCircle size={17} aria-hidden="true" />
        <div>
          <strong>{currentJob?.failureClass === "none" ? t("report.noFailureClass") : currentJob?.failureClass ?? t("report.awaitingJob")}</strong>
          <span>
            {riskCount > 0
              ? `${riskCount} ${t("report.attention")}`
              : currentJob?.failureReason ?? t("report.confidence")}
          </span>
        </div>
      </div>

      <section className="report-detail-block" aria-label={t("app.aria.failureSummary")}>
        <div className="section-heading">
          <h3>{t("report.failureSummary")}</h3>
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
          <EmptyState title={t("empty.noPackagedFailures.title")} detail={t("empty.noPackagedFailures.detail")} />
        )}
      </section>

      <section className="report-detail-block" aria-label={t("app.aria.reportDownloads")}>
        <div className="section-heading">
          <h3>{t("report.artifacts")}</h3>
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
                {t("action.inspect")}
              </button>
              <ArtifactDownloadLink apiBaseUrl={apiBaseUrl} artifact={artifact} currentJob={currentJob} label={t("action.download")} />
            </div>
          </div>
        ))}
      </section>

      <section className="report-detail-block" aria-label={t("app.aria.packageContents")}>
        <div className="section-heading">
          <h3>{t("report.packageContents")}</h3>
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
                      {t("action.locate")}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}
        {evidenceIndex.status === "ready" && indexedPackageContents.length === 0 ? (
          <EmptyState title={t("empty.noPackageIndex.title")} detail={t("empty.noPackageIndex.detail")} />
        ) : null}
      </section>

      <section className="report-detail-block" aria-label={t("app.aria.reportMap")}>
        <div className="section-heading">
          <h3>{t("report.evidenceMap")}</h3>
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
              <span className="visually-hidden">{t("report.filterHidden")}</span>
              <input
                aria-label={t("report.filterHidden")}
                placeholder={t("report.filterPlaceholder")}
                type="search"
                value={reportDetailQuery}
                onChange={(event) => setReportDetailQuery(event.target.value)}
              />
              {reportDetailFilterActive ? (
                <button className="secondary-action compact" type="button" onClick={() => setReportDetailQuery("")}>{t("action.clear")}</button>
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
                                <small>{reportSectionDetailSummary(detail, t)}</small>
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
                      {section.artifactIds.length > 5 ? <span className="report-overflow-note">{section.artifactIds.length - 5} {t("report.more")}</span> : null}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState title={t("empty.noMatchingReportDetails.title")} detail={t("empty.noMatchingReportDetails.detail")} />
            )}
          </>
        ) : null}
        {evidenceIndex.status === "ready" && indexedReportSections.length === 0 ? (
          <EmptyState title={t("empty.noReportMap.title")} detail={t("empty.noReportMap.detail")} />
        ) : null}
      </section>

      <section className="report-detail-block" aria-label={t("app.aria.attachmentIndex")}>
        <div className="section-heading">
          <h3>{t("report.evidenceAttachments")}</h3>
          <span>{evidenceIndex.status === "ready" ? attachments.length : evidenceIndex.status}</span>
        </div>
        {evidenceIndex.status === "loading" ? (
          <div className="preview-message">
            <FileText size={18} aria-hidden="true" />
            {t("report.loadingEvidence")}
          </div>
        ) : null}
        {evidenceIndex.status === "error" ? (
          <div className="preview-message preview-error">
            <AlertCircle size={18} aria-hidden="true" />
            {evidenceIndex.error ?? t("report.evidenceLoadFailed")}
          </div>
        ) : null}
        {evidenceIndex.status === "idle" ? (
          <EmptyState title={t("empty.noEvidenceIndex.title")} detail={t("empty.noEvidenceIndex.detail")} />
        ) : null}
        {evidenceIndex.status === "ready" && attachments.length === 0 ? (
          <EmptyState title={t("empty.noIndexedAttachments.title")} detail={t("empty.noIndexedAttachments.detail")} />
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
                      {t("action.locate")}
                    </button>
                    {linkedArtifact ? (
                      <ArtifactDownloadLink apiBaseUrl={apiBaseUrl} artifact={linkedArtifact} currentJob={currentJob} label={t("action.download")} />
                    ) : (
                      <span className="download-disabled">{t("action.missing")}</span>
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

export function ReportMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="report-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

