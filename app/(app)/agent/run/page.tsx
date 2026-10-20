import { Suspense } from "react";
import AgentRunner from "@/app/components/AgentRunner";

export const metadata = { title: "Run skill · StockScan" };

export default function AgentRunPage() {
  return (
    <>
      <h1 className="page-title">Run a skill</h1>
      <p className="subtitle left">
        Pick a skill and a symbol. The conclusion comes first - verdict, target and stop prices -
        followed by the justification and the supporting analysis.
      </p>
      <Suspense>
        <AgentRunner />
      </Suspense>
    </>
  );
}
