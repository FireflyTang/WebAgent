import Editor from "react-simple-code-editor";
import Prism from "prismjs";
import "prismjs/components/prism-bash";
import "prismjs/components/prism-c";
import "prismjs/components/prism-cpp";
import "prismjs/components/prism-java";
import "prismjs/components/prism-json";
import "prismjs/components/prism-jsx";
import "prismjs/components/prism-markdown";
import "prismjs/components/prism-python";
import "prismjs/components/prism-sql";
import "prismjs/components/prism-typescript";
import "prismjs/components/prism-tsx";
import "prismjs/components/prism-yaml";

const aliases = {
  py: "python",
  js: "javascript",
  jsx: "jsx",
  ts: "typescript",
  tsx: "tsx",
  json: "json",
  html: "markup",
  htm: "markup",
  xml: "markup",
  css: "css",
  md: "markdown",
  yml: "yaml",
  sh: "bash",
  shell: "bash",
  c: "c",
  "c++": "cpp",
  cc: "cpp",
  hpp: "cpp",
  java: "java",
  sql: "sql",
  plaintext: "plain",
  text: "plain",
};

const prismLanguage = (language) => {
  const normalized = String(language || "").toLowerCase();
  return (
    aliases[normalized] || (Prism.languages[normalized] ? normalized : "plain")
  );
};

export default function FileEditorDialog({
  file,
  value,
  onChange,
  onClose,
  onReload,
  onSave,
  saving,
}) {
  const language = prismLanguage(file.language);
  const readOnly = !file.editable;
  const inputLocked = readOnly || saving;
  const highlight = (source) =>
    Prism.highlight(
      source,
      Prism.languages[language] || Prism.languages.plain,
      language,
    );
  const lineNumbers = Array.from(
    { length: Math.max(1, value.split("\n").length) },
    (_, index) => index + 1,
  ).join("\n");
  const onEditorValueChange = inputLocked ? () => {} : onChange;
  const onEditorKeyDown = (event) => {
    if (!inputLocked) return;
    const navigationKeys = new Set([
      "ArrowDown",
      "ArrowLeft",
      "ArrowRight",
      "ArrowUp",
      "End",
      "Home",
      "PageDown",
      "PageUp",
    ]);
    const key = event.key.toLowerCase();
    const allowedShortcut =
      (event.ctrlKey || event.metaKey) && ["a", "c", "f"].includes(key);
    if (!navigationKeys.has(event.key) && !allowedShortcut)
      event.preventDefault();
  };

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <section
        id="fileEditorDialog"
        className="dialog file-editor-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="fileEditorDialog-title"
      >
        <header>
          <div>
            <h2 id="fileEditorDialog-title">{file.path}</h2>
            <p>
              {file.language || "plaintext"} · {file.size} B
            </p>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="关闭"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <div className="file-editor-content">
          {readOnly && (
            <p id="editorReadOnly" className="editor-read-only" role="status">
              {file.reason === "session_busy"
                ? "Agent 正在运行，可以查看文件；任务结束后才能保存修改。"
                : "此文件当前只能查看，不能保存修改。"}
            </p>
          )}
          <div
            id="fileEditorScroll"
            className="file-editor-scroll"
            aria-label="代码滚动区域"
          >
            <div className="file-editor-surface">
              <pre className="file-editor-line-numbers" aria-hidden="true">
                {lineNumbers}
              </pre>
              <Editor
                value={value}
                onValueChange={onEditorValueChange}
                highlight={highlight}
                padding={14}
                textareaId="fileEditorInput"
                textareaClassName="file-editor-input"
                preClassName={`file-editor-highlight language-${language}`}
                className="file-editor"
                readOnly={inputLocked}
                ignoreTabKey={inputLocked}
                onKeyDown={onEditorKeyDown}
                onKeyDownCapture={onEditorKeyDown}
                aria-label="文件编辑器"
              />
            </div>
          </div>
        </div>
        <footer className="editor-actions">
          <button
            type="button"
            className="secondary-button"
            onClick={onReload}
            disabled={saving}
          >
            重新加载
          </button>
          <button
            id="editorSave"
            type="button"
            className="primary-button"
            onClick={() => onSave(false)}
            disabled={readOnly || saving}
          >
            {saving ? "正在保存…" : "保存"}
          </button>
        </footer>
      </section>
    </div>
  );
}
