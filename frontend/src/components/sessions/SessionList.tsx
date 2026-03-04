import { Trash2, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
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
        <Loader2 className="size-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (sessions.length === 0) {
    return (
      <div className="text-center py-12 text-muted-foreground text-sm text-pretty">
        No saved sessions. Sessions are captured when the agent pauses for login.
      </div>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base text-balance">Saved Sessions</CardTitle>
        <CardDescription className="text-pretty">
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
              <div className="flex items-center gap-2">
                <SessionBadge session={session} />
                {session.profile_name && session.profile_name !== "default" && (
                  <Badge variant="secondary" className="text-xs">
                    {session.profile_name}
                  </Badge>
                )}
              </div>
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-7"
                    aria-label={`Delete session for ${session.domain}`}
                  >
                    <Trash2 className="size-3" />
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle className="text-balance">Delete session?</AlertDialogTitle>
                    <AlertDialogDescription className="text-pretty">
                      This will remove the saved cookies for {session.domain}. The agent won't be able to reuse this login session.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancel</AlertDialogCancel>
                    <AlertDialogAction onClick={() => onDelete(session.id)}>
                      Delete
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
