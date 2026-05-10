{
  pkgs,
}:

pkgs.mkShell {
  name = "mokuro-bunko-dev";

  packages = with pkgs; [
    # Python toolchain
    python3
    uv

    # Development tools
    ruff
    mypy

    # Testing
    playwright-driver.browsers
  ];

  env = {
    PLAYWRIGHT_BROWSERS_PATH = pkgs.playwright-driver.browsers;
  };

  shellHook = ''
    echo "Mokuro Bunko development environment"
    echo "Python: $(python3 --version)"
    echo "uv: $(uv --version)"
    echo ""
    echo "Quick commands:"
    echo "  uv sync --extra dev    # Install all dependencies"
    echo "  uv run ruff check .    # Run linter"
    echo "  uv run mypy src        # Run type checker"
    echo "  uv run pytest          # Run tests"
    echo "  uv run mokuro-bunko serve  # Start development server"
  '';
}
