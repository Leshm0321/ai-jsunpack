import { useEffect, useRef, useState } from "react";
import { useGSAP } from "@gsap/react";
import { gsap } from "gsap";
import { Archive, ChevronLeft, ChevronRight, Clock3, Filter, FolderKanban, RefreshCw } from "lucide-react";
import { API_PROJECT_ID, fetchJobs } from "./api";
import { appMotion } from "./app-motion";
import { useLocalization } from "./i18n";
import type { AppRoute } from "./routes";
import { workbenchPath } from "./routes";
import { EmptyState, StatusBanner, StatusToken } from "./workbench-common";
import type { JobHistoryResponse, ProductJob } from "./workbench-types";

const pageSize = 20;
const projectFilterDelayMs = 300;

export function WorkbenchHistory({ onNavigate }: { onNavigate: (route: AppRoute) => void }) {
  const { t } = useLocalization();
  const rootRef = useRef<HTMLDivElement>(null);
  const [projectId, setProjectId] = useState("");
  const [debouncedProjectId, setDebouncedProjectId] = useState("");
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const [response, setResponse] = useState<JobHistoryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    const timeoutId = window.setTimeout(() => {
      setDebouncedProjectId(projectId.trim());
      setOffset(0);
    }, projectFilterDelayMs);
    return () => window.clearTimeout(timeoutId);
  }, [projectId]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    fetchJobs({ limit: pageSize, offset, projectId: debouncedProjectId || undefined, status: status || undefined })
      .then((value) => {
        if (active) setResponse(value);
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
  }, [debouncedProjectId, offset, reloadKey, status]);

  useGSAP(
    () => {
      const media = gsap.matchMedia();
      media.add({ reduceMotion: "(prefers-reduced-motion: reduce)" }, (context) => {
        const rows = gsap.utils.toArray<HTMLElement>(".history-job-card", rootRef.current);
        if (rows.length === 0) return;
        const reduced = Boolean(context.conditions?.reduceMotion);
        if (reduced) {
          gsap.set(rows, { autoAlpha: 1, clearProps: "transform" });
          return;
        }
        gsap.fromTo(
          rows,
          { autoAlpha: 0 },
          {
            autoAlpha: 1,
            duration: appMotion.duration.normal,
            ease: appMotion.ease.enter,
            stagger: appMotion.stagger.compact,
            overwrite: "auto"
          }
        );
      });
      return () => media.revert();
    },
    { dependencies: [response, loading], revertOnUpdate: true, scope: rootRef }
  );

  const items = response?.items ?? [];
  const total = response?.total ?? 0;
  const page = Math.floor(offset / pageSize) + 1;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="history-view" ref={rootRef}>
      <section className="route-panel history-filter-panel">
        <div className="history-filter-heading">
          <div><Filter size={18} aria-hidden="true" /><span><strong>{t("history.filters.title")}</strong><small>{t("history.filters.detail")}</small></span></div>
          <button className="secondary-action compact" type="button" disabled={loading} onClick={() => setReloadKey((value) => value + 1)}>
            <RefreshCw size={15} aria-hidden="true" />{t("action.refresh")}
          </button>
        </div>
        <div className="history-filter-grid">
          <label><span>{t("history.filters.project")}</span><input value={projectId} placeholder={API_PROJECT_ID} onChange={(event) => setProjectId(event.currentTarget.value)} /></label>
          <label><span>{t("history.filters.status")}</span><select value={status} onChange={(event) => { setStatus(event.currentTarget.value); setOffset(0); }}><option value="">{t("history.filters.allStatuses")}</option><option value="completed">{t("stage.completed")}</option><option value="completed_best_effort">{t("stage.completed_best_effort")}</option><option value="failed">{t("stage.failed")}</option><option value="cancelled">{t("stage.cancelled")}</option><option value="reviewing">{t("stage.reviewing")}</option></select></label>
        </div>
      </section>

      {error ? <StatusBanner tone="error" message={error} /> : null}
      <section className="route-panel history-list-panel" aria-busy={loading}>
        <div className="history-list-summary"><span>{t("history.accountScope")}</span><strong className="motion-metric-value">{total}</strong></div>
        {loading && items.length === 0 ? <div className="history-loading"><RefreshCw size={18} aria-hidden="true" />{t("history.loading")}</div> : null}
        {!loading && items.length === 0 ? <EmptyState title={t("history.empty.title")} detail={t("history.empty.detail")} /> : null}
        <div className="history-job-list">
          {items.map(({ job, primaryResult }) => {
            const expired = filesExpired(job, primaryResult);
            return (
              <button className="history-job-card" key={job.id} type="button" onClick={() => onNavigate(workbenchPath(job.id, "result"))}>
                <span className="history-job-icon">{job.inputKind === "folder" ? <FolderKanban size={19} aria-hidden="true" /> : <Archive size={19} aria-hidden="true" />}</span>
                <span className="history-job-main"><strong>{job.inputName || primaryResult?.filename || job.id}</strong><small>{job.projectId} · {formatDateTime(job.createdAt)}</small></span>
                <span className="history-job-delivery"><small>{t("history.delivery")}</small><strong>{deliveryLabel(job, t)}</strong></span>
                <span className="history-job-retention"><Clock3 size={14} aria-hidden="true" /><span>{expired ? t("history.filesExpired") : retentionLabel(job, t)}</span></span>
                <StatusToken status={job.status} />
                <ChevronRight size={17} aria-hidden="true" />
              </button>
            );
          })}
        </div>
        <div className="history-pagination">
          <button className="secondary-action compact" type="button" disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}><ChevronLeft size={15} aria-hidden="true" />{t("history.previous")}</button>
          <span>{t("history.page")} {page} / {pageCount}</span>
          <button className="secondary-action compact" type="button" disabled={loading || offset + pageSize >= total} onClick={() => setOffset(offset + pageSize)}>{t("history.next")}<ChevronRight size={15} aria-hidden="true" /></button>
        </div>
      </section>
    </div>
  );
}

function filesExpired(job: ProductJob, artifact?: { deletedAt?: string | null; expiresAt?: string | null } | null): boolean {
  const expiry = job.filesExpiresAt || artifact?.expiresAt;
  return Boolean(artifact?.deletedAt || (expiry && Date.parse(expiry) <= Date.now()));
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function deliveryLabel(job: ProductJob, t: (key: string) => string): string {
  if (job.deliveryKind === "single_file") return t("history.delivery.singleFile");
  if (job.deliveryKind === "project_package") return t("history.delivery.projectArchive");
  return job.deliveryKind || t("history.delivery.pending");
}

function retentionLabel(job: ProductJob, t: (key: string) => string): string {
  if (!job.filesExpiresAt) return t("history.filesAvailable");
  return `${t("history.expires")} ${formatDateTime(job.filesExpiresAt)}`;
}
