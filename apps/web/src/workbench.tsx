import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import type { CloudMode } from "@ai-jsunpack/shared";
import type { ArtifactPreview, JobEvidence, WorkbenchData } from "./workbench-types";
import { AppView, emptyArtifactPreview, emptyEvidence } from "./workbench-view";
import {
  artifactPreviewSupport,
  buildReportArtifacts,
  buildRuntimeMetrics,
  buildStageItems,
  errorMessage,
  fetchJobEvidence,
  fetchJobWorkspace,
  formatArtifactPreviewText
} from "./workbench-logic";
import { API_BASE_URL, createJob, fetchArtifactText, rerunJob, uploadSource } from "./api";
import type { JobSummary } from "./api";
import { useLocalization } from "./i18n";
import { workbenchPath } from "./routes";
import type { AppRoute, ParsedAppRoute } from "./routes";

export function AppContainer({
  onNavigate,
  route
}: {
  onNavigate: (route: AppRoute) => void;
  route: Extract<ParsedAppRoute, { kind: "workbench" | "workbench-new" }>;
}) {
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);
  const [artifactPreview, setArtifactPreview] = useState<ArtifactPreview>(() => emptyArtifactPreview());
  const [selectedCloudMode, setSelectedCloudMode] = useState<CloudMode>("local_only");
  const [selectedUploadFile, setSelectedUploadFile] = useState<File | null>(null);
  const [jobSummary, setJobSummary] = useState<JobSummary | null>(null);
  const [evidence, setEvidence] = useState<JobEvidence>(() => emptyEvidence());
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isRerunning, setIsRerunning] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);

  const currentJob = jobSummary?.job ?? null;
  const routeJobId = route.kind === "workbench" ? route.jobId : null;
  const artifacts = jobSummary?.artifacts ?? [];
  const latestRuntime = evidence.runtimeValidations.at(-1) ?? null;
  const { t } = useLocalization();
  const selectedArtifact = useMemo(
    () => artifacts.find((artifact) => artifact.id === selectedArtifactId) ?? artifacts[0] ?? null,
    [artifacts, selectedArtifactId]
  );
  const data = useMemo<WorkbenchData>(
    () => ({
      stages: buildStageItems(currentJob?.status, t),
      latestRuntime,
      reportArtifacts: buildReportArtifacts(artifacts),
      runtimeMetrics: buildRuntimeMetrics(latestRuntime, evidence.runtimeValidations.length, t)
    }),
    [artifacts, currentJob?.status, evidence.runtimeValidations.length, latestRuntime, t]
  );

  useEffect(() => {
    if (!routeJobId) {
      setJobSummary(null);
      setEvidence(emptyEvidence());
      setSelectedArtifactId(null);
      setPollError(null);
      return;
    }
    if (currentJob?.id === routeJobId) {
      return;
    }
    let active = true;
    setIsRefreshing(true);
    setPollError(null);
    fetchJobWorkspace(routeJobId)
      .then((workspace) => {
        if (active) {
          setJobSummary(workspace.summary);
          setEvidence(workspace.evidence);
        }
      })
      .catch((error) => {
        if (active) {
          setPollError(errorMessage(error, t));
        }
      })
      .finally(() => {
        if (active) {
          setIsRefreshing(false);
        }
      });
    return () => {
      active = false;
    };
  }, [routeJobId, currentJob?.id, t]);

  useEffect(() => {
    if (!currentJob?.id || !selectedArtifact || route.kind !== "workbench" || route.section !== "artifacts") {
      setArtifactPreview(emptyArtifactPreview());
      return;
    }
    const previewSupport = artifactPreviewSupport(selectedArtifact, t);
    if (!previewSupport.supported) {
      setArtifactPreview({ artifactId: selectedArtifact.id, error: null, reason: previewSupport.reason, status: "unsupported", text: null });
      return;
    }
    const controller = new AbortController();
    setArtifactPreview({ artifactId: selectedArtifact.id, error: null, reason: null, status: "loading", text: null });
    fetchArtifactText(currentJob.id, selectedArtifact.id, controller.signal)
      .then((text) => setArtifactPreview({ artifactId: selectedArtifact.id, error: null, reason: null, status: "ready", text: formatArtifactPreviewText(selectedArtifact, text) }))
      .catch((error) => {
        if (!(error instanceof Error && error.name === "AbortError")) {
          setArtifactPreview({ artifactId: selectedArtifact.id, error: errorMessage(error, t), reason: null, status: "error", text: null });
        }
      });
    return () => controller.abort();
  }, [currentJob?.id, route.kind, route.kind === "workbench" ? route.section : null, selectedArtifact, t]);

  useEffect(() => {
    if (!currentJob?.id) {
      return;
    }
    let cancelled = false;
    const pollJob = async () => {
      try {
        const workspace = await fetchJobWorkspace(currentJob.id);
        if (!cancelled) {
          setJobSummary(workspace.summary);
          setEvidence(workspace.evidence);
          setPollError(null);
        }
      } catch (error) {
        if (!cancelled) {
          setPollError(errorMessage(error, t));
        }
      }
    };
    const intervalId = window.setInterval(pollJob, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [currentJob?.id, t]);

  const handleSubmitJob = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isSubmitting) return;
    if (!selectedUploadFile) {
      setUploadError(t("upload.error.noFile"));
      return;
    }
    setIsSubmitting(true);
    setUploadError(null);
    setPollError(null);
    setEvidence(emptyEvidence());
    setSelectedArtifactId(null);
    try {
      const created = await createJob(selectedCloudMode);
      setJobSummary(created);
      const uploaded = await uploadSource(created.job.id, selectedUploadFile);
      setJobSummary(uploaded);
      setEvidence(await fetchJobEvidence(created.job.id));
      onNavigate(workbenchPath(uploaded.job.id, "overview"));
    } catch (error) {
      setUploadError(errorMessage(error, t));
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleRefreshJob = async () => {
    if (!currentJob?.id || isRefreshing) return;
    setIsRefreshing(true);
    setPollError(null);
    try {
      const workspace = await fetchJobWorkspace(currentJob.id);
      setJobSummary(workspace.summary);
      setEvidence(workspace.evidence);
    } catch (error) {
      setPollError(errorMessage(error, t));
    } finally {
      setIsRefreshing(false);
    }
  };

  const handleRerunJob = async () => {
    if (!currentJob?.id || isRerunning) return;
    setIsRerunning(true);
    setUploadError(null);
    setPollError(null);
    setSelectedArtifactId(null);
    setEvidence(emptyEvidence());
    try {
      const rerun = await rerunJob(currentJob.id);
      setJobSummary(rerun);
      setEvidence(await fetchJobEvidence(rerun.job.id));
      onNavigate(workbenchPath(rerun.job.id, "overview"));
    } catch (error) {
      setPollError(errorMessage(error, t));
    } finally {
      setIsRerunning(false);
    }
  };

  const handleArtifactEvidenceSelect = (artifactId: string) => {
    setSelectedArtifactId(artifactId);
    if (currentJob) {
      onNavigate(workbenchPath(currentJob.id, "artifacts"));
    }
  };

  return (
    <AppView
      apiBaseUrl={API_BASE_URL}
      artifactPreview={artifactPreview}
      artifacts={artifacts}
      currentJob={currentJob}
      data={data}
      evidence={evidence}
      isRefreshing={isRefreshing}
      isRerunning={isRerunning}
      isSubmitting={isSubmitting}
      onArtifactSelect={setSelectedArtifactId}
      onEvidenceArtifactSelect={handleArtifactEvidenceSelect}
      onFileChange={setSelectedUploadFile}
      onNavigate={onNavigate}
      onRefreshJob={handleRefreshJob}
      onRerunJob={handleRerunJob}
      onSelectCloudMode={setSelectedCloudMode}
      onSubmitJob={handleSubmitJob}
      pollError={pollError}
      selectedArtifact={selectedArtifact}
      selectedCloudMode={selectedCloudMode}
      selectedUploadFile={selectedUploadFile}
      uploadError={uploadError}
      view={route.kind === "workbench" ? route.section : "new"}
    />
  );
}
