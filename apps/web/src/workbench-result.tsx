import { useEffect, useMemo, useRef, useState } from "react";
import { useGSAP } from "@gsap/react";
import { gsap } from "gsap";
import { AlertTriangle, CheckCircle2, Download, FileCheck2, FileText, RefreshCw, Sparkles, Trash2 } from "lucide-react";
import { deleteJob, downloadArtifactFile, downloadJobResult, fetchJobResult } from "./api";
import { appMotion } from "./app-motion";
import { useLocalization, type Language } from "./i18n";
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
  const { language, t } = useLocalization();
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

  const sections = useMemo(() => normalizeSummary(result?.summary, t, language), [language, result?.summary, t]);

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

function normalizeSummary(value: unknown, t: (key: string) => string, language: Language): SummarySection[] {
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
    const text = index === 0 ? overviewLines(record, selected, language) : summaryLines(selected);
    return text.length ? [{ key: `${definition.keys[0]}-${index}`, label: definition.label, tone: definition.tone, value: text }] : [];
  });
  if (sections.length > 0) return sections;
  const fallback = summaryLines(value);
  return [{ key: "fallback", label: t("result.summary.ai"), tone: "neutral", value: fallback.length ? fallback : [t("result.summary.pending")] }];
}

function overviewLines(record: Record<string, unknown>, selected: unknown, language: Language): string[] {
  const direct = directSummaryText(selected);
  if (direct && !isAgentStatusBoilerplate(direct)) return [direct];
  const synthesized = synthesizeResultOverview(record, language);
  return synthesized ? [synthesized] : [];
}

function directSummaryText(value: unknown): string | undefined {
  if (typeof value === "string") return value.trim() || undefined;
  if (!isRecord(value)) return undefined;
  for (const key of ["text", "content", "summary", "value"]) {
    const child = value[key];
    if (typeof child === "string" && child.trim()) return child.trim();
  }
  return undefined;
}

function isAgentStatusBoilerplate(value: string): boolean {
  const normalized = value.replace(/\s+/g, " ").trim();
  return (
    /\b[A-Za-z]+Agent\s*已完成.*(?:没有明确的审查决策|未给出明确结论)/i.test(normalized)
    || /\b[A-Za-z]+Agent\s+(?:has\s+)?completed.*(?:without|no).*(?:review decision|conclusion)/i.test(normalized)
  );
}

