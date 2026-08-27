import { Settings } from "lucide-react";
import { api, type Me } from "@/api/client";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

type TopNavProps = {
  user: Me;
  onAdmin?: () => void;
};

export function TopNav({ user, onAdmin }: TopNavProps) {
  const displayName = user.name ?? user.login ?? "User";
  const initials = displayName.slice(0, 2).toUpperCase();

  const handleLogout = async () => {
    try {
      await api.logout();
    } finally {
      window.location.reload();
    }
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className="h-7 w-7 rounded-full bg-muted flex items-center justify-center text-xs font-medium text-muted-foreground hover:bg-muted/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label="User menu"
        >
          {user.avatar_url ? (
            <img
              src={user.avatar_url}
              alt={displayName}
              className="h-7 w-7 rounded-full object-cover"
            />
          ) : (
            initials
          )}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-48">
        <DropdownMenuLabel className="text-xs text-muted-foreground font-normal">
          {displayName}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {onAdmin && (
          <DropdownMenuItem onClick={onAdmin} className="cursor-pointer">
            <Settings className="mr-2 h-4 w-4" /> Admin
          </DropdownMenuItem>
        )}
        <DropdownMenuItem onClick={handleLogout} className="cursor-pointer">
          Logout
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
