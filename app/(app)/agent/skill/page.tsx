import { Suspense } from "react";
import SkillView from "@/app/components/SkillView";

export const metadata = { title: "Skill file · StockScan" };

export default function SkillPage() {
  return (
    <>
      <h1 className="page-title">Skill file</h1>
      <Suspense>
        <SkillView />
      </Suspense>
    </>
  );
}
