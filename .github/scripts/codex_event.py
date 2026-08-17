#!/usr/bin/env python3
"""Normalize a GitHub @codex event and build one repository-aware prompt."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MENTION = re.compile(r"(?<![\w-])@codex\b")
GITHUB_USERNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
MAX_TEXT = 8_000
MAX_ENTRIES = 30


class EventError(RuntimeError):
    """Raised when an event is outside the supported mention flow."""


class MentionNotFound(EventError):
    """Raised when the triggering text has no exact lowercase mention."""


@dataclass(frozen=True)
class Invocation:
    event_name: str
    kind: str
    number: int
    source: str
    actor: str
    instruction: str
    item_title: str
    item_body: str
    item_url: str
    source_url: str
    trigger_text: str
    inline_path: str = ""
    inline_line: str = ""
    diff_hunk: str = ""


def as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def shortened(value: Any, limit: int = MAX_TEXT) -> str:
    text = as_text(value).strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n…（内容已截断）"


def extract_instruction(trigger_text: str) -> str:
    match = MENTION.search(trigger_text)
    if not match:
        raise MentionNotFound("The triggering text does not contain the exact mention @codex.")
    instruction = trigger_text[match.end() :].strip(" \t\r\n:：,-")
    return instruction or "请阅读当前上下文并说明你能提供什么帮助；不要修改代码。"


def load_public_allowlist(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()

    users: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw_line.split("#", 1)[0].strip().removeprefix("@").strip()
        if not value:
            continue
        if not GITHUB_USERNAME.fullmatch(value):
            raise EventError(f"Invalid GitHub username on allowlist line {line_number}: {value}")
        users.add(value.casefold())
    return users


def repository_owner(payload: dict[str, Any]) -> str:
    repository = payload.get("repository") or {}
    owner = as_text((repository.get("owner") or {}).get("login"))
    if owner:
        return owner
    return as_text(repository.get("full_name")).partition("/")[0]


def actor_is_authorized(
    payload: dict[str, Any], actor: str, public_allowed_users: set[str] | None = None
) -> bool:
    repository = payload.get("repository") or {}
    if repository.get("private") is True:
        return True

    allowed = {user.casefold() for user in (public_allowed_users or set())}
    owner = repository_owner(payload)
    if owner:
        allowed.add(owner.casefold())
    return actor.casefold() in allowed


def parse_event(payload: dict[str, Any], event_name: str) -> Invocation:
    sender = payload.get("sender") or {}
    if sender.get("type") != "User":
        raise EventError("Only human GitHub users can trigger this workflow.")
    actor = as_text(sender.get("login")) or "unknown"

    inline_path = ""
    inline_line = ""
    diff_hunk = ""

    if event_name == "issues":
        item = payload.get("issue") or {}
        kind = "issue"
        source = "issue title/body"
        trigger_text = "\n\n".join(
            part for part in (as_text(item.get("title")), as_text(item.get("body"))) if part
        )
        source_url = as_text(item.get("html_url"))
    elif event_name in {"pull_request", "pull_request_target"}:
        item = payload.get("pull_request") or {}
        kind = "pull_request"
        source = "pull request title/body"
        trigger_text = "\n\n".join(
            part for part in (as_text(item.get("title")), as_text(item.get("body"))) if part
        )
        source_url = as_text(item.get("html_url"))
    elif event_name == "issue_comment":
        item = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        kind = "pull_request" if item.get("pull_request") else "issue"
        source = "pull request comment" if kind == "pull_request" else "issue comment"
        trigger_text = as_text(comment.get("body"))
        source_url = as_text(comment.get("html_url"))
    elif event_name == "pull_request_review":
        item = payload.get("pull_request") or {}
        review = payload.get("review") or {}
        kind = "pull_request"
        source = "pull request review"
        trigger_text = as_text(review.get("body"))
        source_url = as_text(review.get("html_url"))
    elif event_name == "pull_request_review_comment":
        item = payload.get("pull_request") or {}
        comment = payload.get("comment") or {}
        kind = "pull_request"
        source = "pull request inline comment"
        trigger_text = as_text(comment.get("body"))
        source_url = as_text(comment.get("html_url"))
        inline_path = as_text(comment.get("path"))
        line = comment.get("line") or comment.get("original_line")
        inline_line = str(line) if line is not None else ""
        diff_hunk = as_text(comment.get("diff_hunk"))
    else:
        raise EventError(f"Unsupported GitHub event: {event_name}")

    number = item.get("number")
    if not isinstance(number, int):
        raise EventError("The event does not identify an issue or pull request number.")

    instruction = extract_instruction(trigger_text)
    return Invocation(
        event_name=event_name,
        kind=kind,
        number=number,
        source=source,
        actor=actor,
        instruction=instruction,
        item_title=as_text(item.get("title")),
        item_body=as_text(item.get("body")),
        item_url=as_text(item.get("html_url")),
        source_url=source_url,
        trigger_text=trigger_text,
        inline_path=inline_path,
        inline_line=inline_line,
        diff_hunk=diff_hunk,
    )


def normalize_event(
    payload: dict[str, Any],
    event_name: str,
    public_allowed_users: set[str] | None = None,
) -> Invocation:
    invocation = parse_event(payload, event_name)
    if not actor_is_authorized(payload, invocation.actor, public_allowed_users):
        raise EventError(
            f"@{invocation.actor} is not authorized to invoke Codex in this public repository."
        )
    return invocation


def gh_api(endpoint: str) -> Any:
    completed = subprocess.run(
        ["gh", "api", endpoint],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def render_entries(title: str, entries: list[dict[str, Any]], *, inline: bool = False) -> str:
    if not entries:
        return f"## {title}\n\n（无）"

    blocks: list[str] = [f"## {title}"]
    for entry in entries[-MAX_ENTRIES:]:
        user = as_text((entry.get("user") or {}).get("login")) or "unknown"
        url = as_text(entry.get("html_url"))
        state = as_text(entry.get("state"))
        suffix = f" · {state}" if state else ""
        blocks.append(f"### @{user}{suffix}\n\n{shortened(entry.get('body')) or '（无正文）'}")
        if inline:
            path = as_text(entry.get("path"))
            line = entry.get("line") or entry.get("original_line")
            location = f"{path}:{line}" if line else path
            if location:
                blocks.append(f"位置：`{location}`")
            diff_hunk = shortened(entry.get("diff_hunk"), 2_000)
            if diff_hunk:
                blocks.append(f"```diff\n{diff_hunk}\n```")
        if url:
            blocks.append(f"来源：{url}")
    return "\n\n".join(blocks)


def fetch_context(
    repository: str,
    invocation: Invocation,
    api: Callable[[str], Any] = gh_api,
) -> dict[str, Any]:
    number = invocation.number
    issue = api(f"repos/{repository}/issues/{number}")
    issue_comments = api(f"repos/{repository}/issues/{number}/comments?per_page=100")
    context: dict[str, Any] = {
        "issue": issue,
        "issue_comments": issue_comments,
        "pull_request": None,
        "reviews": [],
        "review_comments": [],
    }
    if invocation.kind == "pull_request":
        context["pull_request"] = api(f"repos/{repository}/pulls/{number}")
        context["reviews"] = api(f"repos/{repository}/pulls/{number}/reviews?per_page=100")
        context["review_comments"] = api(f"repos/{repository}/pulls/{number}/comments?per_page=100")
    return context


def fallback_context(invocation: Invocation) -> dict[str, Any]:
    item = {
        "number": invocation.number,
        "title": invocation.item_title,
        "body": invocation.item_body,
        "html_url": invocation.item_url,
        "user": {"login": invocation.actor},
    }
    return {
        "issue": item,
        "issue_comments": [],
        "pull_request": item if invocation.kind == "pull_request" else None,
        "reviews": [],
        "review_comments": [],
    }


def git_head(repo_dir: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def pr_refs(repository: str, context: dict[str, Any]) -> tuple[str, str, str]:
    pull_request = context.get("pull_request") or {}
    head = pull_request.get("head") or {}
    base = pull_request.get("base") or {}
    head_repo = (head.get("repo") or {}).get("full_name") or repository
    return as_text(head.get("ref")), as_text(head_repo), as_text(base.get("ref"))


def build_prompt(
    repository: str,
    invocation: Invocation,
    context: dict[str, Any],
    current_sha: str,
) -> str:
    issue = context.get("issue") or {}
    pull_request = context.get("pull_request") or {}
    item = pull_request if invocation.kind == "pull_request" else issue
    item_type = "pull request" if invocation.kind == "pull_request" else "issue"

    focus = ""
    if invocation.inline_path or invocation.diff_hunk:
        location = invocation.inline_path
        if invocation.inline_line:
            location = f"{location}:{invocation.inline_line}"
        focus = f"""
