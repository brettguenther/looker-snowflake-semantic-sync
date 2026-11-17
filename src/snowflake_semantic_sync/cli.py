"""CLI entrypoint for Snowflake Semantic View -> Looker LookML (sql_analytic_model_name) Synchronization."""

import os
from typing import Optional
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from snowflake_semantic_sync.orchestrator import SyncOrchestrator, load_sync_config

console = Console()


@click.group()
def main() -> None:
    """Snowflake Semantic View to Looker LookML (sql_analytic_model_name) Sync CLI."""
    pass


@main.command()
@click.option("--config", "-c", default="config/sync_config.yaml", help="Path to sync_config.yaml")
@click.option(
    "--auth-mode",
    type=click.Choice(["key_pair", "oauth", "oauth_client_credentials", "externalbrowser"], case_sensitive=False),
    default=None,
    help="Override Snowflake authentication mode",
)
@click.option("--database", "-d", default=None, help="Override Snowflake database name")
@click.option("--schema", "-s", default=None, help="Override Snowflake schema name")
@click.option(
    "--semantic-view",
    "-v",
    "semantic_views",
    multiple=True,
    help="Specific Snowflake Semantic View name(s) to sync (defaults to all in schema)",
)
@click.option(
    "--from-yaml",
    "-f",
    "from_yaml_files",
    multiple=True,
    help="Generate LookML from local Snowflake Semantic View YAML file(s) instead of live Snowflake query",
)
@click.option(
    "--deploy/--no-deploy",
    default=False,
    help="Deploy machine-managed base views and explores to Looker dev workspace and run LookML validation",
)
@click.option(
    "--looker-profile",
    default=None,
    help="Optional looker-cli profile name (from ~/.config/looker-cli/config.yaml) for Looker operations",
)
@click.option(
    "--looker-branch",
    default=None,
    help="Optional Looker dev Git branch to checkout before deploying (defaults to current dev workspace branch)",
)
@click.option("--pr", is_flag=True, default=False, help="Create or update a GitHub PR with machine-managed base changes")
@click.option("--git-repo", default=None, help="Override target GitHub repository slug (owner/repo) for --pr")
@click.option(
    "--scaffold",
    is_flag=True,
    default=False,
    help="Scaffold starter curated refinement views (+view) and model file locally if not present",
)
def sync(
    config: str,
    auth_mode: Optional[str],
    database: Optional[str],
    schema: Optional[str],
    semantic_views: tuple[str, ...],
    from_yaml_files: tuple[str, ...],
    deploy: bool,
    looker_profile: Optional[str],
    looker_branch: Optional[str],
    pr: bool,
    git_repo: Optional[str],
    scaffold: bool,
) -> None:
    """Synchronize Snowflake Semantic Views to Looker LookML Analytic Model views (sql_analytic_model_name)."""
    overrides = {}
    if auth_mode:
        overrides["snowflake_auth_mode"] = auth_mode.lower()
    if database:
        overrides["snowflake_database"] = database
    if schema:
        overrides["snowflake_schema"] = schema
    if semantic_views:
        overrides["semantic_views"] = list(semantic_views)
    if looker_profile:
        overrides["looker_profile"] = looker_profile
    if looker_branch:
        overrides["looker_dev_branch"] = looker_branch
    if git_repo:
        overrides["git_repo"] = git_repo

    source_desc = f"Local YAML ({len(from_yaml_files)} file(s))" if from_yaml_files else "Live Snowflake Connection"
    console.print(
        Panel(
            f"[bold cyan]Snowflake Semantic View → LookML (sql_analytic_model_name) Sync[/bold cyan]\n"
            f"Config: {config} | Source: {source_desc} | Deploy to Looker: {deploy} | PR: {pr} | Scaffold: {scaffold}",
            border_style="cyan",
        )
    )

    try:
        orchestrator = SyncOrchestrator(config, **overrides)
        with console.status("[bold green]Extracting Semantic Views and generating LookML...[/bold green]"):
            results = orchestrator.run(
                deploy=deploy,
                create_pr=pr,
                scaffold=scaffold,
                from_yaml_files=list(from_yaml_files) if from_yaml_files else None,
            )

        table = Table(title="Sync Execution Summary", border_style="green")
        table.add_column("Metric / Stage", style="bold white")
        table.add_column("Result", style="green")

        table.add_row("Semantic Views Processed", str(results["semantic_views_processed"]))
        table.add_row("Analytic Model Views Generated", ", ".join(results["views_generated"]))
        if results.get("explores_generated"):
            table.add_row("Base Explores Generated", ", ".join(results["explores_generated"]))
        table.add_row(
            "Semantic Diff Status",
            f"{len(results['files_changed'])} changed, {len(results['files_unchanged'])} unchanged",
        )
        table.add_row("Local Files Managed", str(len(results["files_written"])))
        table.add_row("Looker Deployed (Dev Workspace)", "Yes" if results["looker_deployed"] else "No (skipped)")

        if results["looker_deployed"]:
            val_status = "[bold green]PASS[/bold green]" if results["validation_valid"] else "[bold red]FAIL[/bold red]"
            table.add_row("LookML Validation", val_status)
            if results["validation_message"]:
                table.add_row("Validation Output", results["validation_message"])

        if results.get("pr_result"):
            pr_res = results["pr_result"]
            if pr_res.get("pr_created") or pr_res.get("pr_updated"):
                action_str = "Updated" if pr_res.get("pr_updated") else "Created"
                table.add_row(f"GitHub PR ({action_str})", f"[bold green]{pr_res['pr_url']}[/bold green]")
                table.add_row("PR Branch", pr_res.get("branch", ""))
            else:
                table.add_row("GitHub PR", pr_res.get("message", "No PR created"))

        if results.get("errors"):
            table.add_row("Warnings / Skipped", "\n".join(results["errors"]), style="yellow")

        console.print(table)
    except Exception as e:
        console.print(f"[bold red]Sync Failed:[/bold red] {e}")
        raise click.Abort() from e