function synthesizeResultOverview(record: Record<string, unknown>, language: Language): string | undefined {
  const scope = firstRecord(record, ["processingScope", "processedScope", "scope"]);
  const inputName = stringField(scope, "inputName") || stringField(record, "inputName");
  const transformedFiles = arrayValue(scope?.transformedFiles);
  const transformedFileCount = numberField(scope, "transformedFileCount") ?? transformedFiles.length;

  const review = firstRecord(record, ["review", "reviewSummary"]);
  const reportSections = recordArray(review?.reportSections);
  const reviewRuns = recordArray(review?.runs);
  const namingSection = reportSections.find((section) => stringField(section, "anchor") === "naming-recovery");
  const namingCount = namingInferenceCount(namingSection);
  const acceptedNamingCount = namingSection
    ? recordArray(namingSection.details).filter((detail) => /(?:^|\|\s*)accepted(?:\s*\||$)/i.test(stringField(detail, "label") || "")).length
    : 0;
  const agentReview = latestReviewRecord(reviewRuns, "agent_review");
  const namingApplied = agentReview?.status === "pass"
    && /apply_symbol_rename_map|符号重命名|重命名映射|rename map/i.test(agentReview.decision || "");

  const validation = firstRecord(record, ["validation", "validationSummary", "verification"]);
  const buildRecords = [...recordArray(validation?.build), ...reviewRuns];
  const runtimeRecords = recordArray(validation?.runtime);
  const buildStatus = latestReviewRecord(buildRecords, "build")?.status;
  const typecheckStatus = latestReviewRecord(buildRecords, "typecheck")?.status;
  const latestRuntime = latestAttemptRecord(runtimeRecords);
  const runtimeStatus = latestReviewRecord(reviewRuns, "runtime_compare")?.status
    || stringField(latestRuntime, "status");

  const delivery = firstRecord(record, ["delivery"]);
  const deliveryKind = stringField(delivery, "kind");
  const downgraded = delivery?.downgraded === true;
  const risks = recordArray(record.risks);
  const limitations = arrayValue(record.limitations).filter((item): item is string => typeof item === "string" && Boolean(item.trim()));
  const placeholderCount = dependencyPlaceholderCount(risks, limitations);

  const hasResultEvidence = Boolean(
    inputName
    || transformedFileCount
    || namingCount
    || buildStatus
    || typecheckStatus
    || runtimeStatus
    || deliveryKind
    || placeholderCount
    || limitations.length
  );
  if (!hasResultEvidence) return undefined;

  if (language === "en") {
    const sentences: string[] = [];
    if (inputName) {
      sentences.push(`This run deobfuscated ${inputName} and produced ${transformedFileCount} transformed file${transformedFileCount === 1 ? "" : "s"}.`);
    } else {
      sentences.push(`This run completed deobfuscation and produced ${transformedFileCount} transformed file${transformedFileCount === 1 ? "" : "s"}.`);
    }
    if (namingCount) {
      if (namingApplied) sentences.push(`NamingAgent produced ${namingCount} naming inferences, and high-confidence mappings were applied through deterministic symbol renaming.`);
      else if (acceptedNamingCount) sentences.push(`NamingAgent produced ${namingCount} naming inferences, including ${acceptedNamingCount} high-confidence accepted results.`);
      else sentences.push(`NamingAgent produced ${namingCount} naming inferences.`);
    }
    sentences.push(validationOverviewSentence("en", buildStatus, typecheckStatus, runtimeStatus));
    sentences.push(deliveryOverviewSentence("en", deliveryKind));
    if (placeholderCount) sentences.push(`${placeholderCount} missing static relative ESM dependenc${placeholderCount === 1 ? "y remains" : "ies remain"} represented by explicit placeholder modules, so module-semantic recovery is still best effort and must not be treated as absolute behavioral equivalence.`);
    else if (downgraded || limitations.length) sentences.push(`${limitations.length || 1} known limitation${limitations.length === 1 ? " remains" : "s remain"}; the result should not be treated as complete module-semantic recovery or absolute behavioral equivalence.`);
    return sentences.filter(Boolean).join(" ");
  }

  const sentences: string[] = [];
  if (inputName) sentences.push(`本次对 ${inputName} 完成反混淆并生成 ${transformedFileCount} 个转换文件。`);
  else sentences.push(`本次已完成反混淆并生成 ${transformedFileCount} 个转换文件。`);
  if (namingCount) {
    if (namingApplied) sentences.push(`NamingAgent 生成 ${namingCount} 条命名推断，高置信度映射已用于确定性符号重命名。`);
    else if (acceptedNamingCount) sentences.push(`NamingAgent 生成 ${namingCount} 条命名推断，其中 ${acceptedNamingCount} 条为高置信度 accepted 结果。`);
    else sentences.push(`NamingAgent 生成 ${namingCount} 条命名推断。`);
  }
  sentences.push(validationOverviewSentence("zh", buildStatus, typecheckStatus, runtimeStatus));
  sentences.push(deliveryOverviewSentence("zh", deliveryKind));
  if (placeholderCount) sentences.push(`仍有 ${placeholderCount} 个缺失的静态相对 ESM 依赖使用显式占位模块，因此模块语义恢复仍应按 best effort 理解，不能视为绝对行为等价。`);
  else if (downgraded || limitations.length) sentences.push(`仍有 ${limitations.length || 1} 项已知限制，结果不应视为完整模块语义恢复或绝对行为等价。`);
  return sentences.filter(Boolean).join("");
}

