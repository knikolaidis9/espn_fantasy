import argparse
import html
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
STATE_FILE = HERE / "state.json"
LEAGUE_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{season}/segments/0/leagues/{league}"
GAME_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{season}"
ATHLETE_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{pid}"
POSITIONS = {1: "PG", 2: "SG", 3: "SF", 4: "PF", 5: "C"}
STATS = {"19": "FG%", "20": "FT%", "17": "3PM", "6": "REB", "3": "AST", "2": "STL", "1": "BLK", "11": "TO", "0": "PTS"}
IR_SLOT = 13
KIND = {
    "TRADE_PROPOSAL": "📝 Proposed",
    "TRADE_ACCEPT": "🤝 Accepted",
    "TRADE_DECLINE": "🚫 Declined",
    "TRADE_VETO": "❌ Veto",
    "TRADE_UPHOLD": "✅ Uphold",
}
STATUS_LABEL = {
    "in_review": "⏳ In review",
    "completed": "✅ Completed",
    "vetoed": "❌ Vetoed",
    "canceled": "⚪ Canceled",
    "declined": "🚫 Declined",
    "proposed": "📝 Proposed",
    "unknown": "❔ Unknown",
}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
COMMANDS = [
    ("help", "Show all commands"),
    ("today", "Trades from the last 24h + in review"),
    ("trades", "All trades from the last 7 days"),
    ("season", "Every trade this season"),
    ("opponent", "This week's matchup"),
    ("games", "NBA games per team, this week and next"),
    ("fa", "Free agents with the most games"),
    ("weekly", "Full weekly report"),
    ("check", "Check for trade updates now"),
]
DEFAULTS = {
    "league_id": 4389508,
    "season": 2027,
    "poll_minutes": 10,
    "daily_time": "12:00",
    "weekly_day": "Mon",
    "weekly_time": "12:30",
    "free_agents": 12,
}
ENV = {"espn_s2": "ESPN_S2", "swid": "ESPN_SWID", "telegram_token": "TG_TOKEN", "telegram_chat_id": "TG_CHAT_ID"}


class EspnError(Exception):
    pass


def esc(s):
    return html.escape(str(s), quote=False)


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", flush=True)


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        raw = CONFIG_FILE.read_text(encoding="utf-8-sig").strip()
        if not raw:
            sys.exit(f"{CONFIG_FILE.name} is empty. Paste the JSON config into it and save.")
        try:
            cfg.update(json.loads(raw))
        except json.JSONDecodeError as e:
            sys.exit(f"{CONFIG_FILE.name} is not valid JSON (line {e.lineno}, column {e.colno}): {e.msg}. "
                     "It must start with { and end with }, with a comma after every line except the last.")
    for key, var in ENV.items():
        if os.getenv(var):
            cfg[key] = os.getenv(var)
    swid = str(cfg.get("swid", "")).strip().upper()
    if swid and not swid.startswith("{"):
        swid = "{" + swid + "}"
    cfg["swid"] = swid
    return cfg


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen": {}, "last_daily": "", "last_weekly": "", "auth_alerted": False}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def team_name(t):
    raw = t.get("name") or f'{t.get("location", "")} {t.get("nickname", "")}'
    return " ".join(raw.split())


def fmt_time(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%a %d %b %H:%M") if ms else "-"


def fmt_stat(sid, value):
    if value is None:
        return "-"
    if sid in ("19", "20"):
        return f"{value:.3f}".lstrip("0") or "0"
    return f"{value:g}"


def chunks(text, limit=3900):
    out, cur = [], ""
    for block in text.split("\n\n"):
        candidate = f"{cur}\n\n{block}" if cur else block
        if len(candidate) <= limit:
            cur = candidate
            continue
        if cur:
            out.append(cur)
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            out.append(block[:cut])
            block = block[cut:].lstrip("\n")
        cur = block
    if cur:
        out.append(cur)
    return out


class Telegram:
    def __init__(self, token, chat_id):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id) if chat_id else ""
        self.offset = None

    def send(self, text):
        for part in chunks(text):
            r = requests.post(
                f"{self.base}/sendMessage",
                json={"chat_id": self.chat_id, "text": part, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=20,
            )
            if not r.ok:
                log(f"Telegram send failed: {r.status_code} {r.text[:200]}")

    def set_commands(self, commands):
        try:
            r = requests.post(
                f"{self.base}/setMyCommands",
                json={"commands": [{"command": c, "description": d} for c, d in commands]},
                timeout=20,
            )
            if not r.ok:
                log(f"Could not register commands: {r.status_code} {r.text[:200]}")
        except requests.RequestException as e:
            log(f"Could not register commands: {e}")

    def updates(self, timeout=25):
        params = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}
        if self.offset is not None:
            params["offset"] = self.offset
        r = requests.get(f"{self.base}/getUpdates", params=params, timeout=timeout + 15)
        r.raise_for_status()
        result = r.json().get("result", [])
        if result:
            self.offset = result[-1]["update_id"] + 1
        return result


