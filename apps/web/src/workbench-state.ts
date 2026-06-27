import type { Artifact, JobStatus, RuntimeValidationRun } from "@ai-jsunpack/shared";
import type { MetricStatus, RuntimeMetric, StageItem, StageState } from "./workbench-types";
import { reportArtifactKinds, stageDefinitions, statusOrder } from "./workbench-types";

export function buildStageItems(currentStatus: JobStatus | undefined, t: (key: string) => string): StageItem[] {
  const activeStatus = visibleStageFor(currentStatus);
  const activeIndex = activeStatus ? statusIndex(activeStatus) : -1;
  const failed = currentStatus === "failed" || currentStatus === "cancelled";

  return stageDefinitions.map((stage) => {
    const stageIndex = statusIndex(stage.status);
    let state: StageState = "pending";
    if (failed && stage.status === activeStatus) {
      state = "fail";
    } else if (currentStatus === "completed_best_effort" && stage.status === "completed") {
      state = "warning";
    } else if (stage.status === activeStatus) {
      state = "active";
    } else if (activeIndex >= 0 && stageIndex < activeIndex) {
      state = "done";
    }
    return { ...stage, label: t(stage.labelKey), state };
  });
}

export function buildReportArtifacts(artifacts: Artifact[]): Artifact[] {
  return [...artifacts]
    .filter((artifact) => reportArtifactKinds.has(artifact.kind))
    .sort((left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt));
}

export function buildRuntimeMetrics(
  latestRuntime: RuntimeValidationRun | null,
  runtimeCount: number,
  t: (key: string) => string
): RuntimeMetric[] {
  if (!latestRuntime) {
    return [
      { label: t("runtime.runs"), value: String(runtimeCount), status: "warn" },
      { label: t("runtime.entryLoad"), value: t("runtime.pending"), status: "warn" },
      { label: t("runtime.consoleErrors"), value: t("runtime.pending"), status: "warn" },
      { label: t("runtime.failedRequests"), value: t("runtime.pending"), status: "warn" }
    ];
  }

  const status = runStatusToMetricStatus(latestRuntime.status);
  return [
    { label: t("runtime.runs"), value: String(runtimeCount), status },
    { label: t("runtime.entryLoad"), value: latestRuntime.status, status },
    {
      label: t("runtime.consoleErrors"),
      value: String(latestRuntime.consoleErrors.length + latestRuntime.pageErrors.length),
      status: latestRuntime.consoleErrors.length + latestRuntime.pageErrors.length > 0 ? "fail" : "pass"
    },
    {
      label: t("runtime.failedRequests"),
      value: String(latestRuntime.failedRequests.length),
      status: latestRuntime.failedRequests.length > 0 ? "fail" : "pass"
    }
  ];
}

export function visibleStageFor(status: JobStatus | undefined): JobStatus | undefined {
  if (!status) {
    return undefined;
  }
  if (status === "failed" || status === "cancelled") {
    return "reviewing";
  }
  if (status === "completed_best_effort") {
    return "completed";
  }
  const currentIndex = statusIndex(status);
  return stageDefinitions.find((stage) => statusIndex(stage.status) >= currentIndex)?.status ?? "completed";
}

export function statusIndex(status: JobStatus): number {
  return statusOrder.get(status) ?? Number.MAX_SAFE_INTEGER;
}

export function runStatusToMetricStatus(status: RuntimeValidationRun["status"]): MetricStatus {
  if (status === "pass") {
    return "pass";
  }
  if (status === "fail") {
    return "fail";
  }
  return "warn";
}
