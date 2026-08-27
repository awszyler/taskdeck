"use client";
import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import remarkGfm from "remark-gfm";
import { api } from "@/api/client";

type Props = {
  taskId: string;
  path: string;
};

/** Render an agent-produced markdown file with GFM extensions
 *  (tables, task lists, strikethrough) and code-block syntax
 *  highlighting via Prism. Bound to the drawer's width — overflow
 *  is the parent's problem. */
export function MarkdownViewer({ taskId, path }: Props) {
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

  return (
    <div className="prose prose-sm dark:prose-invert max-w-none">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code({ className, children, ...rest }) {
            const match = /language-(\w+)/.exec(className ?? "");
            const inline = !match && !String(children).includes("\n");
            if (inline) {
              return (
                <code className="bg-muted px-1 py-0.5 rounded text-[13px]" {...rest}>
                  {children}
                </code>
              );
            }
            return (
              <SyntaxHighlighter
                language={match?.[1] ?? "text"}
                style={oneDark}
                customStyle={{
                  margin: 0,
                  borderRadius: 6,
                  fontSize: 12.5,
                }}
                PreTag="div"
              >
                {String(children).replace(/\n$/, "")}
              </SyntaxHighlighter>
            );
          },
        }}
      >
        {q.data?.text ?? ""}
      </ReactMarkdown>
    </div>
  );
}
