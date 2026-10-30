"""Optional interactive configuration."""

import argparse
import sys

from .config import ECOSYSTEMS
from .engine import default_ecosystems, skip_reason


def _tui_cancelled() -> None:
    raise KeyboardInterrupt


# Focus is a quiet cyan caret; checked state is a green checkbox (not a lit row).
TUI_STYLE_RULES = [
    ("qmark", "fg:cyan bold"),
    ("question", "bold fg:white"),
    ("answer", "fg:green bold"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan"),
    ("selected", "fg:green bold"),
    ("separator", "fg:cyan"),
    ("instruction", "fg:#6c7086 italic"),
    ("text", "fg:#cdd6f4"),
    ("disabled", "fg:#6c7086 italic"),
    ("default", "fg:#a6adc8"),
    ("hint", "fg:#6c7086"),
    ("eco", "fg:cyan bold"),
    ("opt", "fg:yellow"),
]

ECOSYSTEM_HINTS = {
    "python": "pyproject.toml + uv.lock + PEP 723 scripts",
    "actions": ".github workflows → SHA pins",
    "docker": "Dockerfile FROM → digests",
    "hooks": "pre-commit autoupdate --freeze",
}


def _tui_checkbox(questionary, style, message: str, choices: list, **kwargs):
    """Checkbox prompt: ☑/☐ marks selection; pointer shows focus only."""
    return questionary.checkbox(
        message,
        choices=choices,
        style=style,
        qmark="❯",
        pointer="›",
        instruction="  space toggle · a all · enter confirm",
        **kwargs,
    )


def _tui_text(questionary, style, message: str, **kwargs):
    return questionary.text(
        message,
        style=style,
        qmark="❯",
        **kwargs,
    )


def prompt_tui(args: argparse.Namespace) -> argparse.Namespace:
    """Fill args from interactive prompts; CLI flags seed the defaults."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("--tui requires an interactive terminal")

    import questionary
    from questionary.prompts import common as qcommon

    # questionary defaults to ●/○ (radio look); use real checkboxes instead.
    qcommon.INDICATOR_SELECTED = "☑"
    qcommon.INDICATOR_UNSELECTED = "☐"
    style = questionary.Style(TUI_STYLE_RULES)

    defaults = set(default_ecosystems(args))
    eco_choices = []
    for ecosystem in ECOSYSTEMS:
        reason = skip_reason(
            ecosystem,
            update_inline_scripts=args.update_python_inline_script_metadata,
        )
        hint = ECOSYSTEM_HINTS[ecosystem]
        eco_choices.append(
            questionary.Choice(
                title=[
                    ("class:eco", f"{ecosystem:<7}"),
                    ("class:hint", f"  {hint}"),
                ],
                value=ecosystem,
                checked=ecosystem in defaults and reason is None,
                disabled=reason,
            )
        )
    selected = _tui_checkbox(
        questionary,
        style,
        "Ecosystems to update",
        choices=eco_choices,
        validate=lambda picked: True if picked else "Select at least one available ecosystem",
    ).ask()
    if selected is None:
        _tui_cancelled()

    option_choices = [
        questionary.Choice(
            title=[
                ("class:opt", "dry-run"),
                ("class:hint", "  preview only, no file changes"),
            ],
            value="dry_run",
            checked=args.dry_run,
        ),
        questionary.Choice(
            title=[
                ("class:opt", "hold majors"),
                ("class:hint", "  pin in place / keep Python upper bounds"),
            ],
            value="no_major",
            checked=not args.allow_major,
        ),
    ]
    if "python" in selected:
        option_choices.append(
            questionary.Choice(
                title=[
                    ("class:opt", "python floors"),
                    ("class:hint", "  write >= for unversioned requirements"),
                ],
                value="add_python_latest",
                checked=args.add_python_latest_version,
            )
        )
        option_choices.append(
            questionary.Choice(
                title=[
                    ("class:opt", "PEP 723 scripts"),
                    ("class:hint", "  update # /// script dependency floors"),
                ],
                value="inline_scripts",
                checked=args.update_python_inline_script_metadata,
            )
        )
    if "docker" in selected:
        option_choices.append(
            questionary.Choice(
                title=[
                    ("class:opt", "docker latest"),
                    ("class:hint", "  also pin/refresh :latest / untagged"),
                ],
                value="docker_latest",
                checked=args.enable_docker_latest_to_sha_updates,
            )
        )
    options = _tui_checkbox(questionary, style, "Options", choices=option_choices).ask()
    if options is None:
        _tui_cancelled()
    options_set = set(options)

    min_age = args.min_age
    if {"actions", "python"} & set(selected):
        answer = _tui_text(
            questionary,
            style,
            "Release min-age (hours)",
            default=str(args.min_age),
            validate=lambda text: True if text.isdigit() else "Enter an integer >= 0",
        ).ask()
        if answer is None:
            _tui_cancelled()
        min_age = int(answer)

    args.ecosystems = list(selected)
    args.dry_run = "dry_run" in options_set
    args.allow_major = "no_major" not in options_set
    args.add_python_latest_version = "add_python_latest" in options_set
    args.update_python_inline_script_metadata = "inline_scripts" in options_set
    args.enable_docker_latest_to_sha_updates = "docker_latest" in options_set
    args.min_age = min_age
    args.only = None
    return args
