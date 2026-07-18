import type { Artifact, CloudMode, InferenceRecord, Job, ReviewRun, RuntimeValidationRun, ToolCall } from "@ai-jsunpack/shared";
import type { EffectiveConfigResponse, ProviderReadinessResponse, RuntimeSettingsResponse } from "./settings-types";

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

export async function updateSystemSettings(settings: Record<string, unknown>, expectedRevision = 0): Promise<RuntimeSettingsResponse> {
  return requestJson<RuntimeSettingsResponse>("/v1/settings/system", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings, expectedRevision, reason: "updated from web settings center" })
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
    body: JSON.stringify({ settings, expectedRevision, reason: "updated from web settings center" })
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
