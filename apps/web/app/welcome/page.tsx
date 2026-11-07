"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Health = { signup_allowed: boolean };
type Lang = "en" | "es" | "pt";

const T = {
  en: {
    title: "An open research platform for financial data",
    lead: "Amaru is a personal research and portfolio project for a master's in data science — not a product and not a paid service. It ingests public market and macro series, stores them with lineage and point-in-time vintages, and answers research questions with deterministic calculations.",
    rule: "Design rule: the language model never computes numbers. Every figure traces to stored data or a quant-engine calculation, and a verification step checks this before a report is shown.",
    start: "Create an account", signin: "Sign in",
    features: [
      ["Verified answers", "Claude (when configured) chooses tools and writes the narrative. Numbers come from stored observations or from the quant engine. Figures that cannot be traced are flagged, not hidden."],
      ["Official data, labelled", "Federal Reserve, FRED, BEA, USDA, CFTC and EIA first, plus the IMF, BIS, World Bank and UN Comtrade — each with freshness, source and licence."],
      ["Point-in-time vintages", "Revised official values are stored as new vintages, so a past as-of date reproduces what was knowable then, not today's revised print."],
      ["Scenario Agent", "Auditable rules scan stored data for rate, dollar, oil, volatility, positioning and supply shocks, estimate historical sensitivities, and walk-forward test them. Rankings are not probabilities."],
      ["U.S. farm and energy markets", "USDA supply-and-demand balances, a price-versus-stocks-to-use model, CFTC positioning, EIA oil and gas inventories, and U.S. trade with China, Mexico and Canada."],
      ["Open to inspect", "Reports in HTML, PDF and Excel; alerts and agents on data changes; workspaces so research stays isolated. Run it locally from the repository."],
    ],
    disclaimer: "Amaru provides general research and decision support, not personalised investment advice. Prices are public reference series published with a lag, not live quotes. This is a research project, not a commercial service.",
  },
  es: {
    title: "Una plataforma abierta de investigación financiera",
    lead: "Amaru es un proyecto personal de investigación y portafolio para un máster en ciencia de datos: no es un producto ni un servicio de pago. Ingesta series públicas de mercados y macro, las guarda con linaje y vintage punto en el tiempo, y responde preguntas con cálculos deterministas.",
    rule: "Regla de diseño: el modelo de lenguaje nunca calcula cifras. Cada número se rastrea hasta datos almacenados o un cálculo del motor cuantitativo, y un paso de verificación lo comprueba antes de mostrar el informe.",
    start: "Crear una cuenta", signin: "Iniciar sesión",
    features: [
      ["Respuestas verificadas", "Claude (si está configurado) elige herramientas y redacta. Los números salen de observaciones almacenadas o del motor cuantitativo. Las cifras que no se pueden rastrear se señalan, no se ocultan."],
      ["Datos oficiales, con su etiqueta", "Primero Reserva Federal, FRED, BEA, USDA, CFTC y EIA, además del FMI, el BPI, el Banco Mundial y UN Comtrade, cada uno con actualidad, fuente y licencia."],
      ["Vintages punto en el tiempo", "Los valores oficiales revisados se guardan como vintages nuevos, así una fecha as-of reproduce lo que se podía saber entonces, no la cifra revisada de hoy."],
      ["Agente de escenarios", "Reglas auditables buscan en los datos almacenados shocks de tasas, dólar, petróleo, volatilidad, posicionamiento y oferta, estiman sensibilidades históricas y las ponen a prueba hacia adelante. El orden no es una probabilidad."],
      ["Mercados agrícolas y energéticos de EE. UU.", "Balances de oferta y demanda del USDA, un modelo de precio frente a existencias-uso, posiciones de la CFTC, inventarios de la EIA y el comercio de EE. UU. con China, México y Canadá."],
      ["Abierto a inspección", "Informes en HTML, PDF y Excel; alertas y agentes cuando cambian los datos; espacios de trabajo para aislar la investigación. Se ejecuta en local desde el repositorio."],
    ],
    disclaimer: "Amaru ofrece investigación general y apoyo a decisiones, no asesoramiento de inversión personalizado. Los precios son series públicas de referencia publicadas con rezago, no cotizaciones en tiempo real. Es un proyecto de investigación, no un servicio comercial.",
  },
  pt: {
    title: "Uma plataforma aberta de pesquisa financeira",
    lead: "O Amaru é um projeto pessoal de pesquisa e portfólio para um mestrado em ciência de dados — não é um produto nem um serviço pago. Ele ingere séries públicas de mercado e macro, guarda-as com linhagem e vintages ponto no tempo, e responde perguntas com cálculos determinísticos.",
    rule: "Regra de desenho: o modelo de linguagem nunca calcula números. Cada cifra rastreia até dados armazenados ou um cálculo do motor quantitativo, e um passo de verificação confere isso antes de mostrar o relatório.",
    start: "Criar uma conta", signin: "Entrar",
    features: [
      ["Respostas verificadas", "O Claude (quando configurado) escolhe ferramentas e escreve a narrativa. Os números vêm de observações armazenadas ou do motor quantitativo. Cifras que não podem ser rastreadas são sinalizadas, não escondidas."],
      ["Dados oficiais, identificados", "Primeiro Federal Reserve, FRED, BEA, USDA, CFTC e EIA, além do FMI, do BIS, do Banco Mundial e da UN Comtrade — cada um com atualização, fonte e licença."],
      ["Vintages ponto no tempo", "Valores oficiais revisados são guardados como vintages novos, para que uma data as-of reproduza o que era conhecível então, não a cifra revisada de hoje."],
      ["Agente de cenários", "Regras auditáveis varrem os dados armazenados em busca de choques de juros, dólar, petróleo, volatilidade, posicionamento e oferta, estimam sensibilidades históricas e as testam walk-forward. A ordem não é uma probabilidade."],
      ["Mercados agrícolas e de energia dos EUA", "Balanços de oferta e demanda do USDA, um modelo de preço versus relação estoque-consumo, posições da CFTC, estoques da EIA e o comércio dos EUA com China, México e Canadá."],
      ["Aberto a inspeção", "Relatórios em HTML, PDF e Excel; alertas e agentes quando os dados mudam; espaços de trabalho para isolar a pesquisa. Rode localmente a partir do repositório."],
    ],
    disclaimer: "O Amaru oferece pesquisa geral e apoio à decisão, não recomendação de investimento personalizada. Os preços são séries públicas de referência publicadas com defasagem, não cotações em tempo real. Este é um projeto de pesquisa, não um serviço comercial.",
  },
} as const;

