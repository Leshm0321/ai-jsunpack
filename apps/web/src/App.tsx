import { useMemo, useRef, useState } from "react";
import { useGSAP } from "@gsap/react";
import { gsap } from "gsap";
import {
  Activity,
  Archive,
  Binary,
  Braces,
  CheckCircle2,
  ChevronRight,
  Download,
  FileCode2,
  GitBranch,
  Network,
  Play,
  Radar,
  SearchCode,
  ShieldCheck,
  Sparkles,
  Upload,
  Workflow,
  XCircle
} from "lucide-react";
import type { JobStatus } from "@ai-jsunpack/shared";

gsap.registerPlugin(useGSAP);

type StageState = "done" | "active" | "pending" | "warning";

interface StageItem {
  status: JobStatus;
  label: string;
  state: StageState;
}

interface AuditRow {
  type: string;
  subject: string;
  confidence: number;
  evidence: string;
  status: "accepted" | "needs_review";
}

interface RuntimeMetric {
  label: string;
  value: string;
  status: "pass" | "warn" | "fail";
}

interface WorkbenchData {
  stages: StageItem[];
  files: string[];
  auditRows: AuditRow[];
  runtimeMetrics: RuntimeMetric[];
}

const stageItems: StageItem[] = [
  { status: "intake", label: "Input inventory", state: "done" },
  { status: "indexing", label: "AST and resource index", state: "done" },
  { status: "agent_pass", label: "Agent inference", state: "active" },
  { status: "reconstructing", label: "Project writer", state: "pending" },
  { status: "runtime_smoke", label: "Browser validation", state: "pending" },
  { status: "reviewing", label: "Review and repair", state: "warning" }
];

const projectFiles = [
  "src/main.tsx",
  "src/routes/AppShell.tsx",
  "src/modules/auth/session.ts",
  "src/modules/cart/pricing.ts",
  "src/types/generated.d.ts",
  "public/assets/runtime-shim.js"
];

const auditRows: AuditRow[] = [
  {
    type: "Naming",
    subject: "n -> calculateDiscount",
    confidence: 0.84,
    evidence: "Symbol called before price total branch",
    status: "accepted"
  },
  {
    type: "Framework",
    subject: "React route tree",
    confidence: 0.79,
    evidence: "JSX factory and root hydrate call",
    status: "accepted"
  },
  {
    type: "Runtime",
    subject: "public path shim",
    confidence: 0.72,
    evidence: "Chunk loader reads asset base at startup",
    status: "accepted"
  },
  {
    type: "Dead code",
    subject: "debug branch retained",
    confidence: 0.58,
    evidence: "No direct reference, behavior risk marked",
    status: "needs_review"
  }
];

const runtimeMetrics: RuntimeMetric[] = [
  { label: "Entry load", value: "1.2s", status: "pass" },
  { label: "Console errors", value: "0", status: "pass" },
  { label: "Failed requests", value: "2", status: "warn" },
  { label: "Screenshot diff", value: "6.8%", status: "warn" }
];

const codePreview = `export function calculateDiscount(cart: CartState) {
  const eligibleItems = cart.items.filter(item => item.price > 0);
  const subtotal = eligibleItems.reduce((sum, item) => sum + item.price, 0);

  if (cart.flags.includes("enterprise")) {
    return subtotal * 0.12;
  }

  return subtotal > 500 ? subtotal * 0.08 : 0;
}`;

export function AppContainer() {
  const [selectedFile, setSelectedFile] = useState(projectFiles[0]);
  const data = useMemo<WorkbenchData>(
    () => ({
      stages: stageItems,
      files: projectFiles,
      auditRows,
      runtimeMetrics
    }),
    []
  );

  return <AppView data={data} selectedFile={selectedFile} onSelectFile={setSelectedFile} />;
}

interface AppViewProps {
  data: WorkbenchData;
  selectedFile: string;
  onSelectFile: (file: string) => void;
}

