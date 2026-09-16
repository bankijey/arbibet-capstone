export function NoSnapshot() {
  return (
    <div className="mx-auto mt-16 max-w-lg rounded-xl border border-dashed border-border p-8 text-center">
      <h1 className="text-xl font-semibold">No data published yet</h1>
      <p className="mt-2 text-sm leading-relaxed text-muted">
        This site shows snapshots the pipeline publishes after each run. None has been uploaded yet; the page will fill
        in after the next run.
      </p>
    </div>
  );
}
