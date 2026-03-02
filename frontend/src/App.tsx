import { useState } from "react";
import { Loader2 } from "lucide-react";
import { useAuth } from "@/hooks/useAuth";
import { useFlows } from "@/hooks/useFlows";
import { useRun } from "@/hooks/useRun";
import { useSessions } from "@/hooks/useSessions";
import { LoginForm } from "@/components/auth/LoginForm";
import { SignupForm } from "@/components/auth/SignupForm";
import { Header } from "@/components/layout/Header";
import { Container } from "@/components/layout/Container";
import { FlowGrid } from "@/components/flows/FlowGrid";
import { FlowForm } from "@/components/flows/FlowForm";
import { FlowRunner } from "@/components/flows/FlowRunner";
import { CreateFlowForm } from "@/components/flows/CreateFlowForm";
import { SessionList } from "@/components/sessions/SessionList";
import { Separator } from "@/components/ui/separator";
import type { Flow } from "@/lib/types";

type AuthView = "login" | "signup";
type AppView = "grid" | "form" | "running" | "create";

export default function App() {
  const auth = useAuth();
  const flows = useFlows();
  const run = useRun();
  const sessions = useSessions();

  const [authView, setAuthView] = useState<AuthView>("login");
  const [appView, setAppView] = useState<AppView>("grid");
  const [selectedFlow, setSelectedFlow] = useState<Flow | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Loading state
  if (auth.loading) {
    return (
      <div className="flex min-h-svh items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  // Not authenticated — show login/signup
  if (!auth.user) {
    return (
      <div className="flex min-h-svh items-center justify-center px-4">
        <div className="w-full space-y-6">
          <div className="text-center">
            <h1 className="text-2xl font-bold tracking-tight">
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

  // Authenticated — main app
  function selectFlow(flow: Flow) {
    setSelectedFlow(flow);
    setAppView("form");
  }

  async function handleRunSubmit(values: Record<string, string>) {
    if (!selectedFlow) return;
    setSubmitting(true);
    setAppView("running");
    try {
      await run.startRun(selectedFlow.id, values);
    } finally {
      setSubmitting(false);
    }
  }

  function handleBack() {
    run.reset();
    setSelectedFlow(null);
    setAppView("grid");
  }

  function handleRunReset() {
    run.reset();
    if (selectedFlow) {
      setAppView("form");
    } else {
      setAppView("grid");
    }
  }

  async function handleCreateFlow(data: {
    name: string;
    domain: string;
    description: string;
    entry_url?: string;
  }) {
    setSubmitting(true);
    try {
      await flows.createFlow(data);
      setAppView("grid");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Container>
      <Header user={auth.user} onLogout={auth.logout} />
      <Separator className="mb-6" />

      {appView === "grid" && (
        <div className="space-y-6">
          <FlowGrid
            flows={flows.flows}
            loading={flows.loading}
            error={flows.error}
            onSelect={selectFlow}
            onCreate={() => setAppView("create")}
          />
          <SessionList
            sessions={sessions.sessions}
            loading={sessions.loading}
            onDelete={sessions.deleteSession}
          />
        </div>
      )}

      {appView === "create" && (
        <CreateFlowForm
          onSubmit={handleCreateFlow}
          onBack={handleBack}
          submitting={submitting}
        />
      )}

      {appView === "form" && selectedFlow && (
        <FlowForm
          flow={selectedFlow}
          onSubmit={handleRunSubmit}
          onBack={handleBack}
          submitting={submitting}
        />
      )}

      {appView === "running" && (
        <FlowRunner
          status={run.status}
          steps={run.steps}
          currentStep={run.currentStep}
          error={run.error}
          novncUrl={run.novncUrl}
          onReset={handleRunReset}
        />
      )}
    </Container>
  );
}
