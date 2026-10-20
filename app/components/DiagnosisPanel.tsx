"use client";

import { useCallback, useEffect, useState } from "react";

type Variable = { name: string; set: boolean; value: string | null };

type Diagnosis = {
  variables: Variable[];
  claude_provider?: string;
  claude_model?: string;
  env_local_present: boolean;
  working_directory: string;
  python_version: string;
  platform: string;
  vercel: boolean;
};

export default function DiagnosisPanel() {
  const [data, setData] = useState<Diagnosis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/py/diagnosis", { cache: "no-store" });
      if (!res.ok) throw new Error(`Backend returned ${res.status}`);
      setData(await res.json());
    } catch (err) {
      setError(
        err instanceof Error
          ? `${err.message}. Is the Python server running? (npm run dev)`
          : "Request failed"
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const missing = data?.variables.filter((v) => !v.set).length ?? 0;

  return (
    <div className="loader">
      <button type="button" className="btn" onClick={refresh} disabled={loading}>
        {loading ? "Checking…" : "Refresh"}
      </button>

      {error && <p className="msg error">{error}</p>}

      {data && (
        <div className="result">
          <h2 className="section-title">Environment variables</h2>
          {missing > 0 ? (
            <p className="msg error">
              {missing} of {data.variables.length} variables are missing.
              {!data.env_local_present && !data.vercel && (
                <> No <code>.env.local</code> file was found in the working directory below.</>
              )}
            </p>
          ) : (
            <p className="msg ok">All variables are set.</p>
          )}
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Variable</th>
                  <th>Status</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                {data.variables.map((v) => (
                  <tr key={v.name}>
                    <td><code>{v.name}</code></td>
                    <td>
                      <span className={`dot ${v.set ? "ok" : "down"}`} />{" "}
                      {v.set ? "set" : "missing"}
                    </td>
                    <td>{v.value ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <h2 className="section-title">Backend</h2>
          <div className="table-wrap">
            <table className="table">
              <tbody>
                <tr>
                  <td>Claude provider</td>
                  <td>{data.claude_provider ?? "—"}{data.claude_model ? ` · model ${data.claude_model}` : ""}</td>
                </tr>
                <tr>
                  <td>.env.local present</td>
                  <td>{data.env_local_present ? "yes" : "no"}</td>
                </tr>
                <tr>
                  <td>Working directory</td>
                  <td><code>{data.working_directory}</code></td>
                </tr>
                <tr>
                  <td>Running on Vercel</td>
                  <td>{data.vercel ? "yes" : "no"}</td>
                </tr>
                <tr>
                  <td>Python</td>
                  <td>{data.python_version}</td>
                </tr>
                <tr>
                  <td>Platform</td>
                  <td>{data.platform}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
