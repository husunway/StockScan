import { Suspense } from "react";
import DecisionView from "@/app/components/DecisionView";

export const metadata = { title: "Decision · StockScan" };

export default function DecisionPage() {
  return (
    <>
      <h1 className="page-title">Decision detail</h1>
      <Suspense>
        <DecisionView />
      </Suspense>
    </>
  );
}
