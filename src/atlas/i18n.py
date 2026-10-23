"""Languages for research briefs and reports (English, Spanish, Portuguese).

Claude writes the brief in the requested language; the deterministic engine writes English and says
so. Spanish and Portuguese briefs use the decimal comma (14,81) and dot thousands (1.234,5), which the
verifier understands.
"""

from __future__ import annotations

LANGUAGES = {"en": "English", "es": "Spanish (Latin American)", "pt": "Portuguese (Brazilian)"}
DECIMAL_COMMA = {"es", "pt"}

SECTIONS = {
    "en": ["Executive summary", "Scope and methodology", "Market snapshot", "Historical analysis", "Drivers and relationships",
           "Outlook and scenarios", "Risks", "Data limitations"],
    "es": ["Resumen ejecutivo", "Alcance y metodología", "Panorama del mercado", "Análisis histórico", "Factores y relaciones",
           "Perspectivas y escenarios", "Riesgos", "Limitaciones de los datos"],
    "pt": ["Resumo executivo", "Escopo e metodologia", "Panorama do mercado", "Análise histórica", "Fatores e relações",
           "Perspectivas e cenários", "Riscos", "Limitações dos dados"],
}

LABELS: dict[str, dict[str, str]] = {
    "en": {
        "brand": "ATLAS · RESEARCH", "question": "Question", "generated": "Generated", "engine": "Engine", "report": "Report",
        "verified": "Verification passed: {ok} of {n} numerical claims traced to stored observations or calculations; all citations resolve.",
        "unverified": "Verification warning: {bad} of {n} numerical claims could not be traced to evidence{cites}. Treat those figures as unverified (listed in the appendix).",
        "invalid_citations": "; invalid citations: {ids}",
        "disclaimer": "Research and decision-support only — not investment advice. Prices are public reference/benchmark series published with a lag, not live tradable quotes.",
        "charts": "Charts", "source": "source", "appendix_a": "Appendix A — Evidence", "tool": "Tool", "citation": "Citation",
        "appendix_b": "Appendix B — Sources and freshness", "dataset": "Dataset", "obs_through": "Observations through",
        "ingested": "Ingested", "freshness": "Freshness", "basis": "Basis", "appendix_c": "Appendix C — Unverified figures",
        "value": "Value", "context": "Context",
        "deterministic_note": "",
    },
    "es": {
        "brand": "ATLAS · INVESTIGACIÓN", "question": "Pregunta", "generated": "Generado", "engine": "Motor", "report": "Informe",
        "verified": "Verificación superada: {ok} de {n} cifras se rastrearon hasta observaciones o cálculos almacenados; todas las citas son válidas.",
        "unverified": "Advertencia de verificación: {bad} de {n} cifras no se pudieron rastrear hasta la evidencia{cites}. Trátelas como no verificadas (listadas en el anexo).",
        "invalid_citations": "; citas no válidas: {ids}",
        "disclaimer": "Solo investigación y apoyo a decisiones; no es asesoramiento de inversión. Los precios son series públicas de referencia publicadas con rezago, no cotizaciones negociables en tiempo real.",
        "charts": "Gráficos", "source": "fuente", "appendix_a": "Anexo A — Evidencia", "tool": "Herramienta", "citation": "Cita",
        "appendix_b": "Anexo B — Fuentes y actualidad", "dataset": "Serie", "obs_through": "Observaciones hasta",
        "ingested": "Ingerido", "freshness": "Actualidad", "basis": "Base", "appendix_c": "Anexo C — Cifras no verificadas",
        "value": "Valor", "context": "Contexto",
        "deterministic_note": "*Este informe lo generó el motor determinista, que redacta solo en inglés. Con Claude configurado, el informe se redacta en español.*",
    },
    "pt": {
        "brand": "ATLAS · PESQUISA", "question": "Pergunta", "generated": "Gerado", "engine": "Motor", "report": "Relatório",
        "verified": "Verificação aprovada: {ok} de {n} números foram rastreados até observações ou cálculos armazenados; todas as citações são válidas.",
        "unverified": "Aviso de verificação: {bad} de {n} números não puderam ser rastreados até as evidências{cites}. Trate-os como não verificados (listados no anexo).",
        "invalid_citations": "; citações inválidas: {ids}",
        "disclaimer": "Apenas pesquisa e apoio à decisão; não é recomendação de investimento. Os preços são séries públicas de referência publicadas com defasagem, não cotações negociáveis em tempo real.",
        "charts": "Gráficos", "source": "fonte", "appendix_a": "Anexo A — Evidências", "tool": "Ferramenta", "citation": "Citação",
        "appendix_b": "Anexo B — Fontes e atualidade", "dataset": "Série", "obs_through": "Observações até",
        "ingested": "Ingerido", "freshness": "Atualidade", "basis": "Base", "appendix_c": "Anexo C — Números não verificados",
        "value": "Valor", "context": "Contexto",
        "deterministic_note": "*Este relatório foi gerado pelo motor determinístico, que escreve apenas em inglês. Com o Claude configurado, o relatório é redigido em português.*",
    },
}


def lang(code: str | None) -> str:
    return code if code in LANGUAGES else "en"


def labels(code: str | None) -> dict[str, str]:
    return LABELS[lang(code)]


def writing_instruction(code: str | None) -> str:
    """Extra instruction for Claude when the brief is not in English."""
    c = lang(code)
    if c == "en":
        return ""
    heads = "\n".join(f"## {h}" for h in SECTIONS[c])
    example = "14,81 y 1.234,5" if c == "es" else "14,81 e 1.234,5"
    return (f"Write the entire brief in {LANGUAGES[c]}, including the title, using these section headings instead of the English ones:\n"
            f"{heads}\nUse that language's number format: decimal comma and a dot for thousands (e.g. {example}); keep dates as "
            "YYYY-MM-DD, keep evidence ids like [E3] exactly as given, and keep dataset names as the tools return them.")
