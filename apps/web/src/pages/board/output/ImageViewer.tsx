"use client";
import { api } from "@/api/client";

type Props = {
  taskId: string;
  path: string;
};

/** Render an image inline. The browser fetches the URL with the
 *  user's session cookie (Caddy forward_auth gates it). Constrained
 *  to drawer width with object-contain so portrait/landscape
 *  images both fit reasonably. */
export function ImageViewer({ taskId, path }: Props) {
  return (
    <div className="flex flex-col items-center gap-2 py-2">
      <img
        src={api.sandboxFileUrl(taskId, path)}
        alt={path}
        className="max-w-full max-h-[600px] object-contain rounded border"
      />
      <div className="text-xs text-muted-foreground font-mono">{path}</div>
    </div>
  );
}
