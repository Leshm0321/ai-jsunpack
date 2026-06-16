import { useMemo } from "react";
import Editor, { loader } from "@monaco-editor/react";
import { FileText } from "lucide-react";
import * as monaco from "monaco-editor/esm/vs/editor/editor.api.js";
import type { editor } from "monaco-editor/esm/vs/editor/editor.api.js";
import "monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution.js";
import "monaco-editor/esm/vs/basic-languages/xml/xml.contribution.js";
import "monaco-editor/esm/vs/language/css/monaco.contribution.js";
import "monaco-editor/esm/vs/language/html/monaco.contribution.js";
import "monaco-editor/esm/vs/language/json/monaco.contribution.js";
import "monaco-editor/esm/vs/language/typescript/monaco.contribution.js";
import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import cssWorker from "monaco-editor/esm/vs/language/css/css.worker?worker";
import htmlWorker from "monaco-editor/esm/vs/language/html/html.worker?worker";
import jsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import tsWorker from "monaco-editor/esm/vs/language/typescript/ts.worker?worker";

type MonacoWorkerFactory = new () => Worker;

const editorWorkerFactory = editorWorker as MonacoWorkerFactory;
const jsonWorkerFactory = jsonWorker as MonacoWorkerFactory;
const cssWorkerFactory = cssWorker as MonacoWorkerFactory;
const htmlWorkerFactory = htmlWorker as MonacoWorkerFactory;
const tsWorkerFactory = tsWorker as MonacoWorkerFactory;

self.MonacoEnvironment = {
  getWorker(_workerId: string, label: string) {
    if (label === "json") {
      return new jsonWorkerFactory();
    }
    if (label === "css" || label === "scss" || label === "less") {
      return new cssWorkerFactory();
    }
    if (label === "html" || label === "handlebars" || label === "razor") {
      return new htmlWorkerFactory();
    }
    if (label === "typescript" || label === "javascript") {
      return new tsWorkerFactory();
    }
    return new editorWorkerFactory();
  }
};

loader.config({ monaco });

const artifactEditorOptions: editor.IStandaloneEditorConstructionOptions = {
  automaticLayout: true,
  contextmenu: true,
  copyWithSyntaxHighlighting: false,
  domReadOnly: true,
  fontFamily: "\"Fira Code\", Consolas, \"Courier New\", monospace",
  fontSize: 12,
  lineHeight: 20,
  minimap: { enabled: false },
  readOnly: true,
  renderWhitespace: "selection",
  scrollBeyondLastLine: false,
  tabSize: 2,
  wordWrap: "on"
};

interface ArtifactTextEditorProps {
  ariaLabel: string;
  language: string;
  text: string;
}

export default function ArtifactTextEditor({ ariaLabel, language, text }: ArtifactTextEditorProps) {
  const options = useMemo<editor.IStandaloneEditorConstructionOptions>(
    () => ({
      ...artifactEditorOptions,
      ariaLabel
    }),
    [ariaLabel]
  );

  return (
    <div className="artifact-preview-editor">
      <Editor
        height="100%"
        language={language}
        loading={
          <div className="preview-message">
            <FileText size={18} aria-hidden="true" />
            Loading editor
          </div>
        }
        options={options}
        theme="vs-dark"
        value={text}
      />
    </div>
  );
}
