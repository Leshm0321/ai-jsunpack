import type { Artifact, AstIndex, InputInventory } from "@ai-jsunpack/shared";
import type { EvidenceGraph, EvidenceGraphEdge, EvidenceGraphNode, EvidenceGraphSourceState, JobEvidence } from "./workbench-types";
import { formatDuration, formatPercent, isRecord, readStringArray, shortId, trimMiddle } from "./workbench-format";
import { statusTokenTone } from "./workbench-audit-logic";

export function buildArtifactLineageGraph(artifacts: Artifact[], selectedArtifactId: string | null, t: (key: string) => string): EvidenceGraph {
  const sortedArtifacts = [...artifacts].sort(compareArtifactsForGraph);
  const nodes = sortedArtifacts.map((artifact) => ({
    artifactId: artifact.id,
    column: artifactGraphColumn(artifact),
    detail: `${artifact.stage} / ${shortId(artifact.id)}`,
    id: artifactNodeId(artifact.id),
    kind: "artifact" as const,
    title: artifact.kind,
    tone: artifact.id === selectedArtifactId ? ("active" as const) : ("neutral" as const)
  }));
  const knownArtifacts = new Set(sortedArtifacts.map((artifact) => artifact.id));
  const edges = sortedArtifacts.flatMap((artifact) =>
    artifact.parentArtifactIds
      .filter((parentId) => knownArtifacts.has(parentId))
      .map((parentId) => ({
        from: artifactNodeId(parentId),
        id: `artifact-edge:${parentId}:${artifact.id}`,
        label: `${shortId(parentId)} -> ${shortId(artifact.id)}`,
        to: artifactNodeId(artifact.id)
      }))
  );

  return {
    edges,
    emptyDetail: t("graph.empty.lineage"),
    nodes,
    summary: `${nodes.length} ${t("graph.summary.artifacts")} / ${edges.length} ${t("graph.summary.lineageLinks")}`,
    title: t("graph.title.lineage")
  };
}

export function buildChunkEvidenceGraph(
  artifacts: Artifact[],
  inventory: InputInventory | null,
  astIndexes: AstIndex[] | null,
  sourceStatus: EvidenceGraphSourceState["status"],
  t: (key: string) => string
): EvidenceGraph {
  if (!inventory && !astIndexes) {
    const fallbackArtifacts = artifacts.filter((artifact) =>
      ["source_input", "input_inventory", "source_index", "ast_index", "runtime_scenario"].includes(artifact.kind)
    );
    return {
      edges: fallbackArtifacts.flatMap((artifact) =>
        artifact.parentArtifactIds.map((parentId) => ({
          from: artifactNodeId(parentId),
          id: `chunk-fallback:${parentId}:${artifact.id}`,
          label: `${shortId(parentId)} -> ${artifact.kind}`,
          to: artifactNodeId(artifact.id)
        }))
      ),
      emptyDetail:
        sourceStatus === "loading"
          ? t("graph.empty.chunkLoading")
          : t("graph.empty.noInputIndex"),
      nodes: fallbackArtifacts.map((artifact) => ({
        artifactId: artifact.id,
        column: artifactGraphColumn(artifact),
        detail: `${artifact.stage} / ${shortId(artifact.id)}`,
        id: artifactNodeId(artifact.id),
        kind: "artifact" as const,
        title: artifact.kind,
        tone: "warn" as const
      })),
      summary: t("graph.summary.fallback"),
      title: t("graph.title.chunk")
    };
  }

  const nodes: EvidenceGraphNode[] = [
    {
      column: 0,
      detail: inventory?.isSingleBundle ? t("graph.detail.singleBundle") : t("graph.detail.inputPackage"),
      id: "chunk:root",
      kind: "resource",
      title: t("graph.node.inputPackage"),
      tone: inventory?.warnings.length ? "warn" : "neutral"
    }
  ];
  const edges: EvidenceGraphEdge[] = [];

  if (inventory) {
    addResourceNodes(nodes, edges, "entry", inventory.entries, 1, t("graph.node.htmlEntry"), t);
    addResourceNodes(nodes, edges, "script", inventory.scripts, 2, t("graph.node.scriptChunk"), t);
    addResourceNodes(nodes, edges, "style", inventory.styles, 2, t("graph.node.stylesheet"), t);
    addResourceNodes(nodes, edges, "asset", inventory.assets, 3, t("graph.node.asset"), t);
    addResourceNodes(nodes, edges, "sourcemap", inventory.sourceMaps, 3, t("graph.node.sourceMap"), t);
    addResourceNodes(nodes, edges, "manifest", inventory.manifests, 3, t("graph.node.manifest"), t);
  }

  if (astIndexes) {
    for (const astIndex of astIndexes.slice(0, 10)) {
      const astNodeId = `chunk:ast:${astIndex.filePath}`;
      nodes.push({
        column: 3,
        detail: `${astIndex.symbols.length} ${t("graph.detail.symbols")} / ${astIndex.imports.length} ${t("graph.detail.imports")}`,
        id: astNodeId,
        kind: "analysis",
        title: trimMiddle(astIndex.filePath, 38),
        tone: astIndex.warnings.length ? "warn" : "pass"
      });
      const scriptNodeId = `chunk:script:${astIndex.filePath}`;
      edges.push({
        from: nodes.some((node) => node.id === scriptNodeId) ? scriptNodeId : "chunk:root",
        id: `chunk-edge:ast:${astIndex.filePath}`,
        label: `${t("graph.edge.astIndex")} ${trimMiddle(astIndex.filePath, 34)}`,
        to: astNodeId
      });
    }
    if (astIndexes.length > 10) {
      nodes.push({
        column: 3,
        detail: `${astIndexes.length - 10} ${t("graph.detail.additionalAstIndexes")}`,
        id: "chunk:ast:more",
        kind: "analysis",
        title: t("graph.node.moreAst"),
        tone: "warn"
      });
      edges.push({ from: "chunk:root", id: "chunk-edge:ast:more", label: t("graph.edge.additionalAst"), to: "chunk:ast:more" });
    }
  }

  return {
    edges,
    emptyDetail: t("graph.empty.noChunkEvidence"),
    nodes,
    summary: `${nodes.length} ${t("graph.summary.chunkNodes")}`,
    title: t("graph.title.chunk")
  };
}

