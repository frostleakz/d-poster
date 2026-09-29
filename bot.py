#!/usr/bin/env python3
"""
Railway Discord poster.

- Stays online 24/7 (tiny health server on $PORT for Railway web services)
- Every day at 20:00 Europe/Oslo posts that day's images
- At 20:03 sends that day's follow-up message
- Schedule today / tomorrow / the day after in schedule.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SCHEDULE_PATH = ROOT / "schedule.json"
STATE_PATH = ROOT / "state.json"


def load_timezone(name: str = "Europe/Oslo"):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        try:
            import tzdata  # noqa: F401
            return ZoneInfo(name)
        except Exception:
            pass
    now = datetime.now(timezone.utc)
    offset_hours = 2 if 3 <= now.month <= 10 else 1
    return timezone(timedelta(hours=offset_hours))


OSLO = load_timezone("Europe/Oslo")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("poster-bot")


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def wrap_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    if url.startswith("<") and url.endswith(">"):
        return url
    return f"<{url}>"


def build_post_content(cfg: dict, post: dict) -> str:
    template = cfg.get("post_template") or "🌴 NAME: {name}\n📦 Mega: {mega}\n\nSKIP ADS!"
    mega = wrap_url(str(post.get("mega", "")))
    name = str(post.get("name", ""))
    skip_ads = "SKIP ADS!"
    skip_url = (post.get("skip_ads_url") or cfg.get("skip_ads_url") or "").strip()
    if skip_url:
        skip_ads = f"[SKIP ADS!]({skip_url})"
    content = template.format(name=name, mega=mega, skip_ads=skip_ads)
    if skip_url and "[SKIP ADS!]" not in content and "SKIP ADS!" in content:
        content = content.replace("SKIP ADS!", skip_ads)
    extra = (post.get("extra") or "").strip()
    if extra:
        content = f"{content}\n{extra}"
    return content


def resolve_image_path(image_value: str) -> Path:
    path = Path(image_value)
    if not path.is_absolute():
        path = ROOT / path
    return path


def today_oslo() -> date:
    return datetime.now(OSLO).date()


def alias_map(now: date | None = None) -> dict[str, date]:
    now = now or today_oslo()
    return {
        "TODAY": now,
        "TOMORROW": now + timedelta(days=1),
        "DAY_AFTER": now + timedelta(days=2),
    }


class PosterBot(commands.Bot):
    def __init__(self, cfg: dict, schedule: dict):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.cfg = cfg
        self.schedule = schedule
        self._job_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        await self.tree.sync()
        self.loop.create_task(start_health_server())
        if not self.daily_drop.is_running():
            self.daily_drop.start()
        log.info("Slash commands synced")

    async def on_ready(self) -> None:
        now = datetime.now(OSLO)
        log.info("Logged in as %s (id=%s)", self.user, getattr(self.user, "id", "?"))
        log.info("Norway time now: %s", now.strftime("%Y-%m-%d %H:%M:%S %Z"))
        log.info("Daily drop: 20:00 Europe/Oslo  |  follow-up: 20:03 Europe/Oslo")
        for label, day in alias_map().items():
            plan = self.plan_for(day)
            n = len((plan or {}).get("posts") or [])
            log.info("  %s %s -> %s post(s)%s", label, day.isoformat(), n, "" if plan else " [empty]")

    def reload_files(self) -> None:
        self.cfg = load_json(CONFIG_PATH, self.cfg)
        self.schedule = load_json(SCHEDULE_PATH, self.schedule)
        env_token = os.getenv("DISCORD_TOKEN")
        env_channel = os.getenv("DISCORD_CHANNEL_ID")
        if env_token:
            self.cfg["token"] = env_token
        if env_channel:
            self.cfg["channel_id"] = env_channel

    def plan_for(self, day: date) -> dict | None:
        days = self.schedule.get("days") or {}
        exact = days.get(day.isoformat())
        if isinstance(exact, dict):
            return exact
        aliases = {v: k for k, v in alias_map().items()}
        alias = aliases.get(day)
        if alias:
            plan = days.get(alias)
            if isinstance(plan, dict):
                return plan
        return None

    def get_target_channel(self) -> discord.abc.Messageable | None:
        raw = str(self.cfg.get("channel_id") or os.getenv("DISCORD_CHANNEL_ID") or "").strip()
        if not raw or raw.startswith("PUT_"):
            return None
        try:
            channel_id = int(raw)
        except ValueError:
            log.error("channel_id must be numeric, got %r", raw)
            return None
        return self.get_channel(channel_id) or self.get_partial_messageable(channel_id)

    async def send_one_post(self, channel: discord.abc.Messageable, post: dict) -> None:
        content = build_post_content(self.cfg, post)
        image_value = str(post.get("image") or "").strip()
        files: list[discord.File] = []
        embed: discord.Embed | None = None

        if image_value:
            embed = discord.Embed(color=0x2B2D31)
            if image_value.startswith(("http://", "https://")):
                embed.set_image(url=image_value)
            else:
                image_path = resolve_image_path(image_value)
                if not image_path.exists():
                    raise FileNotFoundError(f"Image not found: {image_path}")
                safe_name = "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in image_path.name)
                files.append(discord.File(str(image_path), filename=safe_name))
                embed.set_image(url=f"attachment://{safe_name}")

        allowed = discord.AllowedMentions(
            everyone=bool(self.cfg.get("mention_everyone", True)),
            users=True,
            roles=True,
        )
        await channel.send(content=content, files=files, embed=embed, allowed_mentions=allowed)

    def already_posted(self, day: date) -> bool:
        state = load_json(STATE_PATH, {})
        posted = state.get("posted_dates") or []
        return day.isoformat() in posted

    def mark_posted(self, day: date) -> None:
        state = load_json(STATE_PATH, {})
        posted = list(state.get("posted_dates") or [])
        key = day.isoformat()
        if key not in posted:
            posted.append(key)
        state["posted_dates"] = posted[-30:]
        save_json(STATE_PATH, state)

    async def run_day(self, day: date, triggered_by: str, force: bool = False) -> str:
        if self._job_lock.locked():
            return "A drop is already running."

        async with self._job_lock:
            self.reload_files()
            if not force and self.already_posted(day):
                return f"{day.isoformat()} already posted. Use /run_now force:True to send again."

            plan = self.plan_for(day)
            if not plan:
                return f"No schedule for {day.isoformat()}."
            if plan.get("enabled", True) is False:
                return f"{day.isoformat()} is disabled in schedule.json."

            posts = plan.get("posts") or []
            if not posts:
                return f"{day.isoformat()} has no posts."

            channel = self.get_target_channel()
            if channel is None:
                return "Set DISCORD_CHANNEL_ID on Railway, or channel_id in config.json."

            delay = float(self.cfg.get("delay_between_posts_seconds", 2))
            followup = plan.get("followup_message") or self.cfg.get("default_followup_message") or ""

            log.info("Drop %s (%s posts) into %s [%s]", day.isoformat(), len(posts), self.cfg.get("channel_id"), triggered_by)

            sent = 0
            for index, post in enumerate(posts, start=1):
                await self.send_one_post(channel, post)
                sent += 1
                log.info("Posted %s/%s: %s", index, len(posts), post.get("name"))
                if index < len(posts) and delay > 0:
                    await asyncio.sleep(delay)

            if followup:
                now = datetime.now(OSLO)
                follow_at = datetime.combine(
                    day,
                    time(
                        int(self.cfg.get("followup_hour", 20)),
                        int(self.cfg.get("followup_minute", 3)),
                    ),
                    tzinfo=OSLO,
                )
                wait = (follow_at - now).total_seconds()
                if wait < 0:
                    wait = 0.0
                log.info("Waiting %.1f seconds for 20:03 follow-up", wait)
                await asyncio.sleep(wait)
                allowed = discord.AllowedMentions(
                    everyone=bool(self.cfg.get("mention_everyone", True)),
                    users=True,
                    roles=True,
                )
                await channel.send(content=str(followup), allowed_mentions=allowed)
                log.info("Follow-up sent")

            self.mark_posted(day)
            return f"Done {day.isoformat()}: {sent}/{len(posts)} posts + follow-up."

    @tasks.loop(time=time(hour=20, minute=0, tzinfo=OSLO))
    async def daily_drop(self) -> None:
        day = today_oslo()
        log.info("20:00 Europe/Oslo hit — running %s", day.isoformat())
        try:
            result = await self.run_day(day, triggered_by="scheduled 20:00", force=False)
            log.info(result)
        except Exception:
            log.exception("Scheduled drop failed")

    @daily_drop.before_loop
    async def before_daily_drop(self) -> None:
        await self.wait_until_ready()
        hour = int(self.cfg.get("post_hour", 20))
        minute = int(self.cfg.get("post_minute", 0))
        self.daily_drop.change_interval(time=time(hour=hour, minute=minute, tzinfo=OSLO))


async def start_health_server() -> None:
    """Railway web services need something listening on $PORT."""
    port = int(os.getenv("PORT", "8080"))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1024)
        except Exception:
            pass
        body = b"ok"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        try:
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    try:
        server = await asyncio.start_server(handle, "0.0.0.0", port)
        log.info("Health server listening on 0.0.0.0:%s", port)
        async with server:
            await server.serve_forever()
    except OSError as exc:
        log.warning("Health server did not start on port %s: %s", port, exc)


def build_bot() -> PosterBot:
    load_dotenv(ROOT / ".env")
    cfg = load_json(CONFIG_PATH, {})
    schedule = load_json(SCHEDULE_PATH, {"days": {}})
    token = os.getenv("DISCORD_TOKEN") or cfg.get("token") or ""
    channel = os.getenv("DISCORD_CHANNEL_ID")
    if channel:
        cfg["channel_id"] = channel
    cfg["_token"] = token
    return PosterBot(cfg, schedule)


bot = build_bot()


def _parse_day(value: str | None) -> date:
    raw = (value or "today").strip().lower()
    now = today_oslo()
    aliases = {
        "today": now,
        "tomorrow": now + timedelta(days=1),
        "day_after": now + timedelta(days=2),
        "dayafter": now + timedelta(days=2),
        "overmorrow": now + timedelta(days=2),
    }
    if raw in aliases:
        return aliases[raw]
    return date.fromisoformat(value.strip())  # type: ignore[arg-type]


@bot.tree.command(name="status", description="Show the 3-day schedule (today, tomorrow, day after)")
async def status(interaction: discord.Interaction) -> None:
    bot.reload_files()
    lines = [f"Norway time: `{datetime.now(OSLO).strftime('%Y-%m-%d %H:%M %Z')}`", ""]
    for label, day in alias_map().items():
        plan = bot.plan_for(day)
        posted = "already sent" if bot.already_posted(day) else "queued"
        if not plan:
            lines.append(f"**{label}** `{day}` — no posts ({posted})")
            continue
        enabled = "on" if plan.get("enabled", True) else "OFF"
        n = len(plan.get("posts") or [])
        lines.append(f"**{label}** `{day}` — {n} post(s), {enabled}, {posted}")
        for i, post in enumerate(plan.get("posts") or [], start=1):
            lines.append(f"  {i}. {post.get('name')}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="reload", description="Reload config.json and schedule.json from disk")
async def reload_cmd(interaction: discord.Interaction) -> None:
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("Need Manage Messages.", ephemeral=True)
        return
    bot.reload_files()
    await interaction.response.send_message("Reloaded config + schedule.", ephemeral=True)


@bot.tree.command(name="run_now", description="Post a day's drop immediately (does not wait for 20:00)")
@app_commands.describe(day="today, tomorrow, day_after, or YYYY-MM-DD", force="Send even if already posted")
async def run_now(interaction: discord.Interaction, day: str = "today", force: bool = False) -> None:
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("Need Manage Messages.", ephemeral=True)
        return
    try:
        target = _parse_day(day)
    except ValueError:
        await interaction.response.send_message("Use today / tomorrow / day_after / YYYY-MM-DD.", ephemeral=True)
        return
    await interaction.response.send_message(f"Running `{target.isoformat()}` now…", ephemeral=True)
    try:
        result = await bot.run_day(target, triggered_by=f"/run_now by {interaction.user}", force=force)
    except Exception as exc:
        log.exception("run_now failed")
        await interaction.followup.send(f"Failed: `{exc}`", ephemeral=True)
        return
    await interaction.followup.send(result, ephemeral=True)


@bot.tree.command(name="setchannel", description="Save this channel as the drop target")
async def setchannel(interaction: discord.Interaction) -> None:
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Need Manage Server.", ephemeral=True)
        return
    bot.cfg["channel_id"] = str(interaction.channel_id)
    persist = {k: v for k, v in bot.cfg.items() if not k.startswith("_")}
    save_json(CONFIG_PATH, persist)
    await interaction.response.send_message(
        f"Saved channel `{interaction.channel_id}`. On Railway also set DISCORD_CHANNEL_ID.",
        ephemeral=True,
    )


@bot.tree.command(name="setfollowup", description="Set the follow-up message for today, tomorrow, or day_after")
@app_commands.describe(day="today, tomorrow, or day_after", message="Message sent at 20:03")
async def setfollowup(interaction: discord.Interaction, day: str, message: str) -> None:
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("Need Manage Messages.", ephemeral=True)
        return
    key = day.strip().upper().replace(" ", "_")
    if key not in {"TODAY", "TOMORROW", "DAY_AFTER"}:
        try:
            key = date.fromisoformat(day.strip()).isoformat()
        except ValueError:
            await interaction.response.send_message("Use today / tomorrow / day_after / YYYY-MM-DD.", ephemeral=True)
            return
    days = bot.schedule.setdefault("days", {})
    plan = days.setdefault(key, {"enabled": True, "posts": []})
    plan["followup_message"] = message
    save_json(SCHEDULE_PATH, bot.schedule)
    await interaction.response.send_message(f"Follow-up for `{key}` updated.", ephemeral=True)


def main() -> None:
    token = bot.cfg.get("_token") or os.getenv("DISCORD_TOKEN") or ""
    if not token or token.startswith("PUT_") or token.startswith("your_"):
        log.error("Set DISCORD_TOKEN in Railway Variables (or config.json).")
        sys.exit(1)
    if not str(bot.cfg.get("channel_id") or "").strip():
        log.warning("DISCORD_CHANNEL_ID is not set yet.")
    bot.run(token)


if __name__ == "__main__":
    main()
