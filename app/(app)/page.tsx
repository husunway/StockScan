import TickerSearch from "@/app/components/TickerSearch";

const features = [
  {
    title: "Price & Charts",
    body: "Historical prices, volume, and interactive charts.",
  },
  {
    title: "Fundamentals",
    body: "Valuation ratios, earnings, and balance sheet metrics.",
  },
  {
    title: "Technical Indicators",
    body: "Moving averages, RSI, MACD, and trend signals.",
  },
  {
    title: "Risk & Returns",
    body: "Volatility, drawdowns, and return statistics.",
  },
];

export default function Home() {
  return (
    <>
      <section className="hero">
        <h1>Analyze any stock</h1>
        <p className="subtitle">
          Enter a ticker symbol to load its price history and view analysis.
        </p>
        <TickerSearch />
      </section>

      <section className="grid">
        {features.map((f) => (
          <div key={f.title} className="card">
            <h2>{f.title}</h2>
            <p>{f.body}</p>
            <span className="badge">Coming soon</span>
          </div>
        ))}
      </section>
    </>
  );
}
