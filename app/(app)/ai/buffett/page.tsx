import { Suspense } from "react";
import BuffettPanel from "@/app/components/BuffettPanel";

export const metadata = { title: "Warren Buffett analysis · StockScan" };

export default function BuffettPage() {
  return (
    <>
      <h1 className="page-title">Warren Buffett analysis</h1>
      <p className="subtitle left">
        Claude evaluates the company the way Buffett describes it: circle of competence,
        durable moat, management, financial strength, intrinsic value and margin of safety.
        The conclusion comes first, with the supporting analysis below it.
      </p>
      <Suspense>
        <BuffettPanel />
      </Suspense>
    </>
  );
}
