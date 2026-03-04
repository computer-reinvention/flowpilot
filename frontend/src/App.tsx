import { useState, useEffect } from "react";
import { Loader2 } from "lucide-react";
import { useAuth } from "@/hooks/useAuth";
import { useFlows } from "@/hooks/useFlows";
import { useRun } from "@/hooks/useRun";
import { useRuns } from "@/hooks/useRuns";
import { useSessions } from "@/hooks/useSessions";
import { LoginForm } from "@/components/auth/LoginForm";
import { SignupForm } from "@/components/auth/SignupForm";
import { AppShell } from "@/components/layout/AppShell";
import type { ActiveSection } from "@/components/layout/AppSidebar";
import { FlowGrid } from "@/components/flows/FlowGrid";
import { FlowDetail } from "@/components/flows/FlowDetail";
import { FlowRunner } from "@/components/flows/FlowRunner";
import { CreateFlowForm } from "@/components/flows/CreateFlowForm";
import { RunHistory } from "@/components/runs/RunHistory";
import { SessionList } from "@/components/sessions/SessionList";
import { SettingsPanel } from "@/components/settings/SettingsPanel";
import type { Flow, Run } from "@/lib/types";

type AuthView = "login" | "signup";
type FlowSubView = "grid" | "detail" | "create";

export default function App() {
  const auth = useAuth();
  const flows = useFlows();
  const run = useRun();
  const runs = useRuns();
  const sessions = useSessions();

  const [authView, setAuthView] = useState<AuthView>("login");
  const [activeSection, setActiveSection] = useState<ActiveSection>("flows");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [selectedFlow, setSelectedFlow] = useState<Flow | null>(null);
  const [flowSubView, setFlowSubView] = useState<FlowSubView>("grid");
  const [submitting, setSubmitting] = useState(false);

  // Auto-reconnect to active run on page load
  useEffect(() => {
    if (!auth.user || runs.loading || run.runId) return;
    const firstActive = runs.activeRuns[0];
    if (firstActive) {
      setActiveRunId(firstActive.id);
      run.reconnectToRun(firstActive.id);
    }
  }, [auth.user, runs.loading, runs.activeRuns, run.runId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Loading state
  if (auth.loading) {
    return (
      <div className="flex min-h-svh items-center justify-center">
        <Loader2 className="size-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  // Not authenticated — show login/signup
  if (!auth.user) {
    return (
      <div className="flex min-h-svh items-center justify-center px-4">
        <div className="w-full space-y-6">
          <div className="text-center">
            <h1 className="text-2xl font-bold text-balance">
              Flow<span className="text-muted-foreground font-normal">Pilot</span>
            </h1>
          </div>
          {authView === "login" ? (
            <LoginForm
              onLogin={auth.login}
              onSwitchToSignup={() => setAuthView("signup")}
              error={auth.error}
            />
          ) : (
            <SignupForm
              onSignup={auth.signup}
              onSwitchToLogin={() => setAuthView("login")}
              error={auth.error}
            />
          )}
        </div>
      </div>
    );
  }

  // --- Handlers ---

  function selectFlow(flow: Flow) {
    setSelectedFlow(flow);
    setFlowSubView("detail");
  }

  async function handleRunSubmit(values: Record<string, string>, profileId: string) {
    if (!selectedFlow) return;
    setSubmitting(true);
    try {
      const id = await run.startRun(selectedFlow.id, values, profileId || undefined);
      setActiveRunId(id ?? null);
    } finally {
      setSubmitting(false);
    }
  }

  function handleBack() {
    run.reset();
    setSelectedFlow(null);
    setActiveRunId(null);
    setFlowSubView("grid");
  }

  function handleRunReset() {
    const isActive = (
      ["pending", "connecting", "starting", "running", "paused_for_hitl"] as string[]
    ).includes(run.status);

    // If run is still active, just navigate away — don't kill it
    if (!isActive) {
      run.reset();
    }

    setActiveRunId(null);
    runs.refetch();
    if (selectedFlow) {
      setFlowSubView("detail");
    } else {
      setFlowSubView("grid");
    }
  }

  async function handleCreateFlow(data: {
    name: string;
    domain: string;
    task: string;
    entry_url?: string;
  }) {
    setSubmitting(true);
    try {
      await flows.createFlow(data);
      setFlowSubView("grid");
    } finally {
      setSubmitting(false);
    }
  }

  function handleSelectRun(id: string) {
    setActiveRunId(id);
    run.reconnectToRun(id);
    setActiveSection("flows");
  }

  function handleSelectRunObj(r: Run) {
    setActiveRunId(r.id);
    run.reconnectToRun(r.id);
    setActiveSection("flows");
  }

  function handleNavigate(section: ActiveSection) {
    setActiveSection(section);
  }

  // --- Determine what to render ---

  const isViewingRun = activeSection === "flows" && activeRunId !== null && run.status !== "idle";

  function renderContent() {
    // If viewing a run, show FlowRunner
    if (isViewingRun) {
      return (
        <FlowRunner
          status={run.status}
          steps={run.steps}
          currentStep={run.currentStep}
          error={run.error}
          sandboxId={run.sandboxId}
          onReset={handleRunReset}
          onResume={run.resumeRun}
          onCancel={run.cancelRun}
        />
      );
    }

    // Otherwise render based on section
    switch (activeSection) {
      case "flows":
        if (flowSubView === "create") {
          return (
            <div className="p-6 max-w-2xl mx-auto">
              <CreateFlowForm
                onSubmit={handleCreateFlow}
                onBack={() => setFlowSubView("grid")}
                submitting={submitting}
              />
            </div>
          );
        }
        if (flowSubView === "detail" && selectedFlow) {
          return (
            <FlowDetail
              flow={selectedFlow}
              runs={runs.runs}
              runsLoading={runs.loading}
              runsError={runs.error}
              onBack={handleBack}
              onRunSubmit={handleRunSubmit}
              onSelectRun={handleSelectRunObj}
              onUpdateFlow={flows.updateFlow}
              onDeleteFlow={flows.deleteFlow}
              onFlowDeleted={handleBack}
              submitting={submitting}
              defaultProfileId={auth.user?.id ?? ""}
              allFlows={flows.flows}
            />
          );
        }
        return (
          <div className="p-6">
            <FlowGrid
              flows={flows.flows}
              loading={flows.loading}
              error={flows.error}
              onSelect={selectFlow}
              onCreate={() => setFlowSubView("create")}
            />
          </div>
        );

      case "runs":
        return (
          <div className="p-6">
            <RunHistory
              runs={runs.runs}
              flows={flows.flows}
              loading={runs.loading}
              error={runs.error}
              onSelect={handleSelectRunObj}
            />
          </div>
        );

      case "sessions":
        return (
          <div className="p-6">
            <SessionList
              sessions={sessions.sessions}
              loading={sessions.loading}
              onDelete={sessions.deleteSession}
            />
          </div>
        );

      case "settings":
        return <SettingsPanel />;
    }
  }

  return (
    <AppShell
      activeSection={activeSection}
      onNavigate={handleNavigate}
      activeRuns={runs.activeRuns}
      flows={flows.flows}
      onSelectRun={handleSelectRun}
      onLogout={auth.logout}
      userName={auth.user.name}
      activeRunId={activeRunId}
      selectedFlowName={flowSubView === "detail" && selectedFlow ? selectedFlow.name : undefined}
    >
      {renderContent()}
    </AppShell>
  );
}
