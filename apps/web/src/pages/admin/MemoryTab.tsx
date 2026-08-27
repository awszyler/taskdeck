import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Trash2, Plus, BrainCircuit } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/api/client";

type Props = { workspaceId: string };

export function MemoryTab({ workspaceId }: Props) {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [activeQuery, setActiveQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const [newText, setNewText] = useState("");

  const listQ = useQuery({
    queryKey: ["memory", workspaceId, activeQuery],
    queryFn: () => api.listMemory(workspaceId, activeQuery ? { q: activeQuery } : {}),
  });

  const addMut = useMutation({
    mutationFn: (text: string) => api.addMemory({ workspace_id: workspaceId, text }),
    onSuccess: () => {
      toast.success("Memory chunk added");
      setNewText("");
      setAdding(false);
      qc.invalidateQueries({ queryKey: ["memory", workspaceId] });
    },
    onError: (e) => toast.error(`Failed: ${String(e)}`),
  });

  const delMut = useMutation({
    mutationFn: (id: string) => api.deleteMemory(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["memory", workspaceId] });
    },
    onError: (e) => toast.error(`Failed: ${String(e)}`),
  });

  function renderContent() {
    if (listQ.isLoading) {
      return (
        <div className="space-y-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="flex gap-3">
              <Skeleton className="h-4 w-24" />
              <Skeleton className="h-4 flex-1" />
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-4 w-8" />
            </div>
          ))}
        </div>
      );
    }
    if (!listQ.data) {
      return <p className="text-destructive">{String(listQ.error)}</p>;
    }
    if (listQ.data.items.length === 0) {
      return (
        <div className="flex flex-col items-center justify-center py-10 text-center text-muted-foreground">
          <BrainCircuit className="h-10 w-10 mb-3 opacity-40" />
          <p className="text-sm font-medium mb-1">
            {activeQuery ? "No matches" : "No memory chunks"}
          </p>
          <p className="text-xs max-w-xs">
            {activeQuery
              ? "Try a different search query."
              : "Tasks that complete will be auto-ingested when TD_MEMORY_ENABLED=true. Or click + Add to seed manually."}
          </p>
        </div>
      );
    }
    return (
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-32">Source</TableHead>
            <TableHead>Text</TableHead>
            <TableHead className="w-32">Created</TableHead>
            <TableHead className="w-12" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {listQ.data.items.map((c) => (
            <TableRow key={c.id}>
              <TableCell>
                <Badge variant="secondary" className="text-[10px]">{c.source_kind}</Badge>
              </TableCell>
              <TableCell className="max-w-2xl">
                <pre className="text-xs whitespace-pre-wrap break-words font-mono">
                  {c.text.slice(0, 500)}{c.text.length > 500 ? "..." : ""}
                </pre>
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {c.created_at.slice(0, 19).replace("T", " ")}
              </TableCell>
              <TableCell>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => { if (confirm("Delete this memory chunk?")) delMut.mutate(c.id); }}
                >
                  <Trash2 className="h-3.5 w-3.5 text-destructive" />
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    );
  }

  return (
    <div className="space-y-4 mt-4">
      <Card>
        <CardHeader className="flex flex-row items-center gap-2">
          <CardTitle>Memory</CardTitle>
          <div className="ml-auto flex items-center gap-2">
            <Input
              type="text"
              placeholder="Search by similarity..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") setActiveQuery(search.trim()); }}
              className="h-8 w-72"
            />
            <Button size="sm" variant="ghost" onClick={() => setActiveQuery(search.trim())}>Search</Button>
            <Button size="sm" variant="ghost" onClick={() => { setActiveQuery(""); setSearch(""); }}>Clear</Button>
            <Button size="sm" onClick={() => setAdding((v) => !v)}>
              <Plus className="h-3.5 w-3.5 mr-1" />Add
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {adding && (
            <div className="mb-4 space-y-2 border rounded-md p-3 bg-muted/30">
              <Textarea
                placeholder="Free-form memory text — facts, conventions, learnings — that you'd like to be retrievable from future task prompts."
                value={newText}
                onChange={(e) => setNewText(e.target.value)}
                rows={4}
              />
              <div className="flex gap-2 justify-end">
                <Button size="sm" variant="ghost" onClick={() => { setAdding(false); setNewText(""); }}>Cancel</Button>
                <Button
                  size="sm"
                  onClick={() => addMut.mutate(newText.trim())}
                  disabled={!newText.trim() || addMut.isPending}
                >
                  {addMut.isPending ? "Adding..." : "Save"}
                </Button>
              </div>
            </div>
          )}
          {renderContent()}
        </CardContent>
      </Card>
    </div>
  );
}
