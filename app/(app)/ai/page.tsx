import Link from "next/link";

export const metadata = { title: "AI · StockScan" };

const analyses = [
  {
    href: "/ai/buffett",
    title: "Warren Buffett",
    body: "Moat, management, financial strength, intrinsic value and margin of safety. Produces a Buy / Hold / Sell conclusion with buy-below, target and stop prices.",
  },
];

export default function AiPage() {
  return (
    <>
      <h1 className="page-title">AI analysis</h1>
      <p className="subtitle left">
        Claude applies a named investor&apos;s framework to the data gathered for a stock. The
        output is model-generated analysis for your own review, not investment advice.
      </p>
      <section className="grid">
        {analyses.map((a) => (
          <Link key={a.href} href={a.href} className="card card-link">
            <h2>{a.title}</h2>
            <p>{a.body}</p>
            <span className="badge">Open</span>
          </Link>
        ))}
      </section>
    </>
  );
}
