"""Shell completion: a hidden parser-tree walker plus static bridge scripts.

ARCHITECTURE:

  The standard two-part completion pattern, fully offline: the shell
  bridges ask the CLI itself for its candidates, and the CLI derives
  them from its own argparse tree so suggestions can never drift from
  the real command surface.

  * ``_complete`` (hidden verb) — the candidate engine. Bridge scripts
    pass the words of the command line being completed as positionals
    after a ``--`` separator (so partial flags like ``--js`` survive
    argparse); the handler lazily imports ``app.build_parser`` (app is
    already in sys.modules at command time — a module-scope import
    would cycle) and walks the frozen tree to the depth the completed
    words imply, printing ONE CANDIDATE PER LINE, nothing else: the
    shells' candidate consumers (bash ``compgen -W``, zsh ``compadd``,
    PowerShell ``CompletionResult``) need a pure word list on stdout.
  * ``completions SHELL`` — emits a small STATIC bridge script (bash,
    zsh, or PowerShell) that shells out to ``fbk _complete``.

DEPTH RULES:

  Each context word (all but the last) that names a registered
  subcommand descends one level; flag words (``-``-prefixed) and
  unrecognized words (typically flag values) leave the depth unchanged.
  The last word is the partial token every candidate must prefix-match:

  * root level   -> the public families
  * family level -> that family's verbs (subparser choices)
  * verb level   -> no deeper names exist, so the flags the reached
                    parser accepts are the candidates instead
  * ``-`` prefix -> flag completion: the reached parser's option
                    strings plus the common plumbing flags
                    (--root --cookies --no-journal --json --retry
                    --debug --help); ``-h``/``--version`` are
                    suppressed as noise

  Hidden verbs (underscore-prefixed, currently just ``_complete``) are
  never candidates: they exist for the bridges, not for the operator.

LIMITS (v1, by design): no flag VALUE completion and no
positional-choice completion — the walker matches NAMES only, never
choice domains.

HIDDEN-VERB MECHANICS:

  argparse has no supported way to hide one subcommand:
  ``add_parser(help=argparse.SUPPRESS)`` prints literally as
  "==SUPPRESS==" in the choices listing, and the usage braces are
  rendered from the choices map, which must keep the verb for dispatch.
  :func:`_hide_from_help` therefore surgically drops the verb's
  pseudo-action from the root help listing and pins the usage metavar
  to the visible families. That metavar is a registration-time
  snapshot: this module must register AFTER every other family (i.e.
  ``commands.completions`` must be the last entry of
  ``app._COMMAND_MODULES``).

USER-DOC ANCHOR: cli/docs/07-reference-tooling.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse

# The hidden candidate-engine verb. The leading underscore is also the
# walker's hidden-marker: every "_"-prefixed choice is excluded from
# name candidates, so the engine never suggests itself.
_HIDDEN_VERB = "_complete"

# The plumbing flags every leaf accepts (commands.common.add_common_args)
# plus the two root-level globals (--debug, --help). Always offered for
# flag completion: the reached parser may be a family node that carries
# none of its own, but the operator's next token at that point is still
# one of these.
_COMMON_FLAGS = ("--root", "--cookies", "--no-journal", "--json",
                 "--retry", "--debug", "--dry-run", "--help")

# Suppressed flag candidates: -h (the short alias; --help is offered)
# and --version (an action that exits the process, never a completion
# target).
_EXCLUDED_FLAGS = frozenset({"-h", "--version"})


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the completions family onto the root parser.

    Adds the public ``completions SHELL`` script generator and the
    hidden ``_complete`` candidate engine, then hides the latter from
    the root help. Must be called AFTER every other family's register()
    — the hidden-verb metavar snapshot (see _hide_from_help) covers
    only what has already registered.
    """
    comp = sub.add_parser(
        "completions",
        help="emit the shell completion bridge script (bash, zsh, powershell)")
    comp.add_argument("shell", metavar="SHELL", choices=tuple(_SHELLS),
                      help="target shell: bash, zsh, or powershell")
    comp.set_defaults(fn=cmd_completions)

    # help=SUPPRESS alone does NOT hide a subcommand (argparse prints it
    # literally in the choices listing) — _hide_from_help finishes the job.
    hidden = sub.add_parser(_HIDDEN_VERB, help=argparse.SUPPRESS)
    hidden.add_argument(
        "words", nargs="*",
        help="words of the command line being completed, without the "
             "program name; the LAST word is the partial token. Pass them "
             "after a -- separator so partial flags (--js) get through "
             "(v1: no flag-value completion)")
    hidden.set_defaults(fn=cmd_complete)
    _hide_from_help(sub)


def cmd_complete(args: argparse.Namespace) -> int:
    """Print one completion candidate per line for a partial command line.

    ``args.words`` carries the words being completed (bridge scripts
    pass them after a ``--`` separator). The tree is rebuilt from the
    REAL parser — ``app.build_parser()`` imported lazily here — so the
    candidates reflect the full frozen surface, not a stale copy.

    Output contract: one candidate per line, nothing else. The shells'
    candidate consumers read the raw block; any decoration would
    corrupt all three bridges at once.
    """
    from app import build_parser
    for candidate in _candidates(build_parser(), list(args.words)):
        print(candidate)
    return 0


