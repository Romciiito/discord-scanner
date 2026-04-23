import typer
from typing import Optional
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="discord-scanner",
    help="Stage 2 — burner-token Discord scanner producing raw JSONL dumps for Stage 3 curator. Rebuild of dsc-smartscraper.",
    add_completion=False,
)
console = Console()


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output"),
) -> None:
    """Stage 2 — burner-token Discord scanner producing raw JSONL dumps for Stage 3 curator. Rebuild of dsc-smartscraper."""
    if verbose:
        import os
        os.environ["APP_LOG_LEVEL"] = "debug"


@app.command()
def run(
    input_file: Optional[str] = typer.Argument(None, help="Input file path"),
    output: str = typer.Option("stdout", "--output", "-o", help="Output destination"),
) -> None:
    """Run the main command."""
    console.print(f"Running with input={input_file}, output={output}")
    # TODO: implement


@app.command()
def version() -> None:
    """Print the version and exit."""
    from importlib.metadata import version as pkg_version
    try:
        v = pkg_version("discord-scanner")
    except Exception:
        v = "0.1.0-dev"
    console.print(v)


if __name__ == "__main__":
    app()