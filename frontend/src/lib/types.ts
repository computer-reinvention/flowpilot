export interface User {
  id: string;
  email: string;
  name: string;
}

export interface FlowInput {
  name: string;
  key?: string;
  label: string;
  type: "text" | "select" | "textarea" | "number" | "date" | "time";
  placeholder?: string;
  required?: boolean;
  options?: string[];
}

export interface Flow {
  id: string;
  name: string;
  domain: string;
  task: string;
  inputs: FlowInput[];
  entry_url?: string;
  icon?: string;
  created_by: string;
  created_at: string;
  exploration_status?: "pending" | "exploring" | "ready" | "failed" | "paused_for_hitl";
  exploration_error?: string;
  exploration_notes?: string;
  exploration_tree?: ExplorationStage[];
  exploration_sandbox_id?: string;
  exploration_input_request?: { name: string; label: string; description?: string; type?: "text" | "select"; options?: string[] }[];
  exploration_confirmation_request?: string;
}

export interface ExplorationStage {
  id: string;
  title: string;
  url: string;
  type: "page" | "form" | "auth_wall" | "navigation" | "extraction" | "dynamic";
  note?: string;
  reflex_name?: string;
  children: ExplorationStage[];
}

export interface RunStep {
  status: string;
  message: string;
  timestamp: string;
}

export interface Run {
  id: string;
  flow_id: string;
  user_id: string;
  status: "pending" | "connecting" | "starting" | "running" | "paused_for_hitl" | "completed" | "failed" | "cancelled";
  inputs: Record<string, string>;
  sandbox_id?: string;
  browser_cdp_url?: string;
  steps: RunStep[];
  current_step?: string;
  result?: Record<string, unknown>;
  error?: string;
  created_at: string;
  completed_at?: string;
  profile_id?: string;
}

export interface ApiKey {
  id: string;
  user_id: string;
  name: string;
  key_prefix: string;
  created_at: string;
  last_used_at?: string;
  key?: string; // only present on creation
}

export interface Webhook {
  id: string;
  user_id: string;
  url: string;
  events: string[];
  enabled: boolean;
  created_at: string;
  secret?: string; // only present on creation
}

export interface TriggerHook {
  id: string;
  flow_id: string;
  user_id: string;
  name: string;
  token: string;
  url: string;
  created_at: string;
  last_used_at?: string;
}

export interface Session {
  id: string;
  user_id: string;
  domain: string;
  profile_name?: string;
  captured_at: string;
}

export interface AuthResponse {
  token: string;
  user: User;
}

export const ACTIVE_RUN_STATUSES = ["pending", "connecting", "running", "paused_for_hitl"] as const;
