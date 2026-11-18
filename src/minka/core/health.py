"""health — `minka stats --salud` y los chequeos de salud de `minka doctor` (paso 12b, parte 2).

Lee output/metrics/runs y evals/response_feedback.jsonl de cada proyecto; nunca el modelo ni la red.
Columnas: la semana ISO del ancla (hoy o --semana), las 4 anteriores y la base (baseline_v3.json).
El n se muestra SIEMPRE; con menos de HEALTH_MIN_N por categoría la cifra se sustituye por "sin datos
suficientes" (con --crudo se muestra igual, con el aviso "muestra pequeña"). El doctor aplica los
umbrales HEALTH_* sobre los últimos 7 días contra los 7 anteriores y nunca alerta con poca muestra; las
ausencias refutadas son siempre informativas ("para revisar"). Un contador ausente en un registro antiguo
es "sin dato", no 0: ese registro queda fuera del n de esa categoría (`metrics.health_categories`).
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

from minka.core import metrics

DEFAULT_MIN_N = 20
DEFAULT_CACHE_MIN = 0.80
DEFAULT_CITATIONS_MISSING_MAX = 0.05
DEFAULT_COST_RISE_PCT = 0.25
WEEKS = 5
INSUFFICIENT = "sin datos suficientes"
SMALL_SAMPLE = "muestra pequeña"

# (clave en health_categories, etiqueta, formato)
CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("costo_mediano_info_direct/1", "costo mediano info_direct", "usd"),
    ("costo_mediano_llm/1", "costo mediano llm/1", "usd"),
    ("costo_mediano_llm/2+", "costo mediano llm/2+", "usd"),
    ("cache_mediana", "caché mediana", "pct"),
    ("citas_no_encontradas_pct", "% citas no encontradas", "pct"),
    ("citas_simbolo_lejano_o_ausente_pct", "% símbolo lejano/ausente (informativo)", "pct"),
    ("ausencias_refutadas_pct", "% ausencias refutadas", "pct"),
    ("interrumpidas_pct", "% interrumpidas", "pct"),
    ("vueltas_con_tope_pct", "% vueltas con tope", "pct"),
    ("bien_sobre_total", "/bien sobre /bien+/mal", "pct"),
    ("cierres_con_oferta_pct", "% cierres con oferta", "pct"),
)


def min_n(cfg) -> int:
    return int(getattr(cfg, "HEALTH_MIN_N", DEFAULT_MIN_N) or DEFAULT_MIN_N)


def iso_week_range(anchor: date, offset: int = 0) -> tuple[date, date]:
    """(lunes, domingo) de la semana ISO del ancla, desplazada `offset` semanas (negativo = antes)."""
    monday = anchor - timedelta(days=anchor.weekday()) + timedelta(weeks=offset)
    return monday, monday + timedelta(days=6)


def week_label(start: date) -> str:
    year, week, _ = start.isocalendar()
    return f"S{week:02d} {year}"


def _entry_date(entry: dict) -> date | None:
    try:
        return datetime.fromisoformat(str(entry.get("ts"))).date()
    except (TypeError, ValueError):
        return None


def read_response_feedback(cfg, start: date, end: date) -> list[dict]:
    """Entradas de /bien y /mal (verdict) de todos los proyectos con ts en [start, end]."""
    projects = Path(getattr(cfg, "PROJECTS_DIR", Path(getattr(cfg, "OUTPUT_DIR", ".")) / "no"))
    out: list[dict] = []
    if not projects.is_dir():
        return out
    for path in sorted(projects.glob("*/evals/response_feedback.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            day = _entry_date(entry)
            if day is not None and start <= day <= end:
                out.append({"verdict": entry.get("verdict")})
    return out


def categories_between(cfg, start: date, end: date) -> dict[str, dict]:
    records = metrics.records_between(start.isoformat(), end.isoformat(), cfg)
    return metrics.health_categories(records, read_response_feedback(cfg, start, end))


def load_baseline(cfg) -> dict | None:
    path = metrics.baseline_path(cfg)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data.get("categories"), dict) else None


def baseline_filter_matches(baseline: dict | None) -> bool | None:
    """True si la base se congeló con el mismo filtro de fuentes que las columnas semanales; False si difiere;
    None si la base no registró su filtro (anterior a 0.2.5) y por tanto no se puede saber."""
    if not baseline:
        return None
    recorded = baseline.get("source_filter")
    if not isinstance(recorded, dict):
        return None
    return recorded == metrics.SOURCE_FILTER


def health_table(cfg, anchor: date, weeks: int = WEEKS) -> dict:
    """{"columns": [{label, start, end, categories}], "baseline": {...}|None, "baseline_filter_ok": bool|None};
    la semana del ancla primero. Las columnas usan metrics.SOURCE_FILTER (ok, sin eval_quality)."""
    columns = []
    for offset in range(0, -weeks, -1):
        start, end = iso_week_range(anchor, offset)
        columns.append(
            {"label": week_label(start), "start": start.isoformat(), "end": end.isoformat(),
             "categories": categories_between(cfg, start, end)}
        )
    baseline = load_baseline(cfg)
    return {"columns": columns, "baseline": baseline, "baseline_filter_ok": baseline_filter_matches(baseline)}


def fmt_value(value: float | None, kind: str) -> str:
    if value is None:
        return "—"
    return f"${value:.2f}" if kind == "usd" else f"{value:.0%}"


def fmt_cell(cat: dict | None, kind: str, min_n_value: int, crudo: bool) -> str:
    """Cifra con su n. Bajo HEALTH_MIN_N: 'sin datos suficientes (n=K)'; con --crudo la cifra y el aviso."""
    if not cat:
        return "— (n=0)"
    n = int(cat.get("n") or 0)
    value = cat.get("value")
    if value is None:
        return f"— (n={n})"
    if n < min_n_value and not crudo:
        return f"{INSUFFICIENT} (n={n})"
    text = f"{fmt_value(value, kind)} (n={n})"
    return f"{text} · {SMALL_SAMPLE}" if n < min_n_value else text


def table_rows(table: dict, min_n_value: int, crudo: bool) -> tuple[list[str], list[list[str]]]:
    """(cabecera, filas) listas para imprimir: categoría, semanas (actual primero) y base."""
    header = ["categoría", *(c["label"] for c in table["columns"]), "base v3"]
    base = (table.get("baseline") or {}).get("categories") or {}
    rows: list[list[str]] = []
    for key, label, kind in CATEGORIES:
        cells = [fmt_cell(c["categories"].get(key), kind, min_n_value, crudo) for c in table["columns"]]
        cells.append(fmt_cell(base.get(key), kind, min_n_value, crudo) if base else "—")
        rows.append([label, *cells])
    runs = [str((c["categories"].get("corridas") or {}).get("n", 0)) for c in table["columns"]]
    runs.append(str((base.get("corridas") or {}).get("n", "—")) if base else "—")
    rows.append(["corridas (n)", *runs])
    return header, rows


# -- doctor --------------------------------------------------------------------------------------


def _median_cost(records: list[dict]) -> tuple[float | None, int]:
    costs = [
        float(r["cost_usd"]) for r in records
        if r.get("ok") and r.get("source") not in metrics.EXCLUDED_SOURCES and r.get("cost_usd") is not None
    ]
    return (round(statistics.median(costs), 4) if costs else None), len(costs)


def health_alerts(cfg, today: date | None = None) -> list[dict]:
    """Chequeos no bloqueantes para `doctor`: [{name, ok, detail, hint}]. Ventana: últimos 7 días
    (hoy incluido) contra los 7 anteriores para el costo. Con n < HEALTH_MIN_N: ok=True y
    "sin datos suficientes"; nunca alerta."""
    today = today or date.today()
    start, end = today - timedelta(days=6), today
    prev_start, prev_end = start - timedelta(days=7), start - timedelta(days=1)
    need = min_n(cfg)
    cats = categories_between(cfg, start, end)
    out: list[dict] = []

    def add(name: str, cat: dict | None, kind: str, threshold: float, above_is_bad: bool, hint: str, label: str):
        n = int((cat or {}).get("n") or 0)
        value = (cat or {}).get("value")
        if value is None or n < need:
            out.append({"name": name, "ok": True, "informative": False, "detail": f"{INSUFFICIENT} (n={n} < {need})", "hint": hint})
            return
        bad = value > threshold if above_is_bad else value < threshold
        out.append({
            "name": name, "ok": not bad, "informative": False, "hint": hint,
            "detail": f"{fmt_value(value, kind)} (n={n}); umbral {label} {fmt_value(threshold, kind)}, 7 días",
        })

    add("Caché 7 días", cats.get("cache_mediana"), "pct", float(getattr(cfg, "HEALTH_CACHE_MIN", DEFAULT_CACHE_MIN)),
        False, "Un prefijo que cambia (knowledge editado, contexto vivo grande) baja la lectura de caché; ver docs/OPERACION.md §2.",
        "mínimo")
    add("Citas no encontradas 7 días", cats.get("citas_no_encontradas_pct"), "pct",
        float(getattr(cfg, "HEALTH_CITATIONS_MISSING_MAX", DEFAULT_CITATIONS_MISSING_MAX)), True,
        "Revisa `minka stats --salud`; las citas marcadas ⚠ no existe crecieron.", "máximo")
    # Una ausencia refutada es "para revisar", no un error: siempre informativa, nunca alerta ni umbral.
    abs_cat = cats.get("ausencias_refutadas_pct") or {}
    abs_n, abs_value = int(abs_cat.get("n") or 0), abs_cat.get("value")
    out.append({
        "name": "Ausencias refutadas 7 días", "ok": True, "informative": True,
        "detail": (f"{INSUFFICIENT} (n={abs_n} < {need})" if abs_value is None or abs_n < need
                   else f"{fmt_value(abs_value, 'pct')} (n={abs_n}) · para revisar, no es error"),
        "hint": "Nombres negados que sí existen en el repo; revísalos con `minka stats --salud --crudo`.",
    })

    rise = float(getattr(cfg, "HEALTH_COST_RISE_PCT", DEFAULT_COST_RISE_PCT))
    cur, n_cur = _median_cost(metrics.records_between(start.isoformat(), end.isoformat(), cfg))
    prev, n_prev = _median_cost(metrics.records_between(prev_start.isoformat(), prev_end.isoformat(), cfg))
    hint = "Mira rutas del router y prompt_hash en `minka stats`; ver docs/OPERACION.md §2."
    if cur is None or prev is None or n_cur < need or n_prev < need:
        out.append({"name": "Costo mediano 7 días", "ok": True, "informative": False, "hint": hint,
                    "detail": f"{INSUFFICIENT} (n={n_cur} y {n_prev} < {need})"})
    else:
        pct = (cur - prev) / prev if prev else 0.0
        out.append({
            "name": "Costo mediano 7 días", "ok": not (prev > 0 and pct > rise), "informative": False, "hint": hint,
            "detail": f"${cur:.2f} (n={n_cur}) contra ${prev:.2f} (n={n_prev}) de los 7 anteriores: {pct:+.0%}; umbral +{rise:.0%}",
        })
    return out
