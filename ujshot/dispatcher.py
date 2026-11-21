"""Local agent dispatcher for the ujshot review board.

Spawns configured local coding agents (codex, claude, agy, opencode, or custom
``[agents.<id>]`` entries in ``ujshot.toml``) in the background with a strict
per-repository serial queue so concurrent UI fix requests never collide on the
same working copy.
"""

from __future__ import annotations

import datetime
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


MAX_LOG_LINES = 400


@dataclass
class AgentSpec:
    id: str
    label: str
    cmd: List[str]
    resume_cmd: List[str] = field(default_factory=list)
    stdin: bool = True
    available: bool = False
    source: str = "builtin"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "available": self.available,
            "source": self.source,
            "supportsResume": bool(self.resume_cmd),
            "cmd": self.cmd,
        }


BUILTIN_AGENTS: List[AgentSpec] = [
    AgentSpec(
        id="codex",
        label="Codex",
        cmd=["codex", "exec", "--full-auto", "-"],
        resume_cmd=["codex", "exec", "resume", "{session_id}", "-"],
        stdin=True,
    ),
    AgentSpec(
        id="claude",
        label="Claude Code",
        cmd=[
            "claude", "-p",
            "--verbose",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
        ],
        resume_cmd=[
            "claude", "-p",
            "--resume", "{session_id}",
            "--verbose",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
        ],
        stdin=True,
    ),
    AgentSpec(
        id="agy",
        label="Antigravity",
        cmd=[
            "agy", "-p",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "{prompt}",
        ],
        resume_cmd=[
            "agy", "-p",
            "--conversation", "{session_id}",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "{prompt}",
        ],
        stdin=False,
    ),
    AgentSpec(
        id="opencode",
        label="OpenCode",
        cmd=["opencode", "run", "{prompt}"],
        resume_cmd=["opencode", "run", "-s", "{session_id}", "{prompt}"],
        stdin=False,
    ),
]


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_fallback_toml_agents(text: str) -> Dict[str, Dict[str, Any]]:
    """Minimal ``[agents.<id>]`` parser for Python 3.9–3.10 where ``tomllib`` is absent."""
    import re
    agents: Dict[str, Dict[str, Any]] = {}
    cur_id = ""
    sec_re = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*$")
    kv_re = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*(.+?)\s*$")
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m_sec = sec_re.match(raw_line)
        if m_sec:
            sec = m_sec.group(1)
            if sec.startswith("agents.") and len(sec) > len("agents."):
                cur_id = sec[len("agents."):]
                agents.setdefault(cur_id, {})
            else:
                cur_id = ""
            continue
        if not cur_id:
            continue
        m_kv = kv_re.match(raw_line)
        if not m_kv:
            continue
        key, val = m_kv.group(1), m_kv.group(2).strip()
        if val.lower() in ("true", "false"):
            agents[cur_id][key] = val.lower() == "true"
            continue
        try:
            agents[cur_id][key] = json.loads(val)
            continue
        except Exception:
            pass
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            agents[cur_id][key] = val[1:-1]
    return agents


