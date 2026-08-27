import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from "recharts";
import { ReceiptText } from "lucide-react";
import { api } from "@/api/client";

function SkeletonTable({ rows = 5, cols = 6 }: { rows?: number; cols?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex gap-3">
          {Array.from({ length: cols }).map((_, j) => (
            <Skeleton key={j} className="h-4 flex-1" />
          ))}
        </div>
      ))}
    </div>
  );
}

type Props = { workspaceId: string };

export function CostTab({ workspaceId }: Props) {
  const today = new Date().toISOString().slice(0, 10);
  const sevenAgo = new Date(Date.now() - 7 * 86400 * 1000).toISOString().slice(0, 10);
  const [from, setFrom] = useState(sevenAgo);
  const [to, setTo] = useState(today);

  const summaryQ = useQuery({
    queryKey: ["costs", "summary", workspaceId, from, to],
    queryFn: () => api.costsSummary(workspaceId, from, to),
  });

  const eventsQ = useQuery({
    queryKey: ["costs", "events", workspaceId],
    queryFn: () => api.costsEvents(workspaceId, 50),
  });

  const chartData = useMemo(() => {
    return (summaryQ.data?.by_day ?? []).map((d) => ({
      date: d.date,
      usd: parseFloat(d.usd),
    }));
  }, [summaryQ.data]);

  const totalUsd = summaryQ.data ? parseFloat(summaryQ.data.total_usd) : null;

  return (
    <div className="space-y-4 mt-4">
      <Card>
        <CardHeader className="flex flex-row items-center gap-2">
          <CardTitle>Spend</CardTitle>
          <div className="ml-auto flex items-center gap-2">
            <Input
              type="date"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
              className="h-8 w-36"
            />
            <span className="text-muted-foreground">→</span>
            <Input
              type="date"
              value={to}
              onChange={(e) => setTo(e.target.value)}
              className="h-8 w-36"
            />
            <Button size="sm" variant="ghost" onClick={() => summaryQ.refetch()}>
              Refresh
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {summaryQ.isLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-9 w-32" />
              <Skeleton className="h-[200px] w-full" />
            </div>
          ) : summaryQ.data ? (
            totalUsd === 0 ? (
              <div className="flex flex-col items-center justify-center py-10 text-center text-muted-foreground">
                <ReceiptText className="h-10 w-10 mb-3 opacity-40" />
                <p className="text-sm font-medium mb-1">No costs recorded</p>
                <p className="text-xs">LLM call costs appear here once Intent Parser runs.</p>
              </div>
            ) : (
              <>
                <p className="text-3xl font-bold mb-4">${summaryQ.data.total_usd}</p>
                <ResponsiveContainer width="100%" height={200}>
                  <AreaChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                    <XAxis dataKey="date" className="text-xs" />
                    <YAxis className="text-xs" />
                    <Tooltip />
                    <Area
                      type="monotone"
                      dataKey="usd"
                      stroke="hsl(var(--primary))"
                      fill="hsl(var(--primary))"
                      fillOpacity={0.3}
                    />
                  </AreaChart>
                </ResponsiveContainer>
                <div className="grid grid-cols-2 gap-4 mt-6">
                  <div>
                    <h3 className="text-sm font-semibold mb-2">By operation</h3>
                    <Table>
                      <TableBody>
                        {Object.entries(summaryQ.data.by_operation).map(([op, usd]) => (
                          <TableRow key={op}>
                            <TableCell>{op}</TableCell>
                            <TableCell className="text-right tabular-nums">${usd}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                  <div>
                    <h3 className="text-sm font-semibold mb-2">By user</h3>
                    <Table>
                      <TableBody>
                        {Object.entries(summaryQ.data.by_user).map(([uid, usd]) => (
                          <TableRow key={uid}>
                            <TableCell className="font-mono text-xs">{uid.slice(0, 8)}</TableCell>
                            <TableCell className="text-right tabular-nums">${usd}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </div>
              </>
            )
          ) : (
            <p className="text-destructive">{String(summaryQ.error)}</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Recent events (last 50)</CardTitle>
        </CardHeader>
        <CardContent>
          {eventsQ.isLoading ? (
            <SkeletonTable rows={5} cols={6} />
          ) : eventsQ.data ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Time</TableHead>
                  <TableHead>Operation</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead className="text-right">Tokens in</TableHead>
                  <TableHead className="text-right">Tokens out</TableHead>
                  <TableHead className="text-right">USD</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {eventsQ.data.items.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={6} className="text-center text-muted-foreground py-8">
                      No cost events yet.
                    </TableCell>
                  </TableRow>
                ) : (
                  eventsQ.data.items.map((e) => (
                    <TableRow key={e.id}>
                      <TableCell className="text-xs text-muted-foreground">
                        {e.created_at.slice(0, 19).replace("T", " ")}
                      </TableCell>
                      <TableCell>{e.operation}</TableCell>
                      <TableCell className="font-mono text-xs">{e.model ?? "—"}</TableCell>
                      <TableCell className="text-right tabular-nums">{e.tokens_in ?? "—"}</TableCell>
                      <TableCell className="text-right tabular-nums">{e.tokens_out ?? "—"}</TableCell>
                      <TableCell className="text-right tabular-nums">
                        {e.cost_usd ? `$${e.cost_usd}` : "—"}
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          ) : (
            <p className="text-destructive">{String(eventsQ.error)}</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
