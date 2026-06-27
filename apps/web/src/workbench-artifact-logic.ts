import type { Artifact } from "@ai-jsunpack/shared";
import type { ArtifactPreviewSupport } from "./workbench-types";
import { previewMaxBytes, textualArtifactKinds } from "./workbench-types";
import { formatBytes } from "./workbench-format";

export function artifactPreviewSupport(artifact: Artifact): ArtifactPreviewSupport {
  if (artifact.kind === "generated_project") {
    return { supported: false, reason: "Directory artifacts are available through the packaged result download." };
  }
  if (artifact.kind === "result_package") {
    return { supported: false, reason: "Result packages are binary downloads and are not rendered inline." };
  }
  if (artifact.kind === "html_report") {
    return { supported: false, reason: "HTML reports are download-only so report markup is not executed inside the workbench." };
  }
  if (artifact.kind === "runtime_screenshot") {
    return { supported: false, reason: "Screenshots are image evidence. Use the download action to inspect the capture." };
  }
  if (artifact.size > previewMaxBytes) {
    return { supported: false, reason: `Preview is limited to ${formatBytes(previewMaxBytes)} artifacts.` };
  }
  if (textualArtifactKinds.has(artifact.kind) || isTextContentType(artifact.contentType)) {
    return { supported: true, reason: null };
  }
  return { supported: false, reason: `${artifact.contentType} is not treated as browser-previewable text.` };
}

export function canDownloadArtifact(artifact: Artifact): boolean {
  return artifact.kind !== "generated_project";
}

export function isTextContentType(contentType: string): boolean {
  const normalized = contentType.toLowerCase();
  return (
    normalized.startsWith("text/") ||
    normalized.includes("json") ||
    normalized.includes("javascript") ||
    normalized.includes("xml")
  );
}

export function formatArtifactPreviewText(artifact: Artifact, text: string): string {
  if (artifact.contentType.toLowerCase().includes("json") || text.trimStart().startsWith("{") || text.trimStart().startsWith("[")) {
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      return text;
    }
  }
  return text;
}

export function artifactPreviewLanguage(artifact: Artifact, text: string): string {
  const contentType = artifact.contentType.toLowerCase();
  const hint = `${artifact.kind} ${artifact.storageUri} ${artifact.id}`.toLowerCase();
  const trimmed = text.trimStart();

  if (contentType.includes("json") || trimmed.startsWith("{") || trimmed.startsWith("[")) {
    return "json";
  }
  if (contentType.includes("markdown") || hasArtifactExtension(hint, [".md", ".markdown"]) || artifact.kind === "audit_report") {
    return "markdown";
  }
  if (contentType.includes("typescript") || hasArtifactExtension(hint, [".ts", ".tsx"])) {
    return "typescript";
  }
  if (contentType.includes("javascript") || hasArtifactExtension(hint, [".js", ".jsx", ".mjs", ".cjs"])) {
    return "javascript";
  }
  if (contentType.includes("html") || hasArtifactExtension(hint, [".html", ".htm"])) {
    return "html";
  }
  if (contentType.includes("css") || hasArtifactExtension(hint, [".css", ".scss", ".less"])) {
    return "css";
  }
  if (contentType.includes("xml") || hasArtifactExtension(hint, [".xml", ".svg"])) {
    return "xml";
  }
  return "plaintext";
}

export function hasArtifactExtension(value: string, extensions: string[]): boolean {
  return extensions.some((extension) => value.includes(extension));
}
