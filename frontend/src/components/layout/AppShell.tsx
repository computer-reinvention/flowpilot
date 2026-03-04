import type { ReactNode } from "react";
import { SidebarProvider, SidebarInset, SidebarTrigger } from "@/components/ui/sidebar";
import { Separator } from "@/components/ui/separator";
import { AppSidebar } from "./AppSidebar";
import type { ActiveSection } from "./AppSidebar";
import { Badge } from "@/components/ui/badge";
import type { Run, Flow } from "@/lib/types";

interface AppShellProps {
  activeSection: ActiveSection;
  onNavigate: (section: ActiveSection) => void;
  activeRuns: Run[];
  flows: Flow[];
  onSelectRun: (runId: string) => void;
  onLogout: () => void;
  userName?: string;
  activeRunId: string | null;
  children: ReactNode;
}

const SECTION_LABELS: Record<ActiveSection, string> = {
  flows: "Flows",
  runs: "Runs",
  sessions: "Sessions",
};

export function AppShell({
  activeSection,
  onNavigate,
  activeRuns,
  flows,
  onSelectRun,
  onLogout,
  userName,
  activeRunId,
  children,
}: AppShellProps) {
  return (
    <SidebarProvider>
      <AppSidebar
        activeSection={activeSection}
        onNavigate={onNavigate}
        activeRuns={activeRuns}
        flows={flows}
        onSelectRun={onSelectRun}
        onLogout={onLogout}
        userName={userName}
      />
      <SidebarInset className="flex flex-col h-svh overflow-hidden">
        <header className="flex h-12 shrink-0 items-center gap-2 border-b px-4">
          <SidebarTrigger className="-ml-1" />
          <Separator orientation="vertical" className="mr-2 !h-4" />
          <span className="text-sm font-medium">
            {activeRunId ? "Run" : SECTION_LABELS[activeSection]}
          </span>
          <div className="ml-auto flex items-center gap-2">
            {activeRuns.map((run) => {
              const flow = flows.find((f) => f.id === run.flow_id);
              return (
                <Badge
                  key={run.id}
                  variant="outline"
                  className="cursor-pointer gap-1.5 text-xs bg-green-500/10 text-green-600 border-green-500/20 hover:bg-green-500/20"
                  onClick={() => onSelectRun(run.id)}
                >
                  <span className="relative flex size-1.5">
                    <span className="absolute inline-flex size-full animate-ping rounded-full bg-green-400 opacity-75" />
                    <span className="relative inline-flex size-1.5 rounded-full bg-green-500" />
                  </span>
                  {flow?.icon} {flow?.name ?? run.flow_id}
                </Badge>
              );
            })}
          </div>
        </header>
        <main className="flex-1 overflow-auto">
          {children}
        </main>
      </SidebarInset>
    </SidebarProvider>
  );
}
