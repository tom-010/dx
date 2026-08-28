import { createFileRoute, redirect, useNavigate } from "@tanstack/react-router";
import { type ChangeEvent, type FormEvent, type JSX, useState } from "react";
import { useLogin } from "@/api/auth/auth";
import type { TokenOut } from "@/api/model";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { getAccessToken, setAccessToken } from "@/lib/auth";
import { errorMessage } from "@/lib/custom-fetch";

export const Route = createFileRoute("/login")({
  beforeLoad: () => {
    if (getAccessToken() !== null) throw redirect({ to: "/" });
  },
  component: LoginPage,
});

function LoginPage(): JSX.Element {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const login = useLogin({
    mutation: {
      onSuccess: (token: TokenOut): void => {
        setAccessToken(token.access_token);
        navigate({ to: "/" });
      },
    },
  });

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    login.mutate({ data: { username, password } });
  }

  return (
    <div className="flex flex-1 items-center justify-center">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>Sign in to dx</CardTitle>
          <CardDescription>
            Use your account credentials (dev default: admin / admin).
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="login-username">Username</Label>
              <Input
                id="login-username"
                autoComplete="username"
                autoFocus
                required
                value={username}
                onChange={(event: ChangeEvent<HTMLInputElement>): void =>
                  setUsername(event.target.value)
                }
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="login-password">Password</Label>
              <Input
                id="login-password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event: ChangeEvent<HTMLInputElement>): void =>
                  setPassword(event.target.value)
                }
              />
            </div>
            {login.isError && (
              <p className="text-destructive text-sm">
                {errorMessage(login.error)}
              </p>
            )}
            <Button type="submit" disabled={login.isPending}>
              {login.isPending ? "Signing in..." : "Sign in"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
