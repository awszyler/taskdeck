import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PlusCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api, type Workspace } from "@/api/client";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type Props = {
  activeId: string | null;
  onChange: (id: string) => void;
};

export function WorkspacePicker({ activeId, onChange }: Props) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["workspaces"], queryFn: api.listWorkspaces });
  const [open, setOpen] = useState(false);
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");

  const create = useMutation({
    mutationFn: (b: { slug: string; name: string }) => api.createWorkspace(b),
    onSuccess: (ws: Workspace) => {
      qc.invalidateQueries({ queryKey: ["workspaces"] });
      onChange(ws.id);
      setOpen(false);
      setSlug("");
      setName("");
      toast.success(`Workspace "${ws.slug}" created`);
    },
    onError: (err) => {
      toast.error(String(err));
    },
  });

  const items = q.data?.items ?? [];

  useEffect(() => {
    // If the cached activeId points to a workspace the user can't see
    // anymore (membership revoked, fresh Cognito user with stale
    // localStorage), it would silently mis-route POSTs. Swap to the first
    // visible workspace.
    if (q.isLoading) return;
    const inList = activeId && items.some((w) => w.id === activeId);
    if (!inList && items.length > 0 && items[0]) onChange(items[0].id);
  }, [activeId, items, onChange, q.isLoading]);

  return (
    <div className="flex items-center gap-2">
      <Select value={activeId ?? ""} onValueChange={onChange}>
        <SelectTrigger className="w-[180px] h-8 text-sm">
          <SelectValue placeholder="Select workspace" />
        </SelectTrigger>
        <SelectContent>
          {items.length === 0 && (
            <SelectItem value="__none__" disabled>
              No workspaces
            </SelectItem>
          )}
          {items.map((w) => (
            <SelectItem key={w.id} value={w.id}>
              {w.slug}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <Button
        variant="ghost"
        size="sm"
        className="h-8 px-2 text-muted-foreground hover:text-foreground"
        onClick={() => setOpen(true)}
      >
        <PlusCircle className="h-4 w-4" />
        <span className="sr-only">New workspace</span>
      </Button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Create workspace</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 py-2">
            <div className="space-y-1">
              <label className="text-sm font-medium text-foreground">Slug</label>
              <Input
                placeholder="my-team"
                value={slug}
                onChange={(e) => setSlug(e.target.value)}
                className="font-mono"
              />
            </div>
            <div className="space-y-1">
              <label className="text-sm font-medium text-foreground">Name</label>
              <Input
                placeholder="My Team"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={!slug || !name || create.isPending}
              onClick={() => create.mutate({ slug, name })}
            >
              {create.isPending ? "Creating…" : "Create"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
