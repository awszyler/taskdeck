import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ScrollText } from "lucide-react";
import { api } from "@/api/client";

type Props = { workspaceId: string };

const KINDS = [
  "all",
  "login",
  "logout",
  "workspace.create",
  "invite.issue",
  "invite.consume",
  "member.remove",
];

export function AuditTab({ workspaceId }: Props) {
  const [kind, setKind] = useState("all");
  const q = useQuery({
    queryKey: ["audit", workspaceId, kind],
    queryFn: () => api.auditEvents(workspaceId, kind === "all" ? undefined : kind),
  });

  return (
    <Card className="mt-4">
      <CardHeader className="flex flex-row items-center gap-2">
        <CardTitle>Audit log</CardTitle>
        <div className="ml-auto">
          <Select value={kind} onValueChange={setKind}>
            <SelectTrigger className="w-48">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {KINDS.map((k) => (
                <SelectItem key={k} value={k}>
                  {k}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </CardHeader>
      <CardContent>
        {q.isLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="flex gap-3">
                {[120, 80, 64, 80, 100].map((w, j) => (
                  <Skeleton key={j} className="h-4" style={{ width: w }} />
                ))}
              </div>
            ))}
          </div>
        ) : q.data ? (
          q.data.items.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center text-muted-foreground">
              <ScrollText className="h-10 w-10 mb-3 opacity-40" />
              <p className="text-sm font-medium mb-1">No events</p>
              <p className="text-xs">Activity will appear here as users log in, create tasks, etc.</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Time</TableHead>
                  <TableHead>Kind</TableHead>
                  <TableHead>User</TableHead>
                  <TableHead>Target</TableHead>
                  <TableHead>Meta</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {q.data.items.map((e) => (
                  <TableRow key={e.id}>
                    <TableCell className="text-xs text-muted-foreground">
                      {e.created_at.slice(0, 19).replace("T", " ")}
                    </TableCell>
                    <TableCell>
                      <code className="text-xs">{e.kind}</code>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {e.user_id?.slice(0, 8) ?? "—"}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {e.target_type
                        ? `${e.target_type}:${e.target_id?.slice(0, 8) ?? "?"}`
                        : "—"}
                    </TableCell>
                    <TableCell className="text-xs">
                      {Object.keys(e.meta).length > 0 ? JSON.stringify(e.meta) : "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )
        ) : (
          <p className="text-destructive">{String(q.error)}</p>
        )}
      </CardContent>
    </Card>
  );
}
