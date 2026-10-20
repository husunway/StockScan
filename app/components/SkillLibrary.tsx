"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

type Skill = {
  id: string;
  name: string;
  description: string;
  version: string;
  inputs: string;
  path: string;
  data_structure_path: string | null;
};

export default function SkillLibrary() {
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [directory, setDirectory] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/py/agent/skills")
      .then(async (r) => {
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Could not list skills");
        setSkills(d.skills);
        setDirectory(d.directory ?? "");
      })
      .catch((e: Error) => {
        setSkills([]);
        setError(e.message);
      });
  }, []);

  if (error) return <p className="msg error">{error}</p>;
  if (!skills) return <p className="msg">Loading skills…</p>;
  if (skills.length === 0)
    return (
      <p className="msg">
        No skills found. Add a folder under <code>skills/</code> containing a <code>SKILL.md</code>.
      </p>
    );

  return (
    <>
      <section className="grid">
        {skills.map((s) => (
          <div key={s.id} className="card skill-card">
            <h2>{s.name}</h2>
            <p>{s.description}</p>
            <p className="tile-note">
              <code>{s.path}</code>
              {s.data_structure_path && (
                <>
                  {" · "}
                  <code>{s.data_structure_path}</code>
                </>
              )}
              {" · "}v{s.version || "—"} · input: {s.inputs}
            </p>
            <div className="card-actions">
              <Link href={`/agent/run?skill=${s.id}`} className="btn small">
                Run
              </Link>
              <Link href={`/agent/skill?id=${s.id}`} className="btn small ghost">
                View file
              </Link>
              {s.data_structure_path && (
                <Link href={`/agent/skill?id=${s.id}&tab=data`} className="btn small ghost">
                  Data structure
                </Link>
              )}
            </div>
          </div>
        ))}
      </section>
      {directory && (
        <p className="msg hint">
          Skills are read from <code>{directory}</code> each time the list is requested, so editing a
          SKILL.md takes effect on the next run without restarting.
        </p>
      )}
    </>
  );
}
