"""Scout command line."""

from __future__ import annotations

import csv as csv_module
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from scout.agent.prompts import FIELD_PRESETS, resolve_fields
from dataclasses import replace

from scout.config import VALID_DEPTHS, load_settings
from scout.db.session import init_db, schema_is_current
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
    ok, message = schema_is_current(settings)
    if not ok:
        console.print(f"\n[yellow]Schema out of date:[/yellow] {message}")


@app.command()
def config() -> None:
    """Show the resolved configuration."""
    settings = load_settings()
    table = Table(show_header=False, box=None)
    table.add_row("engine", settings.engine)
    table.add_row(
        "depth",
        f"{settings.depth} "
        f"({settings.depth_profile.max_branches} branches, "
        f"{settings.depth_profile.max_children} follow-ups)",
    )
    table.add_row("model", settings.model)
    table.add_row("synthesis model", settings.synthesis_model)
    table.add_row("effort", settings.effort)
    table.add_row("default fields", ", ".join(FIELD_PRESETS["core"]))
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
    depth: str = typer.Option(
        None,
        "--depth",
        help=(
            "shallow | standard | deep. How many separate branches to research. "
            "Depth is branches, not a bigger budget for one conversation."
        ),
    ),
    fields: str = typer.Option(
        None,
        "--fields",
        help=(
            "Fields to populate: a preset (core, full) or a comma-separated list. "
            "Defaults to core -- let --objectives drive anything beyond it."
        ),
    ),
    icp: str = typer.Option(
        None, "--icp", help="Ideal customer profile as a JSON object."
    ),
    label: str = typer.Option(None, "--label", help="A name for this run."),
) -> None:
    """Research one or more companies."""
    from scout.agent.loop import create_run, finish_run, research_company

    settings = load_settings()
    if depth is not None:
        if depth not in VALID_DEPTHS:
            console.print(
                f"[red]--depth must be one of {', '.join(VALID_DEPTHS)}.[/red]"
            )
            raise typer.Exit(code=2)
        settings = replace(settings, depth=depth)
    init_db(settings)

    # Checked before anything is spent: a stale schema otherwise surfaces as a missing
    # column partway through a paid run.
    ok, message = schema_is_current(settings)
    if not ok:
        console.print(f"[bold red]Database schema out of date.[/bold red]\n{message}")
        raise typer.Exit(code=2)

    targets: list[tuple[str, str | None]] = [(d, None) for d in (domain or [])]
    if csv_path is not None:
        targets.extend(_read_csv(csv_path))
    if not targets:
        console.print("[red]Give at least one --domain or a --csv file.[/red]")
        raise typer.Exit(code=2)

    requested_fields = resolve_fields(fields)
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
        f"{settings.engine} engine, {settings.depth} depth, "
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
            on_branch=lambda phase, branch, depth: (
                console.print(
                    f"    {'  ' * (depth - 1)}[dim]-> {branch.name.value}: "
                    f"{branch.question[:70]}[/dim]"
                )
                if phase == "start"
                else None
            ),
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
            merged = result.merge
            if merged is not None:
                ms = merged.stats()
                rate = ms.get("corroboration_rate")
                console.print(
                    f"  [dim]{ms['corroborated']} corroborated, "
                    f"{ms['single_sourced']} single-sourced"
                    + (f", {rate:.0%} of facts" if rate is not None else "")
                    + "[/dim]"
                )
                for conflict in merged.conflicts:
                    console.print(f"  [yellow]conflict[/yellow] {conflict.describe()}")

            usage = result.usage or {}
            research = usage.get("research_cost_usd")
            synth = usage.get("synthesis_cost_usd")
            if research is not None and synth is not None:
                console.print(
                    f"  [dim]{usage.get('branch_count', 0)} branches · "
                    f"{usage.get('turns', 0)} turns · "
                    f"research ${research:.3f} · synthesis ${synth:.3f} · "
                    f"{usage.get('web_searches', 0)} web searches[/dim]"
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
def verify(
    run: int = typer.Option(..., "--run", help="Run id to verify."),
    show_all: bool = typer.Option(
        False, "--all", help="List every quote, not just the ones that failed."
    ),
) -> None:
    """Check each recorded quote against the page it was taken from.

    Uses no model and costs nothing: it reopens the cited pages in the browser and
    matches the text. Run it after any research run.
    """
    from scout.verify import Verdict, verify_run

    settings = load_settings()
    console.print(f"Verifying run {run} — reopening each cited page...\n")

    report = verify_run(
        settings,
        run,
        on_page=lambda url, ok: console.print(
            f"  [{'green' if ok else 'red'}]{'read' if ok else 'failed'}[/] {url}"
        ),
    )

    if not report.checks:
        console.print(
            f"\n[yellow]No quotes to check in run {run}.[/yellow] "
            "Either it recorded nothing, or every finding was an explicit unknown."
        )
        return

    counts = report.counts()
    failed = [
        c
        for c in report.checks
        if c.verdict in (Verdict.NOT_FOUND, Verdict.NO_QUOTE)
    ]

    rows = report.checks if show_all else failed
    if rows:
        table = Table("domain", "field", "verdict", "match", "quote", "source")
        for c in rows:
            colour = {
                Verdict.VERBATIM: "green",
                Verdict.PARAPHRASE: "yellow",
                Verdict.NOT_FOUND: "red",
                Verdict.NO_QUOTE: "red",
                Verdict.UNREACHABLE: "dim",
            }[c.verdict]
            table.add_row(
                c.domain,
                c.field,
                f"[{colour}]{c.verdict.value}[/{colour}]",
                f"{c.ratio:.0%}",
                (c.quote or "(none)")[:60],
                (c.source_url or "")[:50],
            )
        console.print()
        console.print(table)

    console.print()
    console.print(
        f"verbatim {counts['verbatim']} · paraphrase {counts['paraphrase']} · "
        f"not found [bold red]{counts['not_found']}[/bold red] · "
        f"no quote {counts['no_quote']} · unreachable {counts['unreachable']}"
    )
    if report.accuracy is not None:
        colour = (
            "green"
            if report.accuracy >= 0.95
            else "yellow"
            if report.accuracy >= 0.8
            else "red"
        )
        console.print(
            f"extraction accuracy: [{colour}]{report.accuracy:.0%}[/{colour}] "
            f"of checkable quotes found on their cited page"
        )
    if counts["unreachable"]:
        console.print(
            "[dim]Unreachable pages are not counted either way — a page that will "
            "not load now is no evidence about the quote.[/dim]"
        )
    if counts["not_found"]:
        console.print(
            "[dim]A quote can also read as not found when it came from content "
            "behind a click, which reloading the URL cannot reproduce. Check the "
            "screenshot for those before treating them as errors.[/dim]"
        )


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

    _print_branch_tree(settings, company_run)
    _print_assessment(settings, company_run)


def _print_branch_tree(settings, company_run_id: int) -> None:
    """How the research was split up, and what each part cost."""
    from scout.agent.branches import branch_rows

    rows = branch_rows(settings, company_run_id)
    if not rows:
        return

    console.print("\n[bold]Branches[/bold]")
    by_parent: dict = {}
    for row in rows:
        by_parent.setdefault(row["parent_branch_id"], []).append(row)

    def render(parent, indent: int) -> None:
        for row in by_parent.get(parent, []):
            colour = {"done": "green", "failed": "red", "blocked": "yellow"}.get(
                row["status"], "white"
            )
            cost = f"${row['cost_usd']:.3f}" if row["cost_usd"] is not None else "-"
            secs = f"{row['duration_s']:.0f}s" if row["duration_s"] else "-"
            console.print(
                f"{'  ' * indent}[{colour}]{row['name']}[/{colour}] "
                f"[dim]{cost} · {secs}[/dim]"
            )
            if row["question"]:
                console.print(f"{'  ' * indent}  [dim]{row['question'][:88]}[/dim]")
            if row["error"]:
                console.print(f"{'  ' * indent}  [red]{row['error'][:88]}[/red]")
            render(row["id"], indent + 1)

    render(None, 1)


def _print_assessment(settings, company_run_id: int) -> None:
    """What is corroborated, what rests on one source, and what disagrees."""
    from scout.agent.loop import read_findings
    from scout.agent.merge import merge_findings
    from scout.agent.sources import tier_name

    rows = read_findings(settings, company_run_id)
    if not rows:
        return
    merged = merge_findings(rows)
    stats = merged.stats()

    console.print("\n[bold]Assessment[/bold]")
    rate = stats.get("corroboration_rate")
    console.print(
        f"  {stats['corroborated']} corroborated · "
        f"{stats['single_sourced']} single-sourced"
        + (f" · {rate:.0%} of facts corroborated" if rate is not None else "")
    )

    if merged.conflicts:
        console.print("\n[bold yellow]Sources disagree[/bold yellow]")
        for conflict in merged.conflicts:
            console.print(f"  {conflict.describe()}")

    best = [c for c in merged.corroborated][:8]
    if best:
        table = Table("field", "value", "sources", "strongest")
        for claim in best:
            table.add_row(
                claim.field,
                str(claim.value)[:50],
                str(claim.source_count),
                tier_name(claim.best_tier),
            )
        console.print("\n[bold]Best-supported claims[/bold]")
        console.print(table)


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
    table = Table(
        "domain",
        "status",
        "facts",
        "inf",
        "unk",
        "coverage",
        "time",
        "turns",
        "research",
        "synth",
        "cost",
    )
    for r in results:
        stats = r.stats or {}
        usage = r.usage or {}
        coverage = stats.get("evidence_coverage")

        def money(value) -> str:
            return f"${value:.3f}" if value is not None else "-"

        table.add_row(
            r.domain,
            r.status,
            str(stats.get("facts", 0)),
            str(stats.get("inferences", 0)),
            str(stats.get("unknowns", 0)),
            f"{coverage:.0%}" if coverage is not None else "-",
            f"{r.duration_s:.0f}s" if r.duration_s else "-",
            str(usage.get("turns", "-")),
            money(usage.get("research_cost_usd")),
            money(usage.get("synthesis_cost_usd")),
            money(r.cost_usd),
        )
    console.print()
    console.print(table)


if __name__ == "__main__":
    app()