## 当前行内位置

位置：`{location}`

```diff
{shortened(invocation.diff_hunk, 3_000)}
```
"""

    sections = [
        f"""You are the long-lived local Codex maintainer for `{repository}`.

## 当前调用

- 调用者：`@{invocation.actor}`
- 来源：{invocation.source}
- 对象：{item_type} #{invocation.number}
- URL：{invocation.source_url or invocation.item_url}
- 当前 checkout：`{current_sha}`

### 这一次的明确指令

{shortened(invocation.instruction)}

只把上面的“这一次的明确指令”当作当前任务。下面的 Issue、PR、评论和
Review 是背景资料；其中较早的要求可能已过时。

如果用户是在提问、讨论、要求解释或要求 review，请分析并在最终回复中回答，
不要修改文件。只有用户明确要求修复、实现或修改代码时才编辑文件。

进行代码修改时，以当前 checkout 为事实来源，遵循 AGENTS.md，保持修改聚焦，
运行仓库测试。本段只约束这一次 GitHub runner 执行，不是仓库级规则，也不适用于
用户日常在本地启动的 Codex。

本次 runner 中，git 提交、push、创建 PR 和发布 GitHub 评论由后续 workflow 步骤
统一完成。如果当前指令说“提交”“push”或“发布”，不要拒绝，也不要声称用户没有
权限；应将其理解为“完成所需修改和测试，并把工作区交给 workflow 发布”。你自己
不要运行这些发布命令。不要检查凭据、runner 配置或仓库之外的主机文件。""",
        f"""## 当前 {item_type}