function AppView({ data, selectedFile, onSelectFile }: AppViewProps) {
  const rootRef = useRef<HTMLDivElement>(null);

  useGSAP(
    () => {
      const mm = gsap.matchMedia();
      mm.add(
        {
          reduceMotion: "(prefers-reduced-motion: reduce)"
        },
        (context) => {
          const reduceMotion = Boolean(context.conditions?.reduceMotion);
          if (reduceMotion) {
            gsap.set(".motion-item", { autoAlpha: 1, x: 0, y: 0, scale: 1 });
            return;
          }

          const timeline = gsap.timeline({ defaults: { duration: 0.45, ease: "power2.out" } });
          timeline
            .from(".entry-copy", { y: 18, autoAlpha: 0 })
            .from(".entry-visual", { y: 18, autoAlpha: 0 }, "<0.08")
            .from(".workbench-panel", { y: 18, autoAlpha: 0, stagger: 0.06 }, "<0.12")
            .from(".stage-step", { x: -16, autoAlpha: 0, stagger: 0.05 }, "<0.1");

          return () => timeline.kill();
        }
      );

      return () => mm.revert();
    },
    { scope: rootRef }
  );

  return (
    <div className="app-shell" ref={rootRef}>
      <header className="topbar">
        <a className="brand" href="#overview" aria-label="AI JS Unpack overview">
          <span className="brand-mark">
            <Binary size={18} aria-hidden="true" />
          </span>
          <span>AI JS Unpack</span>
        </a>
        <nav className="topnav" aria-label="Primary">
          <a href="#workflow">Workflow</a>
          <a href="#audit">Audit</a>
          <a href="#runtime">Runtime</a>
        </nav>
      </header>

      <main>
        <section className="entry-band" id="overview">
          <div className="entry-copy motion-item">
            <p className="eyebrow">Authorized JavaScript restoration</p>
            <h1>AI-assisted deobfuscation with browser runtime evidence.</h1>
            <p className="entry-text">
              Upload a production build, inspect the recovered project, trace every inference, and validate behavior in
              a controlled browser runtime.
            </p>
            <div className="entry-actions">
              <button className="primary-action" type="button">
                <Upload size={18} aria-hidden="true" />
                Upload build
              </button>
              <button className="secondary-action" type="button">
                <Play size={18} aria-hidden="true" />
                Run sample job
              </button>
            </div>
          </div>

          <div className="entry-visual motion-item" aria-label="Pipeline overview">
            <PipelineMap />
          </div>
        </section>

        <section className="workbench" id="workflow" aria-label="Analysis workbench">
          <section className="workbench-panel upload-panel motion-item">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Source intake</p>
                <h2>Input package</h2>
              </div>
              <ShieldCheck size={22} aria-hidden="true" />
            </div>
            <div className="dropzone" role="button" tabIndex={0}>
              <Archive size={24} aria-hidden="true" />
              <div>
                <strong>dist.zip</strong>
                <span>HTML, chunks, CSS, assets, sourcemaps</span>
              </div>
            </div>
            <div className="mode-grid" aria-label="Processing modes">
              <ModePill label="local_only" active />
              <ModePill label="desensitized" />
              <ModePill label="cloud_allowed" />
            </div>
          </section>

          <section className="workbench-panel timeline-panel motion-item">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Job state</p>
                <h2>Pipeline timeline</h2>
              </div>
              <Workflow size={22} aria-hidden="true" />
            </div>
            <ol className="stage-list">
              {data.stages.map((stage) => (
                <li className={`stage-step stage-${stage.state}`} key={stage.status}>
                  <span className="stage-icon">{stage.state === "done" ? <CheckCircle2 size={16} /> : <Activity size={16} />}</span>
                  <span>{stage.label}</span>
                  <small>{stage.status}</small>
                </li>
              ))}
            </ol>
          </section>

          <section className="workbench-panel tree-panel motion-item">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Generated project</p>
                <h2>File tree</h2>
              </div>
              <FileCode2 size={22} aria-hidden="true" />
            </div>
            <div className="file-list" role="listbox" aria-label="Generated files">
              {data.files.map((file) => (
                <button
                  className={file === selectedFile ? "file-row file-row-active" : "file-row"}
                  key={file}
                  type="button"
                  onClick={() => onSelectFile(file)}
                >
                  <FileCode2 size={15} aria-hidden="true" />
                  <span>{file}</span>
                </button>
              ))}
            </div>
          </section>

          <section className="workbench-panel code-panel motion-item">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Code preview</p>
                <h2>{selectedFile}</h2>
              </div>
              <Braces size={22} aria-hidden="true" />
            </div>
            <pre className="code-preview" aria-label="Recovered TypeScript preview">
              <code>{codePreview}</code>
            </pre>
          </section>

          <section className="workbench-panel audit-panel motion-item" id="audit">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Evidence ledger</p>
                <h2>Inference audit</h2>
              </div>
              <SearchCode size={22} aria-hidden="true" />
            </div>
            <div className="audit-table" role="table" aria-label="Inference audit records">
              <div className="audit-row audit-head" role="row">
                <span>Type</span>
                <span>Subject</span>
                <span>Confidence</span>
                <span>Evidence</span>
              </div>
              {data.auditRows.map((row) => (
                <div className="audit-row" role="row" key={`${row.type}-${row.subject}`}>
                  <span>{row.type}</span>
                  <span>{row.subject}</span>
                  <span>{Math.round(row.confidence * 100)}%</span>
                  <span>{row.evidence}</span>
                </div>
              ))}
            </div>
          </section>

          <section className="workbench-panel runtime-panel motion-item" id="runtime">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Browser evidence</p>
                <h2>Runtime validation</h2>
              </div>
              <Radar size={22} aria-hidden="true" />
            </div>
            <div className="runtime-grid">
              {data.runtimeMetrics.map((metric) => (
                <div className={`runtime-metric runtime-${metric.status}`} key={metric.label}>
                  <span>{metric.label}</span>
                  <strong>{metric.value}</strong>
                </div>
              ))}
            </div>
            <div className="runtime-actions">
              <button className="secondary-action compact" type="button">
                <Network size={16} aria-hidden="true" />
                Network log
              </button>
              <button className="primary-action compact" type="button">
                <Download size={16} aria-hidden="true" />
                Download package
              </button>
            </div>
          </section>
        </section>
      </main>
    </div>
  );
}

function ModePill({ label, active = false }: { label: string; active?: boolean }) {
  return (
    <button className={active ? "mode-pill mode-pill-active" : "mode-pill"} type="button">
      {label}
    </button>
  );
}

function PipelineMap() {
  const nodes = [
    { label: "Input", icon: Upload },
    { label: "AST", icon: GitBranch },
    { label: "Agents", icon: Sparkles },
    { label: "Runtime", icon: Radar },
    { label: "Review", icon: ShieldCheck }
  ];

  return (
    <div className="pipeline-map">
      {nodes.map((node, index) => {
        const Icon = node.icon;
        return (
          <div className="pipeline-node" key={node.label}>
            <span>
              <Icon size={18} aria-hidden="true" />
            </span>
            <strong>{node.label}</strong>
            {index < nodes.length - 1 ? <ChevronRight className="pipeline-arrow" size={18} aria-hidden="true" /> : null}
          </div>
        );
      })}
      <div className="pipeline-status">
        <CheckCircle2 size={18} aria-hidden="true" />
        <span>Traceable artifacts</span>
      </div>
      <div className="pipeline-status warning">
        <XCircle size={18} aria-hidden="true" />
        <span>Runtime diffs retained</span>
      </div>
    </div>
  );
}
