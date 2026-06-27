import type { RuntimeComparisonReport } from "@ai-jsunpack/shared";
import type { RuntimeComparisonFilters, RuntimeComparisonLoaded } from "./workbench-types";
import { formatPercent } from "./workbench-format";

export function parseRuntimeComparisonReport(text: string): RuntimeComparisonReport {
  const payload = JSON.parse(text) as Partial<RuntimeComparisonReport>;
  if (!payload.id || !payload.differences || !payload.original || !payload.reconstructed) {
    throw new Error("Artifact is not a valid runtime comparison report.");
  }
  return payload as RuntimeComparisonReport;
}

export function formatScreenshotDiff(differences: RuntimeComparisonReport["differences"]): string {
  const changed = differences.screenshotDiff.changed ?? differences.screenshotChanged;
  const status = differences.screenshotDiff.pixelDiffStatus;
  return `changed ${String(changed)} / ${status}`;
}

export function formatPixelDiff(differences: RuntimeComparisonReport["differences"], t: (key: string) => string): string {
  const changed = differences.screenshotDiff.changedPixelCount;
  const total = differences.screenshotDiff.pixelCount;
  if (changed === undefined || changed === null || total === undefined || total === null) {
    return t("status.unavailable");
  }
  const ratio = differences.screenshotDiff.changedPixelRatio;
  const percent = typeof ratio === "number" ? ` (${formatPercent(ratio)})` : "";
  return `${changed}/${total}${percent}`;
}

export function runtimeComparisonScopeLabel(report: RuntimeComparisonReport, t: (key: string) => string): string {
  const scope = report.differences.comparisonScope;
  const viewport = scope.viewport;
  const viewportLabel = viewport
    ? `${viewport.name ? `${viewport.name} ` : ""}${viewport.width}x${viewport.height}`
    : t("status.defaultViewport");
  return `${scope.scenarioName} / ${viewportLabel}`;
}

export function formatRuntimeGroups(groups: Record<string, string[]>, t: (key: string) => string): string {
  const keys = Object.keys(groups);
  if (keys.length === 0) {
    return t("status.noneLower");
  }
  return keys.slice(0, 3).join(", ");
}

export function runtimeEvidenceLabel(artifactId: string, report: RuntimeComparisonReport, t: (key: string) => string): string {
  if (artifactId === report.scenarioArtifactId) {
    return t("runtime.scenario").toLowerCase();
  }
  if (report.traceArtifactIds.includes(artifactId)) {
    return "trace";
  }
  if (report.screenshotArtifactIds.includes(artifactId)) {
    return artifactId === report.differences.screenshotDiff.diffArtifactId ? t("runtime.pixelDiff") : t("runtime.screenshot");
  }
  if (artifactId === report.differences.screenshotDiff.diffArtifactId) {
    return t("runtime.pixelDiff");
  }
  return "comparison";
}

export function uniqueRuntimeComparisonScenarios(reports: RuntimeComparisonLoaded[]): string[] {
  return [...new Set(reports.map((item) => item.report.differences.comparisonScope.scenarioName))].sort();
}

export function uniqueRuntimeComparisonViewports(reports: RuntimeComparisonLoaded[]): Array<{ label: string; value: string }> {
  const viewports = new Map<string, string>();
  for (const item of reports) {
    const value = runtimeComparisonViewportValue(item.report);
    viewports.set(value, runtimeComparisonViewportLabel(item.report));
  }
  return [...viewports.entries()].map(([value, label]) => ({ label, value })).sort((left, right) => left.label.localeCompare(right.label));
}

export function runtimeComparisonMatchesFilters(report: RuntimeComparisonReport, filters: RuntimeComparisonFilters): boolean {
  if (filters.scenario !== "all" && report.differences.comparisonScope.scenarioName !== filters.scenario) {
    return false;
  }
  if (filters.viewport !== "all" && runtimeComparisonViewportValue(report) !== filters.viewport) {
    return false;
  }
  if (filters.status !== "all" && report.status !== filters.status) {
    return false;
  }
  return true;
}

export function runtimeComparisonViewportLabel(report: RuntimeComparisonReport): string {
  const viewport = report.differences.comparisonScope.viewport;
  if (!viewport) {
    return "default viewport";
  }
  const name = viewport.name ? `${viewport.name} ` : "";
  return `${name}${viewport.width}x${viewport.height}`;
}

export function runtimeComparisonViewportValue(report: RuntimeComparisonReport): string {
  const viewport = report.differences.comparisonScope.viewport;
  if (!viewport) {
    return "default";
  }
  return `${viewport.name ?? "viewport"}:${viewport.width}x${viewport.height}`;
}

export function virtualListRange(totalItems: number, scrollTop: number, rowHeight: number, viewportHeight: number): { end: number; start: number } {
  const overscan = 4;
  const start = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const visibleCount = Math.ceil(viewportHeight / rowHeight) + overscan * 2;
  return {
    end: Math.min(totalItems, start + visibleCount),
    start
  };
}

export function runtimeScreenshotPreviewItems(
  report: RuntimeComparisonReport,
  t: (key: string) => string
): Array<{ artifactId: string; detail: string; label: string }> {
  const items = [
    {
      artifactId: report.original.screenshotArtifactId ?? null,
      detail: t("runtime.originalDetail"),
      label: t("runtime.original")
    },
    {
      artifactId: report.reconstructed.screenshotArtifactId ?? null,
      detail: t("runtime.reconstructedDetail"),
      label: t("runtime.reconstructed")
    },
    {
      artifactId: report.differences.screenshotDiff.diffArtifactId ?? null,
      detail: t("runtime.pixelDiffDetail"),
      label: t("runtime.pixelDiff")
    }
  ];
  return items.filter((item): item is { artifactId: string; detail: string; label: string } => Boolean(item.artifactId));
}
