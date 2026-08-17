import { useMemo, useState } from "react";
import {
  ArrowClockwise,
  CaretDown,
  CaretRight,
  FileCode,
  Folder,
  FolderOpen,
  FolderPlus,
  MagnifyingGlass,
  UploadSimple,
  X,
} from "@phosphor-icons/react";

const fileSize = (bytes) =>
  bytes == null
    ? ""
    : bytes < 1024
      ? `${bytes} B`
      : bytes < 1048576
        ? `${(bytes / 1024).toFixed(1)} KB`
        : `${(bytes / 1048576).toFixed(1)} MB`;

function IconButton({ label, children, ...props }) {
  return (
    <button
      type="button"
      className="icon-button"
      aria-label={label}
      title={label}
      {...props}
    >
      {children}
    </button>
  );
}

function buildTree(files) {
  const root = {
    name: "/workspace",
    path: "",
    type: "folder",
    children: new Map(),
  };
  for (const item of files) {
    const path = String(item.path || item.name || "").replace(/^\/+/, "");
    if (!path) continue;
    let node = root;
    const parts = path.split("/");
    for (const [index, name] of parts.entries()) {
      if (!node.children.has(name)) {
        node.children.set(name, {
          name,
          path: parts.slice(0, index + 1).join("/"),
          type: index === parts.length - 1 ? item.type || "file" : "folder",
          size: index === parts.length - 1 ? item.size : undefined,
          children: new Map(),
        });
      }
      node = node.children.get(name);
    }
  }
  return root;
}

function FileTree({ sessionId, files, query, onOpen }) {
  const [closedBySession, setClosedBySession] = useState(() => new Map());
  const closed = closedBySession.get(sessionId) || new Set();
  const root = useMemo(() => buildTree(files), [files]);
  const normalized = query.trim().toLowerCase();
  const sorted = (node) =>
    [...node.children.values()].sort((a, b) =>
      a.type === b.type
        ? a.name.localeCompare(b.name)
        : a.type === "folder"
          ? -1
          : 1,
    );
  const visible = [];

  if (normalized) {
    const matching = new Set();
    const mark = (node, ancestorMatched = false) => {
      const matches =
        ancestorMatched ||
        node.name.toLowerCase().includes(normalized) ||
        node.path.toLowerCase().includes(normalized);
      const childMatches = sorted(node)
        .map((child) => mark(child, matches))
        .some(Boolean);
      if (matches || childMatches) matching.add(node.path);
      return matches || childMatches;
    };
    sorted(root).forEach((node) => mark(node));
    const walkMatches = (node, depth) => {
      if (!matching.has(node.path)) return;
      visible.push({ node, depth });
      sorted(node).forEach((child) => walkMatches(child, depth + 1));
    };
    sorted(root).forEach((node) => walkMatches(node, 0));
  } else {
    const walk = (node, depth) => {
      visible.push({ node, depth });
      if (node.type === "folder" && !closed.has(node.path)) {
        sorted(node).forEach((child) => walk(child, depth + 1));
      }
    };
    sorted(root).forEach((node) => walk(node, 0));
  }

  return (
    <div className="file-tree" role="tree" aria-label="沙箱文件">
      {visible.map(({ node, depth }) => {
        const folder = node.type === "folder";
        const isClosed = !normalized && closed.has(node.path);
        return (
          <button
            type="button"
            role="treeitem"
            aria-expanded={folder ? !isClosed : undefined}
            className="file-row"
            style={{ "--depth": depth }}
            key={`${node.type}-${node.path}`}
            title={folder ? node.path : "双击打开或下载"}
            onClick={() => {
              if (!folder || normalized) return;
              setClosedBySession((current) => {
                const nextBySession = new Map(current);
                const next = new Set(nextBySession.get(sessionId) || []);
                if (next.has(node.path)) next.delete(node.path);
                else next.add(node.path);
                nextBySession.set(sessionId, next);
                return nextBySession;
              });
            }}
            onDoubleClick={() => {
              if (!folder) onOpen(node.path);
            }}
          >
            <span className="tree-caret">
              {folder ? isClosed ? <CaretRight /> : <CaretDown /> : null}
            </span>
            <span className={`file-icon ${node.type}`}>
              {folder ? (
                isClosed ? (
                  <Folder weight="fill" />
                ) : (
                  <FolderOpen weight="fill" />
                )
              ) : (
                <FileCode />
              )}
            </span>
            <span className="file-name">{node.name}</span>
            <span className="file-size">{fileSize(node.size)}</span>
          </button>
        );
      })}
      {(!visible.length || (!files.length && !normalized)) && (
        <div className="empty-state">
          {files.length ? "没有匹配的文件" : "尚无文件"}
        </div>
      )}
    </div>
  );
}

export default function FilesPanel({
  sessionId,
  files,
  query,
  setQuery,
  busy,
  uploadDisabled,
  uploadedFileCount = 0,
  uploadLimit = 0,
  feedback,
  onRefresh,
  onUpload,
  onOpen,
  onClose,
  inputPrefix = "",
}) {
  const normalizedCount = Math.max(0, Number(uploadedFileCount) || 0);
  const normalizedLimit = Math.max(0, Number(uploadLimit) || 0);
  const limitReached =
    normalizedLimit > 0 && normalizedCount >= normalizedLimit;
  const uploadUnavailable =
    !sessionId || busy || uploadDisabled || limitReached;
  const selectUploads = (event) => {
    const selected = Array.from(event.target.files || []);
    event.target.value = "";
    onUpload(selected);
  };
  return (
    <aside className="files-panel">
      <header>
        <h2>沙箱文件</h2>
        {onClose && (
          <IconButton label="关闭文件面板" onClick={onClose}>
            <X />
          </IconButton>
        )}
      </header>
      <label className="file-search">
        <MagnifyingGlass />
        <input
          aria-label="搜索文件和目录"
          placeholder="搜索文件和目录"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>
      <div className="file-actions">
        <label
          className={`action-button ${uploadUnavailable ? "disabled" : ""}`}
          aria-disabled={uploadUnavailable}
        >
          <UploadSimple />
          上传文件
          <input
            id={`${inputPrefix}fileInput`}
            hidden
            type="file"
            multiple
            disabled={uploadUnavailable}
            onChange={selectUploads}
          />
        </label>
        <label
          className={`action-button ${uploadUnavailable ? "disabled" : ""}`}
          aria-disabled={uploadUnavailable}
        >
          <FolderPlus />
          上传目录
          <input
            id={`${inputPrefix}directoryInput`}
            hidden
            type="file"
            multiple
            webkitdirectory=""
            directory=""
            disabled={uploadUnavailable}
            onChange={selectUploads}
          />
        </label>
        <IconButton
          label="刷新文件"
          disabled={!sessionId || busy}
          onClick={onRefresh}
        >
          <ArrowClockwise className={busy ? "spin" : ""} />
        </IconButton>
      </div>
      <p className="file-upload-usage" aria-live="polite">
        已上传 {normalizedCount} / {normalizedLimit || "—"}
      </p>
      {feedback && (
        <div className="file-feedback" role="status">
          {feedback}
        </div>
      )}
      <div className="file-head">
        <span>名称</span>
        <span>大小</span>
      </div>
      <FileTree
        sessionId={sessionId}
        files={files}
        query={query}
        onOpen={onOpen}
      />
    </aside>
  );
}
