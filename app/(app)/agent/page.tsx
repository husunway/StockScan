import SkillLibrary from "@/app/components/SkillLibrary";

export const metadata = { title: "AI agent · StockScan" };

export default function AgentPage() {
  return (
    <>
      <h1 className="page-title">AI agent</h1>
      <p className="subtitle left">
        The agent runs a skill - a SKILL.md file stored in the repository under <code>skills/</code> -
        against the data gathered for a stock, using Claude. Each skill defines the investor
        framework, decision rules and evidence rules; the system enforces a fixed verdict structure.
      </p>
      <SkillLibrary />
    </>
  );
}
