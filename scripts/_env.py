import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from order_flow.config import Settings, load_dotenv  # noqa: E402

load_dotenv(str(ROOT / ".env"))


def settings() -> Settings:
    return Settings.from_env()


def set_env_value(key: str, value: str, path: Path = ROOT / ".env") -> None:
    """Writes KEY=value into .env (git-ignored) without ever printing the value."""
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if not line.startswith(f"{key}=")] + [f"{key}={value}"]
    path.write_text("\n".join(lines) + "\n")
