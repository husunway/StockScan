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
  data_structure_path: string | null;
  data_structure: unknown;
};

export default function SkillView() {
  const params = useSearchParams();
  const id = params.get("id") ?? "";
  const [skill, setSkill] = useState<Skill | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"skill" | "data">(params.get("tab") === "data" ? "data" : "skill");

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
            <tr>
              <td>Data structure</td>
              <td>{skill.data_structure_path ? <code>{skill.data_structure_path}</code> : "— (built-in output schema)"}</td>
            </tr>
          </tbody>
        </table>
      </div>
      {skill.data_structure_path && (
        <div className="seg" role="tablist" style={{ marginBottom: 10 }}>
          <button type="button" className={tab === "skill" ? "on" : ""} onClick={() => setTab("skill")}>
            SKILL.md
          </button>
          <button type="button" className={tab === "data" ? "on" : ""} onClick={() => setTab("data")}>
            data-structure.json
          </button>
        </div>
      )}
      <pre className="skill-md">
        {tab === "data" ? JSON.stringify(skill.data_structure, null, 2) : skill.content}
      </pre>
      <p className="msg hint">
        SKILL.md controls how the agent reasons; data-structure.json documents every input field it
        receives and defines the output it must return. Edit either in the repository and commit.
      </p>
    </div>
  );
}
