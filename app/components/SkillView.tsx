"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

type Skill = {
  id: string;
  name: string;
  description: string;
  version: string;
  inputs: string;
  output: string;
  path: string;
  content: string;
};

export default function SkillView() {
  const params = useSearchParams();
  const id = params.get("id") ?? "";
  const [skill, setSkill] = useState<Skill | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) {
      setError("No skill id given.");
      return;
    }
    fetch(`/api/py/agent/skills/${encodeURIComponent(id)}`)
      .then(async (r) => {
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Could not load skill");
        setSkill(d);
      })
      .catch((e: Error) => setError(e.message));
  }, [id]);

  if (error) return <p className="msg error">{error}</p>;
  if (!skill) return <p className="msg">Loading…</p>;

  return (
    <div className="single">
      <div className="filters">
        <Link href={`/agent/run?skill=${skill.id}`} className="btn small">
          Run this skill
        </Link>
        <Link href="/agent" className="btn small ghost">
          All skills
        </Link>
      </div>
      <div className="table-wrap" style={{ margin: "12px 0" }}>
        <table className="table">
          <tbody>
            <tr><td>Name</td><td>{skill.name}</td></tr>
            <tr><td>Description</td><td className="wrap">{skill.description}</td></tr>
            <tr><td>Version</td><td>{skill.version || "—"}</td></tr>
            <tr><td>Inputs</td><td>{skill.inputs}</td></tr>
            <tr><td>Output schema</td><td>{skill.output}</td></tr>
            <tr><td>File</td><td><code>{skill.path}</code></td></tr>
          </tbody>
        </table>
      </div>
      <pre className="skill-md">{skill.content}</pre>
      <p className="msg hint">
        To change how the agent reasons, edit this file in the repository and commit it. The system
        enforces the output structure; the file controls everything else.
      </p>
    </div>
  );
}