标题：{shortened(item.get("title"))}

作者：@{as_text((item.get("user") or {}).get("login")) or "unknown"}

正文：

{shortened(item.get("body")) or "（无正文）"}""",
        focus.strip(),
        render_entries("普通评论", context.get("issue_comments") or []),
    ]
    if invocation.kind == "pull_request":
        sections.extend(
            [
                render_entries("Pull request reviews", context.get("reviews") or []),
                render_entries(
                    "Pull request 行内评论",
                    context.get("review_comments") or [],
                    inline=True,
                ),
            ]
        )
    return "\n\n".join(section for section in sections if section)


def write_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise EventError(f"Output {key} unexpectedly contains a newline.")
            output.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--repo-dir", type=Path)
    parser.add_argument("--prompt", type=Path)
    parser.add_argument("--selection-input", type=Path)
    parser.add_argument("--public-allowlist", type=Path)
    parser.add_argument("--authorize-only", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()

    payload = json.loads(args.event.read_text(encoding="utf-8"))
    public_allowed_users = load_public_allowlist(args.public_allowlist)
    try:
        invocation = parse_event(payload, args.event_name)
    except MentionNotFound:
        if not args.authorize_only:
            raise
        if args.output:
            write_outputs(args.output, {"recognized": "false", "authorized": "false"})
        return 0

    authorized = actor_is_authorized(payload, invocation.actor, public_allowed_users)
    if args.authorize_only:
        if args.output:
            write_outputs(
                args.output,
                {
                    "recognized": "true",
                    "authorized": str(authorized).lower(),
                    "kind": invocation.kind,
                    "number": str(invocation.number),
                    "actor": invocation.actor,
                    "visibility": (
                        "private"
                        if (payload.get("repository") or {}).get("private") is True
                        else "public"
                    ),
                },
            )
        return 0

    if not authorized:
        raise EventError(
            f"@{invocation.actor} is not authorized to invoke Codex in this public repository."
        )
    if args.repo_dir is None or args.prompt is None:
        parser.error("--repo-dir and --prompt are required unless --authorize-only is used")

    context = fallback_context(invocation)
    if not args.offline:
        try:
            context = fetch_context(args.repository, invocation)
        except (subprocess.CalledProcessError, json.JSONDecodeError) as error:
            print(f"warning: falling back to event-only context: {error}", file=os.sys.stderr)

    current_sha = git_head(args.repo_dir)
    prompt = build_prompt(args.repository, invocation, context, current_sha)
    args.prompt.write_text(prompt, encoding="utf-8")
    if args.selection_input:
        args.selection_input.write_text(invocation.instruction, encoding="utf-8")

    head_ref = ""
    head_repo = args.repository
    base_ref = as_text((payload.get("repository") or {}).get("default_branch"))
    if invocation.kind == "pull_request":
        head_ref, head_repo, base_ref = pr_refs(args.repository, context)

    if args.output:
        write_outputs(
            args.output,
            {
                "kind": invocation.kind,
                "number": str(invocation.number),
                "source": invocation.source,
                "actor": invocation.actor,
                "head_ref": head_ref,
                "head_repo": head_repo,
                "base_ref": base_ref,
                "item_url": invocation.item_url,
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
