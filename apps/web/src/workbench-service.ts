import { fetchInferenceRecords, fetchJobSummary, fetchReviewRuns, fetchRuntimeValidations, fetchToolCalls } from "./api";
import type { JobEvidence, JobWorkspace } from "./workbench-types";

export async function fetchJobWorkspace(jobId: string): Promise<JobWorkspace> {
  const [summary, evidence] = await Promise.all([fetchJobSummary(jobId), fetchJobEvidence(jobId)]);
  return { summary, evidence };
}

export async function fetchJobEvidence(jobId: string): Promise<JobEvidence> {
  const [runtimeValidations, inferenceRecords, reviewRuns, toolCalls] = await Promise.all([
    fetchRuntimeValidations(jobId),
    fetchInferenceRecords(jobId),
    fetchReviewRuns(jobId),
    fetchToolCalls(jobId)
  ]);
  return { runtimeValidations, inferenceRecords, reviewRuns, toolCalls };
}
