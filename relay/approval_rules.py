"""Contextual auto-approval rules for Telegram permission requests.

A rule says "for this project, this subject is safe". Subjects are the text of
an OpenCode permission dialog (the `← ...` summary plus its detail lines), so
one rule covers e.g. one repeated bash command or one file-access pattern.

Dangerous commands never auto-approve, regardless of rules.
"""
import json
import os
import re

RULES_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "herdr-remote", "approval_rules.json"
)

# ponytail: substring/regex blocklist, not a shell parser — a determined agent
# can evade it; the gate is "don't auto-approve scary things", not sandboxing.
DANGEROUS_RE = re.compile(
    r"\brm\s+[^|]*-\w*[rf]"
    r"|\bsudo\b"
    r"|\bch(mod|own)\b"
    r"|git\s+reset\s+--hard"
    r"|git\s+push\b[^|]*--force"
    r"|git\s+clean\s+-\w*f"
    r"|\bdrop\s+(table|database)\b"
    r"|curl[^|]*\|\s*(ba)?sh"
    r"|\bmkfs\b"
    r"|\bdd\b[^|]*\bof=",
    re.IGNORECASE,
)


def is_dangerous(subject: str) -> bool:
    return bool(DANGEROUS_RE.search(subject or ""))


def severity(subject: str) -> str:
    """🔴 destructive · 🟡 touches files · 🟢 routine."""
    if is_dangerous(subject):
        return "🔴"
    lowered = (subject or "").lower()
    if re.search(r"\b(edit|write|patterns|directory|external)\b", lowered):
        return "🟡"
    return "🟢"


def load() -> list:
    try:
        with open(RULES_PATH, encoding="utf-8") as fh:
            rules = json.load(fh)
        return rules if isinstance(rules, list) else []
    except (OSError, ValueError):
        return []


def save(rules: list):
    os.makedirs(os.path.dirname(RULES_PATH), exist_ok=True)
    with open(RULES_PATH, "w", encoding="utf-8") as fh:
        json.dump(rules, fh, indent=2, ensure_ascii=False)


def allows(project: str, subject: str, rules: list | None = None) -> bool:
    """True when a rule covers this project+subject and it is not dangerous."""
    if is_dangerous(subject):
        return False
    subject = (subject or "").strip()
    for rule in rules if rules is not None else load():
        if rule.get("project") != project:
            continue
        pattern = rule.get("subject")
        if pattern is None or pattern in subject:
            return True
    return False


def add_command_rule(project: str, subject: str):
    subject = (subject or "").strip()
    if not subject or is_dangerous(subject):
        return
    rules = [r for r in load() if not (r.get("project") == project and r.get("subject") == subject)]
    rules.append({"project": project, "subject": subject})
    save(rules)


def add_project_rule(project: str):
    rules = [r for r in load() if not (r.get("project") == project and r.get("subject") is None)]
    rules.append({"project": project})
    save(rules)
