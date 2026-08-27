"use client";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, FileCode, Folder, Loader2 } from "lucide-react";
import { useState } from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { api } from "@/api/client";
import { cn } from "@/lib/utils";

type Props = {
  taskId: string;
  /** entry from the manifest. Can be a single file ("src/main.py")
   *  or a directory ("src/"). For directories, we fetch the tree
   *  and let the user pick a file. */
  path: string;
};

const EXT_TO_LANG: Record<string, string> = {
  ts: "typescript", tsx: "tsx", js: "javascript", jsx: "jsx",
  py: "python", rb: "ruby", go: "go", rs: "rust", java: "java",
  c: "c", cpp: "cpp", h: "c", hpp: "cpp",
  sh: "bash", zsh: "bash", fish: "bash",
  yml: "yaml", yaml: "yaml", json: "json", toml: "toml",
  md: "markdown", html: "html", css: "css", scss: "scss",
  sql: "sql", graphql: "graphql", proto: "protobuf",
};

function langForFile(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  return EXT_TO_LANG[ext] ?? "text";
}

export function CodeViewer({ taskId, path }: Props) {
  // We don't know up front whether `path` is a file or a directory.
  // Try fetching its content as a file first; on failure (could be
  // 400 "is a directory"), fall through to the tree view.
  const treeQ = useQuery({
    queryKey: ["sandbox-tree", taskId],
    queryFn: () => api.getSandboxTree(taskId),
    staleTime: 60_000,
  });

  // Determine if `path` is a directory by looking it up in the tree.
  const treeEntries = treeQ.data?.entries ?? [];
  const pathEntry = treeEntries.find((e) => e.path === path.replace(/\/$/, ""));
  const isDir = pathEntry?.kind === "dir" || path.endsWith("/");

  const [selectedFile, setSelectedFile] = useState<string | null>(
    isDir ? null : path,
  );

  // Effective file (selected, or the manifest single-file entry).
  const fileToShow = selectedFile ?? (isDir ? null : path);

  if (treeQ.isLoading) {
    return (
      <div className="flex items-center gap-2 py-4 text-muted-foreground text-sm">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading file tree…
      </div>
    );
  }

  // Subset the tree to entries under `path` when the manifest entry
  // is a directory; otherwise show all tree files.
  const filtered = isDir
    ? treeEntries.filter((e) => e.path.startsWith(path) || e.path === path.replace(/\/$/, ""))
    : treeEntries;
  const fileEntries = filtered.filter((e) => e.kind === "file");

  return (
    <div className="grid grid-cols-[200px_1fr] gap-3 max-h-[600px]">
      {/* File tree */}
      <div className="overflow-y-auto border rounded p-2 text-xs space-y-0.5">
        {fileEntries.length === 0 ? (
          <div className="text-muted-foreground italic">no files</div>
        ) : (
          fileEntries.map((e) => (
            <button
              key={e.path}
              onClick={() => setSelectedFile(e.path)}
              className={cn(
                "w-full flex items-center gap-1.5 px-1.5 py-0.5 rounded text-left hover:bg-accent",
                selectedFile === e.path && "bg-accent font-medium",
              )}
            >
              <FileCode className="h-3 w-3 shrink-0 text-muted-foreground" />
              <span className="truncate">{e.path}</span>
            </button>
          ))
        )}
      </div>

      {/* File content */}
      <div className="overflow-y-auto border rounded">
        {fileToShow ? (
          <FileContent taskId={taskId} path={fileToShow} />
        ) : (
          <div className="p-4 text-sm text-muted-foreground">
            Select a file from the tree.
          </div>
        )}
      </div>
    </div>
  );
}

function FileContent({ taskId, path }: { taskId: string; path: string }) {
  const q = useQuery({
    queryKey: ["sandbox-file", taskId, path],
    queryFn: () => api.fetchSandboxFile(taskId, path),
    staleTime: 30_000,
  });

  if (q.isLoading) {
    return (
      <div className="flex items-center gap-2 p-4 text-muted-foreground text-sm">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading {path}…
      </div>
    );
  }
  if (q.isError) {
    return (
      <div className="p-4 text-sm text-destructive">
        Failed to load {path}: {String(q.error)}
      </div>
    );
  }

  const lang = langForFile(path);
  return (
    <div className="text-xs">
      <div className="px-3 py-1.5 border-b bg-muted/40 font-mono text-muted-foreground">
        {path}
      </div>
      <SyntaxHighlighter
        language={lang}
        style={oneDark}
        customStyle={{
          margin: 0,
          borderRadius: 0,
          fontSize: 12,
        }}
        showLineNumbers
      >
        {q.data?.text ?? ""}
      </SyntaxHighlighter>
    </div>
  );
}
