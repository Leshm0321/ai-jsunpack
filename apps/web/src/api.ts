import type { Artifact, CloudMode, InferenceRecord, Job, ReviewRun, RuntimeValidationRun, ToolCall } from "@ai-jsunpack/shared";
import type { EffectiveConfigResponse, ProviderReadinessResponse, RuntimeSettingsResponse } from "./settings-types";
import type { JobHistoryResponse, JobResultResponse } from "./workbench-types";

export interface JobSummary {
  job: Job;
  artifacts: Artifact[];
}

const runtimeConfig = window.__AI_JSUNPACK_CONFIG__ || {};
const configuredBaseUrl = (runtimeConfig.apiBaseUrl || import.meta.env.VITE_API_BASE_URL)?.replace(/\/+$/, "");
const configuredUserId = (runtimeConfig.userId || import.meta.env.VITE_API_USER_ID)?.trim();
const configuredProjectId = (runtimeConfig.projectId || import.meta.env.VITE_API_PROJECT_ID)?.trim();
const configuredAuthToken = (runtimeConfig.authToken || import.meta.env.VITE_API_AUTH_TOKEN)?.trim();

export const API_BASE_URL = configuredBaseUrl || "http://127.0.0.1:8000";
export const API_USER_ID = configuredUserId || "local-user";
export const API_PROJECT_ID = configuredProjectId || "default";
export const API_AUTH_TOKEN = configuredAuthToken || "";

export async function createJob(cloudMode: CloudMode): Promise<JobSummary> {
  return requestJson<JobSummary>("/jobs", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...accessHeaders()
    },
    body: JSON.stringify({
      projectId: API_PROJECT_ID,
      ownerId: API_USER_ID,
      cloudMode,
      config: {
        source: "web",
        submittedAt: new Date().toISOString()
      }
    })
  });
}

export async function uploadSource(jobId: string, file: File): Promise<JobSummary> {
  const body = new FormData();
  body.append("file", file);

  return requestJson<JobSummary>(`/jobs/${encodeURIComponent(jobId)}/upload`, {
    method: "POST",
    headers: accessHeaders(),
    body
  });
}

export async function uploadDirectory(jobId: string, files: File[]): Promise<JobSummary> {
  const body = new FormData();
  for (const file of files) {
    body.append("files", file, file.name);
    body.append("paths", file.webkitRelativePath || file.name);
  }
  return requestJson<JobSummary>(`/jobs/${encodeURIComponent(jobId)}/upload-directory`, {
    method: "POST",
    headers: accessHeaders(),
    body
  });
}

export async function fetchJobs({
  limit = 20,
  offset = 0,
  projectId,
  status
}: {
  limit?: number;
  offset?: number;
  projectId?: string;
  status?: string;
} = {}): Promise<JobHistoryResponse> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (projectId) query.set("projectId", projectId);
  if (status) query.set("status", status);
  return requestJson<JobHistoryResponse>(`/jobs?${query.toString()}`);
}

export async function fetchJobResult(jobId: string): Promise<JobResultResponse> {
  return requestJson<JobResultResponse>(`/jobs/${encodeURIComponent(jobId)}/result`);
}

export async function deleteJob(jobId: string): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/jobs/${encodeURIComponent(jobId)}`, {
    method: "DELETE",
    headers: accessHeaders()
  });
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
}

export async function downloadJobResult(jobId: string, fallbackFilename: string): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/jobs/${encodeURIComponent(jobId)}/result/download`, {
    headers: accessHeaders()
  });
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") ?? "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const quoted = disposition.match(/filename="([^"]+)"/i)?.[1];
  const filename = encoded ? decodeURIComponent(encoded) : quoted || fallbackFilename;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export async function downloadArtifactFile(jobId: string, artifactId: string, fallbackFilename: string): Promise<void> {
  const response = await fetch(
    `${API_BASE_URL}/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(artifactId)}/download`,
    { headers: accessHeaders() }
  );
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") ?? "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const quoted = disposition.match(/filename="([^"]+)"/i)?.[1];
  const filename = encoded ? decodeURIComponent(encoded) : quoted || fallbackFilename;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export async function fetchJobSummary(jobId: string): Promise<JobSummary> {
  return requestJson<JobSummary>(`/jobs/${encodeURIComponent(jobId)}`);
}