const LANGS: [Lang, string][] = [["en", "English"], ["es", "Español"], ["pt", "Português"]];

export default function Welcome() {
  const [lang, setLang] = useState<Lang>("en");
  const [signupAllowed, setSignupAllowed] = useState(true);
  useEffect(() => {
    let saved: string | null = null;
    try { saved = localStorage.getItem("atlas-lang"); } catch { /* storage unavailable */ }
    const guess = (saved || navigator.language || "en").slice(0, 2);
    if (guess === "es" || guess === "pt") setLang(guess);
    api<Health>("/health").then((h) => setSignupAllowed(h.signup_allowed)).catch(() => {});
  }, []);
  useEffect(() => { document.documentElement.lang = lang; }, [lang]);
  const choose = (l: Lang) => { setLang(l); try { localStorage.setItem("atlas-lang", l); } catch { /* ignore */ } };
  const t = T[lang];

  return (
    <div className="landing">
      <header className="row" style={{ justifyContent: "space-between", marginBottom: 40 }}>
        <div className="brand" style={{ padding: 0 }}><span className="brand-mark">◆</span> Amaru</div>
        <div className="row" style={{ gap: 10 }}>
          <div className="seg" role="group" aria-label="Language">
            {LANGS.map(([k, label]) => <button key={k} type="button" className={lang === k ? "on" : ""} onClick={() => choose(k)}>{label}</button>)}
          </div>
          <Link href="/login">{t.signin}</Link>
        </div>
      </header>

      <section style={{ maxWidth: 720 }}>
        <h1 className="landing-title">{t.title}</h1>
        <p className="landing-lead">{t.lead}</p>
        <p className="landing-lead" style={{ marginTop: 14 }}>{t.rule}</p>
        <div className="row" style={{ gap: 12, marginTop: 20 }}>
          {signupAllowed && <Link className="button" href="/login?mode=signup">{t.start}</Link>}
          <Link href="/login">{t.signin}</Link>
        </div>
      </section>

      <section className="landing-features">
        {t.features.map(([title, body]) => (
          <div key={title} className="card"><h3 style={{ marginTop: 0, fontSize: 15 }}>{title}</h3><p className="muted" style={{ margin: 0, fontSize: 13.5, lineHeight: 1.55 }}>{body}</p></div>
        ))}
      </section>

      <footer className="muted" style={{ fontSize: 12, marginTop: 48, borderTop: "1px solid var(--line)", paddingTop: 16 }}>
        {t.disclaimer}
      </footer>
    </div>
  );
}
