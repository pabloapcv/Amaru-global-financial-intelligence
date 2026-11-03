"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Plan = { key: string; limits: Record<string, number | null>; price_per_month: number | null };
type Plans = { plans: Plan[]; currency: string; signup_allowed: boolean; self_service_billing: boolean; sales_email: string | null;
  terms_url: string | null; privacy_url: string | null };
type Lang = "en" | "es" | "pt";

const T = {
  en: {
    title: "Financial research you can check",
    lead: "Research on U.S. markets and the U.S. economy: the Fed, Treasury yields, the dollar, inflation and jobs, energy, and the farm and commodity markets, with 25 other economies for context. Amaru researches with Claude, then traces every number back to official data or a calculation before you see it.",
    start: "Create a free account", signin: "Sign in", invite: "Ask for access",
    features: [
      ["Verified answers", "Every figure is matched to a stored observation or a calculation and every citation must resolve. Figures that can't be traced are flagged, not hidden."],
      ["Official data, labelled", "Federal Reserve, FRED, BEA, USDA, CFTC and EIA data first, plus the IMF, BIS, World Bank and UN Comtrade, each with its freshness, source and licence."],
      ["U.S. farm and energy markets", "USDA supply-and-demand balances, a price-versus-stocks-to-use model, CFTC positioning, EIA oil and gas inventories, and U.S. trade with China, Mexico and Canada."],
      ["Reports and newsletters", "HTML, PDF and Excel reports on a schedule, and a newsletter for your readers, in English or in Spanish or Portuguese."],
      ["Monitoring", "Alerts and agents that run research when data or official releases change, delivered by email, webhook or Slack."],
      ["Built for teams", "Workspaces, roles, two-factor sign-in, an activity log, an API and monthly caps on AI spending."],
    ],
    pricing: "Plans", free: "Free", contact: "Contact us", perMonth: "/month",
    rows: { seats: "People", research_per_day: "Research runs per day", llm_research_per_day: "Claude research runs per day", llm_budget_usd_per_month: "Claude spending cap per month (USD)",
      schedules: "Scheduled reports", alerts: "Alerts", agents: "Monitoring agents", newsletter_subscribers: "Newsletter subscribers", api_keys: "API keys", requests_per_minute: "API requests per minute" },
    names: { explorer: "Explorer", investor: "Investor", research_pro: "Research Pro", pro: "Pro" },
    terms: "Terms of Service", privacy: "Privacy Policy",
    disclaimer: "Amaru provides general research and decision support, not personalised investment advice. Prices are public reference series published with a lag, not live quotes.",
  },
  es: {
    title: "Investigación financiera que puedes comprobar",
    lead: "Investigación sobre los mercados y la economía de Estados Unidos: la Reserva Federal, los bonos del Tesoro, el dólar, la inflación y el empleo, la energía y los mercados agrícolas y de materias primas, con otras 25 economías como contexto. Amaru investiga con Claude y luego rastrea cada cifra hasta datos oficiales o un cálculo antes de mostrártela.",
    start: "Crear una cuenta gratis", signin: "Iniciar sesión", invite: "Solicitar acceso",
    features: [
      ["Respuestas verificadas", "Cada cifra se compara con una observación almacenada o un cálculo, y cada cita debe ser válida. Las cifras que no se pueden rastrear se señalan, no se ocultan."],
      ["Datos oficiales, con su etiqueta", "Primero datos de la Reserva Federal, FRED, BEA, USDA, CFTC y EIA, además del FMI, el BPI, el Banco Mundial y UN Comtrade, cada uno con su actualidad, fuente y licencia."],
      ["Mercados agrícolas y energéticos de EE. UU.", "Balances de oferta y demanda del USDA, un modelo de precio frente a la relación existencias-uso, posiciones de la CFTC, inventarios de petróleo y gas de la EIA y el comercio de EE. UU. con China, México y Canadá."],
      ["Informes y boletines", "Informes en HTML, PDF y Excel programados, y un boletín para tus lectores en inglés, español o portugués."],
      ["Monitoreo", "Alertas y agentes que investigan cuando cambian los datos o los comunicados oficiales, por correo, webhook o Slack."],
      ["Para equipos", "Espacios de trabajo, roles, inicio de sesión en dos pasos, registro de actividad, API y límites mensuales de gasto en IA."],
    ],
    pricing: "Planes", free: "Gratis", contact: "Contáctanos", perMonth: "/mes",
    rows: { seats: "Personas", research_per_day: "Investigaciones por día", llm_research_per_day: "Investigaciones con Claude por día", llm_budget_usd_per_month: "Límite mensual de gasto en Claude (USD)",
      schedules: "Informes programados", alerts: "Alertas", agents: "Agentes de monitoreo", newsletter_subscribers: "Suscriptores del boletín", api_keys: "Claves de API", requests_per_minute: "Solicitudes a la API por minuto" },
    names: { explorer: "Explorer", investor: "Investor", research_pro: "Research Pro", pro: "Pro" },
    terms: "Términos del servicio", privacy: "Política de privacidad",
    disclaimer: "Amaru ofrece investigación general y apoyo a decisiones, no asesoramiento de inversión personalizado. Los precios son series públicas de referencia publicadas con rezago, no cotizaciones en tiempo real.",
  },
  pt: {
    title: "Pesquisa financeira que você pode conferir",
    lead: "Pesquisa sobre os mercados e a economia dos Estados Unidos: o Federal Reserve, os títulos do Tesouro, o dólar, a inflação e o emprego, a energia e os mercados agrícolas e de commodities, com outras 25 economias como contexto. O Amaru pesquisa com o Claude e depois rastreia cada número até dados oficiais ou um cálculo antes de mostrá-lo a você.",
    start: "Criar uma conta grátis", signin: "Entrar", invite: "Solicitar acesso",
    features: [
      ["Respostas verificadas", "Cada número é comparado a uma observação armazenada ou a um cálculo, e cada citação precisa ser válida. Números que não podem ser rastreados são sinalizados, não escondidos."],
      ["Dados oficiais, identificados", "Primeiro dados do Federal Reserve, FRED, BEA, USDA, CFTC e EIA, além do FMI, do BIS, do Banco Mundial e da UN Comtrade, cada um com sua atualização, fonte e licença."],
      ["Mercados agrícolas e de energia dos EUA", "Balanços de oferta e demanda do USDA, um modelo de preço versus relação estoque-consumo, posições da CFTC, estoques de petróleo e gás da EIA e o comércio dos EUA com China, México e Canadá."],
      ["Relatórios e newsletters", "Relatórios em HTML, PDF e Excel agendados, e uma newsletter para seus leitores em inglês, espanhol ou português."],
      ["Monitoramento", "Alertas e agentes que pesquisam quando os dados ou comunicados oficiais mudam, por e-mail, webhook ou Slack."],
      ["Feito para equipes", "Espaços de trabalho, funções, login em duas etapas, registro de atividades, API e limites mensais de gasto com IA."],
    ],
    pricing: "Planos", free: "Grátis", contact: "Fale conosco", perMonth: "/mês",
    rows: { seats: "Pessoas", research_per_day: "Pesquisas por dia", llm_research_per_day: "Pesquisas com o Claude por dia", llm_budget_usd_per_month: "Limite mensal de gasto com o Claude (USD)",
      schedules: "Relatórios agendados", alerts: "Alertas", agents: "Agentes de monitoramento", newsletter_subscribers: "Inscritos na newsletter", api_keys: "Chaves de API", requests_per_minute: "Requisições à API por minuto" },
    names: { explorer: "Explorer", investor: "Investor", research_pro: "Research Pro", pro: "Pro" },
    terms: "Termos de serviço", privacy: "Política de privacidade",
    disclaimer: "O Amaru oferece pesquisa geral e apoio à decisão, não recomendação de investimento personalizada. Os preços são séries públicas de referência publicadas com defasagem, não cotações em tempo real.",
  },
} as const;

