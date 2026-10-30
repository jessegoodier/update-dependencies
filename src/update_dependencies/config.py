"""Validated configuration shared by the CLI and GitHub Action."""

import tomllib
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

ECOSYSTEMS = ("python", "actions", "docker", "hooks")


@dataclass(frozen=True)
class Config:
    schema_version: int = 1
    ecosystems: tuple[str, ...] = ECOSYSTEMS
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    allow_major: bool = True
    min_age_hours: int = 48
    stale_days: int = 180
    dry_run: bool = False
    check: bool = False
    timeout: int = 120
    retries: int = 2
    progress: str = "auto"
    python_inline_scripts: bool = True
    python_add_minimums: bool = False
    python_sync: bool = False
    python_update_build_backend: bool = False
    docker_pin_latest: bool = False

    def validate(self) -> Config:
        defaults = Config()
        for item in fields(self):
            value = getattr(self, item.name)
            expected = type(getattr(defaults, item.name))
            if type(value) is not expected:
                raise ValueError(f"{item.name.replace('_', '-')} must be {expected.__name__}")
            if expected is tuple and any(not isinstance(v, str) for v in value):
                raise ValueError(f"{item.name} must contain strings")
        if self.progress not in {"auto", "plain", "off"}:
            raise ValueError("progress must be auto, plain, or off")
        if self.schema_version != 1:
            raise ValueError("Only schema-version = 1 is supported")
        if not self.ecosystems or set(self.ecosystems) - set(ECOSYSTEMS):
            raise ValueError(f"ecosystems must be a nonempty selection from {ECOSYSTEMS}")
        if min(self.min_age_hours, self.stale_days, self.retries) < 0 or self.timeout < 1:
            raise ValueError("Ages and retries must be >= 0; timeout must be >= 1")
        return self

    def overlay(self, values: dict) -> Config:
        return replace(self, **{k: v for k, v in values.items() if v is not None}).validate()

    def to_dict(self) -> dict:
        return asdict(self)


SECTIONS = {
    "python": {
        "inline-scripts": "python_inline_scripts",
        "add-missing-minimums": "python_add_minimums",
        "sync": "python_sync",
        "update-build-backend": "python_update_build_backend",
    },
    "docker": {"pin-latest": "docker_pin_latest"},
}


def load_config(directory: Path, explicit: Path | None = None, disabled: bool = False) -> Config:
    if disabled:
        return Config()
    path = explicit
    if path is not None:
        path = path if path.is_absolute() else directory / path
    elif (directory / "update-dependencies.toml").is_file():
        path = directory / "update-dependencies.toml"
    elif (directory / "pyproject.toml").is_file():
        path = directory / "pyproject.toml"
    else:
        return Config()
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if path.name == "pyproject.toml":
        data = data.get("tool", {}).get("update-dependencies", {})
    if not isinstance(data, dict):
        raise TypeError("Expected a configuration TOML table")
    values = {}
    top_level = {
        f.name.replace("_", "-"): f.name
        for f in fields(Config)
        if not f.name.startswith(("python_", "docker_"))
    }
    for key, value in data.items():
        if key in SECTIONS:
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be a table")
            for option, setting in value.items():
                if option not in SECTIONS[key]:
                    raise ValueError(f"Unknown configuration option: {key}.{option}")
                values[SECTIONS[key][option]] = setting
        elif key in top_level:
            name = top_level[key]
            if name in {"ecosystems", "include", "exclude"} and isinstance(value, list):
                value = tuple(value)
            values[name] = value
        else:
            raise ValueError(f"Unknown configuration option: {key}")
    return Config().overlay(values)
