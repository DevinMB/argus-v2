"""Leave Argus (design decision 18): what Argus put outside itself, for the
owner to remove or keep. Argus deletes none of it: each item says where it
lives and how, with a link."""

from __future__ import annotations

from argus import ghauth
from argus.alerts import TOKEN as TELEGRAM
from argus.alerts import WATCH_URL
from argus.deploys import PULL_TOKEN, WRITE_TOKEN
from argus.settings import Settings


def outside(settings: Settings, watchers: list[dict] | None = None) -> list[dict]:
    out = []
    app = settings.get(ghauth.APP)
    if app:
        org = app.get("owner_type") == "Organization"
        where = (f"https://github.com/organizations/{app['owner']}/settings/apps/{app['slug']}/advanced"
                 if org else f"https://github.com/settings/apps/{app['slug']}/advanced")
        out.append({"key": "github_app", "title": f"The GitHub App {app['name']}",
                    "how": "The link opens its Advanced page on GitHub; press Delete GitHub App "
                           "at the bottom. Its installations go with it.", "url": where})
    if settings.has_secret(ghauth.TOKEN):
        out.append({"key": "github_token", "title": "The GitHub token you pasted",
                    "how": "Delete it under Settings, Developer settings, Personal access tokens.",
                    "url": "https://github.com/settings/personal-access-tokens"})
    repo = settings.get("deploy.repo")
    if settings.has_secret(WRITE_TOKEN):
        out.append({"key": "write_token",
                    "title": f"The write token for {repo or 'the deploy repo'}",
                    "how": "Delete it under Settings, Developer settings, Personal access tokens.",
                    "url": "https://github.com/settings/personal-access-tokens"})
    if settings.has_secret(PULL_TOKEN):
        out.append({"key": "pull_token", "title": "The read token for private images",
                    "how": "Delete it under Personal access tokens, Tokens (classic), unless the "
                           "hosts still pull with it.",
                    "url": "https://github.com/settings/tokens"})
    if repo:
        out.append({"key": "deploy_repo", "title": f"Your deploy repo, {repo}",
                    "how": "Yours to keep. Argus wrote its hosts/ files and "
                           ".github/dependabot.yml; "
                           "without Argus nothing deploys from it, but Dependabot keeps opening "
                           "PRs until you delete that file.",
                    "url": f"https://github.com/{repo}"})
    if settings.has_secret(TELEGRAM):
        out.append({"key": "telegram", "title": "The Telegram bot",
                    "how": "In Telegram, send @BotFather /deletebot, or keep the bot for "
                           "something else.", "url": "https://t.me/BotFather"})
    if settings.has_secret(WATCH_URL):
        out.append({"key": "watcher", "title": "The service that watches Argus",
                    "how": "Pause or delete the check there, or it alerts you that Argus stopped.",
                    "url": None})
    for bot in sorted({w["bot"] for w in watchers or [] if w.get("bot")}):
        hosts = ", ".join(w["name"] for w in watchers or [] if w.get("bot") == bot)
        out.append({"key": f"watchdog:{bot}", "title": f"The watchdog bot @{bot}",
                    "how": f"{hosts} watch Argus with it. Uninstalling a host stops it; on a "
                           "host you keep, run sudo /usr/local/bin/argus-companion watch --off "
                           "first, or it tells you Argus stopped. Then send @BotFather /deletebot.",
                    "url": "https://t.me/BotFather"})
    return out
