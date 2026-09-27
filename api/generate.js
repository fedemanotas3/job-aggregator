/**
 * api/generate.js
 * -----------------
 * Función serverless (Vercel: /api/generate, Netlify: adaptar a
 * netlify/functions/generate.js con el mismo cuerpo). Llama a la API de
 * Claude DEL LADO DEL SERVIDOR, para que tu API key nunca quede expuesta en
 * el navegador. El frontend (JobDashboard.jsx) le pega a este endpoint, no a
 * api.anthropic.com directamente.
 *
 * Variables de entorno requeridas en tu proyecto de Vercel/Netlify:
 *   ANTHROPIC_API_KEY   (nunca la pongas en el código ni en el frontend)
 *   CLAUDE_MODEL         (opcional, default: claude-sonnet-5 — revisa
 *                         https://docs.claude.com para el modelo vigente)
 */

const CV_SYSTEM_PROMPT = `Eres un redactor de CVs pragmático. Reescribes la hoja de vida
de un candidato para que encaje con una vacante específica, SIN inventar
experiencia que el candidato no tiene.

Reglas estrictas:
- Prohibido usar clichés de IA: "apasionado", "orientado a resultados",
  "sinergia", "revolucionario", "inestimable", "entusiasta", "proactivo".
- Tono natural, profesional, en primera persona, como lo escribiría la
  persona misma (no un robot de RRHH).
- Reordena y reescribe las viñetas de experiencia para que las que mejor
  matchean con los requisitos técnicos del puesto queden primero.
- Prioriza logros cuantitativos (%, KPIs, tiempo ahorrado, tamaño de
  datasets, usuarios impactados) sobre descripciones genéricas de tareas.
- Usa el vocabulario técnico exacto de la vacante cuando el candidato sí
  tiene esa habilidad (para ATS), pero no le atribuyas herramientas que no
  mencionó en su CV base.
- Devuelve el CV adaptado completo, listo para copiar y pegar. Sin
  comentarios tuyos antes o después.`;

const COVER_LETTER_SYSTEM_PROMPT = `Eres un redactor de cartas de presentación
pragmático. Escribes cartas cortas y concretas, nunca genéricas.

Reglas estrictas:
- Máximo 3 párrafos cortos.
- Prohibido usar los mismos clichés de IA listados para el CV.
- Párrafo 1: por qué esta vacante específica interesa (algo real del puesto
  o la empresa, no una frase que serviría para cualquier oferta).
- Párrafo 2: 1-2 logros concretos y cuantitativos del CV base que hacen
  match directo con los requisitos técnicos del puesto.
- Párrafo 3: aspecto logístico ejecutivo — disponibilidad remota inmediata,
  o apertura a reubicarse a Japón con patrocinio de visa, según aplique al
  puesto. Sin relleno emocional.
- Tono profesional, directo, primera persona. Devuelve solo la carta.`;

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Method not allowed" });
    return;
  }

  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    res.status(500).json({ error: "Falta ANTHROPIC_API_KEY en las variables de entorno del servidor." });
    return;
  }

  const { mode, job, cvBase } = req.body || {};
  if (!job || !cvBase) {
    res.status(400).json({ error: "Faltan 'job' o 'cvBase' en el cuerpo de la solicitud." });
    return;
  }

  const system = mode === "letter" ? COVER_LETTER_SYSTEM_PROMPT : CV_SYSTEM_PROMPT;
  const userPrompt =
    `HOJA DE VIDA BASE:\n${cvBase}\n\n` +
    `VACANTE:\nTítulo: ${job.title}\nEmpresa: ${job.company}\n` +
    `Descripción: ${job.description || "(sin descripción detallada disponible, usa el título y tags)"}\n` +
    `Tags: ${(job.tags || []).join(", ")}`;

  try {
    const upstream = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify({
        model: process.env.CLAUDE_MODEL || "claude-sonnet-5",
        max_tokens: 1200,
        system,
        messages: [{ role: "user", content: userPrompt }],
      }),
    });

    if (!upstream.ok) {
      const errText = await upstream.text();
      res.status(upstream.status).json({ error: `Claude API error: ${errText}` });
      return;
    }

    const data = await upstream.json();
    const text = (data.content || [])
      .filter((block) => block.type === "text")
      .map((block) => block.text)
      .join("\n");

    res.status(200).json({ text });
  } catch (err) {
    res.status(500).json({ error: err.message || "Error desconocido llamando a Claude" });
  }
}