function validationOverviewSentence(language: Language, build?: string, typecheck?: string, runtime?: string): string {
  const checks = [["build", build], ["typecheck", typecheck], ["runtime", runtime]] as const;
  if (checks.every(([, status]) => status === "pass")) {
    return language === "zh" ? "构建、类型检查和运行时对比均通过。" : "Build, typecheck, and runtime comparison all passed.";
  }
  const available = checks.filter(([, status]) => Boolean(status));
  if (!available.length) return "";
  const labels = language === "zh"
    ? { build: "构建", typecheck: "类型检查", runtime: "运行时对比" }
    : { build: "Build", typecheck: "typecheck", runtime: "runtime comparison" };
  const statuses = language === "zh"
    ? { pass: "通过", fail: "未通过", best_effort: "按 best effort 完成", retry: "需要重试", skipped: "已跳过", unknown: "状态未知" }
    : { pass: "passed", fail: "failed", best_effort: "completed best effort", retry: "needs retry", skipped: "was skipped", unknown: "has unknown status" };
  const details = available.map(([key, status]) => `${labels[key]}${language === "zh" ? "" : " "}${statuses[status as keyof typeof statuses] || status}`);
  return language === "zh" ? `验证结果：${details.join("、")}。` : `Validation: ${details.join(", ")}.`;
}

function deliveryOverviewSentence(language: Language, kind?: string): string {
  if (kind === "single_file") return language === "zh" ? "结果以独立 JavaScript 文件交付。" : "The result is delivered as a standalone JavaScript file.";
  if (kind === "project_package") return language === "zh" ? "结果以可运行项目 ZIP 交付。" : "The result is delivered as a runnable project ZIP.";
  if (kind === "unavailable") return language === "zh" ? "当前未生成可下载的主要结果。" : "No downloadable primary result was generated.";
  return kind ? (language === "zh" ? "主要结果已生成。" : "The primary result is ready.") : "";
}

function namingInferenceCount(section: Record<string, unknown> | undefined): number {
  if (!section) return 0;
  const narrative = [stringField(section, "content"), stringField(section, "summary")].filter(Boolean).join(" ");
  const match = narrative.match(/(?:生成|generated)\s*(\d+)\s*(?:条|naming)/i)
    || narrative.match(/(\d+)\s*(?:条\s*)?(?:命名推断|naming inferences?)/i);
  if (match) return Number(match[1]);
  return recordArray(section.details).length;
}

function dependencyPlaceholderCount(risks: Array<Record<string, unknown>>, limitations: string[]): number {
  for (const risk of risks) {
    if (stringField(risk, "source") !== "dependency_placeholders") continue;
    const match = (stringField(risk, "summary") || "").match(/\d+/);
    if (match) return Number(match[0]);
  }
  for (const limitation of limitations) {
    if (!/(?:ESM|dependency|依赖)/i.test(limitation) || !/(?:placeholder|占位)/i.test(limitation)) continue;
    const match = limitation.match(/\d+/);
    if (match) return Number(match[0]);
  }
  return 0;
}

function latestReviewRecord(records: Array<Record<string, unknown>>, reviewType: string): { status?: string; decision?: string } | undefined {
  const matching = records.filter((record) => stringField(record, "reviewType") === reviewType);
  const latest = latestAttemptRecord(matching);
  if (!latest) return undefined;
  return { status: stringField(latest, "status"), decision: stringField(latest, "decision") };
}

function latestAttemptRecord(records: Array<Record<string, unknown>>): Record<string, unknown> | undefined {
  return records.reduce<Record<string, unknown> | undefined>((latest, record) => {
    if (!latest) return record;
    return (numberField(record, "attempt") || 0) >= (numberField(latest, "attempt") || 0) ? record : latest;
  }, undefined);
}

function firstRecord(record: Record<string, unknown>, keys: string[]): Record<string, unknown> | undefined {
  return keys.map((key) => record[key]).find(isRecord);
}

function recordArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function stringField(record: Record<string, unknown> | undefined, key: string): string | undefined {
  const value = record?.[key];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function numberField(record: Record<string, unknown> | undefined, key: string): number | undefined {
  const value = record?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
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
