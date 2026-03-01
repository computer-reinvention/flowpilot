import { Globe } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { Flow } from "@/lib/types";

interface FlowCardProps {
  flow: Flow;
  onClick: () => void;
}

export function FlowCard({ flow, onClick }: FlowCardProps) {
  return (
    <Card
      className="cursor-pointer transition-colors hover:bg-accent"
      onClick={onClick}
    >
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2">
            {flow.icon ? (
              <span className="text-2xl">{flow.icon}</span>
            ) : (
              <Globe className="h-5 w-5 text-muted-foreground" />
            )}
            <CardTitle className="text-lg">{flow.name}</CardTitle>
          </div>
          <Badge variant="secondary">{flow.domain}</Badge>
        </div>
      </CardHeader>
      <CardContent>
        <CardDescription className="line-clamp-2">
          {flow.description}
        </CardDescription>
      </CardContent>
    </Card>
  );
}