class Espn:
    def __init__(self, cfg):
        self.league_url = LEAGUE_URL.format(season=cfg["season"], league=cfg["league_id"])
        self.game_url = GAME_URL.format(season=cfg["season"])
        self.swid = cfg["swid"]
        self.session = requests.Session()
        self.session.cookies.update({"espn_s2": cfg["espn_s2"], "SWID": cfg["swid"]})
        self.session.headers.update({"Accept": "application/json"})
        self._schedule = (None, None)

    def get(self, views, period=None, flt=None, url=None):
        params = {"view": views}
        if period:
            params["scoringPeriodId"] = period
        headers = {"x-fantasy-filter": json.dumps(flt)} if flt else None
        r = self.session.get(url or self.league_url, params=params, headers=headers, timeout=30)
        if r.status_code in (401, 403):
            raise EspnError("ESPN rejected the login cookies (espn_s2 / SWID). Copy fresh ones from Chrome into config.json.")
        if not r.ok:
            raise EspnError(f"ESPN returned HTTP {r.status_code} for {','.join(views)}: {r.text[:150]}")
        return r.json()

    def league(self):
        data = self.get(["mTeam", "mSettings", "mStatus", "mRoster"])
        if "teams" not in data:
            raise EspnError("ESPN did not return league data. Check league_id, season and cookies.")
        return data

    def transactions(self, periods):
        txs = {}
        for p in periods:
            for tx in self.get(["mTransactions2"], p).get("transactions", []):
                txs[tx["id"]] = tx
        for tx in self.get(["mPendingTransactions"]).get("transactions", []):
            txs.setdefault(tx["id"], tx)
        return [t for t in txs.values() if "TRADE" in t.get("type", "")]

    def players(self, ids):
        ids = sorted(i for i in ids if isinstance(i, int) and i > 0)
        out = {}
        for start in range(0, len(ids), 50):
            batch = ids[start:start + 50]
            try:
                data = self.get(["kona_player_info"], flt={"players": {"filterIds": {"value": batch}}})
            except EspnError as e:
                log(f"Player name lookup failed, using ids instead: {e}")
                continue
            for p in data.get("players", []):
                info = p.get("player", {})
                pid = p.get("id") or info.get("id")
                out[pid] = {"name": info.get("fullName", str(pid)), "position": POSITIONS.get(info.get("defaultPositionId"), "")}
        for pid in [i for i in ids if i not in out][:25]:
            try:
                r = requests.get(ATHLETE_URL.format(pid=pid), timeout=10)
                a = r.json().get("athlete", {}) if r.ok else {}
                if a.get("displayName"):
                    out[pid] = {"name": a["displayName"], "position": (a.get("position") or {}).get("abbreviation", "")}
            except (requests.RequestException, ValueError):
                continue
        return out

    def schedule(self):
        today = datetime.now().date()
        if self._schedule[0] == today:
            return self._schedule[1]
        data = self.get(["proTeamSchedules_wl"], url=self.game_url)
        teams = {}
        for t in data.get("settings", {}).get("proTeams", []):
            if not t.get("id"):
                continue
            games = {}
            for period, lst in (t.get("proGamesByScoringPeriod") or {}).items():
                if lst:
                    games[int(period)] = [g.get("date") for g in lst]
            teams[t["id"]] = {"abbrev": t.get("abbrev", "?").upper(), "name": f'{t.get("location", "")} {t.get("name", "")}'.strip(), "games": games}
        self._schedule = (today, teams)
        return teams

    def matchups(self, period):
        return self.get(["mMatchupScore", "mScoreboard"], period).get("schedule", [])

    def free_agents(self, period, limit=80):
        flt = {"players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "limit": limit,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }}
        return self.get(["kona_player_info"], period, flt).get("players", [])


def group_trades(txs):
    groups = {}
    for tx in txs:
        groups.setdefault(tx.get("relatedTransactionId") or tx["id"], []).append(tx)
    for g in groups.values():
        g.sort(key=lambda t: t.get("proposedDate") or 0)
    return groups


def first(txs, kind):
    return next((t for t in txs if t.get("type") == kind), None)


def trade_status(txs):
    proposal, accept, decline = first(txs, "TRADE_PROPOSAL"), first(txs, "TRADE_ACCEPT"), first(txs, "TRADE_DECLINE")
    if decline:
        return "declined"
    status = (proposal or {}).get("status", "")
    if status == "CANCELED":
        return "canceled"
    if status == "EXECUTED":
        return "completed"
    if status == "VETOED":
        return "vetoed"
    if accept:
        return "in_review"
    if proposal:
        return "proposed"
    return "unknown"


class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.espn = Espn(cfg)
        self.tg = Telegram(cfg["telegram_token"], cfg["telegram_chat_id"])
        self.state = load_state()
        self.ctx = None

    def refresh(self):
        lg = self.espn.league()
        teams = {t["id"]: team_name(t) for t in lg["teams"]}
        my = next((t["id"] for t in lg["teams"] if self.cfg["swid"] in [o.upper() for o in t.get("owners", [])]), None)
        current = lg.get("scoringPeriodId") or lg.get("status", {}).get("latestScoringPeriod") or 1
        mps = {int(k): v for k, v in lg.get("settings", {}).get("scheduleSettings", {}).get("matchupPeriods", {}).items()}
        mp = next((k for k, v in mps.items() if current in v), lg.get("status", {}).get("currentMatchupPeriod"))
        known = {}
        for t in lg["teams"]:
            for e in t.get("roster", {}).get("entries", []):
                p = e.get("playerPoolEntry", {}).get("player", {})
                pid = p.get("id") or e.get("playerId")
                if pid:
                    known[pid] = {"name": p.get("fullName", str(pid)), "position": POSITIONS.get(p.get("defaultPositionId"), "")}
        self.ctx = {"league": lg, "teams": teams, "my": my, "current": current, "mp": mp, "mps": mps,
                    "periods": mps.get(mp, [current]), "known": known}
        return self.ctx

    def name(self, tid):
        if tid == self.ctx["my"]:
            return f"{self.ctx['teams'].get(tid, tid)} (you)"
        return self.ctx["teams"].get(tid, f"Team {tid}") if tid and tid > 0 else "Free agency"

    def describe(self, key, txs, players, new_ids=()):
        status = trade_status(txs)
        moves, seen = [], set()
        for t in txs:
            for i in t.get("items", []):
                k = (i.get("playerId"), i.get("fromTeamId"), i.get("toTeamId"))
                if k not in seen:
                    seen.add(k)
                    moves.append(i)
        parties = []
        for t in txs:
            if t.get("type") in ("TRADE_PROPOSAL", "TRADE_ACCEPT", "TRADE_DECLINE") and t.get("teamId", 0) > 0 and t["teamId"] not in parties:
                parties.append(t["teamId"])
        for i in moves:
            for x in (i.get("fromTeamId"), i.get("toTeamId")):
                if isinstance(x, int) and x > 0 and x not in parties:
                    parties.append(x)

        def label(i):
            p = players.get(i.get("playerId"), {})
            n = esc(p.get("name", i.get("playerId")))
            return f"{n} ({p['position']})" if p.get("position") else n

        mine = self.ctx["my"] in parties
        lines = [f"{'⭐ ' if mine else ''}<b>{STATUS_LABEL[status]}</b> · {fmt_time(txs[0].get('proposedDate'))}"]
        if moves:
            for tid in parties:
                gets = [label(i) for i in moves if i.get("toTeamId") == tid and i.get("type") != "DROP"]
                drops = [label(i) for i in moves if i.get("fromTeamId") == tid and (i.get("type") == "DROP" or not (i.get("toTeamId") or 0) > 0)]
                if gets:
                    lines.append(f"<b>{esc(self.name(tid))}</b> gets: {', '.join(gets)}")
                if drops:
                    lines.append(f"<b>{esc(self.name(tid))}</b> drops: {', '.join(drops)}")
        else:
            who = ", ".join(esc(self.name(t)) for t in parties) or "unknown teams"
            lines.append(f"Teams: {who}")
            lines.append("<i>Players not visible on ESPN yet (usually shown after the review period).</i>")
        vetoes = [t for t in txs if t.get("type") == "TRADE_VETO"]
        upholds = [t for t in txs if t.get("type") == "TRADE_UPHOLD"]
        if vetoes or upholds:
            lines.append(f"Votes: ❌ {len(vetoes)} · ✅ {len(upholds)}")
            if vetoes:
                lines.append("❌ " + ", ".join(esc(self.name(t.get("teamId"))) for t in vetoes))
            if upholds:
                lines.append("✅ " + ", ".join(esc(self.name(t.get("teamId"))) for t in upholds))
        news = [t for t in txs if t["id"] in new_ids]
        if news:
            lines.append("<b>New:</b>")
            for t in news:
                kind = KIND.get(t.get("type"), t.get("type"))
                extra = f" ({t['status'].lower()})" if t.get("type") == "TRADE_PROPOSAL" and t.get("status") not in (None, "EXECUTED") else ""
                lines.append(f"• {kind}{extra} — {esc(self.name(t.get('teamId')))} at {fmt_time(t.get('proposedDate'))}")
        lines.append(f"<code>{esc(key[:8])}</code>")
        return "\n".join(lines)

    def scan(self, days_back):
        ctx = self.refresh()
        periods = range(max(1, ctx["current"] - days_back), ctx["current"] + 1)
        groups = group_trades(self.espn.transactions(periods))
        ids = {i["playerId"] for g in groups.values() for t in g for i in t.get("items", []) if i.get("playerId")}
        players = {i: ctx["known"][i] for i in ids if i in ctx["known"]}
        players.update(self.espn.players(ids - set(players)))
        return groups, players

    def check_trades(self, notify=True):
        groups, players = self.scan(2)
        seen = self.state["seen"]
        sent = 0
        for key, txs in sorted(groups.items(), key=lambda kv: kv[1][-1].get("proposedDate") or 0):
            new_ids = {t["id"] for t in txs if seen.get(t["id"]) != (t.get("status") or "")}
            if new_ids and notify:
                self.tg.send("🔔 <b>Trade update</b>\n" + self.describe(key, txs, players, new_ids))
                sent += 1
            for t in txs:
                seen[t["id"]] = t.get("status") or ""
        self.state["auth_alerted"] = False
        save_state(self.state)
        log(f"Checked trades: {len(groups)} trade(s), {sent} update(s) sent")
        return sent

    def trade_report(self, hours=24 * 7, days_back=7, title="📋 <b>Trades · last 7 days</b>"):
        groups, players = self.scan(days_back)
        cutoff = (time.time() - hours * 3600) * 1000
        active = [(k, g) for k, g in groups.items() if (g[-1].get("proposedDate") or 0) >= cutoff or trade_status(g) == "in_review"]
        active.sort(key=lambda kv: kv[1][-1].get("proposedDate") or 0, reverse=True)
        window = "All season" if days_back >= 60 else f"Last {hours}h" if hours < 48 else f"Last {hours // 24} days"
        counts = {}
        for _, g in active:
            counts[trade_status(g)] = counts.get(trade_status(g), 0) + 1
        summary = " · ".join(f"{STATUS_LABEL[k]} {v}" for k, v in counts.items())
        head = f"{title}\n<i>{window} of activity plus every trade still in review</i>"
        if not active:
            return f"{head}\n\nNo trade activity."
        return f"{head}\n{len(active)} trade(s): {summary}\n\n" + "\n\n".join(self.describe(k, g, players) for k, g in active)

    def season_report(self):
        ctx = self.refresh()
        return self.trade_report(hours=24 * 400, days_back=max(60, ctx["current"]), title="📚 <b>All trades this season</b>")

    def games_left(self, pro_team, periods):
        sched = self.espn.schedule()
        return sum(1 for p in periods if p in sched.get(pro_team, {}).get("games", {}))

    def roster(self, team_id):
        team = next(t for t in self.ctx["league"]["teams"] if t["id"] == team_id)
        out = []
        for e in team.get("roster", {}).get("entries", []):
            p = e.get("playerPoolEntry", {}).get("player", {})
            out.append({
                "name": p.get("fullName", "?"),
                "pro": p.get("proTeamId"),
                "ir": e.get("lineupSlotId") == IR_SLOT,
                "injury": p.get("injuryStatus") if p.get("injuryStatus") not in (None, "ACTIVE", "NORMAL") else "",
            })
        return out

    def opponent_report(self):
        ctx = self.refresh()
        my, mp = ctx["my"], ctx["mp"]
        if my is None:
            return "Couldn't work out which team is yours. Check that swid in config.json matches your ESPN login."
        match = next((m for m in self.espn.matchups(ctx["current"]) if m.get("matchupPeriodId") == mp
                      and my in (m.get("home", {}).get("teamId"), m.get("away", {}).get("teamId"))), None)
        if not match or "away" not in match:
            return f"🆚 No opponent found for matchup week {mp} (bye week?)."
        me, opp = (match["home"], match["away"]) if match["home"]["teamId"] == my else (match["away"], match["home"])
        remaining = [p for p in ctx["periods"] if p >= ctx["current"]]

        lines = [f"🆚 <b>Week {mp}: {esc(ctx['teams'][my])} vs {esc(ctx['teams'][opp['teamId']])}</b>"]
        cs_me, cs_opp = me.get("cumulativeScore", {}), opp.get("cumulativeScore", {})
        by_me, by_opp = cs_me.get("scoreByStat") or {}, cs_opp.get("scoreByStat") or {}
        if by_me:
            w, l, t = cs_me.get("wins", 0), cs_me.get("losses", 0), cs_me.get("ties", 0)
            verdict = "you lead" if w > l else "you trail" if l > w else "level"
            lines.append(f"Categories: <b>{w}-{l}-{t}</b> ({verdict})")
            rows = ["CAT    YOU     OPP"]
            for sid, cat in STATS.items():
                if sid in by_me:
                    res = {"WIN": "W", "LOSS": "L", "TIE": "T"}.get(by_me[sid].get("result"), "")
                    rows.append(f"{cat:<5} {fmt_stat(sid, by_me[sid].get('score')):>6}  {fmt_stat(sid, by_opp.get(sid, {}).get('score')):>6}  {res}")
            lines.append("<pre>" + esc("\n".join(rows)) + "</pre>")
        else:
            lines.append("The matchup hasn't started scoring yet.")

        mine, theirs = self.roster(my), self.roster(opp["teamId"])
        g_me = sum(self.games_left(p["pro"], remaining) for p in mine if not p["ir"] and p["injury"] != "OUT")
        g_opp = sum(self.games_left(p["pro"], remaining) for p in theirs if not p["ir"] and p["injury"] != "OUT")
        lines.append(f"Games left this week (healthy, non-IR players): <b>you {g_me}</b> · them {g_opp}")
        low = sorted((p for p in mine if not p["ir"]), key=lambda p: self.games_left(p["pro"], remaining))[:4]
        if low:
            lines.append("Your fewest games left: " + ", ".join(f"{esc(p['name'])} ({self.games_left(p['pro'], remaining)})" for p in low))
        hurt = [p for p in mine if p["injury"] and not p["ir"]]
        if hurt:
            lines.append("Injured on your active roster: " + ", ".join(f"{esc(p['name'])} ({p['injury'].replace('_', ' ').lower()})" for p in hurt))
        return "\n".join(lines)

    def games_block(self, title, periods, sched):
        if not periods:
            return f"{title}\nNo games left."
        dates = [d for t in sched.values() for p in periods for d in t["games"].get(p, []) if d]
        span = f" ({datetime.fromtimestamp(min(dates) / 1000):%a %d %b} – {datetime.fromtimestamp(max(dates) / 1000):%a %d %b})" if dates else ""
        by = {}
        for t in sched.values():
            by.setdefault(sum(1 for p in periods if p in t["games"]), []).append(t["abbrev"])
        lines = [f"{title}{span}"]
        for rank, (n, teams) in enumerate(sorted(by.items(), reverse=True)):
            icon = "🔥" if rank == 0 and n > 0 else "▫️"
            lines.append(f"{icon} <b>{n} games:</b> {', '.join(sorted(teams))}")
        return "\n".join(lines)

    def games_report(self):
        ctx = self.refresh()
        sched = self.espn.schedule()
        mp = ctx["mp"]
        remaining = [p for p in ctx["periods"] if p >= ctx["current"]]
        label = "full week" if len(remaining) == len(ctx["periods"]) else "still to play"
        blocks = [f"📅 <b>NBA games per team</b>",
                  self.games_block(f"<b>This week · week {mp} ({label})</b>", remaining, sched)]
        nxt = ctx["mps"].get(mp + 1) if mp else None
        if nxt:
            blocks.append(self.games_block(f"<b>Next week · week {mp + 1}</b>", nxt, sched))
        return "\n\n".join(blocks)

    def free_agent_report(self):
        ctx = self.refresh()
        sched = self.espn.schedule()
        remaining = [p for p in ctx["periods"] if p >= ctx["current"]]
        nxt = ctx["mps"].get(ctx["mp"] + 1, []) if ctx["mp"] else []
        rows = []
        for p in self.espn.free_agents(ctx["current"]):
            info = p.get("player", {})
            inj = info.get("injuryStatus") or ""
            if inj == "OUT":
                continue
            pro = info.get("proTeamId")
            rows.append({
                "name": info.get("fullName", "?"),
                "pos": POSITIONS.get(info.get("defaultPositionId"), ""),
                "team": sched.get(pro, {}).get("abbrev", "FA"),
                "games": self.games_left(pro, remaining),
                "next": self.games_left(pro, nxt),
                "own": (info.get("ownership") or {}).get("percentOwned", 0.0),
                "dtd": inj == "DAY_TO_DAY",
            })
        rows.sort(key=lambda r: (-r["games"], -r["next"], -r["own"]))
        rows = rows[: int(self.cfg["free_agents"])]
        if not rows:
            return "🛒 No free agents returned by ESPN."
        table = ["PLAYER             POS TEAM  G NW  OWN"]
        for r in rows:
            nm = (r["name"][:17] + ("*" if r["dtd"] else "")).ljust(18)
            table.append(f"{nm} {r['pos']:<3} {r['team']:<4} {r['games']:>2} {r['next']:>2} {r['own']:>3.0f}%")
        return ("🛒 <b>Free agents with the most games</b>\n"
                "<i>G = games left this week, NW = games next week. Sorted by G, then NW, then % owned. * = day-to-day</i>\n"
                "<pre>" + esc("\n".join(table)) + "</pre>")

    def daily(self):
        parts = [self.trade_report(hours=24, days_back=3, title=f"☀️ <b>Daily report · {datetime.now():%a %d %b}</b>")]
        try:
            parts.append(self.opponent_report())
        except Exception as e:
            parts.append(f"Opponent report failed: {esc(e)}")
        return "\n\n".join(parts)

    def weekly(self):
        return "\n\n".join([f"🗓 <b>Weekly report · week {self.refresh()['mp']}</b>", self.opponent_report(), self.games_report(), self.free_agent_report()])

    def help_text(self):
        return ("🤖 <b>Fantasy bot commands</b>\n"
                "/today – trades from the last 24h + in review\n"
                "/trades – all trades from the last 7 days\n"
                "/season – every trade this season\n"
                "/opponent – this week's matchup\n"
                "/games – NBA games per team, this week and next\n"
                "/fa – free agents with the most games\n"
                "/weekly – full weekly report\n"
                "/check – check for trade updates now")

    def handle(self, update):
        msg = update.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != self.tg.chat_id:
            return
        cmd = (msg.get("text") or "").split()[0].split("@")[0].lower() if msg.get("text") else ""
        actions = {
            "/start": self.help_text, "/help": self.help_text,
            "/today": lambda: self.trade_report(hours=24, days_back=3, title="📋 <b>Trades · last 24h</b>"),
            "/trades": self.trade_report, "/week": self.trade_report,
            "/season": self.season_report,
            "/opponent": self.opponent_report, "/games": self.games_report,
            "/fa": self.free_agent_report, "/weekly": self.weekly,
            "/check": lambda: f"Checked: {self.check_trades()} update(s).",
        }
        if cmd in actions:
            self.safe(lambda: self.tg.send(actions[cmd]()))

    def safe(self, fn):
        try:
            return fn()
        except EspnError as e:
            log(f"ESPN error: {e}")
            if "cookies" in str(e) and not self.state.get("auth_alerted"):
                self.tg.send(f"⚠️ {esc(e)}")
                self.state["auth_alerted"] = True
                save_state(self.state)
        except requests.RequestException as e:
            log(f"Network error: {e}")
        except Exception:
            log(traceback.format_exc())

    def due(self, hhmm, last_key, key):
        now = datetime.now()
        return now.strftime("%H:%M") >= hhmm and self.state.get(last_key) != key

    def run_schedules(self):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self.due(self.cfg["daily_time"], "last_daily", today):
            self.state["last_daily"] = today
            save_state(self.state)
            self.safe(lambda: self.tg.send(self.daily()))
        week_key = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]}"
        if DAYS[now.weekday()] == self.cfg["weekly_day"] and self.due(self.cfg["weekly_time"], "last_weekly", week_key):
            self.state["last_weekly"] = week_key
            save_state(self.state)
            self.safe(lambda: self.tg.send(self.weekly()))

    def run(self):
        self.tg.set_commands(COMMANDS)
        first_run = not self.state["seen"]
        if first_run:
            self.state["last_daily"] = datetime.now().strftime("%Y-%m-%d")
            self.safe(lambda: self.check_trades(notify=False))
            self.safe(lambda: self.tg.send("🤖 Fantasy bot is running. I'll message you on every trade update. Send /help for commands.\n\n" + self.trade_report()))
        else:
            self.tg.send("🤖 Fantasy bot restarted. Send /help for commands.")
        next_poll = 0 if not first_run else time.time() + self.cfg["poll_minutes"] * 60
        while True:
            if time.time() >= next_poll:
                self.safe(self.check_trades)
                next_poll = time.time() + int(self.cfg["poll_minutes"]) * 60
            self.run_schedules()
            try:
                for u in self.tg.updates():
                    self.handle(u)
            except requests.RequestException as e:
                log(f"Telegram polling error: {e}")
                time.sleep(10)

    def run_once(self):
        self.tg.set_commands(COMMANDS)
        self.tg.offset = self.state.get("tg_offset")
        if not self.state["seen"]:
            self.state["last_daily"] = datetime.now().strftime("%Y-%m-%d")
            self.safe(lambda: self.check_trades(notify=False))
            self.safe(lambda: self.tg.send("🤖 Fantasy bot is running on GitHub Actions. Send /help for commands.\n\n" + self.trade_report()))
        else:
            self.safe(self.check_trades)
        self.run_schedules()
        try:
            for u in self.tg.updates(timeout=0):
                self.handle(u)
        except requests.RequestException as e:
            log(f"Telegram polling error: {e}")
        self.state["tg_offset"] = self.tg.offset
        save_state(self.state)
        log("Run finished")