@main.command("list-views")
@click.option("--config", "-c", default="config/sync_config.yaml", help="Path to sync_config.yaml")
@click.option(
    "--auth-mode",
    type=click.Choice(["key_pair", "oauth", "oauth_client_credentials", "externalbrowser"], case_sensitive=False),
    default=None,
    help="Override Snowflake authentication mode",
)
@click.option("--database", "-d", default=None, help="Override Snowflake database name")
@click.option("--schema", "-s", default=None, help="Override Snowflake schema name")
def list_views(
    config: str,
    auth_mode: Optional[str],
    database: Optional[str],
    schema: Optional[str],
) -> None:
    """List available Snowflake Semantic Views in the configured database and schema."""
    overrides = {}
    if auth_mode:
        overrides["snowflake_auth_mode"] = auth_mode.lower()
    if database:
        overrides["snowflake_database"] = database
    if schema:
        overrides["snowflake_schema"] = schema

    try:
        orchestrator = SyncOrchestrator(config, **overrides)
        cfg = orchestrator.config
        if not cfg.snowflake_database:
            raise ValueError("SNOWFLAKE_DATABASE is required.")
        with console.status(
            f"[bold green]Querying SHOW SEMANTIC VIEWS IN SCHEMA {cfg.snowflake_database}.{cfg.snowflake_schema}...[/bold green]"
        ):
            views = orchestrator.snowflake_client.list_semantic_views(
                database=cfg.snowflake_database,
                schema=cfg.snowflake_schema,
            )
        table = Table(
            title=f"Snowflake Semantic Views in {cfg.snowflake_database}.{cfg.snowflake_schema}",
            border_style="cyan",
        )
        table.add_column("#", style="dim")
        table.add_column("Semantic View Name", style="bold white")
        for idx, v in enumerate(views, start=1):
            table.add_row(str(idx), v)
        console.print(table)
    except Exception as e:
        console.print(f"[bold red]Failed to list Snowflake Semantic Views:[/bold red] {e}")
        raise click.Abort() from e


@main.command()
@click.option("--project", "-p", default=lambda: os.environ.get("LOOKER_PROJECT_ID", ""), help="Looker project ID")
@click.option("--looker-profile", default=None, help="Optional looker-cli profile name")
def validate(project: str, looker_profile: Optional[str]) -> None:
    """Run LookML validation against a Looker project using Looker SDK or looker-cli."""
    cfg = load_sync_config("config/sync_config.yaml")
    if not project:
        project = cfg.looker_project_id
    profile = looker_profile or cfg.looker_profile
    if not project:
        console.print("[bold red]Error:[/bold red] Please provide --project or set LOOKER_PROJECT_ID.")
        raise click.Abort()

    from snowflake_semantic_sync.clients.looker import LookerClient
    client = LookerClient(profile=profile)
    with console.status(f"[bold green]Validating Looker project '{project}'...[/bold green]"):
        is_valid, msg = client.validate_project(project)
    if is_valid:
        console.print(f"[bold green]✓ Project '{project}' is valid![/bold green]")
    else:
        console.print(f"[bold red]✗ Project '{project}' validation failed:[/bold red]\n{msg}")


if __name__ == "__main__":
    main()
