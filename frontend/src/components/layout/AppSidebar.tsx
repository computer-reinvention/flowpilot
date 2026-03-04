import {
  Workflow,
  History,
  Globe,
  LogOut,
  Settings,
} from "lucide-react";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuItem,
  SidebarMenuButton,
  SidebarSeparator,
} from "@/components/ui/sidebar";
import type { Run, Flow } from "@/lib/types";

export type ActiveSection = "flows" | "runs" | "sessions" | "settings";

interface AppSidebarProps {
  activeSection: ActiveSection;
  onNavigate: (section: ActiveSection) => void;
  activeRuns: Run[];
  flows: Flow[];
  onSelectRun: (runId: string) => void;
  onLogout: () => void;
  userName?: string;
}

const NAV_ITEMS: { key: ActiveSection; label: string; icon: typeof Workflow }[] = [
  { key: "flows",    label: "Flows",    icon: Workflow },
  { key: "runs",     label: "Runs",     icon: History  },
  { key: "sessions", label: "Auth Sessions", icon: Globe    },
];

export function AppSidebar({
  activeSection,
  onNavigate,
  activeRuns,
  flows,
  onSelectRun,
  onLogout,
  userName,
}: AppSidebarProps) {
  const flowMap = new Map(flows.map((f) => [f.id, f]));

  return (
    <Sidebar>
      <SidebarHeader className="px-4 py-3">
        <span className="text-lg font-bold">
          Flow<span className="text-muted-foreground font-normal">Pilot</span>
        </span>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              {NAV_ITEMS.map(({ key, label, icon: Icon }) => (
                <SidebarMenuItem key={key}>
                  <SidebarMenuButton
                    isActive={activeSection === key}
                    onClick={() => onNavigate(key)}
                    tooltip={label}
                  >
                    <Icon className="size-4" />
                    <span>{label}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        {activeRuns.length > 0 && (
          <>
            <SidebarSeparator />
            <SidebarGroup>
              <SidebarGroupLabel>Active Runs</SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>
                  {activeRuns.map((run) => {
                    const flow = flowMap.get(run.flow_id);
                    return (
                      <SidebarMenuItem key={run.id}>
                        <SidebarMenuButton onClick={() => onSelectRun(run.id)}>
                          <span className="relative flex size-2 shrink-0">
                            <span className="absolute inline-flex size-full animate-ping rounded-full bg-green-400 opacity-75" />
                            <span className="relative inline-flex size-2 rounded-full bg-green-500" />
                          </span>
                          <span className="truncate text-xs">
                            {flow?.icon} {flow?.name ?? run.flow_id}
                          </span>
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    );
                  })}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          </>
        )}
      </SidebarContent>

      <SidebarFooter className="px-4 py-3 space-y-1">
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              isActive={activeSection === "settings"}
              onClick={() => onNavigate("settings")}
              tooltip="Settings"
            >
              <Settings className="size-4" />
              <span>Settings</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
        <div className="flex items-center justify-between pt-1">
          <span className="text-xs text-muted-foreground truncate">{userName}</span>
          <button
            onClick={onLogout}
            className="text-muted-foreground hover:text-foreground"
            aria-label="Log out"
          >
            <LogOut className="size-4" />
          </button>
        </div>
      </SidebarFooter>
    </Sidebar>
  );
}
