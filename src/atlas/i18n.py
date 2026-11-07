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
        "brand": "AMARU · RESEARCH", "question": "Question", "generated": "Generated", "engine": "Engine", "report": "Report",
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
        "brand": "AMARU · INVESTIGACIÓN", "question": "Pregunta", "generated": "Generado", "engine": "Motor", "report": "Informe",
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
        "brand": "AMARU · PESQUISA", "question": "Pergunta", "generated": "Gerado", "engine": "Motor", "report": "Relatório",
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


# Newsletter emails and public pages ({name} is the newsletter's name).
NEWSLETTER: dict[str, dict[str, str]] = {
    "en": {
        "confirm_subject": "Confirm your subscription to {name}",
        "confirm_body": "Someone (hopefully you) asked to receive {name} at this address.\n\nConfirm: {link}\n\n"
                        "If this wasn't you, ignore this email and you won't hear from us. The link is valid for 7 days.",
        "why": "You receive {name} because you subscribed and confirmed your address.",
        "unsubscribe": "Unsubscribe", "read_online": "Read online", "evidence": "Evidence", "sources": "Sources",
        "disclaimer": "Based on public reference data published with a lag; not a live quote. General research, not personalised investment advice.",
        "confirmed": "You're subscribed to {name}. Thank you!",
        "confirm_invalid": "This confirmation link is not valid or has expired. Subscribe again to get a new one.",
        "unsubscribed": "You won't receive {name} any more.", "unsubscribe_invalid": "This unsubscribe link is not valid.",
        "subscribe": "Subscribe", "email_placeholder": "you@example.com",
        "check_inbox": "Check your inbox: we sent a link to confirm your subscription.",
        "free_note": "Unsubscribe with one click from any email. General research based on public data, not personalised investment advice.",
        "past_issues": "Past issues", "published_with": "Published with Amaru", "not_found": "This newsletter does not exist.",
        "issue_not_found": "This issue is not available.", "subscribe_to": "Subscribe to {name}",
    },
    "es": {
        "confirm_subject": "Confirma tu suscripción a {name}",
        "confirm_body": "Alguien (esperamos que tú) pidió recibir {name} en esta dirección.\n\nConfirmar: {link}\n\n"
                        "Si no fuiste tú, ignora este correo y no volverás a saber de nosotros. El enlace es válido por 7 días.",
        "why": "Recibes {name} porque te suscribiste y confirmaste tu dirección.",
        "unsubscribe": "Cancelar suscripción", "read_online": "Leer en línea", "evidence": "Evidencia", "sources": "Fuentes",
        "disclaimer": "Basado en datos públicos de referencia publicados con rezago; no es una cotización en tiempo real. Investigación general, no asesoramiento de inversión personalizado.",
        "confirmed": "Te suscribiste a {name}. ¡Gracias!",
        "confirm_invalid": "Este enlace de confirmación no es válido o venció. Suscríbete de nuevo para recibir otro.",
        "unsubscribed": "Ya no recibirás {name}.", "unsubscribe_invalid": "Este enlace para cancelar la suscripción no es válido.",
        "subscribe": "Suscribirme", "email_placeholder": "tu@ejemplo.com",
        "check_inbox": "Revisa tu correo: te enviamos un enlace para confirmar tu suscripción.",
        "free_note": "Cancela con un clic desde cualquier correo. Investigación general basada en datos públicos, no asesoramiento de inversión personalizado.",
        "past_issues": "Ediciones anteriores", "published_with": "Publicado con Amaru", "not_found": "Este boletín no existe.",
        "issue_not_found": "Esta edición no está disponible.", "subscribe_to": "Suscríbete a {name}",
    },
    "pt": {
        "confirm_subject": "Confirme sua inscrição em {name}",
        "confirm_body": "Alguém (esperamos que você) pediu para receber {name} neste endereço.\n\nConfirmar: {link}\n\n"
                        "Se não foi você, ignore este e-mail e não entraremos em contato. O link é válido por 7 dias.",
        "why": "Você recebe {name} porque se inscreveu e confirmou seu endereço.",
        "unsubscribe": "Cancelar inscrição", "read_online": "Ler online", "evidence": "Evidências", "sources": "Fontes",
        "disclaimer": "Baseado em dados públicos de referência publicados com defasagem; não é uma cotação em tempo real. Pesquisa geral, não recomendação de investimento personalizada.",
        "confirmed": "Sua inscrição em {name} está confirmada. Obrigado!",
        "confirm_invalid": "Este link de confirmação é inválido ou expirou. Inscreva-se novamente para receber outro.",
        "unsubscribed": "Você não receberá mais {name}.", "unsubscribe_invalid": "Este link de cancelamento é inválido.",
        "subscribe": "Inscrever-me", "email_placeholder": "voce@exemplo.com",
        "check_inbox": "Verifique sua caixa de entrada: enviamos um link para confirmar sua inscrição.",
        "free_note": "Cancele com um clique em qualquer e-mail. Pesquisa geral baseada em dados públicos, não recomendação de investimento personalizada.",
        "past_issues": "Edições anteriores", "published_with": "Publicado com Amaru", "not_found": "Esta newsletter não existe.",
        "issue_not_found": "Esta edição não está disponível.", "subscribe_to": "Inscreva-se em {name}",
    },
}


def newsletter_text(code: str | None, key: str, **kw: str) -> str:
    return NEWSLETTER[lang(code)][key].format(**kw)
