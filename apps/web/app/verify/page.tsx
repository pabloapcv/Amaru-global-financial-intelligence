"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";

function Verify() {
  const token = useSearchParams().get("token") ?? "";
  const [state, setState] = useState<"pending" | "ok" | "error">("pending");
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!token) { setState("error"); setMessage("This link is missing its token."); return; }
    api("/api/v1/auth/verify-email", { method: "POST", body: JSON.stringify({ token }) })
      .then(() => setState("ok"))
      .catch((e) => { setState("error"); setMessage(e instanceof Error ? e.message : String(e)); });
  }, [token]);

  return (
    <div className="login-page" style={{ maxWidth: 400, margin: "8vh auto" }}>
      <div className="brand" style={{ padding: "0 0 12px" }}><span className="brand-mark">◆</span> Amaru</div>
      <div className="card">
        <h3 style={{ marginBottom: 12 }}>Email verification</h3>
        {state === "pending" && <p className="muted">Verifying…</p>}
        {state === "ok" && <div className="note note-ok">Your email is verified. <a href="/">Continue to Amaru</a></div>}
        {state === "error" && <div className="note note-error">{message || "This link is invalid or has expired."} You can request a new link from the Account page.</div>}
      </div>
    </div>
  );
}

export default function VerifyPage() {
  return <Suspense><Verify /></Suspense>;
}
