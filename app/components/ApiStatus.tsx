"use client";

import { useEffect, useState } from "react";

export default function ApiStatus() {
  const [ok, setOk] = useState<boolean | null>(null);

  useEffect(() => {
    fetch("/api/py/health")
      .then((r) => setOk(r.ok))
      .catch(() => setOk(false));
  }, []);

  const label = ok === null ? "Checking API…" : ok ? "API online" : "API offline";
  const cls = ok === null ? "" : ok ? "ok" : "down";

  return (
    <span className="status">
      <span className={`dot ${cls}`} /> {label}
    </span>
  );
}
