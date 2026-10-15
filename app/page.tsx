import TickerSearch from "./components/TickerSearch";
import ApiStatus from "./components/ApiStatus";

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
    <div className="page">
      <header className="header">
        <div className="brand">
          <span className="logo">◆</span> StockScan
        </div>
        <nav className="nav">
          <a href="#">Dashboard</a>
          <a href="#">Watchlist</a>
          <a href="/docs">API</a>
        </nav>
      </header>

      <main className="main">
        <section className="hero">
          <h1>Analyze any stock</h1>
          <p className="subtitle">
            Enter a ticker symbol to view prices, fundamentals, and technical
            signals.
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
      </main>

      <footer className="footer">
        <ApiStatus />
        <span>For informational purposes only. Not investment advice.</span>
      </footer>
    </div>
  );
}
