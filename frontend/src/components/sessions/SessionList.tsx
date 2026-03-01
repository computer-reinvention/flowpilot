import { Trash2, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { SessionBadge } from "./SessionBadge";
import type { Session } from "@/lib/types";

interface SessionListProps {
  sessions: Session[];
  loading: boolean;
  onDelete: (id: string) => void;
}

export function SessionList({ sessions, loading, onDelete }: SessionListProps) {
  if (loading) {
    return (
      <div className="flex justify-center py-8">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (sessions.length === 0) {
    return null;
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Saved Sessions</CardTitle>
        <CardDescription>
          Browser sessions captured by domain
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-2">
          {sessions.map((session) => (
            <div
              key={session.id}
              className="flex items-center justify-between py-1"
            >
              <SessionBadge session={session} />
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                onClick={() => onDelete(session.id)}
              >
                <Trash2 className="h-3 w-3" />
              </Button>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
