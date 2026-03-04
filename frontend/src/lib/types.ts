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
  description: string;
  inputs: FlowInput[];
  entry_url?: string;
  icon?: string;
  created_by: string;
  created_at: string;
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
  status: "pending" | "connecting" | "starting" | "running" | "paused_for_auth" | "completed" | "failed" | "cancelled";
  inputs: Record<string, string>;
  sandbox_id?: string;
  steps: RunStep[];
  current_step?: string;
  result?: Record<string, unknown>;
  error?: string;
  created_at: string;
  completed_at?: string;
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

export const ACTIVE_RUN_STATUSES = ["pending", "connecting", "running", "paused_for_auth"] as const;
