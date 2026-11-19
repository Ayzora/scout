"""Scout command line."""

from __future__ import annotations

import csv as csv_module
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from scout.config import load_settings
from scout.db.session import init_db
from scout.export import export_run_csv

app = typer.Typer(
    add_completion=False,
    help="Autonomous web research and lead enrichment agent.",
    no_args_is_help=True,
)
console = Console()


def _client():
    """Build the API client, with a clear message if no key is configured."""
    from anthropic import Anthropic

    settings = load_settings()
    if settings.api_key() is None:
        console.print(
            "[bold red]No Anthropic API key found.[/bold red]\n\n"
            "Scout uses your own key and you pay your own usage. Set it with:\n"
            "  cp .env.example .env    then edit ANTHROPIC_API_KEY\n"
            "or export it:\n"
            "  export ANTHROPIC_API_KEY=sk-ant-...\n"
        )
        raise typer.Exit(code=2)
    return Anthropic(api_key=settings.api_key())


@app.command()
def init() -> None:
    """Create the local database and data directories."""
    settings = load_settings()
    path = init_db(settings)
    console.print(f"[green]Database ready[/green] at {path}")
    console.print(f"Screenshots will be written to {settings.screenshot_dir}")


@app.command()
def config() -> None:
    """Show the resolved configuration."""
    settings = load_settings()
    table = Table(show_header=False, box=None)
    table.add_row("model", settings.model)
    table.add_row("effort", settings.effort)
    table.add_row("task budget (tokens)", str(settings.task_budget_tokens))
    table.add_row("max steps per company", str(settings.max_steps))
    table.add_row("headed browser", str(settings.headed))
    table.add_row("context editing", str(settings.context_editing))
    table.add_row("page timeout (ms)", str(settings.page_timeout_ms))
    table.add_row("data dir", str(settings.data_dir))
    table.add_row(
        "API key", "set" if settings.api_key() else "[red]not set[/red]"
    )
    console.print(table)


@app.command()
def research(
    domain: list[str] = typer.Option(
        None, "--domain", "-d", help="Company domain. Repeatable."
    ),
    csv_path: Path = typer.Option(
        None, "--csv", help="CSV with a 'domain' column (optional 'name')."
    ),
    objectives: str = typer.Option(
        None, "--objectives", "-o", help="What you want to learn about each company."
    ),
    fields: str = typer.Option(
        None, "--fields", help="Comma-separated list of fields to populate."
    ),
    icp: str = typer.Option(
        None, "--icp", help="Ideal customer profile as a JSON object."
    ),
    label: str = typer.Option(None, "--label", help="A name for this run."),
) -> None:
    """Research one or more companies."""
    from scout.agent.loop import create_run, finish_run, research_company

    settings = load_settings()
    init_db(settings)

    targets: list[tuple[str, str | None]] = [(d, None) for d in (domain or [])]
    if csv_path is not None:
        targets.extend(_read_csv(csv_path))
    if not targets:
        console.print("[red]Give at least one --domain or a --csv file.[/red]")
        raise typer.Exit(code=2)

    requested_fields = (
        [f.strip() for f in fields.split(",") if f.strip()] if fields else None
    )
    icp_dict = json.loads(icp) if icp else None

    client = _client()
    run_id = create_run(
        settings,
        label=label,
        objectives=objectives,
        requested_fields=requested_fields,
        icp=icp_dict,
    )
    console.print(
        f"[bold]Run {run_id}[/bold] — {len(targets)} companies, "
        f"model {settings.model} at effort {settings.effort}\n"
    )

    results = []
    for index, (dom, name) in enumerate(targets, start=1):
        console.print(f"[cyan]({index}/{len(targets)}) {dom}[/cyan]")
        result = research_company(
            client=client,
            settings=settings,
            run_id=run_id,
            domain=dom,
            name=name,
            objectives=objectives,
            requested_fields=requested_fields,
            icp=icp_dict,
        )
        results.append(result)
        if result.error:
            console.print(f"  [red]{result.error}[/red]")
        else:
            stats = result.stats
            console.print(
                f"  {stats.get('facts', 0)} facts, "
                f"{stats.get('inferences', 0)} inferences, "
                f"{stats.get('unknowns', 0)} unknowns in "
                f"{result.duration_s:.0f}s"
                + (
                    f", ${result.cost_usd:.3f}"
                    if result.cost_usd is not None
                    else ""
                )
            )

    finish_run(settings, run_id)
    _print_summary(results)
    console.print(
        f"\nExport with: [bold]scout export --run {run_id} -o results.csv[/bold]"
    )


@app.command()
def export(
    run: int = typer.Option(..., "--run", help="Run id to export."),
    out: Path = typer.Option(Path("results.csv"), "--out", "-o"),
) -> None:
    """Export a run's results to CSV."""
    settings = load_settings()
    rows = export_run_csv(settings, run, out)
    console.print(f"[green]Wrote {rows} rows[/green] to {out}")


@app.command()
def show(
    company_run: int = typer.Option(..., "--company-run", help="CompanyRun id."),
) -> None:
    """Print one company's findings with their sources."""
    from scout.db import models as m
    from scout.db.session import session_scope

    settings = load_settings()
    with session_scope(settings) as s:
        cr = s.get(m.CompanyRun, company_run)
        if cr is None:
            console.print(f"[red]No company run {company_run}.[/red]")
            raise typer.Exit(code=1)

        console.print(f"\n[bold]{cr.company.name or cr.company.domain}[/bold]")
        if cr.summary:
            console.print(cr.summary + "\n")

        table = Table("field", "kind", "value", "source")
        for f in cr.findings:
            table.add_row(
                f.field,
                f.kind,
                (str(f.value) if f.value is not None else f.reason or "")[:60],
                (f.evidence[0].source_url if f.evidence else ""),
            )
        console.print(table)

        if cr.people:
            people = Table("name", "role", "contact", "source")
            for p in cr.people:
                people.add_row(
                    p.name,
                    p.role or "",
                    json.dumps(p.contact) if p.contact else "",
                    p.evidence[0].source_url if p.evidence else "",
                )
            console.print(people)


def _read_csv(path: Path) -> list[tuple[str, str | None]]:
    if not path.exists():
        console.print(f"[red]No such file: {path}[/red]")
        raise typer.Exit(code=2)
    out: list[tuple[str, str | None]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv_module.DictReader(handle)
        if reader.fieldnames is None or "domain" not in reader.fieldnames:
            console.print(
                f"[red]{path} needs a 'domain' column. Found: "
                f"{reader.fieldnames}[/red]"
            )
            raise typer.Exit(code=2)
        for row in reader:
            dom = (row.get("domain") or "").strip()
            if dom:
                out.append((dom, (row.get("name") or "").strip() or None))
    return out


def _print_summary(results) -> None:
    table = Table("domain", "status", "facts", "inf", "unk", "coverage", "time", "cost")
    for r in results:
        stats = r.stats or {}
        coverage = stats.get("evidence_coverage")
        table.add_row(
            r.domain,
            r.status,
            str(stats.get("facts", 0)),
            str(stats.get("inferences", 0)),
            str(stats.get("unknowns", 0)),
            f"{coverage:.0%}" if coverage is not None else "-",
            f"{r.duration_s:.0f}s" if r.duration_s else "-",
            f"${r.cost_usd:.3f}" if r.cost_usd is not None else "-",
        )
    console.print()
    console.print(table)


if __name__ == "__main__":
    app()
