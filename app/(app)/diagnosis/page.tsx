import DiagnosisPanel from "@/app/components/DiagnosisPanel";

export const metadata = { title: "Diagnosis · StockScan" };

export default function DiagnosisPage() {
  return (
    <>
      <h1 className="page-title">Diagnosis</h1>
      <p className="subtitle left">
        Shows the configuration the Python backend can see. Secret values are
        never displayed, only whether they are set.
      </p>
      <DiagnosisPanel />
    </>
  );
}