def _load_toml_agents(project_dir: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    if not project_dir:
        return {}
    cfg_path = project_dir / "ujshot.toml"
    if not cfg_path.is_file():
        return {}
    try:
        content = cfg_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if tomllib is not None:
        try:
            raw = tomllib.loads(content)
        except Exception:
            return {}
        agents = raw.get("agents")
        return agents if isinstance(agents, dict) else {}
    return _parse_fallback_toml_agents(content)


def discover_agents(project_dir: Optional[Path] = None) -> List[AgentSpec]:
    """Return available built-in + ``ujshot.toml`` ``[agents.<id>]`` specs."""
    by_id: Dict[str, AgentSpec] = {}
    for spec in BUILTIN_AGENTS:
        exe = spec.cmd[0] if spec.cmd else ""
        by_id[spec.id] = AgentSpec(
            id=spec.id,
            label=spec.label,
            cmd=list(spec.cmd),
            resume_cmd=list(spec.resume_cmd),
            stdin=spec.stdin,
            available=bool(exe and shutil.which(exe)),
            source="builtin",
        )

    for agent_id, row in _load_toml_agents(project_dir).items():
        if not isinstance(row, dict):
            continue
        cmd = row.get("cmd")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            continue
        resume_cmd = row.get("resume_cmd") or []
        if not isinstance(resume_cmd, list) or not all(isinstance(x, str) for x in resume_cmd):
            resume_cmd = []
        label = str(row.get("label") or agent_id)
        stdin = bool(row.get("stdin", "{prompt}" not in " ".join(cmd)))
        exe = cmd[0]
        avail = bool(shutil.which(exe) or (os.path.isabs(exe) and os.access(exe, os.X_OK)))
        by_id[agent_id] = AgentSpec(
            id=agent_id,
            label=label,
            cmd=list(cmd),
            resume_cmd=list(resume_cmd),
            stdin=stdin,
            available=avail,
            source="config",
        )

    return list(by_id.values())


def resolve_screenshot_path(board_dir: Path, image: str, appearance: str, raised_in: str = "") -> Path:
    """Resolve the exact screenshot file on disk for an annotation."""
    is_light = appearance == "light"
    if raised_in:
        cand = (board_dir / "after" / "light" / raised_in / image) if is_light \
            else (board_dir / "after" / raised_in / image)
        if cand.is_file():
            return cand
    cand_root = (board_dir / "light" / image) if is_light else (board_dir / image)
    if cand_root.is_file():
        return cand_root
    return cand_root


def build_prompt(
    board_dir: Path,
    project_dir: Path,
    annotations: List[Dict[str, Any]],
    appearance: str = "dark",
    image: str = "",
) -> str:
    """Construct a structured, closed-loop prompt for the dispatched agent."""
    mode = "light" if appearance == "light" else "dark"
    mode_cn = "亮色" if mode == "light" else "暗色"
    ann_file = board_dir / "annotations.json"

    lines = [
        f"你正在项目 `{project_dir}` 中处理来自 ujshot 评审板的 UI 批注修改任务。",
        f"- 评审板目录：`{board_dir}`",
        f"- 批注文件：`{ann_file}`",
        f"- 外观模式：`{mode}`（{mode_cn}）",
        "",
        "## 待处理批注",
    ]
    for a in annotations:
        aid = a.get("id", "?")
        img = a.get("image") or image
        rb = str(a.get("raisedIn") or "")
        shot_path = resolve_screenshot_path(board_dir, img, mode, rb)
        kind = a.get("kind", "pin")
        x = round(float(a.get("x", 0)) * 100)
        y = round(float(a.get("y", 0)) * 100)
        if kind == "box":
            w = round(float(a.get("w", 0)) * 100)
            h = round(float(a.get("h", 0)) * 100)
            loc = f"框选 ({x}%, {y}%, 宽 {w}% × 高 {h}%)"
        else:
            loc = f"图钉 ({x}%, {y}%)"
        lines.append(f"### {aid} · `{img}`（{mode_cn}，提出于 Build {rb or '未知'}）")
        lines.append(f"- 原图路径：`{shot_path}`")
        lines.append(f"- 标注位置：{loc}")
        lines.append(f"- 问题描述：{a.get('text', '').strip()}")
        if a.get("reply"):
            lines.append(f"- 补充说明：{a['reply'].strip()}")
        if a.get("rejectReason"):
            lines.append(f"- 上轮驳回原因：{a['rejectReason'].strip()}")
        lines.append("")

    lines += [
        "## 执行要求（闭环终点）",
        "1. 先阅读原图与项目 `AGENTS.md` 规范，定位根因并在代码中修复上述全部问题。",
        "2. 运行必要的构建与单测验证修复生效。",
        f"3. 使用 `ujshot run --appearance {mode} --board-dir \"{board_dir}\" --board-as-after` 在**相同外观（{mode}）**下重新截图灌回评审板（严禁把暗色/亮色混槽）。",
        f"4. 更新 `{ann_file}` 中对应批注的 `status` 为 `\"fixed\"`、写入本次 `fixedIn` 构建号与简洁的 `aiReply`（说明改了什么、验证结果）。",
        "5. **不要执行 `git commit` 或 `git push`**，保留改动待人工在评审板点击「通过」后再提交。",
    ]
    return "\n".join(lines) + "\n"


@dataclass
class DispatchJob:
    id: str
    board_dir: str
    cwd: str
    agent_id: str
    agent_label: str
    ann_ids: List[str]
    image: str
    appearance: str
    prompt: str
    cmd: List[str]
    use_stdin: bool
    log_path: str
    session_id: str = ""
    resumed_from: str = ""
    status: str = "queued"  # queued | running | done | failed | cancelled | interrupted
    created_at: str = field(default_factory=_iso_now)
    started_at: str = ""
    finished_at: str = ""
    exit_code: Optional[int] = None
    error: str = ""
    log_lines: List[str] = field(default_factory=list)
    _proc: Optional[subprocess.Popen] = field(default=None, repr=False)

    def to_dict(self, include_logs: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "boardDir": self.board_dir,
            "cwd": self.cwd,
            "agentId": self.agent_id,
            "agentLabel": self.agent_label,
            "annIds": list(self.ann_ids),
            "image": self.image,
            "appearance": self.appearance,
            "sessionId": self.session_id,
            "resumedFrom": self.resumed_from,
            "status": self.status,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "exitCode": self.exit_code,
            "error": self.error,
            "logPath": self.log_path,
            "logCount": len(self.log_lines),
        }
        if include_logs:
            out["logLines"] = list(self.log_lines)
        return out


class DispatchManager:
    """Per-repository serial dispatch queue with on-disk job & session persistence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, DispatchJob] = {}
        self._order: List[str] = []
        self._loaded_boards: set = set()
        self._queues: Dict[str, "queue.Queue[str]"] = {}
        self._workers: Dict[str, threading.Thread] = {}

    def _jobs_file(self, board_dir: Path) -> Path:
        return board_dir / ".dispatch-logs" / "jobs.json"

    def _ensure_loaded(self, board_dir: Optional[Path]) -> None:
        if not board_dir:
            return
        key = str(board_dir.resolve())
        with self._lock:
            if key in self._loaded_boards:
                return
            self._loaded_boards.add(key)
        path = self._jobs_file(board_dir)
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        rows = raw.get("jobs") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return
        changed = False
        with self._lock:
            for r in rows:
                if not isinstance(r, dict) or not r.get("id"):
                    continue
                jid = str(r["id"])
                if jid in self._jobs:
                    continue
                st = str(r.get("status") or "done")
                if st in ("queued", "running"):
                    st = "interrupted"
                    changed = True
                job = DispatchJob(
                    id=jid,
                    board_dir=str(r.get("boardDir") or key),
                    cwd=str(r.get("cwd") or ""),
                    agent_id=str(r.get("agentId") or ""),
                    agent_label=str(r.get("agentLabel") or r.get("agentId") or ""),
                    ann_ids=list(r.get("annIds") or []),
                    image=str(r.get("image") or ""),
                    appearance=str(r.get("appearance") or "dark"),
                    prompt="",
                    cmd=[],
                    use_stdin=True,
                    log_path=str(r.get("logPath") or ""),
                    session_id=str(r.get("sessionId") or ""),
                    resumed_from=str(r.get("resumedFrom") or ""),
                    status=st,
                    created_at=str(r.get("createdAt") or ""),
                    started_at=str(r.get("startedAt") or ""),
                    finished_at=str(r.get("finishedAt") or ""),
                    exit_code=r.get("exitCode"),
                    error=str(r.get("error") or ("服务重启中断" if st == "interrupted" else "")),
                    log_lines=list(r.get("logLines") or []),
                )
                self._jobs[jid] = job
                self._order.append(jid)
        if changed:
            self._save_board_jobs(board_dir)

    def _save_board_jobs(self, board_dir: Path) -> None:
        key = str(board_dir.resolve())
        with self._lock:
            rows = [
                self._jobs[jid].to_dict(include_logs=True)
                for jid in self._order
                if self._jobs[jid].board_dir == key
            ][-50:]
        for r in rows:
            if len(r.get("logLines", [])) > 80:
                r["logLines"] = r["logLines"][-80:]
        path = self._jobs_file(board_dir)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "jobs": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _ensure_worker(self, repo_key: str) -> "queue.Queue[str]":
        q = self._queues.get(repo_key)
        if q is None:
            q = queue.Queue()
            self._queues[repo_key] = q
            t = threading.Thread(target=self._worker_loop, args=(repo_key, q), daemon=True)
            self._workers[repo_key] = t
            t.start()
        return q

    def enqueue(
        self,
        board_dir: Path,
        project_dir: Path,
        agent: AgentSpec,
        annotations: List[Dict[str, Any]],
        appearance: str = "dark",
        image: str = "",
        resume_session_id: str = "",
        follow_up: str = "",
    ) -> DispatchJob:
        self._ensure_loaded(board_dir)
        if not agent.available:
            raise ValueError(f"Agent `{agent.id}` 在本机不可用（未在 PATH 中找到 `{agent.cmd[0]}`）")
        if not annotations:
            raise ValueError("没有可派发的批注")

        job_id = "job-" + uuid.uuid4().hex[:8]
        use_resume = bool(resume_session_id and agent.resume_cmd)
        if use_resume:
            base_prompt = build_prompt(board_dir, project_dir, annotations, appearance=appearance, image=image)
            prompt = (follow_up.strip() + "\n\n" + base_prompt) if follow_up.strip() else (
                "请基于上一次会话的上下文继续完成以下批注修复、验证与同外观补拍灌板（不要 git commit）：\n\n" + base_prompt
            )
            template = agent.resume_cmd
        else:
            prompt = build_prompt(board_dir, project_dir, annotations, appearance=appearance, image=image)
            template = agent.cmd

        argv = [
            part.replace("{prompt}", prompt)
                .replace("{session_id}", resume_session_id)
                .replace("{cwd}", str(project_dir))
                .replace("{board_dir}", str(board_dir))
                .replace("{annotations}", str(board_dir / "annotations.json"))
            for part in template
        ]
        log_dir = board_dir / ".dispatch-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(log_dir / f"{job_id}.log")

        job = DispatchJob(
            id=job_id,
            board_dir=str(board_dir.resolve()),
            cwd=str(project_dir.resolve()),
            agent_id=agent.id,
            agent_label=agent.label,
            ann_ids=[str(a.get("id") or "") for a in annotations],
            image=image or str(annotations[0].get("image") or ""),
            appearance="light" if appearance == "light" else "dark",
            prompt=prompt,
            cmd=argv,
            use_stdin=agent.stdin,
            log_path=log_path,
            session_id=resume_session_id if use_resume else "",
            resumed_from=resume_session_id if use_resume else "",
        )
        repo_key = str(project_dir.resolve())
        with self._lock:
            self._jobs[job_id] = job
            self._order.append(job_id)
            q = self._ensure_worker(repo_key)
        self._save_board_jobs(board_dir)
        q.put(job_id)
        return job

    def list_jobs(self, board_dir: Optional[Path] = None, limit: int = 20) -> List[Dict[str, Any]]:
        self._ensure_loaded(board_dir)
        target = str(board_dir.resolve()) if board_dir else None
        with self._lock:
            ids = list(reversed(self._order))
            out: List[Dict[str, Any]] = []
            for jid in ids:
                j = self._jobs[jid]
                if target and j.board_dir != target:
                    continue
                out.append(j.to_dict(include_logs=False))
                if len(out) >= limit:
                    break
            return out

    def get_job(self, job_id: str, include_logs: bool = True) -> Optional[Dict[str, Any]]:
        with self._lock:
            j = self._jobs.get(job_id)
            return j.to_dict(include_logs=include_logs) if j else None

    def cancel(self, job_id: str) -> bool:
        proc: Optional[subprocess.Popen] = None
        bdir: Optional[Path] = None
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return False
            bdir = Path(j.board_dir)
            if j.status == "queued":
                j.status = "cancelled"
                j.finished_at = _iso_now()
                j.log_lines.append("[ujshot] 已取消排队")
                proc = None
            elif j.status == "running":
                j.status = "cancelled"
                proc = j._proc
            else:
                return False
        if bdir is not None:
            self._save_board_jobs(bdir)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        return True

    def _format_display_lines(self, raw: str) -> List[str]:
        """Turn NDJSON ``stream-json`` events into readable live progress lines."""
        s = raw.strip()
        if not s:
            return []
        if not s.startswith("{"):
            return [s]
        try:
            obj = json.loads(s)
        except Exception:
            return [s]
        if not isinstance(obj, dict):
            return [s]
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            model = obj.get("model") or ""
            sid = str(obj.get("session_id") or "")[:8]
            return [f"[会话] 已连接 model={model} id={sid}"]
        if kind == "assistant":
            msg = obj.get("message") or {}
            blocks = msg.get("content") if isinstance(msg, dict) else None
            out: List[str] = []
            if isinstance(blocks, list):
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "tool_use":
                        name = b.get("name") or "tool"
                        inp = b.get("input") or {}
                        arg = ""
                        if isinstance(inp, dict):
                            for k in ("file_path", "path", "command", "pattern", "query", "description"):
                                if inp.get(k):
                                    arg = str(inp[k]).replace("\n", " ")[:120]
                                    break
                        out.append(f"[工具] {name}" + (f" · {arg}" if arg else ""))
                    elif bt == "text":
                        txt = str(b.get("text") or "").strip()
                        if txt:
                            first = txt.splitlines()[0][:200]
                            out.append(f"[回复] {first}")
            return out
        if kind == "result":
            res = str(obj.get("result") or "").strip()
            is_err = bool(obj.get("is_error"))
            turns = obj.get("num_turns")
            prefix = "[错误]" if is_err else "[结果]"
            summary = res.splitlines()[0][:200] if res else ("执行失败" if is_err else "已完成")
            extra = f"（{turns} 轮）" if turns else ""
            return [f"{prefix} {summary}{extra}"]
        if kind in ("user", "rate_limit_event", "stream_event"):
            return []
        return []

    def _extract_session_id(self, raw: str) -> str:
        s = raw.strip()
        if not s.startswith("{"):
            return ""
        try:
            obj = json.loads(s)
        except Exception:
            return ""
        if isinstance(obj, dict) and obj.get("session_id"):
            return str(obj["session_id"])
        return ""

    def _append_log(self, job: DispatchJob, line: str, log_fp: Any) -> None:
        text = line.rstrip("\r\n")
        try:
            log_fp.write(text + "\n")
            log_fp.flush()
        except OSError:
            pass
        sid = self._extract_session_id(text)
        sid_updated = False
        if sid:
            with self._lock:
                if not job.session_id:
                    job.session_id = sid
                    sid_updated = True
        display_lines = self._format_display_lines(text)
        if display_lines:
            with self._lock:
                job.log_lines.extend(display_lines)
                if len(job.log_lines) > MAX_LOG_LINES:
                    del job.log_lines[: len(job.log_lines) - MAX_LOG_LINES]
        if sid_updated:
            self._save_board_jobs(Path(job.board_dir))

    def _worker_loop(self, repo_key: str, q: "queue.Queue[str]") -> None:
        while True:
            job_id = q.get()
            try:
                self._run_one(job_id)
            finally:
                q.task_done()

    def _run_one(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status == "cancelled":
                return
            job.status = "running"
            job.started_at = _iso_now()
        self._save_board_jobs(Path(job.board_dir))

        try:
            with open(job.log_path, "w", encoding="utf-8") as log_fp:
                resume_note = f" · 续接={job.resumed_from[:8]}" if job.resumed_from else ""
                self._append_log(
                    job,
                    f"[ujshot] 启动 {job.agent_label} ({job.agent_id}){resume_note} · cwd={job.cwd} · 批注={','.join(job.ann_ids)}",
                    log_fp,
                )
                proc = subprocess.Popen(
                    job.cmd,
                    cwd=job.cwd,
                    stdin=subprocess.PIPE if job.use_stdin else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                with self._lock:
                    job._proc = proc
                if job.use_stdin and proc.stdin is not None:
                    try:
                        proc.stdin.write(job.prompt)
                        proc.stdin.close()
                    except OSError:
                        pass
                if proc.stdout is not None:
                    for line in proc.stdout:
                        self._append_log(job, line, log_fp)
                rc = proc.wait()
                with self._lock:
                    job._proc = None
                    job.exit_code = rc
                    job.finished_at = _iso_now()
                    if job.status != "cancelled":
                        job.status = "done" if rc == 0 else "failed"
                        if rc != 0:
                            job.error = f"进程退出码 {rc}"
                self._append_log(job, f"[ujshot] 结束：status={job.status} exit={rc}", log_fp)
        except Exception as exc:
            with self._lock:
                job._proc = None
                job.finished_at = _iso_now()
                if job.status != "cancelled":
                    job.status = "failed"
                    job.error = str(exc)
                    job.log_lines.append(f"[ujshot] 启动失败：{exc}")
        finally:
            self._save_board_jobs(Path(job.board_dir))