def cmd_completions(args: argparse.Namespace) -> int:
    """Print the static bridge script for the requested shell.

    NOT routed through commands.common.emit: emit's discipline appends
    a JSON line (and, in human mode, a summary before it), which would
    corrupt the script. A completion script is code, not payload — it
    must be byte-pure text on stdout. The completions parser
    accordingly registers no common flags: ``--json`` on a script
    generator is meaningless, and argparse rejecting it documents that
    louder than a flag that silently lies.
    """
    print(_SHELLS[args.shell], end="")
    return 0


def _candidates(parser: argparse.ArgumentParser,
                words: list[str]) -> list[str]:
    """Walk the frozen parser tree; return the candidates for ``words``.

    See the module docstring's DEPTH RULES. Everything is derived from
    the tree itself — never a hardcoded family list — which is what
    keeps completions from drifting from the real command surface.
    """
    if not words:
        words = [""]  # bare invocation: the first token is being completed
    partial = words[-1]
    node = parser
    for word in words[:-1]:
        if word.startswith("-"):
            continue  # flag words do not change depth (values: v1 limit)
        sub = _find_subparsers(node)
        if sub is not None and word in sub.choices:
            node = sub.choices[word]
    if partial.startswith("-"):
        return _flag_candidates(node, partial)
    sub = _find_subparsers(node)
    if sub is not None:
        names = sorted(name for name in sub.choices
                       if not name.startswith("_"))
        return [name for name in names if name.startswith(partial)]
    return _flag_candidates(node, partial)


def _find_subparsers(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction[argparse.ArgumentParser] | None:
    """The parser's subparsers action, if it has one.

    ``parser._actions`` is the stable argparse seam for enumerating
    _SubParsersAction nodes — the same seam app._disallow_abbrev uses;
    no public accessor exists.
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _flag_candidates(parser: argparse.ArgumentParser,
                     partial: str) -> list[str]:
    """Flag names the reached parser accepts (plus the common plumbing
    flags), deduplicated, prefix-matched against ``partial``."""
    names: list[str] = []
    for action in parser._actions:
        for option in action.option_strings:
            if option not in _EXCLUDED_FLAGS:
                names.append(option)
    names.extend(_COMMON_FLAGS)
    return sorted({name for name in names if name.startswith(partial)})


def _hide_from_help(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Keep the hidden verb out of the root parser's help and usage.

    argparse renders the usage braces from the full ``choices`` map
    (which must keep the verb for dispatch) and the choices listing
    from ``_choices_actions`` pseudo-actions — so the verb's pseudo-action
    is dropped from the listing and the usage metavar is pinned to the
    visible families. The metavar is a registration-time snapshot: this
    must run after every other family has registered (see module
    docstring, HIDDEN-VERB MECHANICS).
    """
    sub._choices_actions = [action for action in sub._choices_actions
                            if action.dest != _HIDDEN_VERB]
    visible = ",".join(name for name in sub.choices
                      if not name.startswith("_"))
    sub.metavar = "{" + visible + "}"


# ---------------------------------------------------------------- bridges
# Static, self-contained scripts (no external deps). One template per
# shell; every one delegates to `fbk _complete -- <words...>` and ends
# with an install hint. Lines are kept <=100 columns (ruff counts them
# like any other source line).

_BASH_TEMPLATE = """\
_fbk_complete() {
    local cur candidates
    cur="${COMP_WORDS[COMP_CWORD]}"
    candidates="$(fbk _complete -- "${COMP_WORDS[@]:1:COMP_CWORD}")"
    COMPREPLY=($(compgen -W "${candidates}" -- "${cur}"))
}
complete -F _fbk_complete fbk
# fbk bash completion -- generated by 'fbk completions bash'
# install hint: source this from your ~/.bashrc, e.g.
#   source <(fbk completions bash)
"""

_ZSH_TEMPLATE = """\
_fbk_complete() {
    local -a args cand
    local i
    for ((i = 2; i <= CURRENT; i++)); do
        args+=("${words[i]}")
    done
    cand=("${(f)$(fbk _complete -- "${args[@]}")}")
    (( ${#cand[@]} )) && compadd -- "${cand[@]}"
}
if (( $+functions[compdef] )); then
    compdef _fbk_complete fbk
fi
# fbk zsh completion -- generated by 'fbk completions zsh'
# install hint: source this from your ~/.zshrc, e.g.
#   source <(fbk completions zsh)
"""

_POWERSHELL_TEMPLATE = """\
Register-ArgumentCompleter -Native -CommandName fbk -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $words = @($commandAst.CommandElements | Select-Object -Skip 1 |
        ForEach-Object { $_.ToString() })
    if ($wordToComplete -eq '') {
        $words += ''
    }
    & fbk _complete -- @words | ForEach-Object {
        if ($_.StartsWith($wordToComplete,
                [System.StringComparison]::OrdinalIgnoreCase)) {
            [System.Management.Automation.CompletionResult]::new($_)
        }
    }
}
# fbk PowerShell completion -- generated by 'fbk completions powershell'
# install hint: add this to your $PROFILE, e.g.
#   fbk completions powershell | Out-String | Invoke-Expression
"""

_SHELLS: dict[str, str] = {
    "bash": _BASH_TEMPLATE,
    "zsh": _ZSH_TEMPLATE,
    "powershell": _POWERSHELL_TEMPLATE,
}
