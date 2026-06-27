import { AlertCircle, Download, Link2 } from "lucide-react";
import type { Artifact, EvidenceRef, Job } from "@ai-jsunpack/shared";
import { useLocalization } from "./i18n";
import { artifactDownloadUrl, canDownloadArtifact, statusTokenTone } from "./workbench-logic";

export function StatusToken({ status }: { status: string }) {
  const tone = statusTokenTone(status);
  return <span className={`status-token status-token-${tone}`}>{status}</span>;
}

export function EvidenceRefButtons({
  onArtifactSelect,
  refs
}: {
  onArtifactSelect: (artifactId: string) => void;
  refs: EvidenceRef[];
}) {
  const { t } = useLocalization();
  if (refs.length === 0) {
    return <span className="muted-inline">{t("common.none")}</span>;
  }

  return (
    <div className="evidence-ref-list">
      {refs.map((ref) => (
        <button
          className="evidence-ref-chip"
          key={`${ref.artifactId}-${ref.label}-${ref.locator ?? ""}`}
          type="button"
          onClick={() => onArtifactSelect(ref.artifactId)}
          title={[ref.label, ref.locator, ref.excerpt].filter(Boolean).join(" / ")}
        >
          <Link2 size={14} aria-hidden="true" />
          <span>{ref.label}</span>
          {ref.locator ? <small>{ref.locator}</small> : null}
        </button>
      ))}
    </div>
  );
}

export function ArtifactDownloadLink({
  apiBaseUrl,
  artifact,
  currentJob,
  label
}: {
  apiBaseUrl: string;
  artifact: Artifact;
  currentJob: Job | null;
  label: string;
}) {
  const { t } = useLocalization();
  if (!currentJob) {
    return null;
  }

  if (!canDownloadArtifact(artifact)) {
    return (
      <span className="download-disabled">
        <AlertCircle size={15} aria-hidden="true" />
        {t("action.packageRequired")}
      </span>
    );
  }

  return (
    <a className="download-link" href={artifactDownloadUrl(apiBaseUrl, currentJob.id, artifact.id)}>
      <Download size={15} aria-hidden="true" />
      {label}
    </a>
  );
}

export function DetailItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="detail-item">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function EmptyState({ detail, title }: { detail: string; title: string }) {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      <span>{detail}</span>
    </div>
  );
}

export function StatusBanner({ message, tone }: { message: string; tone: "error" | "warning" }) {
  return <div className={`status-banner status-${tone}`}>{message}</div>;
}