const LANGS: [Lang, string][] = [["en", "English"], ["es", "Español"], ["pt", "Português"]];

export default function Welcome() {
  const [lang, setLang] = useState<Lang>("en");
  const [plans, setPlans] = useState<Plans | null>(null);
  useEffect(() => {
    let saved: string | null = null;
    try { saved = localStorage.getItem("atlas-lang"); } catch { /* storage unavailable */ }
    const guess = (saved || navigator.language || "en").slice(0, 2);
    if (guess === "es" || guess === "pt") setLang(guess);
    api<Plans>("/api/v1/public/plans").then(setPlans).catch(() => {});
  }, []);
  useEffect(() => { document.documentElement.lang = lang; }, [lang]);
  const choose = (l: Lang) => { setLang(l); try { localStorage.setItem("atlas-lang", l); } catch { /* ignore */ } };
  const t = T[lang];
  const locale = lang === "en" ? "en-US" : lang === "es" ? "es-419" : "pt-BR";
  const num = (v: number | null | undefined) => (v == null ? "∞" : v.toLocaleString(locale));
  const price = (p: Plan) => p.price_per_month === 0 ? t.free
    : p.price_per_month == null ? t.contact
    : `${new Intl.NumberFormat(locale, { style: "currency", currency: plans?.currency ?? "USD", maximumFractionDigits: 0 }).format(p.price_per_month)}${t.perMonth}`;
  const cta = plans && !plans.signup_allowed
    ? (plans.sales_email ? <a className="button" href={`mailto:${plans.sales_email}`}>{t.invite}</a> : null)
    : <Link className="button" href="/login?mode=signup">{t.start}</Link>;

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
        <div className="row" style={{ gap: 12, marginTop: 20 }}>{cta}</div>
      </section>

      <section className="landing-features">
        {t.features.map(([title, body]) => (
          <div key={title} className="card"><h3 style={{ marginTop: 0, fontSize: 15 }}>{title}</h3><p className="muted" style={{ margin: 0, fontSize: 13.5, lineHeight: 1.55 }}>{body}</p></div>
        ))}
      </section>

      {plans && (
        <section style={{ marginTop: 48 }}>
          <h2>{t.pricing}</h2>
          <div className="card" style={{ overflowX: "auto" }}>
            <table>
              <thead>
                <tr><th></th>{plans.plans.map((p) => <th key={p.key} className="num">{t.names[p.key as keyof typeof t.names] ?? p.key}</th>)}</tr>
                <tr><td></td>{plans.plans.map((p) => <td key={p.key} className="num"><strong>{price(p)}</strong></td>)}</tr>
              </thead>
              <tbody>
                {Object.entries(t.rows).map(([k, label]) => (
                  <tr key={k}><td className="muted">{label}</td>{plans.plans.map((p) => <td key={p.key} className="num">{num(p.limits[k])}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
          {plans.sales_email && <p className="muted" style={{ fontSize: 13 }}><a href={`mailto:${plans.sales_email}`}>{plans.sales_email}</a></p>}
        </section>
      )}

      <footer className="muted" style={{ fontSize: 12, marginTop: 48, borderTop: "1px solid var(--line)", paddingTop: 16 }}>
        {t.disclaimer}
        {(plans?.terms_url || plans?.privacy_url) && (
          <div className="row" style={{ gap: 14, marginTop: 8 }}>
            {plans.terms_url && <a href={plans.terms_url} target="_blank" rel="noreferrer">{t.terms}</a>}
            {plans.privacy_url && <a href={plans.privacy_url} target="_blank" rel="noreferrer">{t.privacy}</a>}
          </div>
        )}
      </footer>
    </div>
  );
}
