import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { Card, Chip, Row, Spinner } from "@/components/ui";
import { useWebcam } from "@/hooks/useWebcam";
import { ENROL_DRIVER_KEY, useDriverSession } from "@/hooks/DriverSession";

// Driver face-enrolment wizard (Identity / C2). After pairing, a driver completes
// their profile, uploads identity documents, gives explicit biometric consent,
// captures 2–3 reference frames with the shared webcam hook, and submits. The
// request lands as PENDING; an admin approves it in the web portal before the
// driver can be verified at the gate. Reuses the dashboard's capture + alignment
// logic (useWebcam) — no second camera component.

const MIN_FACES = 2;
const MAX_FACES = 3;

type Step = "profile" | "documents" | "consent" | "capture" | "review" | "submitted";

const VALIDATION_TEXT: Record<string, string> = {
  no_face_detected: "No face detected — look straight at the camera.",
  multiple_faces: "Multiple faces in frame — only you should be visible.",
  face_not_centered: "Center your face within the guide.",
  move_closer: "Move a little closer to the camera.",
  move_back: "Move back slightly from the camera.",
  camera_not_ready: "Camera is not ready yet.",
};

interface Profile {
  driver_id: string;
  name: string;
  license_no: string;
  mobile: string;
  vehicle_no: string;
  aadhaar: string;
  emergency_contact: string;
}

const EMPTY_PROFILE: Profile = {
  driver_id: "",
  name: "",
  license_no: "",
  mobile: "",
  vehicle_no: "",
  aadhaar: "",
  emergency_contact: "",
};

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