export function buildAgentFlowGraph(artifacts: Artifact[], evidence: JobEvidence, t: (key: string) => string): EvidenceGraph {
  const nodes = new Map<string, EvidenceGraphNode>();
  const edges: EvidenceGraphEdge[] = [];
  const artifactsById = new Map(artifacts.map((artifact) => [artifact.id, artifact]));
  const ensureArtifactNode = (artifactId: string, column: number) => {
    if (nodes.has(artifactNodeId(artifactId))) {
      return;
    }
    const artifact = artifactsById.get(artifactId);
    nodes.set(artifactNodeId(artifactId), {
      artifactId,
      column,
      detail: artifact ? `${artifact.stage} / ${shortId(artifact.id)}` : shortId(artifactId),
      id: artifactNodeId(artifactId),
      kind: "artifact",
      title: artifact?.kind ?? "artifact",
      tone: artifact ? "neutral" : "warn"
    });
  };

  for (const record of evidence.inferenceRecords.slice(0, 18)) {
    const nodeId = `agent:inference:${record.id}`;
    nodes.set(nodeId, {
      column: 1,
      detail: `${record.agentName} / ${formatPercent(record.confidence)}`,
      id: nodeId,
      kind: "agent",
      title: record.type,
      tone: statusTokenTone(record.validationStatus)
    });
    for (const artifactId of record.inputArtifactIds) {
      ensureArtifactNode(artifactId, 0);
      edges.push({ from: artifactNodeId(artifactId), id: `agent-edge:${artifactId}:${record.id}:in`, label: t("graph.edge.agentInput"), to: nodeId });
    }
    for (const artifactId of record.outputArtifactIds) {
      ensureArtifactNode(artifactId, 2);
      edges.push({ from: nodeId, id: `agent-edge:${record.id}:${artifactId}:out`, label: t("graph.edge.agentOutput"), to: artifactNodeId(artifactId) });
    }
    for (const ref of record.evidenceRefs) {
      ensureArtifactNode(ref.artifactId, 3);
      edges.push({ from: nodeId, id: `agent-edge:${record.id}:${ref.artifactId}:evidence`, label: ref.label, to: artifactNodeId(ref.artifactId) });
    }
  }

  for (const call of evidence.toolCalls.slice(0, 18)) {
    const nodeId = `agent:tool:${call.id}`;
    nodes.set(nodeId, {
      column: 1,
      detail: `${call.caller} / ${formatDuration(call.duration)}`,
      id: nodeId,
      kind: "tool",
      title: call.toolName,
      tone: call.failureClass === "none" ? statusTokenTone(call.status) : "fail"
    });
    for (const artifactId of call.inputArtifactIds) {
      ensureArtifactNode(artifactId, 0);
      edges.push({ from: artifactNodeId(artifactId), id: `tool-edge:${artifactId}:${call.id}:in`, label: t("graph.edge.toolInput"), to: nodeId });
    }
    for (const artifactId of call.outputArtifactIds) {
      ensureArtifactNode(artifactId, 2);
      edges.push({ from: nodeId, id: `tool-edge:${call.id}:${artifactId}:out`, label: t("graph.edge.toolOutput"), to: artifactNodeId(artifactId) });
    }
  }

  for (const run of evidence.reviewRuns.slice(0, 18)) {
    const nodeId = `agent:review:${run.id}`;
    nodes.set(nodeId, {
      column: 3,
      detail: `${run.reviewType} / ${t("graph.detail.attempt")} ${run.attempt}`,
      id: nodeId,
      kind: "review",
      title: run.decision,
      tone: run.failureClass === "none" ? statusTokenTone(run.status) : "fail"
    });
    for (const artifactId of [run.logsArtifactId, ...run.repairInstructionIds].filter((value): value is string => Boolean(value))) {
      ensureArtifactNode(artifactId, 2);
      edges.push({ from: artifactNodeId(artifactId), id: `review-edge:${artifactId}:${run.id}`, label: t("graph.edge.reviewEvidence"), to: nodeId });
    }
    for (const ref of run.evidenceRefs) {
      ensureArtifactNode(ref.artifactId, 2);
      edges.push({ from: artifactNodeId(ref.artifactId), id: `review-edge:${ref.artifactId}:${run.id}:ref`, label: ref.label, to: nodeId });
    }
  }

  return {
    edges,
    emptyDetail: t("graph.empty.agent"),
    nodes: [...nodes.values()],
    summary: `${nodes.size} ${t("graph.summary.agentNodes")} / ${edges.length} ${t("graph.summary.links")}`,
    title: t("graph.title.agent")
  };
}

