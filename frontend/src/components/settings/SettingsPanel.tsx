import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Key, Webhook, AlertCircle } from "lucide-react";
import { useApiKeys } from "@/hooks/useApiKeys";
import { useWebhooks } from "@/hooks/useWebhooks";
import { ApiKeySettings } from "./ApiKeySettings";
import { WebhookSettings } from "./WebhookSettings";

export function SettingsPanel() {
  const apiKeys = useApiKeys();
  const webhooks = useWebhooks();

  return (
    <div className="p-6 max-w-2xl mx-auto space-y-6">
      <div>
        <h2 className="text-lg font-semibold">Settings</h2>
        <p className="text-sm text-muted-foreground mt-0.5">
          Manage API access and configure webhook integrations.
        </p>
      </div>

      {(apiKeys.error || webhooks.error) && (
        <div className="flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          <AlertCircle className="size-3.5 shrink-0" />
          {apiKeys.error ?? webhooks.error}
        </div>
      )}

      <Tabs defaultValue="api-keys">
        <TabsList className="grid w-full grid-cols-2">
          <TabsTrigger value="api-keys" className="gap-1.5">
            <Key className="size-3.5" />
            API Keys
          </TabsTrigger>
          <TabsTrigger value="webhooks" className="gap-1.5">
            <Webhook className="size-3.5" />
            Webhooks
          </TabsTrigger>
        </TabsList>

        <TabsContent value="api-keys" className="mt-4">
          <ApiKeySettings
            apiKeys={apiKeys.apiKeys}
            loading={apiKeys.loading}
            onCreate={apiKeys.createApiKey}
            onDelete={apiKeys.deleteApiKey}
          />
        </TabsContent>

        <TabsContent value="webhooks" className="mt-4">
          <WebhookSettings
            webhooks={webhooks.webhooks}
            loading={webhooks.loading}
            onCreate={webhooks.createWebhook}
            onDelete={webhooks.deleteWebhook}
            onTest={webhooks.testWebhook}
          />
        </TabsContent>
      </Tabs>
    </div>
  );
}