export async function fetchRuntimeValidations(jobId: string): Promise<RuntimeValidationRun[]> {
  return requestJson<RuntimeValidationRun[]>(`/jobs/${encodeURIComponent(jobId)}/runtime-validations`);
}

export async function fetchInferenceRecords(jobId: string): Promise<InferenceRecord[]> {
  return requestJson<InferenceRecord[]>(`/jobs/${encodeURIComponent(jobId)}/inference-records`);
}

export async function fetchReviewRuns(jobId: string): Promise<ReviewRun[]> {
  return requestJson<ReviewRun[]>(`/jobs/${encodeURIComponent(jobId)}/review-runs`);
}

export async function fetchToolCalls(jobId: string): Promise<ToolCall[]> {
  return requestJson<ToolCall[]>(`/jobs/${encodeURIComponent(jobId)}/tool-calls`);
}

export async function fetchEffectiveConfig(): Promise<EffectiveConfigResponse> {
  return requestJson<EffectiveConfigResponse>("/v1/config/effective");
}

export async function fetchProviderReadiness(): Promise<ProviderReadinessResponse> {
  return requestJson<ProviderReadinessResponse>("/v1/providers/readiness");
}

export async function fetchSystemSettings(): Promise<RuntimeSettingsResponse> {
  return requestJson<RuntimeSettingsResponse>("/v1/settings/system");
}

export async function fetchAccountSettings(): Promise<RuntimeSettingsResponse> {
  return requestJson<RuntimeSettingsResponse>("/v1/settings/account");
}

export async function updateAccountSettings(settings: Record<string, unknown>, expectedRevision = 0): Promise<RuntimeSettingsResponse> {
  return requestJson<RuntimeSettingsResponse>("/v1/settings/account", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings, expectedRevision, reason: "Update account file retention from the web settings center" })
  });
}

export async function updateSystemSettings(settings: Record<string, unknown>, expectedRevision = 0): Promise<RuntimeSettingsResponse> {
  return requestJson<RuntimeSettingsResponse>("/v1/settings/system", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings, expectedRevision, reason: "通过网页设置中心更新" })
  });
}

export async function fetchProjectSettings(projectId: string): Promise<RuntimeSettingsResponse> {
  return requestJson<RuntimeSettingsResponse>(`/v1/projects/${encodeURIComponent(projectId)}/settings`);
}

export async function updateProjectSettings(
  projectId: string,
  settings: Record<string, unknown>,
  expectedRevision = 0
): Promise<RuntimeSettingsResponse> {
  return requestJson<RuntimeSettingsResponse>(`/v1/projects/${encodeURIComponent(projectId)}/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings, expectedRevision, reason: "通过网页设置中心更新" })
  });
}

export async function rerunJob(jobId: string): Promise<JobSummary> {
  return requestJson<JobSummary>(`/jobs/${encodeURIComponent(jobId)}/rerun`, {
    method: "POST"
  });
}

export async function fetchArtifactText(jobId: string, artifactId: string, signal?: AbortSignal): Promise<string> {
  const response = await fetch(
    `${API_BASE_URL}/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(artifactId)}/download`,
    { headers: accessHeaders(), signal }
  );
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
  return response.text();
}

async function requestJson<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      ...accessHeaders(),
      ...options.headers
    }
  });
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
  return (await response.json()) as T;
}

function accessHeaders(): Record<string, string> {
  return API_AUTH_TOKEN ? { Authorization: `Bearer ${API_AUTH_TOKEN}` } : {};
}

async function responseErrorMessage(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === "string") {
      return payload.detail;
    }
    if (payload.detail) {
      return JSON.stringify(payload.detail);
    }
  } catch {
    // 响应不是 JSON 时，回退到下面的状态行。
  }
  return `${response.status} ${response.statusText}`;
}
