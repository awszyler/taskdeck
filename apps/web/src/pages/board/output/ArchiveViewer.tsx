"use client";
import { Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api } from "@/api/client";

type Props = {
  taskId: string;
  path: string;
};

/** Binary / archive: just offer a download link. The browser fetches
 *  the URL with the user's session cookie. */
export function ArchiveViewer({ taskId, path }: Props) {
  return (
    <div className="flex flex-col items-start gap-3 py-3">
      <div className="text-xs text-muted-foreground font-mono">{path}</div>
      <Button asChild size="sm" variant="outline">
        {/* `download` with the file's basename is a hint to the browser
            in case Content-Disposition isn't forwarded by some proxy
            in the chain. The basename keeps CJK / spaces intact. */}
        <a
          href={api.sandboxFileUrl(taskId, path)}
          download={path.split("/").pop()}
        >
          <Download className="h-3.5 w-3.5 mr-1.5" />
          Download
        </a>
      </Button>
    </div>
  );
}
