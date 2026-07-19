import { useEffect, useMemo, useState } from "react";
import { AlertCircle, FileText, GitBranch, Link2, Sparkles } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { Artifact, AstIndex, InputInventory, Job } from "@ai-jsunpack/shared";
import { fetchArtifactText } from "./api";
import { useLocalization } from "./i18n";
import type { EvidenceGraph, EvidenceGraphMode, EvidenceGraphSourceState, JobEvidence } from "./workbench-types";
import {
  buildAgentFlowGraph,
  buildArtifactLineageGraph,
  buildChunkEvidenceGraph,
  latestArtifactOfKind,
  layoutEvidenceGraph,
  parseAstIndexArtifact,
  parseInputInventoryArtifact
} from "./workbench-logic";
import { EmptyState } from "./workbench-common";

export function EvidenceGraphPanel({
  artifacts,
  currentJob,
  evidence,
  onArtifactSelect,
  selectedArtifactId
}: {
  artifacts: Artifact[];
  currentJob: Job | null;
  evidence: JobEvidence;
  onArtifactSelect: (artifactId: string) => void;
  selectedArtifactId: string | null;
}) {
  const { t } = useLocalization();
  const [mode, setMode] = useState<EvidenceGraphMode>("lineage");
  const inventoryArtifact = useMemo(() => latestArtifactOfKind(artifacts, "input_inventory"), [artifacts]);
  const astIndexArtifact = useMemo(() => latestArtifactOfKind(artifacts, "ast_index"), [artifacts]);
  const [sources, setSources] = useState<EvidenceGraphSourceState>({
    astIndexes: null,
    error: null,
    inventory: null,
    status: "idle"
  });

  useEffect(() => {
    if (!currentJob || (!inventoryArtifact && !astIndexArtifact)) {
      setSources({ astIndexes: null, error: null, inventory: null, status: "idle" });
      return;
    }

    const controller = new AbortController();
    let active = true;
    setSources((current) => ({ ...current, error: null, status: "loading" }));

    Promise.all([
      inventoryArtifact
        ? fetchArtifactText(currentJob.id, inventoryArtifact.id, controller.signal).then((text) => parseInputInventoryArtifact(text, t))
        : Promise.resolve<InputInventory | null>(null),
      astIndexArtifact
        ? fetchArtifactText(currentJob.id, astIndexArtifact.id, controller.signal).then((text) => parseAstIndexArtifact(text, t))
        : Promise.resolve<AstIndex[] | null>(null)
    ])
      .then(([inventory, astIndexes]) => {
        if (active) {
          setSources({ astIndexes, error: null, inventory, status: "ready" });
        }
      })
      .catch((error: Error) => {
        if (active) {
          setSources({ astIndexes: null, error: error.message, inventory: null, status: "error" });
        }
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [currentJob?.id, inventoryArtifact?.id, astIndexArtifact?.id, t]);

  const graph = useMemo(() => {
    if (mode === "chunks") {
      return buildChunkEvidenceGraph(artifacts, sources.inventory, sources.astIndexes, sources.status, t);
    }
    if (mode === "agents") {
      return buildAgentFlowGraph(artifacts, evidence, t);
    }
    return buildArtifactLineageGraph(artifacts, selectedArtifactId, t);
  }, [artifacts, evidence, mode, selectedArtifactId, sources.astIndexes, sources.inventory, sources.status, t]);

  return (
    <div className="evidence-graph">
      <div className="graph-toolbar" role="tablist" aria-label={t("app.aria.graphViews")}>
        <GraphModeButton active={mode === "lineage"} icon={Link2} label={t("graph.mode.lineage")} onClick={() => setMode("lineage")} />
        <GraphModeButton active={mode === "chunks"} icon={GitBranch} label={t("graph.mode.chunks")} onClick={() => setMode("chunks")} />
        <GraphModeButton active={mode === "agents"} icon={Sparkles} label={t("graph.mode.agents")} onClick={() => setMode("agents")} />
      </div>

      <div className="graph-summary-grid" aria-label={t("app.aria.graphSummary")}>
        <GraphMetric label={t("graph.metric.nodes")} value={String(graph.nodes.length)} />
        <GraphMetric label={t("graph.metric.edges")} value={String(graph.edges.length)} />
        <GraphMetric label={t("graph.metric.mode")} value={graph.title} />
      </div>

      {mode === "chunks" && sources.status === "loading" ? (
        <div className="preview-message">
          <FileText size={18} aria-hidden="true" />
          {t("graph.loadingChunks")}
        </div>
      ) : null}
      {mode === "chunks" && sources.status === "error" ? (
        <div className="preview-message preview-error">
          <AlertCircle size={18} aria-hidden="true" />
          {sources.error ?? t("graph.chunkLoadError")}
        </div>
      ) : null}

      <EvidenceGraphCanvas graph={graph} onArtifactSelect={onArtifactSelect} selectedArtifactId={selectedArtifactId} />
    </div>
  );
}

export function GraphModeButton({
  active,
  icon: Icon,
  label,
  onClick
}: {
  active: boolean;
  icon: LucideIcon;
  label: string;
  onClick: () => void;
}) {
  const { t } = useLocalization();
  return (
    <button aria-pressed={active} className={active ? "graph-mode graph-mode-active" : "graph-mode"} type="button" onClick={onClick}>
      <Icon size={16} aria-hidden="true" />
      {label}
    </button>
  );
}

export function GraphMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="graph-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function EvidenceGraphCanvas({
  graph,
  onArtifactSelect,
  selectedArtifactId
}: {
  graph: EvidenceGraph;
  onArtifactSelect: (artifactId: string) => void;
  selectedArtifactId: string | null;
}) {
  const { t } = useLocalization();
  const layout = useMemo(() => layoutEvidenceGraph(graph), [graph]);

  if (graph.nodes.length === 0) {
    return <EmptyState title={graph.title} detail={graph.emptyDetail} />;
  }

  return (
    <div className="graph-viewport" aria-label={graph.summary}>
      <div className="graph-canvas" style={{ height: `${layout.height}px`, width: `${layout.width}px` }}>
        <svg className="graph-edges" height={layout.height} width={layout.width} aria-hidden="true">
          <defs>
            <marker id="graph-arrow" markerHeight="8" markerWidth="8" orient="auto" refX="7" refY="4">
              <path d="M0,0 L8,4 L0,8 Z" />
            </marker>
          </defs>
          {layout.edges.map((edge) => (
            <path className="graph-edge-path" d={edge.path} key={edge.id} markerEnd="url(#graph-arrow)" />
          ))}
        </svg>
        {layout.nodes.map((node) => {
          const isActive = node.artifactId === selectedArtifactId;
          const className = [
            "graph-node",
            `graph-node-${node.kind}`,
            `graph-node-${node.tone ?? "neutral"}`,
            isActive ? "graph-node-selected" : ""
          ]
            .filter(Boolean)
            .join(" ");
          const style = { left: `${node.x}px`, top: `${node.y}px` };
          const content = (
            <>
              <span>{node.title}</span>
              <small>{node.detail}</small>
            </>
          );

          return node.artifactId ? (
            <button className={className} key={node.id} style={style} type="button" onClick={() => onArtifactSelect(node.artifactId!)}>
              {content}
            </button>
          ) : (
            <div className={className} key={node.id} style={style}>
              {content}
            </div>
          );
        })}
      </div>
      <div className="graph-edge-list" aria-label={t("app.aria.graphEdges")}>
        {graph.edges.slice(0, 12).map((edge) => (
          <span key={edge.id}>{edge.label}</span>
        ))}
        {graph.edges.length > 12 ? <span>{graph.edges.length - 12} {t("graph.moreEdges")}</span> : null}
      </div>
    </div>
  );
}
