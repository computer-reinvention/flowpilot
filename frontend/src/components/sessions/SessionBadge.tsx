import { Globe } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { Session } from "@/lib/types";

interface SessionBadgeProps {
  session: Session;
}

export function SessionBadge({ session }: SessionBadgeProps) {
  const timeAgo = formatTimeAgo(session.captured_at);

  return (
    <div className="flex items-center gap-2">
      <Globe className="size-3 text-muted-foreground" />
      <Badge variant="outline">{session.domain}</Badge>
      <span className="text-xs text-muted-foreground">{timeAgo}</span>
    </div>
  );
}

function formatTimeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}
