export function formatBytes(size: number): string {
  if (size < 1024) {
    return `${size} B`;
  }
  const kib = size / 1024;
  if (kib < 1024) {
    return `${kib.toFixed(1)} KiB`;
  }
  return `${(kib / 1024).toFixed(1)} MiB`;
}

export function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function formatDuration(seconds: number): string {
  if (seconds < 1) {
    return `${Math.round(seconds * 1000)} ms`;
  }
  return `${seconds.toFixed(2)} s`;
}

export function formatIdList(ids: string[], t?: (key: string) => string): string {
  return ids.length > 0 ? ids.join(", ") : t ? t("common.none") : "None";
}

export function artifactDownloadUrl(apiBaseUrl: string, jobId: string, artifactId: string): string {
  return `${apiBaseUrl}/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(artifactId)}/download`;
}

export function downloadJsonFile(filename: string, payload: unknown): void {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return "Request failed.";
}

export function readStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function shortId(value: string): string {
  return value.length > 10 ? `${value.slice(0, 6)}...${value.slice(-4)}` : value;
}

export function trimMiddle(value: string, maxLength: number): string {
  if (value.length <= maxLength) {
    return value;
  }
  const side = Math.max(4, Math.floor((maxLength - 3) / 2));
  return `${value.slice(0, side)}...${value.slice(-side)}`;
}

export function formatUnknownValue(value: unknown): string {
  if (value === undefined) {
    return "undefined";
  }
  if (value === null) {
    return "null";
  }
  if (typeof value === "string") {
    return value.length > 90 ? `${value.slice(0, 87)}...` : value;
  }
  try {
    const text = JSON.stringify(value);
    return text.length > 90 ? `${text.slice(0, 87)}...` : text;
  } catch {
    return String(value);
  }
}

export function safeJsonText(value: unknown): string {
  try {
    return JSON.stringify(value) ?? "";
  } catch {
    return String(value);
  }
}
