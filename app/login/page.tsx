import { Suspense } from "react";
import LoginForm from "@/app/components/LoginForm";

export const metadata = { title: "Sign in · StockScan" };

export default function LoginPage() {
  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="brand">
          <span className="logo">◆</span> StockScan
        </div>
        <h1>Sign in</h1>
        <Suspense>
          <LoginForm />
        </Suspense>
      </div>
    </div>
  );
}
