import type { Artifact, CloudMode, InferenceRecord, Job, ReviewRun, RuntimeValidationRun, ToolCall } from "@ai-jsunpack/shared";

export interface JobSummary {
  job: Job;
  artifacts: Artifact[];
}

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.replace(/\/+$/, "");

export const API_BASE_URL = configuredBaseUrl || "http://127.0.0.1:8000";

export async function createJob(cloudMode: CloudMode): Promise<JobSummary> {
  return requestJson<JobSummary>("/jobs", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      projectId: "default",
      ownerId: "local-user",
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

export async function fetchArtifactText(jobId: string, artifactId: string, signal?: AbortSignal): Promise<string> {
  const response = await fetch(
    `${API_BASE_URL}/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(artifactId)}/download`,
    { signal }
  );
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
  return response.text();
}

async function requestJson<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, options);
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
  return (await response.json()) as T;
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
    // Fall back to the status line below when the response is not JSON.
  }
  return `${response.status} ${response.statusText}`;
}