export function addResourceNodes(
  nodes: EvidenceGraphNode[],
  edges: EvidenceGraphEdge[],
  group: string,
  paths: string[],
  column: number,
  title: string,
  t: (key: string) => string
): void {
  for (const filePath of paths.slice(0, 8)) {
    const nodeId = `chunk:${group}:${filePath}`;
    nodes.push({
      column,
      detail: title,
      id: nodeId,
      kind: "resource",
      title: trimMiddle(filePath, 38)
    });
    edges.push({ from: "chunk:root", id: `chunk-edge:${group}:${filePath}`, label: title, to: nodeId });
  }
  if (paths.length > 8) {
    const moreNodeId = `chunk:${group}:more`;
    nodes.push({
      column,
      detail: `${paths.length - 8} ${t("graph.detail.additionalRecords")}`,
      id: moreNodeId,
      kind: "resource",
      title: `${t("graph.node.more")} ${title}`
    });
    edges.push({ from: "chunk:root", id: `chunk-edge:${group}:more`, label: `${title} ${t("graph.edge.overflow")}`, to: moreNodeId });
  }
}

export function layoutEvidenceGraph(graph: EvidenceGraph): {
  edges: Array<EvidenceGraphEdge & { path: string }>;
  height: number;
  nodes: Array<EvidenceGraphNode & { x: number; y: number }>;
  width: number;
} {
  const columnWidth = 246;
  const nodeHeight = 72;
  const nodeWidth = 210;
  const rowGap = 18;
  const origin = 24;
  const rowByColumn = new Map<number, number>();
  const laidOutNodes = graph.nodes.map((node) => {
    const row = rowByColumn.get(node.column) ?? 0;
    rowByColumn.set(node.column, row + 1);
    return {
      ...node,
      x: origin + node.column * columnWidth,
      y: origin + row * (nodeHeight + rowGap)
    };
  });
  const maxColumn = Math.max(0, ...laidOutNodes.map((node) => node.column));
  const maxRows = Math.max(1, ...rowByColumn.values());
  const nodesById = new Map(laidOutNodes.map((node) => [node.id, node]));
  const laidOutEdges = graph.edges
    .map((edge) => {
      const from = nodesById.get(edge.from);
      const to = nodesById.get(edge.to);
      if (!from || !to) {
        return null;
      }
      const startX = from.x + nodeWidth;
      const startY = from.y + nodeHeight / 2;
      const endX = to.x;
      const endY = to.y + nodeHeight / 2;
      const controlOffset = Math.max(54, Math.abs(endX - startX) * 0.42);
      return {
        ...edge,
        path: `M ${startX} ${startY} C ${startX + controlOffset} ${startY}, ${endX - controlOffset} ${endY}, ${endX} ${endY}`
      };
    })
    .filter((edge): edge is EvidenceGraphEdge & { path: string } => Boolean(edge));

  return {
    edges: laidOutEdges,
    height: origin * 2 + maxRows * nodeHeight + Math.max(0, maxRows - 1) * rowGap,
    nodes: laidOutNodes,
    width: origin * 2 + (maxColumn + 1) * nodeWidth + maxColumn * (columnWidth - nodeWidth)
  };
}

