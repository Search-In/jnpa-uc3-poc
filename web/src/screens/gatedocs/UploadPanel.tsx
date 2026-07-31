/**
 * Gate-document Data-Upload panel (EIR / PIN ticket / Form-13).
 *
 * Same three-step workflow as the CFS-ECY and shipping-lines panels — download
 * template → validate (dry-run preview) → confirm import → history — so the
 * operator learns one flow for every module. Backed by /api/gate-docs/*.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Download, FileUp, Loader2 } from "lucide-react";

import { api } from "../../lib/api";

const DOC_TYPES = [
  { value: "EIR", label: "EIR (Equipment Interchange Report)" },
  { value: "PIN", label: "PIN ticket" },
  { value: "FORM13", label: "Form 13" },
] as const;

export default function GateDocUploadPanel() {
  const qc = useQueryClient();
  const [docType, setDocType] = useState<string>("EIR");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<any>(null);

  const historyQ = useQuery({
    queryKey: ["gate-doc-uploads", docType],
    queryFn: () => api.gateDocUploads({ doc_type: docType, limit: 20 }),
  });

  const validate = useMutation({
    mutationFn: () => api.gateDocUploadValidate(docType, file as File),
    onSuccess: setPreview,
  });

  const upload = useMutation({
    mutationFn: () => api.gateDocUpload(docType, file as File),
    onSuccess: () => {
      setPreview(null);
      setFile(null);
      qc.invalidateQueries({ queryKey: ["gate-doc-uploads"] });
      qc.invalidateQueries({ queryKey: ["uc3-docs"] });
    },
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-slate-800 bg-slate-900/60 p-3">
        <label className="text-sm">
          <span className="mb-1 block text-xs text-slate-500">Document type</span>
          <select
            value={docType}
            onChange={(e) => {
              setDocType(e.target.value);
              setPreview(null);
            }}
            className="rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm text-slate-200"
          >
            {DOC_TYPES.map((d) => (
              <option key={d.value} value={d.value}>
                {d.label}
              </option>
            ))}
          </select>
        </label>

        <button
          onClick={() => api.gateDocDownloadTemplate(docType)}
          className="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
        >
          <Download className="mr-1 inline h-4 w-4" />
          Template
        </button>

        <label className="text-sm">
          <span className="mb-1 block text-xs text-slate-500">File (CSV / XLS / XLSX)</span>
          <input
            type="file"
            accept=".csv,.xls,.xlsx"
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              setPreview(null);
            }}
            className="text-sm text-slate-400 file:mr-2 file:rounded file:border-0 file:bg-slate-800 file:px-2 file:py-1 file:text-slate-300"
          />
        </label>

        <button
          disabled={!file || validate.isPending}
          onClick={() => validate.mutate()}
          className="rounded-md bg-slate-800 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-700 disabled:opacity-40"
        >
          {validate.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : "Validate"}
        </button>

        <button
          disabled={!preview?.valid || upload.isPending}
          onClick={() => upload.mutate()}
          className="rounded-md bg-sky-500/20 px-3 py-1.5 text-sm text-sky-200 ring-1 ring-sky-500/40 hover:bg-sky-500/30 disabled:opacity-40"
        >
          {upload.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : "Import"}
        </button>
      </div>

      {(validate.isError || upload.isError) && (
        <p className="rounded border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          {String(((validate.error || upload.error) as Error).message)}
        </p>
      )}

      {upload.isSuccess && (
        <p className="rounded border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">
          <CheckCircle2 className="mr-1 inline h-4 w-4" />
          {upload.data.status}: imported {upload.data.imported}, skipped {upload.data.skipped},
          invalid {upload.data.invalid}
        </p>
      )}

      {preview && (
        <section className="rounded-lg border border-slate-800 bg-slate-900/60 p-3">
          <h3 className="mb-2 text-sm text-slate-300">
            <FileUp className="mr-1 inline h-4 w-4" />
            Validation: {preview.status} — {preview.summary?.valid}/{preview.summary?.rows} rows
            importable
          </h3>
          {preview.preview?.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-slate-500">
                  <tr>
                    {Object.keys(preview.preview[0]).map((k) => (
                      <th key={k} className="px-2 py-1">
                        {k}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800 text-slate-300">
                  {preview.preview.map((row: any, i: number) => (
                    <tr key={i}>
                      {Object.values(row).map((v: any, j: number) => (
                        <td key={j} className="px-2 py-1 font-mono">
                          {String(v)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {preview.errors?.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-xs text-rose-300">
              {preview.errors.slice(0, 10).map((e: any, i: number) => (
                <li key={i}>
                  <AlertTriangle className="mr-1 inline h-3 w-3" />
                  {e.row_number ? `row ${e.row_number}: ` : ""}
                  {e.error_detail}
                </li>
              ))}
            </ul>
          )}
          {preview.warnings?.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-xs text-amber-300">
              {preview.warnings.slice(0, 10).map((w: any, i: number) => (
                <li key={i}>
                  {w.row_number ? `row ${w.row_number}: ` : ""}
                  {w.error_detail}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <section className="rounded-lg border border-slate-800 bg-slate-900/60">
        <h3 className="border-b border-slate-800 px-3 py-2 text-sm text-slate-300">
          Import history
        </h3>
        <table className="w-full text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-1.5">File</th>
              <th className="px-3 py-1.5">Type</th>
              <th className="px-3 py-1.5">Rows</th>
              <th className="px-3 py-1.5">Imported</th>
              <th className="px-3 py-1.5">Status</th>
              <th className="px-3 py-1.5">When</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800 text-slate-300">
            {historyQ.data?.items?.map((f: any) => (
              <tr key={f.id}>
                <td className="px-3 py-1.5 font-mono">{f.source_file}</td>
                <td className="px-3 py-1.5">{f.doc_type}</td>
                <td className="px-3 py-1.5">{f.record_count}</td>
                <td className="px-3 py-1.5">{f.imported_count}</td>
                <td className="px-3 py-1.5">{f.import_status}</td>
                <td className="px-3 py-1.5 text-slate-500">
                  {f.created_at ? new Date(f.created_at).toLocaleString() : "—"}
                </td>
              </tr>
            ))}
            {historyQ.data?.items?.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-slate-500">
                  No uploads yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
