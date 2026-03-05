import { useState, useEffect, useCallback } from "react";
import { ArrowLeft, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { RunHistory } from "@/components/runs/RunHistory";
import { FlowIntegration } from "./FlowIntegration";
import { FlowSettings } from "./FlowSettings";
import { ExplorationTree } from "./ExplorationTree";
import { BrowserCanvas } from "@/components/BrowserCanvas";
import { useTriggerHooks } from "@/hooks/useTriggerHooks";
import { apiFetch } from "@/lib/api";
import type { Flow, FlowInput, Run } from "@/lib/types";

function inputId(input: FlowInput): string {
  return input.name || input.key || input.label;
}

interface FlowDetailProps {
  flow: Flow;
  runs: Run[];
  runsLoading: boolean;
  runsError: string | null;
  onBack: () => void;
  onRunSubmit: (values: Record<string, string>, profileId: string) => void;
  onSelectRun: (run: Run) => void;
  onUpdateFlow: (flowId: string, updates: Record<string, unknown>) => Promise<Flow>;
  onDeleteFlow: (flowId: string) => Promise<void>;
  onFlowDeleted: () => void;
  submitting: boolean;
  defaultProfileId: string;
  allFlows: Flow[];
}

function renderInput(
  input: FlowInput,
  value: string,
  onChange: (val: string) => void,
  index: number,
) {
  const id = `sheet-input-${index}`;
  switch (input.type) {
    case "textarea":
      return <Textarea id={id} placeholder={input.placeholder} value={value} onChange={(e) => onChange(e.target.value)} rows={3} />;
    case "select":
      return (
        <Select value={value} onValueChange={onChange}>
          <SelectTrigger id={id}><SelectValue placeholder={input.placeholder || "Select..."} /></SelectTrigger>
          <SelectContent>
            {(input.options || []).map((opt) => <SelectItem key={opt} value={opt}>{opt}</SelectItem>)}
          </SelectContent>
        </Select>
      );
    case "number":
      return <Input id={id} type="number" placeholder={input.placeholder} value={value} onChange={(e) => onChange(e.target.value)} />;
    case "date":
      return <Input id={id} type="date" value={value} onChange={(e) => onChange(e.target.value)} />;
    case "time":
      return <Input id={id} type="time" value={value} onChange={(e) => onChange(e.target.value)} />;
    default:
      return <Input id={id} type="text" placeholder={input.placeholder} value={value} onChange={(e) => onChange(e.target.value)} />;
  }
}

export function FlowDetail({
  flow,
  runs,
  runsLoading,
  runsError,
  onBack,
  onRunSubmit,
  onSelectRun,
  onUpdateFlow,
  onDeleteFlow,
  onFlowDeleted,
  submitting,
  defaultProfileId,
  allFlows,
}: FlowDetailProps) {
  const [sheetOpen, setSheetOpen] = useState(false);
  const [values, setValues] = useState<string[]>(() => flow.inputs.map(() => ""));
  const [profileId, setProfileId] = useState(defaultProfileId);
  const [liveFlow, setLiveFlow] = useState(flow);
  const [explorationLoading, setExplorationLoading] = useState(false);
  const triggerHooks = useTriggerHooks(flow.id);

  // Keep liveFlow in sync when parent flow prop changes
  useEffect(() => setLiveFlow(flow), [flow]);

  // Resize values when explorer discovers inputs after creation
  useEffect(() => {
    setValues((prev) => {
      const len = liveFlow.inputs.length;
      if (prev.length === len) return prev;
      return Array.from({ length: len }, (_, i) => prev[i] ?? "");
    });
  }, [liveFlow.inputs.length]);

  // Stream exploration updates via WebSocket
  useEffect(() => {
    if (liveFlow.exploration_status !== "exploring" && liveFlow.exploration_status !== "paused_for_hitl" && liveFlow.exploration_status !== "awaiting_auth") return;
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${protocol}//${location.host}/ws/flows/${flow.id}`);
    ws.onmessage = (event) => {
      try {
        const updated = JSON.parse(event.data) as Flow;
        setLiveFlow(updated);
      } catch { /* ignore parse errors */ }
    };
    return () => ws.close();
  }, [liveFlow.exploration_status, flow.id]);

  const handleExplore = useCallback(async () => {
    setExplorationLoading(true);
    try {
      await apiFetch(`/api/flows/${flow.id}/explore`, { method: "POST", body: "{}" });
      setLiveFlow((f) => ({ ...f, exploration_status: "exploring", exploration_tree: [] }));
    } catch { /* ignore */ }
    finally { setExplorationLoading(false); }
  }, [flow.id]);

  const handleAuthDone = useCallback(async () => {
    try {
      await apiFetch(`/api/flows/${flow.id}/auth/done`, { method: "POST" });
      setLiveFlow((f) => ({ ...f, exploration_status: "exploring", auth_sandbox_id: undefined }));
    } catch { /* ignore */ }
  }, [flow.id]);

  const [hitlInputValues, setHitlInputValues] = useState<Record<string, string>>({});

  const handleExploreResume = useCallback(async (body?: Record<string, unknown>) => {
    try {
      await apiFetch(`/api/flows/${flow.id}/explore/resume`, {
        method: "POST",
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      setLiveFlow((f) => ({
        ...f,
        exploration_status: "exploring",
        exploration_input_request: undefined,
        exploration_confirmation_request: undefined,
      }));
      setHitlInputValues({});
    } catch { /* ignore */ }
  }, [flow.id]);

  // Filter runs to this flow
  const flowRuns = runs.filter((r) => r.flow_id === flow.id);

  function handleChange(index: number, value: string) {
    setValues((prev) => prev.map((v, i) => (i === index ? value : v)));
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const result: Record<string, string> = {};
    liveFlow.inputs.forEach((input, i) => {
      result[inputId(input)] = values[i] ?? "";
    });
    onRunSubmit(result, profileId);
    setSheetOpen(false);
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 px-6 py-4 border-b shrink-0">
        <Button variant="ghost" size="icon" onClick={onBack} aria-label="Go back">
          <ArrowLeft className="size-4" />
        </Button>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            {flow.icon && <span className="text-lg">{flow.icon}</span>}
            <h1 className="text-lg font-semibold truncate">{flow.name}</h1>
            <Badge variant="secondary" className="shrink-0">{flow.domain}</Badge>
          </div>
        </div>
        <Button size="sm" className="gap-1.5 shrink-0" onClick={() => setSheetOpen(true)}>
          <Play className="size-3.5" />
          Run
        </Button>
      </div>

      {/* Auth pre-step browser view */}
      {liveFlow.exploration_status === "awaiting_auth" && liveFlow.auth_sandbox_id && (
        <div className="flex flex-col shrink-0" style={{ height: "50%" }}>
          <div className="flex items-center justify-between px-4 py-2 bg-amber-50 border-b text-sm">
            <span className="text-amber-800">Please log in, then click Done</span>
            <Button size="sm" variant="outline" onClick={handleAuthDone}>
              Done — I've logged in
            </Button>
          </div>
          <BrowserCanvas sandboxId={liveFlow.auth_sandbox_id} className="flex-1" />
        </div>
      )}

      {/* Live browser during exploration */}
      {(liveFlow.exploration_status === "exploring" || liveFlow.exploration_status === "paused_for_hitl") && liveFlow.exploration_sandbox_id && (
        <div className="flex flex-col shrink-0" style={{ height: "50%" }}>
          <BrowserCanvas
            sandboxId={liveFlow.exploration_sandbox_id}
            isPaused={liveFlow.exploration_status === "paused_for_hitl"}
            className="flex-1"
          />
        </div>
      )}

      {/* Tabs */}
      <Tabs defaultValue={liveFlow.exploration_status && liveFlow.exploration_status !== "pending" ? "exploration" : "runs"} className="flex-1 flex flex-col min-h-0 overflow-hidden">
        <div className="px-6 pt-2 shrink-0">
          <TabsList variant="line">
            <TabsTrigger value="exploration" className="gap-1.5">
              Exploration
              {(liveFlow.exploration_status === "exploring" || liveFlow.exploration_status === "awaiting_auth") && (
                <span className="size-1.5 rounded-full bg-blue-500 animate-pulse" />
              )}
              {liveFlow.exploration_status === "ready" && (
                <span className="size-1.5 rounded-full bg-green-500" />
              )}
            </TabsTrigger>
            <TabsTrigger value="runs">Runs</TabsTrigger>
            <TabsTrigger value="integration">Integration</TabsTrigger>
            <TabsTrigger value="settings">Settings</TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="exploration" className="flex-1 overflow-auto px-6 py-4">
          <div className="max-w-2xl">
            <ExplorationTree
              flow={liveFlow}
              onExplore={handleExplore}
              onResume={handleExploreResume}
              hitlInputValues={hitlInputValues}
              onHitlInputChange={setHitlInputValues}
              exploring={explorationLoading || liveFlow.exploration_status === "exploring"}
            />
          </div>
        </TabsContent>

        <TabsContent value="runs" className="flex-1 overflow-auto px-6 py-4">
          <RunHistory
            runs={flowRuns}
            flows={allFlows}
            loading={runsLoading}
            error={runsError}
            onSelect={onSelectRun}
          />
        </TabsContent>

        <TabsContent value="integration" className="flex-1 overflow-auto px-6 py-4">
          <FlowIntegration
            flow={liveFlow}
            hooks={triggerHooks.hooks}
            hooksLoading={triggerHooks.loading}
            onCreateHook={triggerHooks.createHook}
            onDeleteHook={triggerHooks.deleteHook}
          />
        </TabsContent>

        <TabsContent value="settings" className="flex-1 overflow-auto px-6 py-4">
          <div className="max-w-xl">
            <FlowSettings
              flow={liveFlow}
              onUpdate={onUpdateFlow}
              onDelete={onDeleteFlow}
              onDeleted={onFlowDeleted}
            />
          </div>
        </TabsContent>
      </Tabs>

      {/* Run Sheet */}
      <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
        <SheetContent side="right" className="sm:max-w-md overflow-y-auto">
          <SheetHeader>
            <SheetTitle>Run {liveFlow.name}</SheetTitle>
            <SheetDescription>{liveFlow.task}</SheetDescription>
          </SheetHeader>
          <form onSubmit={handleSubmit} className="space-y-4 px-4 pb-4">
            {liveFlow.inputs.map((input, index) => (
              <div key={inputId(input)} className="space-y-2">
                <Label htmlFor={`sheet-input-${index}`}>{input.label}</Label>
                {renderInput(input, values[index] ?? "", (val) => handleChange(index, val), index)}
              </div>
            ))}
            <div className="space-y-2 border-t pt-4">
              <Label htmlFor="sheet-profile-id" className="text-muted-foreground text-xs">
                Profile ID <span className="font-normal">(session scope)</span>
              </Label>
              <Input
                id="sheet-profile-id"
                type="text"
                value={profileId}
                onChange={(e) => setProfileId(e.target.value)}
                placeholder="defaults to your user ID"
              />
            </div>
            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? "Starting..." : "Start Run"}
            </Button>
          </form>
        </SheetContent>
      </Sheet>
    </div>
  );
}