export function parseInputInventoryArtifact(text: string): InputInventory {
  const payload = JSON.parse(text) as { inventory?: unknown; kind?: unknown };
  if (payload.kind !== "input_inventory" || !isRecord(payload.inventory)) {
    throw new Error("Artifact is not a valid input inventory.");
  }
  const inventory = payload.inventory;
  return {
    assets: readStringArray(inventory.assets),
    entries: readStringArray(inventory.entries),
    files: Array.isArray(inventory.files) ? (inventory.files as InputInventory["files"]) : [],
    isSingleBundle: Boolean(inventory.isSingleBundle),
    manifests: readStringArray(inventory.manifests),
    scripts: readStringArray(inventory.scripts),
    sourceMaps: readStringArray(inventory.sourceMaps),
    styles: readStringArray(inventory.styles),
    warnings: readStringArray(inventory.warnings)
  };
}

export function parseAstIndexArtifact(text: string): AstIndex[] {
  const payload = JSON.parse(text) as { astIndexes?: unknown; kind?: unknown };
  if (payload.kind !== "ast_index" || !Array.isArray(payload.astIndexes)) {
    throw new Error("Artifact is not a valid AST index.");
  }
  return payload.astIndexes.filter(isRecord).map((index) => ({
    exports: readStringArray(index.exports),
    filePath: typeof index.filePath === "string" ? index.filePath : "unknown",
    imports: readStringArray(index.imports),
    sourceHash: typeof index.sourceHash === "string" ? index.sourceHash : "",
    symbols: Array.isArray(index.symbols) ? (index.symbols as AstIndex["symbols"]) : [],
    warnings: readStringArray(index.warnings)
  }));
}

export function latestArtifactOfKind(artifacts: Artifact[], kind: Artifact["kind"]): Artifact | null {
  return (
    [...artifacts]
      .filter((artifact) => artifact.kind === kind)
      .sort((left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt))[0] ?? null
  );
}

export function compareArtifactsForGraph(left: Artifact, right: Artifact): number {
  const columnDiff = artifactGraphColumn(left) - artifactGraphColumn(right);
  if (columnDiff !== 0) {
    return columnDiff;
  }
  return Date.parse(left.createdAt) - Date.parse(right.createdAt);
}

export function artifactGraphColumn(artifact: Artifact): number {
  if (["source_input", "input_inventory", "source_index"].includes(artifact.kind)) {
    return 0;
  }
  if (["ast_index", "agent_plan", "inference_record", "memory_record", "knowledge_evidence", "tool_call"].includes(artifact.kind)) {
    return 1;
  }
  if (
    [
      "reconstruction_plan",
      "generated_project",
      "build_log",
      "build_artifact",
      "runtime_validation",
      "runtime_trace",
      "runtime_screenshot",
      "runtime_scenario",
      "runtime_comparison",
      "repair_instruction"
    ].includes(artifact.kind)
  ) {
    return 2;
  }
  return 3;
}

export function artifactNodeId(artifactId: string): string {
  return `artifact:${artifactId}`;
}