export default function Enrol({ plate }: { deviceId: string; plate?: string | null }) {
  const { t } = useTranslation();
  const cam = useWebcam();
  const { session, applyEnrolment } = useDriverSession();

  // Auto-fill identity from the global session — the spec's "no manual re-entry
  // of login data". A field that the session already knows is locked (read-only);
  // a first-time enrolment (session has no driver yet) leaves them editable.
  const lockDriverId = !!session.driverId;
  const lockName = !!session.name;
  const lockVehicle = !!session.vehicle;

  const [step, setStep] = useState<Step>("profile");
  const [profile, setProfile] = useState<Profile>({
    ...EMPTY_PROFILE,
    driver_id: session.driverId ?? "",
    name: session.name ?? "",
    vehicle_no: session.vehicle ?? plate ?? "",
  });
  const [documents, setDocuments] = useState<{ kind: string; image: string }[]>([]);
  const [consent, setConsent] = useState(false);
  const [images, setImages] = useState<string[]>([]);
  const [notice, setNotice] = useState<{ kind: "warn" | "ok"; text: string } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [rejection, setRejection] = useState<string | null>(null);

  const set = (k: keyof Profile) => (e: { target: { value: string } }) =>
    setProfile((p) => ({ ...p, [k]: e.target.value }));

  // --- face capture ---
  const onCapture = useCallback(async () => {
    if (images.length >= MAX_FACES) return;
    const v = await cam.validate();
    if (!v.ok) {
      setNotice({ kind: "warn", text: VALIDATION_TEXT[v.reason] ?? "Face check failed — try again." });
      return;
    }
    const frame = cam.capture();
    if (!frame) {
      setNotice({ kind: "warn", text: "Could not capture a frame — try again." });
      return;
    }
    setNotice(null);
    setImages((xs) => [...xs, frame]);
  }, [cam, images.length]);

  const removeImage = (i: number) => setImages((xs) => xs.filter((_, j) => j !== i));

  // --- documents ---
  const onDoc = (kind: string) => async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const image = await readFileAsDataUrl(file);
      setDocuments((d) => [...d.filter((x) => x.kind !== kind), { kind, image }]);
    } catch {
      setNotice({ kind: "warn", text: "Could not read that file." });
    }
    e.target.value = "";
  };

  // --- submit ---
  const onSubmit = async () => {
    setSubmitting(true);
    setNotice(null);
    try {
      const res = await api.enrolRequest({
        driver_id: profile.driver_id.trim(),
        name: profile.name.trim(),
        license_no: profile.license_no.trim(),
        mobile: profile.mobile.trim(),
        vehicle_no: profile.vehicle_no.trim(),
        aadhaar: profile.aadhaar.trim(),
        emergency_contact: profile.emergency_contact.trim(),
        consent,
        images,
        documents,
      });
      try {
        localStorage.setItem(ENROL_DRIVER_KEY, profile.driver_id.trim());
      } catch {
        /* storage unavailable */
      }
      // Push the freshly-known identity + status into the global session so Home
      // and the rest of the app reflect it without an extra fetch.
      applyEnrolment({
        driverId: profile.driver_id.trim(),
        name: profile.name.trim(),
        vehicle: profile.vehicle_no.trim() || session.vehicle,
        status: (res.status || "PENDING").toUpperCase() as any,
      });
      cam.stop();
      setStatus(res.status || "PENDING");
      setStep("submitted");
    } catch (err) {
      setNotice({
        kind: "warn",
        text: `Submission failed: ${(err as Error)?.message ?? "unknown error"}`,
      });
    } finally {
      setSubmitting(false);
    }
  };

  // Resume a prior submission: if this device already submitted, show its status.
  useEffect(() => {
    let saved: string | null = null;
    try {
      saved = localStorage.getItem(ENROL_DRIVER_KEY);
    } catch {
      /* ignore */
    }
    if (!saved) return;
    let alive = true;
    (async () => {
      try {
        const s = await api.enrolStatus(saved!);
        if (!alive) return;
        setProfile((p) => ({ ...p, driver_id: saved! }));
        setStatus(s.status);
        setRejection(s.rejection_reason ?? null);
        setStep("submitted");
      } catch {
        /* no prior request — start fresh */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // Poll status while waiting on the submitted screen.
  const pollRef = useRef<number | null>(null);
  useEffect(() => {
    if (step !== "submitted" || !profile.driver_id) return;
    const tick = async () => {
      try {
        const s = await api.enrolStatus(profile.driver_id.trim());
        setStatus(s.status);
        setRejection(s.rejection_reason ?? null);
        applyEnrolment({ status: (s.status || "PENDING").toUpperCase() as any });
      } catch {
        /* keep last known */
      }
    };
    pollRef.current = window.setInterval(tick, 5000);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [step, profile.driver_id, applyEnrolment]);

  const profileValid =
    profile.driver_id.trim() && profile.name.trim() && profile.license_no.trim();

  // ---------------------------------------------------------------- submitted view
  if (step === "submitted") {
    const s = (status || "PENDING").toUpperCase();
    const chip = s === "ACTIVE" ? "ok" : s === "REJECTED" ? "down" : "warn";
    const label =
      s === "ACTIVE"
        ? t("enrol.statusActive")
        : s === "REJECTED"
          ? t("enrol.statusRejected")
          : s === "REENROLL"
            ? t("enrol.statusReenroll")
            : t("enrol.statusPending");
    return (
      <>
        <Card title={t("enrol.title")}>
          <div style={{ marginBottom: 10 }}>
            <Chip status={chip as any}>{s}</Chip>
          </div>
          <p className="muted" style={{ fontSize: 13 }}>
            {label}
          </p>
          {s === "REJECTED" && rejection ? (
            <div className="banner warn" style={{ marginTop: 10 }}>
              {t("enrol.reason")}: {rejection}
            </div>
          ) : null}
          <Row k={t("enrol.driverId")} v={profile.driver_id} />
        </Card>
        {(s === "REJECTED" || s === "REENROLL") && (
          <Card>
            <button
              className="btn primary"
              onClick={() => {
                setImages([]);
                setConsent(false);
                setStep("profile");
                setStatus(null);
              }}
            >
              {t("enrol.resubmit")}
            </button>
          </Card>
        )}
      </>
    );
  }

  // ---------------------------------------------------------------- wizard
  const STEPS: Step[] = ["profile", "documents", "consent", "capture", "review"];
  const idx = STEPS.indexOf(step);

  return (
    <>
      <Card title={t("enrol.title")} className="tight">
        <div className="enrol-steps">
          {STEPS.map((s, i) => (
            <span key={s} className={`enrol-step ${i === idx ? "active" : i < idx ? "done" : ""}`}>
              {i + 1}
            </span>
          ))}
        </div>
        <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
          {t("enrol.dpdpNote")}
        </p>
      </Card>

      {notice && <div className={`banner ${notice.kind === "ok" ? "" : "warn"}`}>{notice.text}</div>}

      {step === "profile" && (
        <Card title={t("enrol.completeProfile")}>
          {(lockDriverId || lockName || lockVehicle) && (
            <p className="muted enrol-autofill" style={{ fontSize: 12, marginBottom: 10 }}>
              {t("enrol.autofillNote")}
            </p>
          )}
          <div className="enrol-form">
            <Field label={t("enrol.driverId")} required locked={lockDriverId}>
              <input
                value={profile.driver_id}
                onChange={set("driver_id")}
                placeholder="DRV-1001"
                readOnly={lockDriverId}
              />
            </Field>
            <Field label={t("enrol.name")} required locked={lockName}>
              <input value={profile.name} onChange={set("name")} readOnly={lockName} />
            </Field>
            <Field label={t("enrol.license")} required>
              <input value={profile.license_no} onChange={set("license_no")} placeholder="MH04 ..." />
            </Field>
            <Field label={t("enrol.mobile")}>
              <input value={profile.mobile} onChange={set("mobile")} inputMode="tel" />
            </Field>
            <Field label={t("enrol.vehicle")} locked={lockVehicle}>
              <input value={profile.vehicle_no} onChange={set("vehicle_no")} readOnly={lockVehicle} />
            </Field>
            <Field label={t("enrol.aadhaar")}>
              <input value={profile.aadhaar} onChange={set("aadhaar")} inputMode="numeric" />
            </Field>
            <Field label={t("enrol.emergency")}>
              <input value={profile.emergency_contact} onChange={set("emergency_contact")} />
            </Field>
          </div>
          <button
            className="btn primary"
            disabled={!profileValid}
            onClick={() => setStep("documents")}
            style={{ marginTop: 12 }}
          >
            {t("enrol.next")}
          </button>
        </Card>
      )}

      {step === "documents" && (
        <Card title={t("enrol.documents")}>
          <p className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
            {t("enrol.documentsNote")}
          </p>
          <DocUpload
            label={t("enrol.docLicense")}
            doc={documents.find((d) => d.kind === "license")}
            onChange={onDoc("license")}
          />
          <DocUpload
            label={t("enrol.docId")}
            doc={documents.find((d) => d.kind === "id")}
            onChange={onDoc("id")}
          />
          <div className="btn-row" style={{ marginTop: 12 }}>
            <button className="btn ghost" onClick={() => setStep("profile")}>
              {t("enrol.back")}
            </button>
            <button className="btn primary" onClick={() => setStep("consent")}>
              {t("enrol.next")}
            </button>
          </div>
        </Card>
      )}

      {step === "consent" && (
        <Card title={t("enrol.consentTitle")}>
          <div className="banner">{t("enrol.consentBody")}</div>
          <label className="enrol-consent">
            <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
            <span>{t("enrol.consentCheckbox")}</span>
          </label>
          <div className="btn-row" style={{ marginTop: 12 }}>
            <button className="btn ghost" onClick={() => setStep("documents")}>
              {t("enrol.back")}
            </button>
            <button className="btn primary" disabled={!consent} onClick={() => setStep("capture")}>
              {t("enrol.next")}
            </button>
          </div>
        </Card>
      )}

      {step === "capture" && (
        <Card title={t("enrol.faceCapture")}>
          <p className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
            {t("enrol.faceNote", { min: MIN_FACES, max: MAX_FACES })}
          </p>
          <div className="enrol-cam">
            <video ref={cam.videoRef} muted playsInline />
            {cam.status === "live" && <div className="enrol-faceguide" aria-hidden />}
            {cam.status !== "live" && (
              <div className="enrol-cam-overlay">
                {cam.status === "requesting"
                  ? t("enrol.cameraStarting")
                  : cam.status === "denied"
                    ? t("enrol.cameraDenied")
                    : cam.status === "error"
                      ? cam.error ?? t("enrol.cameraError")
                      : t("enrol.cameraOff")}
              </div>
            )}
            <span className="enrol-cam-chip">{cam.status === "live" ? "● Live" : "○ Off"}</span>
          </div>

          {images.length > 0 && (
            <div className="enrol-thumbs">
              {images.map((src, i) => (
                <div key={i} className="enrol-thumb">
                  <img src={src} alt={`capture ${i + 1}`} />
                  <button onClick={() => removeImage(i)} aria-label="remove">
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}

          <div className="btn-row" style={{ marginTop: 12 }}>
            {cam.status !== "live" ? (
              <button className="btn primary" onClick={() => void cam.start()}>
                {t("enrol.startCamera")}
              </button>
            ) : (
              <button
                className="btn primary"
                onClick={() => void onCapture()}
                disabled={images.length >= MAX_FACES}
              >
                {t("enrol.capture")} ({images.length}/{MAX_FACES})
              </button>
            )}
            <button className="btn ghost" onClick={() => cam.stop()} disabled={cam.status !== "live"}>
              {t("enrol.stopCamera")}
            </button>
          </div>
          <div className="btn-row" style={{ marginTop: 10 }}>
            <button
              className="btn ghost"
              onClick={() => {
                cam.stop();
                setStep("consent");
              }}
            >
              {t("enrol.back")}
            </button>
            <button
              className="btn primary"
              disabled={images.length < MIN_FACES}
              onClick={() => {
                cam.stop();
                setStep("review");
              }}
            >
              {t("enrol.next")}
            </button>
          </div>
        </Card>
      )}

      {step === "review" && (
        <Card title={t("enrol.review")}>
          <Row k={t("enrol.driverId")} v={profile.driver_id} />
          <Row k={t("enrol.name")} v={profile.name} />
          <Row k={t("enrol.license")} v={profile.license_no} />
          <Row k={t("enrol.mobile")} v={profile.mobile || "—"} />
          <Row k={t("enrol.vehicle")} v={profile.vehicle_no || "—"} />
          <Row k={t("enrol.emergency")} v={profile.emergency_contact || "—"} />
          <Row k={t("enrol.faces")} v={String(images.length)} />
          <Row k={t("enrol.documents")} v={String(documents.length)} />
          <Row k={t("enrol.consentTitle")} v={consent ? "✓" : "✗"} />
          {images.length > 0 && (
            <div className="enrol-thumbs" style={{ marginTop: 10 }}>
              {images.map((src, i) => (
                <div key={i} className="enrol-thumb">
                  <img src={src} alt={`capture ${i + 1}`} />
                </div>
              ))}
            </div>
          )}
          <div className="btn-row" style={{ marginTop: 12 }}>
            <button className="btn ghost" onClick={() => setStep("capture")} disabled={submitting}>
              {t("enrol.back")}
            </button>
            <button
              className="btn success"
              onClick={() => void onSubmit()}
              disabled={submitting || !consent || images.length < MIN_FACES}
            >
              {submitting ? <Spinner /> : null} {t("enrol.submit")}
            </button>
          </div>
        </Card>
      )}
    </>
  );
}

function Field({
  label,
  required,
  locked,
  children,
}: {
  label: string;
  required?: boolean;
  locked?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label className={`enrol-field ${locked ? "locked" : ""}`}>
      <span>
        {label}
        {required ? <em> *</em> : null}
        {locked ? <span className="enrol-lock" aria-hidden> 🔒</span> : null}
      </span>
      {children}
    </label>
  );
}

function DocUpload({
  label,
  doc,
  onChange,
}: {
  label: string;
  doc?: { kind: string; image: string };
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
}) {
  return (
    <div className="enrol-doc">
      <span>{label}</span>
      {doc ? <img src={doc.image} alt={label} /> : <span className="muted">—</span>}
      <label className="btn ghost enrol-doc-btn">
        {doc ? "Replace" : "Upload"}
        <input type="file" accept="image/*" capture="environment" onChange={onChange} hidden />
      </label>
    </div>
  );
}
