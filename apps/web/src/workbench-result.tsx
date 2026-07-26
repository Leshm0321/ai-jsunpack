import { useEffect, useMemo, useRef, useState } from "react";
import { useGSAP } from "@gsap/react";
import { gsap } from "gsap";
import { AlertTriangle, CheckCircle2, Download, FileCheck2, FileText, RefreshCw, Sparkles, Trash2 } from "lucide-react";
import { deleteJob, downloadArtifactFile, downloadJobResult, fetchJobResult } from "./api";
import { appMotion } from "./app-motion";
import { useLocalization } from "./i18n";
import type { AppRoute } from "./routes";
import { EmptyState, StatusBanner, StatusToken } from "./workbench-common";
import { formatBytes } from "./workbench-logic";
import type { JobResultResponse, ProductArtifact } from "./workbench-types";

interface SummarySection {
  key: string;
  label: string;
  tone: "neutral" | "pass" | "warning";
  value: string[];
}

export function WorkbenchResult({ jobId, onNavigate }: { jobId: string; onNavigate: (route: AppRoute) => void }) {
  const { t } = useLocalization();
  const rootRef = useRef<HTMLDivElement>(null);
  const [result, setResult] = useState<JobResultResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [downloading, setDownloading] = useState(false);
  const [downloadingReportId, setDownloadingReportId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    fetchJobResult(jobId)
      .then((value) => {
        if (active) setResult(value);
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [jobId]);

  useEffect(() => {
    if (!result || isTerminal(result.job.status)) return;
    let active = true;
    const intervalId = window.setInterval(() => {
      fetchJobResult(jobId)
        .then((value) => {
          if (active) setResult(value);
        })
        .catch((reason) => {
          if (active) setError(reason instanceof Error ? reason.message : String(reason));
        });
    }, 2500);
    return () => {
      active = false;
      window.clearInterval(intervalId);
    };
  }, [jobId, result?.job.status]);

  const sections = useMemo(() => normalizeSummary(result?.summary, t), [result?.summary, t]);

  useGSAP(
    () => {
      const media = gsap.matchMedia();
      media.add({ reduceMotion: "(prefers-reduced-motion: reduce)" }, (context) => {
        const targets = gsap.utils.toArray<HTMLElement>(".result-hero, .result-report-card, .result-secondary-card", rootRef.current);
        if (targets.length === 0) return;
        const reduced = Boolean(context.conditions?.reduceMotion);
        if (reduced) {
          gsap.set(targets, { autoAlpha: 1, clearProps: "transform" });
          return;
        }
        gsap.fromTo(targets, { autoAlpha: 0 }, { autoAlpha: 1, duration: appMotion.duration.normal, ease: appMotion.ease.enter, stagger: appMotion.stagger.compact, overwrite: "auto" });
      });
      return () => media.revert();
    },
    { dependencies: [result, loading], revertOnUpdate: true, scope: rootRef }
  );

  const expired = result ? filesExpired(result) : false;
  const downloadable = Boolean(result?.primaryResult && result.downloadUrl) && !expired;
  const fallbackFilename = result?.primaryResult?.filename || result?.job.inputName || `ai-jsunpack-${jobId}-result`;

  const handleDownload = async () => {
    if (!downloadable || downloading) return;
    setDownloading(true);
    setError(null);
    try {
      await downloadJobResult(jobId, fallbackFilename);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setDownloading(false);
    }
  };

  const handleDelete = async () => {
    if (!confirmDelete || deleting) return;
    setDeleting(true);
    setError(null);
    try {
      await deleteJob(jobId);
      onNavigate("/workbench/history");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setDeleting(false);
    }
  };

  const handleReportDownload = async (artifact: ProductArtifact) => {
    if (downloadingReportId) return;
    setDownloadingReportId(artifact.id);
    setError(null);
    try {
      await downloadArtifactFile(jobId, artifact.id, artifact.filename || reportLabel(artifact, t));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setDownloadingReportId(null);
    }
  };

  if (loading && !result) {
    return <section className="route-panel result-loading"><RefreshCw size={18} aria-hidden="true" />{t("result.loading")}</section>;
  }
  if (!result) {
    return <section className="route-panel"><StatusBanner tone="error" message={error || t("result.unavailable")} /><EmptyState title={t("result.unavailable")} detail={t("result.unavailableDetail")} /></section>;
  }

  return (
    <div className="result-view" ref={rootRef}>
      {error ? <StatusBanner tone="error" message={error} /> : null}
      <section className="route-panel result-hero">
        <div className="result-hero-copy">
          <span className="result-icon"><FileCheck2 size={24} aria-hidden="true" /></span>
          <div><p className="panel-kicker">{t("result.deliveryKicker")}</p><h2>{result.job.inputName || result.primaryResult?.filename || t("result.ready")}</h2><p>{expired ? t("result.expiredExplanation") : deliveryDescription(result, t)}</p></div>
        </div>
        <div className="result-hero-status"><StatusToken status={result.job.status} />{result.primaryResult ? <span>{formatBytes(result.primaryResult.size)}</span> : null}</div>
        <button className="primary-action result-download-action" type="button" disabled={!downloadable || downloading} onClick={() => void handleDownload()}>
          <Download size={18} aria-hidden="true" />{downloading ? t("result.downloading") : expired ? t("result.filesExpired") : t("result.downloadPrimary")}
        </button>
      </section>

      {expired ? <div className="result-expired-banner"><AlertTriangle size={18} aria-hidden="true" /><div><strong>{t("result.historyRetained")}</strong><span>{t("result.expiredExplanation")}</span></div></div> : null}

      <section className="result-report-grid" aria-label={t("result.reportTitle")}>
        {sections.map((section) => (
          <article className={`result-report-card result-tone-${section.tone}`} key={section.key}>
            <div className="result-report-heading">{section.tone === "pass" ? <CheckCircle2 size={18} aria-hidden="true" /> : section.tone === "warning" ? <AlertTriangle size={18} aria-hidden="true" /> : <Sparkles size={18} aria-hidden="true" />}<h3>{section.label}</h3></div>
            {section.value.length === 1 ? <p>{section.value[0]}</p> : <ul>{section.value.map((item, index) => <li key={`${section.key}-${index}`}>{item}</li>)}</ul>}
          </article>
        ))}
      </section>

      <section className="route-panel result-secondary-card">
        <div className="result-section-heading"><div><FileText size={19} aria-hidden="true" /><span><strong>{t("result.reportDownloads")}</strong><small>{t("result.reportDownloadsDetail")}</small></span></div></div>
        <div className="result-report-downloads">
          {visibleReports(result.reportArtifacts).map((artifact) => (
            <button className="download-link" disabled={Boolean(downloadingReportId)} key={artifact.id} type="button" onClick={() => void handleReportDownload(artifact)}><FileText size={15} aria-hidden="true" /><span>{downloadingReportId === artifact.id ? t("result.downloading") : artifact.filename || reportLabel(artifact, t)}</span></button>
          ))}
          {visibleReports(result.reportArtifacts).length === 0 ? <span className="muted-inline">{t("result.noDownloadableReports")}</span> : null}
        </div>
      </section>

      <section className="route-panel result-secondary-card result-danger-zone">
        <div><Trash2 size={19} aria-hidden="true" /><span><strong>{t("result.deleteTitle")}</strong><small>{t("result.deleteDetail")}</small></span></div>
        {!confirmDelete ? <button className="danger-action" type="button" onClick={() => setConfirmDelete(true)}><Trash2 size={16} aria-hidden="true" />{t("action.delete")}</button> : <div className="delete-confirmation"><strong>{t("result.deleteConfirm")}</strong><div><button className="secondary-action compact" type="button" onClick={() => setConfirmDelete(false)}>{t("action.cancel")}</button><button className="danger-action" type="button" disabled={deleting} onClick={() => void handleDelete()}>{deleting ? t("result.deleting") : t("result.deletePermanently")}</button></div></div>}
      </section>
    </div>
  );
}

function visibleReports(artifacts: ProductArtifact[]): ProductArtifact[] {
  return artifacts.filter((artifact) => artifact.kind !== "evidence_package" && artifact.kind !== "result_summary" && (artifact.kind === "html_report" || artifact.kind === "audit_report" || /markdown|html|text\/plain/i.test(artifact.contentType)));
}

function isTerminal(status: string): boolean {
  return status === "completed" || status === "completed_best_effort" || status === "failed" || status === "cancelled";
}

function filesExpired(result: JobResultResponse): boolean {
  const expiry = result.job.filesExpiresAt || result.primaryResult?.expiresAt;
  return Boolean(result.primaryResult?.deletedAt || (expiry && Date.parse(expiry) <= Date.now()));
}

function deliveryDescription(result: JobResultResponse, t: (key: string) => string): string {
  if (!result.primaryResult) return t("result.processing");
  if (result.job.deliveryKind === "single_file") return t("result.delivery.singleFile");
  if (result.job.deliveryKind === "project_package") return t("result.delivery.projectArchive");
  return t("result.delivery.ready");
}

function reportLabel(artifact: ProductArtifact, t: (key: string) => string): string {
  if (artifact.kind === "html_report") return t("result.report.html");
  if (/markdown/i.test(artifact.contentType) || artifact.filename?.endsWith(".md")) return t("result.report.markdown");
  return t("result.report.other");
}

function normalizeSummary(value: unknown, t: (key: string) => string): SummarySection[] {
  const record = isRecord(value) ? value : {};
  const sectionDefinitions: Array<{ keys: string[]; label: string; tone: SummarySection["tone"] }> = [
    { keys: ["aiSummary", "summary", "overview", "executiveSummary"], label: t("result.summary.ai"), tone: "neutral" },
    { keys: ["processedScope", "processingScope", "scope", "processedFiles", "files"], label: t("result.summary.scope"), tone: "neutral" },
    { keys: ["majorChanges", "changes", "transformations"], label: t("result.summary.changes"), tone: "neutral" },
    { keys: ["validation", "validationSummary", "verification", "verificationSummary"], label: t("result.summary.validation"), tone: "pass" },
    { keys: ["review", "reviewSummary", "reviewConclusion"], label: t("result.summary.review"), tone: "pass" },
    { keys: ["risks", "risk", "riskSummary"], label: t("result.summary.risks"), tone: "warning" },
    { keys: ["limitations", "constraints", "knownLimitations"], label: t("result.summary.limitations"), tone: "warning" },
    { keys: ["fallbackReason", "downgradeReason", "deliveryReason"], label: t("result.summary.fallback"), tone: "warning" }
  ];
  const sections = sectionDefinitions.flatMap((definition, index) => {
    const selected = definition.keys.map((key) => record[key]).find((item) => item !== undefined && item !== null && item !== "");
    const text = summaryLines(selected);
    return text.length ? [{ key: `${definition.keys[0]}-${index}`, label: definition.label, tone: definition.tone, value: text }] : [];
  });
  if (sections.length > 0) return sections;
  const fallback = summaryLines(value);
  return [{ key: "fallback", label: t("result.summary.ai"), tone: "neutral", value: fallback.length ? fallback : [t("result.summary.pending")] }];
}

function summaryLines(value: unknown): string[] {
  if (typeof value === "string") return value.trim() ? [value.trim()] : [];
  if (typeof value === "number" || typeof value === "boolean") return [String(value)];
  if (Array.isArray(value)) return value.flatMap(summaryLines).slice(0, 24);
  if (isRecord(value)) {
    return Object.entries(value).flatMap(([key, child]) => summaryLines(child).map((line) => `${humanizeKey(key)}: ${line}`)).slice(0, 24);
  }
  return [];
}

function humanizeKey(value: string): string {
  return value.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_-]+/g, " ");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
