import { Settings } from "lucide-react";

export function SettingsPanel() {
  return (
    <div className="p-6 max-w-2xl mx-auto space-y-6">
      <div>
        <h2 className="text-lg font-semibold">Settings</h2>
        <p className="text-sm text-muted-foreground mt-0.5">
          General application settings.
        </p>
      </div>

      <div className="flex flex-col items-center justify-center py-12 text-center text-muted-foreground">
        <Settings className="size-8 mb-3 opacity-40" />
        <p className="text-sm">No configurable settings yet.</p>
        <p className="text-xs mt-1">
          API Keys and Webhooks have moved to the sidebar.
        </p>
      </div>
    </div>
  );
}
