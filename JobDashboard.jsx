import React, { useState, useEffect, useMemo, useCallback, useRef } from "react";

/**
 * JobDashboard.jsx
 * -----------------
 * Dashboard de vacantes Data Analyst / Analytics Engineer.
 *
 * Espera encontrar un archivo `jobs_data.json` servible en la misma app
 * (por ejemplo en /public/jobs_data.json en un proyecto Vite/Next), generado
 * por scraper_aggregator.py vía el cron de GitHub Actions. Ver DEPLOYMENT_GUIDE.md.
 *
 * La generación de CV/Carta llama a POST /api/generate (función serverless,
 * ver api/generate.js) para no exponer tu API key de Anthropic en el
 * navegador. Este componente NO llama a api.anthropic.com directamente.
 */

const REFRESH_SECONDS = 20 * 60;
const JOBS_JSON_URL = "/jobs_data.json";
const CV_STORAGE_KEY = "job_dashboard_cv_base_v1";

// --- Utilidades --------------------------------------------------------

function formatSalary(job) {
  if (!job.salary_min && !job.salary_max) return "No especificado";
  const cur = (job.currency || "usd").toUpperCase();
  const symbol = { USD: "$", EUR: "€", JPY: "¥", GBP: "£" }[cur] || "";
  const fmt = (n) => (n >= 1000 ? `${Math.round(n / 1000)}k` : n);
  if (job.salary_min && job.salary_max && job.salary_min !== job.salary_max) {
    return `${symbol}${fmt(job.salary_min)}–${symbol}${fmt(job.salary_max)} ${cur}`;
  }
  return `${symbol}${fmt(job.salary_max || job.salary_min)} ${cur}`;
}

