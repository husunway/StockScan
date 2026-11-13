import StorageMigration from "@/app/components/StorageMigration";

export const metadata = { title: "Storage migration · StockScan" };

export default function StorageMigrationPage() {
  return (
    <>
      <h1 className="page-title">Storage migration</h1>
      <p className="subtitle left">
        Copies every file from Vercel Blob to Azure Blob Storage under the same path, so the folder structure
        (reports, data bundles, run archives) is kept. Files already in Azure with the same size are skipped, so
        you can run it again after a stop or failure. Nothing is deleted from Vercel Blob.
      </p>
      <StorageMigration />
    </>
  );
}
