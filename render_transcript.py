#!/usr/bin/env python3
"""Python renderer for opencode-resume session transcripts.

Replaces the bash render_transcript_impl() to remove the per-message sed
forks and the per-tool python3 -m json.tool forks. Produces output
byte-identical to the bash version so the shared transcripts/ cache and
the existing search/preview paths keep working.

Called by session_transcript_cache() in opencode-resume when python3 is
available; the bash render_transcript_impl() remains as a fallback.

Colors are passed in via OPENCODE_RESUME_ANSI_* env vars (populated by
init_colors/export_ansi_for_py in opencode-resume). Unset/empty => plain
output.

Usage:
  python3 render_transcript.py <sid> --db <path> [--short|--full|--timestamps] \
                                        --first N --last N
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys

# ANSI helpers -- read once at startup
ANSI = {name: os.environ.get(f"OPENCODE_RESUME_ANSI_{name}", "")
        for name in ("RESET", "BOLD", "DIM", "CYAN", "GREEN", "GREEN_BOLD",
                     "BLUE_BOLD", "YELLOW_BOLD", "RED", "USER", "ASSISTANT",
                     "TOOL_TAG", "TOOL_USE", "TOOL_RESULT", "SKIPPED",
                     "TIMESTAMP")}


def human_ts(ms: int) -> str:
    if not ms or ms <= 0:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %a %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def model_name(model_json: str) -> str:
    if not model_json:
        return "?"
    # Match bash: sed 's/.*"id":"//; s/".*//', fallback sed 's/.*"providerID":"//; s/".*//'.
    # bash's sed greedy matches from the FIRST '"id":"' (leftmost) by default — but
    # actually sed BRE `.*"id":"` is greedy left-to-right: .* matches as much as
    # possible while still allowing the rest to match. So it finds the LAST
    # '"id":"' before the closing `"`. For our purposes the JSON has at most one
    # id; both approaches give the same result. Use json for robustness.
    try:
        obj = json.loads(model_json)
        mid = obj.get("id")
        if mid:
            return mid
        prov = obj.get("providerID")
        if prov:
            return prov
    except (json.JSONDecodeError, TypeError):
        pass
    return "?"


def short_text(text: str, nl_placeholder: str) -> str:
    # bash: text%%NL_PLACEHOLDER* then text%%$'\n'* then text[:120]
    if nl_placeholder in text:
        text = text.split(nl_placeholder, 1)[0]
    text = text.split("\n", 1)[0]
    return text[:120]


def indent_lines(text: str) -> str:
    # bash: while IFS= read -r _line; do printf '  %s\n' "$_line"; done <<< "$mtext"
    # Equivalent: splitlines keeps the original newline semantics; bash's read
    # over a here-string gives each line including a final empty line if the
    # string ends with \n. Python str.splitlines() drops the trailing empty line.
    # Use split('\n') to match: if text ends with \n, that yields a trailing ''.
    lines = text.split("\n")
    return "".join(f"  {ln}\n" for ln in lines)


def fmt_age(seconds: int) -> str:
    # Match bash format_age exactly.
    s = max(int(seconds), 0)
    if s < 60:        return f"{s:2d}s"
    if s < 3600:      return f"{s // 60:2d}m"
    if s < 86400:     return f"{s // 3600:2d}h"
    if s < 604800:    return f"{s // 86400:2d}d"
    if s < 2592000:   return f"{s // 604800:2d}w"
    return f"{s // 2592000:2d}mo"


def session_header(cur: sqlite3.Cursor, sid: str) -> tuple[list, str] | None:
    row = cur.execute(
        """
        SELECT COALESCE(id, ''), COALESCE(slug, ''), COALESCE(title, ''),
               COALESCE(directory, ''), COALESCE(agent, ''), COALESCE(model, ''),
               COALESCE(time_created, 0), COALESCE(time_updated, 0),
               COALESCE(version, ''), COALESCE(parent_id, ''),
               (SELECT COUNT(*) FROM message WHERE session_id = s.id),
               (SELECT COUNT(*) FROM message WHERE session_id = s.id
                  AND json_extract(data, '$.role') = 'user')
        FROM session s WHERE s.id = ?
        """, (sid,)).fetchone()
    if not row:
        sys.stderr.write(f"{ANSI['RED']}Session not found: {sid}{ANSI['RESET']}\n")
        return None
    (id_, slug, title, directory, agent, model_json, time_created, time_updated,
     version, parent_id, msg_count, user_msg_count) = row
    display_title = title or "Untitled"
    if display_title == slug:
        display_title = "Untitled"
    out: list[str] = []
    out.append(f"{ANSI['BOLD']}# {display_title} ({slug}){ANSI['RESET']}\n\n")
    # Match bash printf '%s**Session:**%s   \`%s\`\n' exactly — bash printf
    # leaves the backslash-before-backtick literal, so the cache contains
    # `\id\` around the session id. Keep it for cache compatibility.
    out.append(f"{ANSI['DIM']}**Session:**{ANSI['RESET']}   \\`{id_}\\`\n")
    out.append(f"{ANSI['DIM']}**Slug:**{ANSI['RESET']}      {slug}\n")
    if directory:
        out.append(f"{ANSI['DIM']}**Directory:**{ANSI['RESET']} {directory}\n")
    if agent:
        out.append(f"{ANSI['DIM']}**Agent:**{ANSI['RESET']}     {agent}\n")
    mn = model_name(model_json)
    if mn and mn != "?":
        out.append(f"{ANSI['DIM']}**Model:**{ANSI['RESET']}     {mn}\n")
    if parent_id:
        out.append(f"{ANSI['DIM']}**Fork of:**{ANSI['RESET']}   {parent_id}\n")
    if version:
        out.append(f"{ANSI['DIM']}**Version:**{ANSI['RESET']}   {version}\n")
    ct = human_ts(time_created)
    if ct:
        out.append(f"{ANSI['DIM']}**Created:**{ANSI['RESET']}  {ct}\n")
    ut = human_ts(time_updated)
    if ut:
        out.append(f"{ANSI['DIM']}**Updated:**{ANSI['RESET']}  {ut}\n")
    out.append(f"{ANSI['DIM']}**Messages:**{ANSI['RESET']} {msg_count or 0} total, {user_msg_count or 0} user\n\n")
    meta = {"msg_count": msg_count or 0, "user_msg_count": user_msg_count or 0}
    return out, json.dumps(meta)


def assemble_messages(cur: sqlite3.Cursor, sid: str, full_mode: bool) -> tuple[list, list] | tuple[str, list]:
    # Returns (messages, tool_records). Each message is dict with keys
    # role, time, agent, model, text, tool_start, tool_end. If no rows,
    # returns the sentinel ('empty', []).
    if full_mode:
        input_expr = "REPLACE(COALESCE(json_extract(p.data, '$.state.input'), ''), CHAR(10), '_NL_')"
        output_expr = "REPLACE(COALESCE(json_extract(p.data, '$.state.output'), ''), CHAR(10), '_NL_')"
    else:
        input_expr = "''"
        output_expr = "''"
    q = f"""
        SELECT m.id,
               json_extract(m.data, '$.role'),
               COALESCE(json_extract(m.data, '$.time.created'), '0'),
               COALESCE(json_extract(m.data, '$.agent'), ''),
               COALESCE(json_extract(m.data, '$.modelID'), ''),
               json_extract(p.data, '$.type'),
               REPLACE(COALESCE(json_extract(p.data, '$.text'), ''), CHAR(10), '_NL_'),
               COALESCE(json_extract(p.data, '$.tool'), ''),
               COALESCE(json_extract(p.data, '$.state.status'), ''),
               COALESCE(json_extract(p.data, '$.callID'), ''),
               {input_expr},
               {output_expr}
        FROM message m JOIN part p ON p.message_id = m.id
        WHERE m.session_id = ?
        ORDER BY m.time_created, p.time_created
    """
    messages: list[dict] = []
    tool_records: list[tuple[str, str, str, str, str]] = []
    cur_mid: str | None = None
    cur_msg: dict | None = None
    for row in cur.execute(q, (sid,)):
        (mid, role, mtime, agent, model, ptype, ptext, ptool, pstatus, pcid, pinput, poutput) = row
        if not mid:
            continue
        if mid != cur_mid:
            if cur_mid is not None and cur_msg is not None:
                cur_msg["tool_end"] = len(tool_records)
                messages.append(cur_msg)
            cur_mid = mid
            cur_msg = {
                "role": role or "",
                "time": int(mtime or 0),
                "agent": agent or "",
                "model": model or "",
                "text": "",
                "tool_start": len(tool_records),
                "tool_end": len(tool_records),
            }
        if ptype == "text":
            if ptext:
                cur_msg["text"] += ptext + "\n"
        elif ptype == "reasoning":
            if full_mode and ptext:
                cur_msg["text"] += f"{ANSI['DIM']}∘ {ptext}{ANSI['RESET']}\n"
        elif ptype == "tool":
            tool_records.append((ptool or "", pstatus or "", pinput or "", poutput or "", pcid or ""))
    if cur_msg is not None:
        cur_msg["tool_end"] = len(tool_records)
        messages.append(cur_msg)
    if not messages:
        return "empty", []
    return messages, tool_records


def render_short(messages: list[dict], first_n: int, last_n: int) -> str:
    total = len(messages)
    out: list[str] = []
    shown = 0
    for i, m in enumerate(messages):
        if not (i < first_n or i >= total - last_n):
            continue
        role = m["role"]
        icon = (f"{ANSI['USER']}❯{ANSI['RESET']}" if role == "user"
                else f"{ANSI['BLUE_BOLD']}●{ANSI['RESET']}" if role == "assistant"
                else " ")
        text = short_text(m["text"], "_NL_")
        if not text:
            text = "_(no text)_"
        # bash: printf '%s %s %s\n' "$sicon" "${srole^}" "$stext"
        # ${srole^} capitalises first character. Python: role[:1].upper() + role[1:].
        role_cap = role[:1].upper() + role[1:] if role else role
        out.append(f"{icon} {role_cap} {text}\n")
        shown += 1
    if shown < total:
        out.append(f"{ANSI['SKIPPED']}  … ({total - shown} messages hidden) …{ANSI['RESET']}\n")
    return "".join(out)


def pretty_json(text: str) -> str:
    # Match bash's `python3 -m json.tool` default indent (4 spaces). We do NOT
    # reproduce pygments syntax highlighting — that required a per-tool fork
    # and a pygments dependency, which is exactly what Tier B removes. Plain
    # JSON is functionally equivalent for search/grep and avoids the fork.
    try:
        return json.dumps(json.loads(text), indent=4)
    except (json.JSONDecodeError, TypeError):
        return ""


def render_tool(name: str, status: str, tinput: str, toutput: str, cid: str) -> str:
    # Note: cid is the 5th field but bash never renders it. Match.
    out: list[str] = []
    tinput = tinput.replace("_NL_", "\n")
    toutput = toutput.replace("_NL_", "\n")
    status_color = ANSI["TOOL_TAG"]
    if status == "error":
        status_color = ANSI["RED"]
    # printf '  %s∘ %s%s [%s]%s\n' "$color_dim" "$tname" "$color_reset" "$status_color$tstatus$color_reset" "$color_reset"
    out.append(f"  {ANSI['DIM']}∘ {name}{ANSI['RESET']} [{status_color}{status}{ANSI['RESET']}]\n")
    if tinput and tinput != "null":
        out.append(f"    {ANSI['TOOL_USE']}Input:{ANSI['RESET']}\n")
        pretty = pretty_json(tinput)
        if pretty:
            for iline in pretty.splitlines()[:10]:
                out.append(f"      {iline}\n")
        else:
            out.append(f"      {tinput[:300]}\n")
    if toutput and toutput != "null":
        # bash: first 2000 chars, then first 20 lines
        out.append(f"    {ANSI['TOOL_RESULT']}── output ──{ANSI['RESET']}\n")
        preview = toutput[:2000]
        lines = preview.splitlines()[:20]
        for oline in lines:
            out.append(f"      {oline}\n")
        out.append(f"    {ANSI['TOOL_RESULT']}────────────{ANSI['RESET']}\n")
    return "".join(out)


def render_full_or_conv(messages: list[dict], tool_records: list[tuple[str, str, str, str, str]],
                        full_mode: bool, ts_mode: bool) -> str:
    out: list[str] = []
    for i, m in enumerate(messages):
        if i > 0:
            out.append("\n")
        role = m["role"]
        mtime = m["time"]
        agent = m["agent"]
        model = m["model"]
        if role == "user":
            icon = f"{ANSI['USER']}❯{ANSI['RESET']}"
            label = f"{ANSI['USER']}User{ANSI['RESET']}"
            if agent:
                label += f" {ANSI['DIM']}({agent}){ANSI['RESET']}"
        elif role == "assistant":
            icon = f"{ANSI['BLUE_BOLD']}●{ANSI['RESET']}"
            label = "Assistant"
            if ANSI["ASSISTANT"]:
                label = f"{ANSI['ASSISTANT']}{label}{ANSI['RESET']}"
            if model:
                label += f" {ANSI['DIM']}({model}){ANSI['RESET']}"
        else:
            icon = " "
            label = role
        if ts_mode and mtime > 0:
            ts = human_ts(mtime)
            if ts:
                label += f" {ANSI['TIMESTAMP']}[{ts}]{ANSI['RESET']}"
        out.append(f"{icon} {label}\n")
        text = m["text"]
        if text:
            # Translate _NL_ -> \n, then indent each line by 2 spaces.
            text = text.replace("_NL_", "\n")
            out.append(indent_lines(text))
        if full_mode:
            t_start = m["tool_start"]
            t_end = m["tool_end"]
            if t_end > t_start:
                out.append("\n")
                for j in range(t_start, t_end):
                    out.append(render_tool(*tool_records[j]))
    out.append(f"\n{ANSI['DIM']}" + "─" * 40 + f"{ANSI['RESET']}\n")
    return "".join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("sid")
    p.add_argument("--db", required=True)
    p.add_argument("--short", action="store_true")
    p.add_argument("--full", action="store_true")
    p.add_argument("--timestamps", action="store_true")
    p.add_argument("--first", type=int, default=int(os.environ.get("OPENCODE_RESUME_PREVIEW_FIRST", "15")))
    p.add_argument("--last", type=int, default=int(os.environ.get("OPENCODE_RESUME_PREVIEW_LAST", "15")))
    args = p.parse_args()

    # Validate sid format (matches bash validate_sid).
    import re
    if not re.match(r"^ses_[a-zA-Z0-9_]+$", args.sid):
        sys.stderr.write(f"{ANSI['RED']}Invalid session ID: {args.sid}{ANSI['RESET']}\n")
        return 1

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    hdr = session_header(cur, args.sid)
    if hdr is None:
        return 1
    header_lines, _meta = hdr
    assembled = assemble_messages(cur, args.sid, args.full)
    if isinstance(assembled, tuple) and assembled[0] == "empty":
        sys.stdout.write("".join(header_lines))
        sys.stdout.write("_(no messages)_\n")
        return 0
    messages, tool_records = assembled

    sys.stdout.write("".join(header_lines))

    total = len(messages)
    if args.short and total > args.first + args.last + 2:
        sys.stdout.write(render_short(messages, args.first, args.last))
        return 0
    sys.stdout.write(render_full_or_conv(messages, tool_records, args.full, args.timestamps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
