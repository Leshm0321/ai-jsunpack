import { lazy, Suspense } from "react";
import { AlertCircle, Eye, FileCode2, FileText, Link2 } from "lucide-react";
import type { Artifact, Job } from "@ai-jsunpack/shared";
import type { ArtifactPreview } from "./workbench-types";
import { useLocalization } from "./i18n";
import { artifactPreviewLanguage, formatBytes, formatIdList, formatTimestamp } from "./workbench-logic";
import { ArtifactDownloadLink, DetailItem, EmptyState } from "./workbench-common";

const ArtifactTextEditor = lazy(() => import("./ArtifactTextEditor"));

export function ArtifactList({
  artifacts,
  onArtifactSelect,
  selectedArtifact
}: {
  artifacts: Artifact[];
  onArtifactSelect: (artifactId: string) => void;
  selectedArtifact: Artifact | null;
}) {
  const { t } = useLocalization();
  if (artifacts.length === 0) {
    return <EmptyState title={t("empty.noArtifacts.title")} detail={t("empty.noArtifacts.detail")} />;
  }

  return (
    <div className="file-list artifact-picker" role="listbox" aria-label={t("app.aria.artifacts")}>
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

export function ArtifactDetail({
  apiBaseUrl,
  artifact,
  artifactPreview,
  artifacts,
  onArtifactSelect,
  currentJob
}: {
  apiBaseUrl: string;
  artifact: Artifact | null;
  artifactPreview: ArtifactPreview;
  artifacts: Artifact[];
  currentJob: Job | null;
  onArtifactSelect: (artifactId: string) => void;
}) {
  const { t } = useLocalization();
  if (!artifact) {
    return (
      <div className="detail-surface">
        <EmptyState title={t("empty.selectArtifact.title")} detail={t("empty.selectArtifact.detail")} />
      </div>
    );
  }

  return (
    <div className="detail-surface">
      <div className="detail-grid">
        <DetailItem label={t("detail.artifactId")} value={artifact.id} />
        <DetailItem label={t("detail.kind")} value={artifact.kind} />
        <DetailItem label={t("detail.stage")} value={artifact.stage} />
        <DetailItem label={t("detail.attempt")} value={String(artifact.attempt)} />
        <DetailItem label={t("detail.producer")} value={artifact.producer} />
        <DetailItem label={t("detail.contentType")} value={artifact.contentType} />
        <DetailItem label={t("detail.size")} value={formatBytes(artifact.size)} />
        <DetailItem label={t("detail.hash")} value={artifact.hash} />
        <DetailItem label={t("detail.sensitivity")} value={artifact.sensitivityClass} />
        <DetailItem label={t("detail.retention")} value={artifact.retentionClass} />
        <DetailItem label={t("detail.created")} value={formatTimestamp(artifact.createdAt)} />
        <DetailItem label={t("detail.schema")} value={artifact.schemaVersion} />
      </div>
      <div className="detail-block">
        <span>{t("detail.storageUri")}</span>
        <code>{artifact.storageUri}</code>
      </div>
      <div className="detail-block">
        <span>{t("detail.parentArtifacts")}</span>
        <code>{formatIdList(artifact.parentArtifactIds, t)}</code>
      </div>
      <ArtifactLineage artifact={artifact} artifacts={artifacts} onArtifactSelect={onArtifactSelect} />
      <ArtifactPreviewPane artifact={artifact} preview={artifactPreview} />
      <div className="detail-actions">
        <ArtifactDownloadLink apiBaseUrl={apiBaseUrl} artifact={artifact} currentJob={currentJob} label={t("action.downloadArtifact")} />
      </div>
    </div>
  );
}

export function ArtifactLineage({
  artifact,
  artifacts,
  onArtifactSelect
}: {
  artifact: Artifact;
  artifacts: Artifact[];
  onArtifactSelect: (artifactId: string) => void;
}) {
  const { t } = useLocalization();
  const parents = artifact.parentArtifactIds
    .map((artifactId) => artifacts.find((candidate) => candidate.id === artifactId))
    .filter((candidate): candidate is Artifact => Boolean(candidate));
  const children = artifacts.filter((candidate) => candidate.parentArtifactIds.includes(artifact.id));

  if (parents.length === 0 && children.length === 0) {
    return (
      <div className="lineage-panel">
        <div className="lineage-heading">
          <Link2 size={16} aria-hidden="true" />
          <strong>{t("lineage.title")}</strong>
        </div>
        <EmptyState title={t("empty.noLinkedArtifacts.title")} detail={t("empty.noLinkedArtifacts.detail")} />
      </div>
    );
  }

  return (
    <div className="lineage-panel">
      <div className="lineage-heading">
        <Link2 size={16} aria-hidden="true" />
        <strong>{t("lineage.title")}</strong>
      </div>
      <ArtifactLineageGroup label={t("lineage.parents")} artifacts={parents} onArtifactSelect={onArtifactSelect} />
      <ArtifactLineageGroup label={t("lineage.children")} artifacts={children} onArtifactSelect={onArtifactSelect} />
    </div>
  );
}

export function ArtifactLineageGroup({
  artifacts,
  label,
  onArtifactSelect
}: {
  artifacts: Artifact[];
  label: string;
  onArtifactSelect: (artifactId: string) => void;
}) {
  const { t } = useLocalization();
  return (
    <div className="lineage-group">
      <span>{label}</span>
      {artifacts.length > 0 ? (
        <div className="lineage-links">
          {artifacts.map((artifact) => (
            <button className="lineage-chip" key={artifact.id} type="button" onClick={() => onArtifactSelect(artifact.id)}>
              <FileCode2 size={14} aria-hidden="true" />
              {artifact.kind}
              <small>{artifact.id}</small>
            </button>
          ))}
        </div>
      ) : (
        <strong>{t("common.none")}</strong>
      )}
    </div>
  );
}

export function ArtifactPreviewPane({ artifact, preview }: { artifact: Artifact; preview: ArtifactPreview }) {
  const { t } = useLocalization();
  const isCurrentPreview = preview.artifactId === artifact.id;
  const status = isCurrentPreview ? preview.status : "idle";
  const language = preview.text ? artifactPreviewLanguage(artifact, preview.text) : "plaintext";

  return (
    <div className="preview-panel">
      <div className="preview-heading">
        <div>
          <span>{t("preview.content")}</span>
          <strong>{artifact.contentType}</strong>
        </div>
        <Eye size={18} aria-hidden="true" />
      </div>
      {status === "loading" ? (
        <div className="preview-message">
          <FileText size={18} aria-hidden="true" />
          {t("preview.loadingArtifact")}
        </div>
      ) : null}
      {status === "ready" && preview.text ? (
        <Suspense
          fallback={
            <div className="preview-message">
              <FileText size={18} aria-hidden="true" />
              {t("preview.loadingEditor")}
            </div>
          }
        >
          <ArtifactTextEditor ariaLabel={`${artifact.kind} ${t("preview.aria")}`} language={language} text={preview.text} />
        </Suspense>
      ) : null}
      {status === "unsupported" ? (
        <div className="preview-message preview-muted">
          <AlertCircle size={18} aria-hidden="true" />
          {preview.reason ?? t("preview.unsupported")}
        </div>
      ) : null}
      {status === "error" ? (
        <div className="preview-message preview-error">
          <AlertCircle size={18} aria-hidden="true" />
          {preview.error ?? t("preview.failed")}
        </div>
      ) : null}
      {status === "idle" ? (
        <div className="preview-message preview-muted">
          <FileText size={18} aria-hidden="true" />
          {t("preview.idle")}
        </div>
      ) : null}
    </div>
  );
}

