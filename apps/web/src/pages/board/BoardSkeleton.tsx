import { Skeleton } from "@/components/ui/skeleton";

export function BoardSkeleton() {
  return (
    <div className="flex gap-3 overflow-x-auto pb-2">
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="flex w-[280px] shrink-0 flex-col rounded-xl bg-muted/40 p-2">
          <div className="mb-2 flex items-center gap-2 px-1.5">
            <Skeleton className="h-3.5 w-3.5 rounded-full" />
            <Skeleton className="h-3 w-16" />
          </div>
          <div className="space-y-2">
            {[0, 1, 2].map((j) => (
              <div key={j} className="rounded-md border bg-background p-2.5">
                <Skeleton className="h-4 w-full mb-2" />
                <Skeleton className="h-3 w-2/3" />
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
