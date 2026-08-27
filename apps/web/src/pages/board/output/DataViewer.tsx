"use client";
import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import Papa from "papaparse";
import { useMemo } from "react";
import { api } from "@/api/client";

type Props = {
  taskId: string;
  path: string;
};

const TABLE_ROW_CAP = 500;
const TABLE_COL_CHAR_CAP = 200;

export function DataViewer({ taskId, path }: Props) {
  const q = useQuery({
    queryKey: ["sandbox-file", taskId, path],
    queryFn: () => api.fetchSandboxFile(taskId, path),
    staleTime: 30_000,
  });

  if (q.isLoading) {
    return (
      <div className="flex items-center gap-2 py-4 text-muted-foreground text-sm">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading {path}…
      </div>
    );
  }
  if (q.isError) {
    return (
      <div className="text-sm text-destructive py-4">
        Failed to load {path}: {String(q.error)}
      </div>
    );
  }

  const text = q.data?.text ?? "";
  const lower = path.toLowerCase();

  if (lower.endsWith(".json") || lower.endsWith(".jsonl") || lower.endsWith(".ndjson")) {
    return <JsonView text={text} path={path} />;
  }
  // Default: treat as CSV/TSV.
  return <CsvView text={text} path={path} delimiter={lower.endsWith(".tsv") ? "\t" : ","} />;
}

function CsvView({ text, path, delimiter }: { text: string; path: string; delimiter: string }) {
  const parsed = useMemo(
    () => Papa.parse<string[]>(text, {
      delimiter,
      skipEmptyLines: true,
    }),
    [text, delimiter],
  );

  if (parsed.errors.length > 0) {
    return (
      <div className="text-sm text-destructive py-2">
        Parse errors: {parsed.errors.slice(0, 3).map((e) => e.message).join("; ")}
      </div>
    );
  }

  const rows = parsed.data;
  if (rows.length === 0) return <div className="text-muted-foreground text-sm">empty file</div>;

  const [header, ...body] = rows;
  const visibleBody = body.slice(0, TABLE_ROW_CAP);
  const truncated = body.length > TABLE_ROW_CAP;

  return (
    <div className="space-y-2">
      <div className="text-xs text-muted-foreground font-mono">
        {path} · {body.length} row{body.length === 1 ? "" : "s"}
        {truncated && ` (showing first ${TABLE_ROW_CAP})`}
      </div>
      <div className="overflow-auto border rounded max-h-[500px]">
        <table className="text-xs w-full">
          <thead className="sticky top-0 bg-muted/80 backdrop-blur">
            <tr>
              {(header ?? []).map((h, i) => (
                <th key={i} className="px-2 py-1.5 text-left font-medium border-b">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visibleBody.map((row, ri) => (
              <tr key={ri} className="hover:bg-accent/40">
                {row.map((cell, ci) => (
                  <td key={ci} className="px-2 py-1 border-b border-border/40 align-top">
                    {cell.length > TABLE_COL_CHAR_CAP
                      ? cell.slice(0, TABLE_COL_CHAR_CAP) + "…"
                      : cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function JsonView({ text, path }: { text: string; path: string }) {
  const lower = path.toLowerCase();
  const parsed = useMemo(() => {
    try {
      if (lower.endsWith(".jsonl") || lower.endsWith(".ndjson")) {
        // Each line is its own JSON value.
        return text
          .split("\n")
          .filter((l) => l.trim())
          .map((l, i) => {
            try { return JSON.parse(l); }
            catch { return { __parse_error_line: i + 1, __raw: l.slice(0, 200) }; }
          });
      }
      return JSON.parse(text);
    } catch (e) {
      return { __parse_error: String(e) };
    }
  }, [text, lower]);

  return (
    <div className="space-y-2">
      <div className="text-xs text-muted-foreground font-mono">{path}</div>
      <pre className="text-xs bg-muted/40 border rounded p-3 overflow-auto max-h-[500px] whitespace-pre-wrap break-words">
        {JSON.stringify(parsed, null, 2)}
      </pre>
    </div>
  );
}
