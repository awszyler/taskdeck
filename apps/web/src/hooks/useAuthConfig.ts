import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";

export type AuthConfig = {
  auth_mode: "disabled" | "cognito";
  allow_signup: boolean;
  cognito_pool_name: string | null;
};

export function useAuthConfig() {
  return useQuery<AuthConfig>({
    queryKey: ["auth", "config"],
    queryFn: api.authConfig,
    staleTime: 5 * 60 * 1000, // 5 minutes — config doesn't change often
    retry: false,
  });
}
