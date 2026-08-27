import type { ReactNode } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { ArrowLeft } from "lucide-react";
import { CostTab } from "./admin/CostTab";
import { AuditTab } from "./admin/AuditTab";
import { MemoryTab } from "./admin/MemoryTab";

type Props = {
  activeWorkspaceId: string | null;
  onBack: () => void;
  topSlot: ReactNode;
};

export function AdminPage({ activeWorkspaceId, onBack, topSlot }: Props) {
  return (
    <div className="min-h-screen bg-background flex flex-col">
      {topSlot}
      <main className="flex-1 max-w-6xl mx-auto px-4 py-6 pb-16 sm:pb-6 w-full">
        <div className="flex items-center gap-2 mb-6">
          <Button variant="ghost" size="sm" onClick={onBack}>
            <ArrowLeft className="h-4 w-4 mr-1" /> Back
          </Button>
          <h1 className="text-2xl font-bold">Admin</h1>
        </div>

        {!activeWorkspaceId ? (
          <Card>
            <CardContent className="py-8 text-center text-muted-foreground">
              Select a workspace from the top navigation to see admin data.
            </CardContent>
          </Card>
        ) : (
          <Tabs defaultValue="cost">
            <TabsList>
              <TabsTrigger value="cost">Cost</TabsTrigger>
              <TabsTrigger value="audit">Audit</TabsTrigger>
              <TabsTrigger value="memory">Memory</TabsTrigger>
            </TabsList>
            <TabsContent value="cost">
              <CostTab workspaceId={activeWorkspaceId} />
            </TabsContent>
            <TabsContent value="audit">
              <AuditTab workspaceId={activeWorkspaceId} />
            </TabsContent>
            <TabsContent value="memory">
              <MemoryTab workspaceId={activeWorkspaceId} />
            </TabsContent>
          </Tabs>
        )}
      </main>
    </div>
  );
}