def find_chat(token):
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    r.raise_for_status()
    chats = {}
    for u in r.json().get("result", []):
        c = (u.get("message") or {}).get("chat", {})
        if c:
            chats[c["id"]] = c.get("username") or c.get("title") or c.get("first_name")
    if not chats:
        print("No messages yet. Open your bot in Telegram, send it /start, then run this again.")
    for cid, name in chats.items():
        print(f"chat id: {cid}   ({name})")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--find-chat", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    cfg = load_config()

    if not cfg.get("telegram_token"):
        sys.exit("Missing telegram_token in config.json")
    if a.find_chat:
        return find_chat(cfg["telegram_token"])
    missing = [k for k in ("espn_s2", "swid", "telegram_chat_id") if not cfg.get(k) or "PASTE" in str(cfg.get(k))]
    if missing:
        sys.exit(f"Missing in config.json: {', '.join(missing)}")

    bot = Bot(cfg)
    if a.test:
        bot.tg.send("✅ Test message from your fantasy bot")
        for name, fn in [("trades (7 days)", bot.trade_report), ("opponent", bot.opponent_report), ("games", bot.games_report), ("free agents", bot.free_agent_report)]:
            try:
                bot.tg.send(fn())
                print(f"sent {name} report")
            except Exception as e:
                print(f"{name} report failed: {e}")
                traceback.print_exc()
        return
    if a.once:
        return bot.run_once()
    log("Fantasy bot started")
    bot.run()


if __name__ == "__main__":
    main()