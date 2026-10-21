import RunHistory from "@/app/components/RunHistory";

export const metadata = { title: "Run history · StockScan" };

export default function RunHistoryPage() {
  return (
    <>
      <h1 className="page-title">Run history</h1>
      <p className="subtitle left">
        Every agent run: which skill, when, which Mondays, how many succeeded or failed, and why.
        Click a row for the per-Monday detail; in-progress runs refresh every 5 seconds.
      </p>
      <RunHistory />
    </>
  );
}