function formatCountdown(seconds) {
  const m = Math.floor(seconds / 60).toString().padStart(2, "0");
  const s = Math.floor(seconds % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

// Recalcula el match % en el navegador cada vez que cambia el CV base, sin
// depender de que el backend haya vuelto a correr. Heurística ligera de
// overlap de palabras — el TF-IDF "real" del backend (ver scraper_aggregator.py)
// es más riguroso mnemos se usa aquí como valor inicial antes de que el
// usuario edite su CV.
function computeLiveMatchScore(cvText, job) {
  if (!cvText || !cvText.trim()) return job.match_score ?? null;
  const tokenize = (t) =>
    new Set((t.toLowerCase().match(/[a-záéíóúñ]{3,}/g) || []));
  const cvWords = tokenize(cvText);
  const jobWords = tokenize(`${job.title} ${(job.tags || []).join(" ")} ${job.description || ""}`);
  if (cvWords.size === 0 || jobWords.size === 0) return job.match_score ?? null;
  let overlap = 0;
  jobWords.forEach((w) => {
    if (cvWords.has(w)) overlap += 1;
  });
  const union = new Set([...cvWords, ...jobWords]).size;
  const score = Math.min(100, Math.round((overlap / union) * 250));
  return score;
}

// --- Subcomponentes ------------------------------------------------------

function MatchBadge({ score }) {
  if (score === null || score === undefined) {
    return <span className="text-xs text-slate-400">Sin CV cargado</span>;
  }
  const color =
    score >= 70 ? "bg-emerald-100 text-emerald-800 border-emerald-300" :
    score >= 40 ? "bg-amber-100 text-amber-800 border-amber-300" :
    "bg-slate-100 text-slate-600 border-slate-300";
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-mono font-semibold ${color}`}>
      {score}% match
    </span>
  );
}

function JobCard({ job, cvText, onAdaptCV, onGenerateLetter }) {
  const liveScore = useMemo(() => computeLiveMatchScore(cvText, job), [cvText, job]);
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm hover:shadow-md transition-shadow">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="font-semibold text-slate-900 leading-snug">{job.title}</h3>
          <p className="text-sm text-slate-500">{job.company} · {job.location}</p>
        </div>
        <MatchBadge score={liveScore} />
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
        <span className="rounded bg-slate-100 px-2 py-1 font-mono text-slate-700">{formatSalary(job)}</span>
        {job.language && (
          <span className="rounded bg-indigo-50 px-2 py-1 text-indigo-700">
            {{ english: "Inglés", spanish: "Español", bilingual: "Bilingüe", unknown: "Idioma ?" }[job.language]}
          </span>
        )}
        {job.region === "japan" && (
          <span className="rounded bg-rose-50 px-2 py-1 text-rose-700">🗾 Japón</span>
        )}
        {(job.tags || []).slice(0, 4).map((t) => (
          <span key={t} className="rounded bg-slate-50 px-2 py-1 text-slate-500 border border-slate-200">{t}</span>
        ))}
      </div>

      <div className="mt-4 flex gap-2">
        <a
          href={job.url}
          target="_blank"
          rel="noreferrer"
          className="flex-1 text-center rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-700"
        >
          🔗 Aplicar Ahora
        </a>
        <button
          onClick={() => onAdaptCV(job)}
          className="flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
        >
          📄 Adaptar CV
        </button>
        <button
          onClick={() => onGenerateLetter(job)}
          className="flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
        >
          ✉️ Generar Carta
        </button>
      </div>
    </div>
  );
}

function GenerationModal({ job, mode, cvText, onClose }) {
  const [content, setContent] = useState("");
  const [status, setStatus] = useState("loading"); // loading | done | error
  const [errorMsg, setErrorMsg] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function run() {
      setStatus("loading");
      try {
        const resp = await fetch("/api/generate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode, job, cvBase: cvText }),
        });
        if (!resp.ok) throw new Error(`El servidor respondió ${resp.status}`);
        const data = await resp.json();
        if (!cancelled) {
          setContent(data.text || "");
          setStatus("done");
        }
      } catch (err) {
        if (!cancelled) {
          setErrorMsg(err.message || "Error desconocido");
          setStatus("error");
        }
      }
    }
    run();
    return () => { cancelled = true; };
  }, [job, mode, cvText]);

  const title = mode === "cv" ? "CV adaptado" : "Carta de presentación";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-lg bg-white p-6 shadow-xl">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-slate-900">{title} — {job.title}</h2>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700">✕</button>
        </div>

        {status === "loading" && (
          <p className="text-sm text-slate-500">Generando con tu CV base y la descripción de la vacante…</p>
        )}
        {status === "error" && (
          <div className="rounded border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
            No se pudo generar el contenido ({errorMsg}). Revisa que /api/generate esté desplegado
            y que ANTHROPIC_API_KEY esté configurada en el servidor (ver DEPLOYMENT_GUIDE.md).
          </div>
        )}
        {status === "done" && (
          <>
            <textarea
              readOnly
              value={content}
              className="h-96 w-full resize-none rounded border border-slate-200 p-3 font-mono text-sm"
            />
            <div className="mt-3 flex justify-end gap-2">
              <button
                onClick={() => navigator.clipboard.writeText(content)}
                className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
              >
                Copiar
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function CVBaseModal({ initialText, onSave, onClose }) {
  const [text, setText] = useState(initialText);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-lg bg-white p-6 shadow-xl">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-slate-900">Mi CV Base</h2>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700">✕</button>
        </div>
        <p className="mb-2 text-sm text-slate-500">
          Pega tu hoja de vida en texto plano o Markdown. Se guarda solo en este navegador
          y alimenta el cálculo de Match % y la generación de CV/carta.
        </p>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Ej: Analista de datos con 3 años de experiencia en SQL, Power BI..."
          className="h-72 w-full resize-none rounded border border-slate-200 p-3 text-sm"
        />
        <div className="mt-3 flex justify-end gap-2">
          <button onClick={onClose} className="rounded-md px-4 py-2 text-sm text-slate-500 hover:bg-slate-50">
            Cancelar
          </button>
          <button
            onClick={() => onSave(text)}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
          >
            Guardar CV
          </button>
        </div>
      </div>
    </div>
  );
}

// --- Componente principal -------------------------------------------------

export default function JobDashboard() {
  const [jobsData, setJobsData] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [secondsLeft, setSecondsLeft] = useState(REFRESH_SECONDS);

  const [activeTab, setActiveTab] = useState("global"); // "global" | "japan"
  const [keyword, setKeyword] = useState("");
  const [language, setLanguage] = useState("all");
  const [minSalary, setMinSalary] = useState(0);
  const [sortBy, setSortBy] = useState("match"); // "match" | "salary"

  const [cvText, setCvText] = useState("");
  const [showCvModal, setShowCvModal] = useState(false);
  const [modalJob, setModalJob] = useState(null); // { job, mode }

  const timerRef = useRef(null);

  const loadJobs = useCallback(async () => {
    setRefreshing(true);
    try {
      const resp = await fetch(`${JOBS_JSON_URL}?t=${Date.now()}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setJobsData(data);
      setLoadError(null);
    } catch (err) {
      setLoadError(err.message);
    } finally {
      setRefreshing(false);
      setSecondsLeft(REFRESH_SECONDS);
    }
  }, []);

  // Carga inicial + CV guardado en localStorage
  useEffect(() => {
    loadJobs();
    try {
      const saved = window.localStorage.getItem(CV_STORAGE_KEY);
      if (saved) setCvText(saved);
    } catch (_) { /* localStorage no disponible, seguimos sin CV persistido */ }
  }, [loadJobs]);

  // Temporizador regresivo + auto-refresh
  useEffect(() => {
    timerRef.current = setInterval(() => {
      setSecondsLeft((prev) => {
        if (prev <= 1) {
          loadJobs();
          return REFRESH_SECONDS;
        }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(timerRef.current);
  }, [loadJobs]);

  const handleSaveCv = (text) => {
    setCvText(text);
    try {
      window.localStorage.setItem(CV_STORAGE_KEY, text);
    } catch (_) { /* seguimos sin persistir si el navegador lo bloquea */ }
    setShowCvModal(false);
  };

  const rawJobs = jobsData ? (activeTab === "global" ? jobsData.remote_global : jobsData.japan) : [];

  const filteredJobs = useMemo(() => {
    let list = [...rawJobs];
    if (keyword.trim()) {
      const kw = keyword.toLowerCase();
      list = list.filter((j) =>
        `${j.title} ${j.company} ${(j.tags || []).join(" ")}`.toLowerCase().includes(kw)
      );
    }
    if (language !== "all") {
      list = list.filter((j) => j.language === language);
    }
    if (minSalary > 0) {
      list = list.filter((j) => (j.salary_usd_approx || 0) >= minSalary);
    }
    list.forEach((j) => { j._liveScore = computeLiveMatchScore(cvText, j); });
    list.sort((a, b) => {
      if (sortBy === "salary") return (b.salary_usd_approx || 0) - (a.salary_usd_approx || 0);
      return (b._liveScore ?? -1) - (a._liveScore ?? -1);
    });
    return list;
  }, [rawJobs, keyword, language, minSalary, sortBy, cvText]);

  return (
    <div className="min-h-screen bg-slate-50 p-4 md:p-8">
      {/* Barra superior */}
      <div className="mx-auto mb-6 flex max-w-6xl flex-col gap-3 rounded-lg border border-slate-200 bg-white p-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-900">Data Analyst / Analytics Engineer — Radar de vacantes</h1>
          <p className="text-sm text-slate-500">
            {jobsData ? `Última actualización: ${new Date(jobsData.generated_at).toLocaleString()}` : "Cargando…"}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-sm">
            {refreshing ? "🔄 Refrescando…" : "🟢 Actualizado"}
          </span>
          <span className="rounded bg-slate-100 px-2 py-1 font-mono text-sm text-slate-700">
            {formatCountdown(secondsLeft)}
          </span>
          <button
            onClick={loadJobs}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700"
          >
            Refrescar Ahora
          </button>
          <button
            onClick={() => setShowCvModal(true)}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            {cvText ? "✏️ Editar Mi CV" : "📎 Cargar Mi CV Base"}
          </button>
        </div>
      </div>

      {loadError && (
        <div className="mx-auto mb-4 max-w-6xl rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
          No se pudo cargar jobs_data.json ({loadError}). Verifica que el archivo generado por
          scraper_aggregator.py esté publicado en /jobs_data.json.
        </div>
      )}

      {/* Tabs */}
      <div className="mx-auto mb-4 flex max-w-6xl gap-2">
        <button
          onClick={() => setActiveTab("global")}
          className={`rounded-md px-4 py-2 text-sm font-medium ${activeTab === "global" ? "bg-slate-900 text-white" : "bg-white text-slate-600 border border-slate-200"}`}
        >
          🌍 Vacantes Remotas Globales
        </button>
        <button
          onClick={() => setActiveTab("japan")}
          className={`rounded-md px-4 py-2 text-sm font-medium ${activeTab === "japan" ? "bg-slate-900 text-white" : "bg-white text-slate-600 border border-slate-200"}`}
        >
          🗾 Oportunidades en Japón
        </button>
      </div>

      {/* Filtros */}
      <div className="mx-auto mb-6 grid max-w-6xl grid-cols-1 gap-3 rounded-lg border border-slate-200 bg-white p-4 sm:grid-cols-4">
        <input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder="Buscar por palabra clave…"
          className="rounded border border-slate-200 px-3 py-2 text-sm sm:col-span-2"
        />
        <select
          value={language}
          onChange={(e) => setLanguage(e.target.value)}
          className="rounded border border-slate-200 px-3 py-2 text-sm"
        >
          <option value="all">Todos los idiomas</option>
          <option value="spanish">Español</option>
          <option value="english">Inglés</option>
          <option value="bilingual">Bilingüe</option>
        </select>
        <select
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value)}
          className="rounded border border-slate-200 px-3 py-2 text-sm"
        >
          <option value="match">Ordenar por Match %</option>
          <option value="salary">Ordenar por Salario</option>
        </select>
        <div className="sm:col-span-4">
          <label className="text-xs text-slate-500">Salario mínimo aprox. (USD): ${minSalary.toLocaleString()}</label>
          <input
            type="range"
            min="0"
            max="150000"
            step="5000"
            value={minSalary}
            onChange={(e) => setMinSalary(Number(e.target.value))}
            className="w-full"
          />
        </div>
      </div>

      {/* Grid de vacantes */}
      <div className="mx-auto grid max-w-6xl grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {filteredJobs.map((job) => (
          <JobCard
            key={job.id}
            job={job}
            cvText={cvText}
            onAdaptCV={(j) => setModalJob({ job: j, mode: "cv" })}
            onGenerateLetter={(j) => setModalJob({ job: j, mode: "letter" })}
          />
        ))}
        {jobsData && filteredJobs.length === 0 && (
          <p className="col-span-full text-center text-sm text-slate-400">
            Ninguna vacante coincide con estos filtros.
          </p>
        )}
      </div>

      {showCvModal && (
        <CVBaseModal initialText={cvText} onSave={handleSaveCv} onClose={() => setShowCvModal(false)} />
      )}
      {modalJob && (
        <GenerationModal
          job={modalJob.job}
          mode={modalJob.mode}
          cvText={cvText}
          onClose={() => setModalJob(null)}
        />
      )}
    </div>
  );
}
