"""In-app update check/apply via git — powers the GUI's "update available" banner.

เครื่องแล็บรันจาก git clone ของ repo สาธารณะ ดังนั้น "อัปเดต" = fast-forward pull
ของ origin/<branch>. โมดูลนี้ตั้งใจไม่ import Qt และใช้ subprocess ล้วน เพื่อให้
เทสต์ได้และรันนอก UI thread ได้ ทุก failure (ไม่มี git, ออฟไลน์, ไม่ใช่ repo, history
แยกทาง) จะถูกคืนเป็นผลลัพธ์ที่สะอาด — ไม่โยน exception เข้า UI.

ความปลอดภัย: apply ใช้ ``pull --ff-only`` เท่านั้น — ถ้า fast-forward ไม่ได้ (มี local
commit/แก้ค้างที่ชนกัน) จะปฏิเสธแทนที่จะสร้าง merge หรือทิ้ง tree ที่ conflict.
ไฟล์ที่ gitignore (config.json, cloud_token.txt) ไม่ถูกแตะ.
"""
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 20   # s (fetch/pull ให้ยาวกว่านี้เผื่อเน็ตช้า)


def _git_env():
    """env ที่ทำให้ git ไม่บล็อกรอ input เด็ดขาด — ถ้า remote จะถาม credential (เช่น
    URL เพี้ยน) ให้ล้มเหลวทันทีแทนที่จะค้าง subprocess ค้าง UI ปุ่ม 'Updating…' ตลอดกาล."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"    # ห้าม prompt ที่ terminal (git จะ fail แทนค้าง)
    env["GCM_INTERACTIVE"] = "never"    # Git Credential Manager: ห้าม popup ถาม login
    return env


def _run_git(args, cwd, timeout=_GIT_TIMEOUT):
    """รัน git command — คืน (rc, stdout, stderr). rc=-1 ถ้ารัน git ไม่ได้เลย.

    encoding=utf-8/errors=replace: commit subject ภาษาไทยจะไม่ทำ decode พังบน console
    ที่ตั้ง codepage เป็น cp1252 (default ของ subprocess text-mode บน Windows)."""
    try:
        p = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            env=_git_env(),
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return -1, "", str(e)


def repo_root(start=None):
    """path เต็มของ git repo root ที่มีแพ็กเกจนี้อยู่ — None ถ้าไม่ใช่ repo/ไม่มี git."""
    start = start or os.path.dirname(os.path.abspath(__file__))
    rc, out, _ = _run_git(["rev-parse", "--show-toplevel"], cwd=start)
    return out if rc == 0 and out else None


def current_branch(repo_dir):
    rc, out, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
    return out if rc == 0 and out else "main"


def check_for_updates(repo_dir):
    """Fetch แล้วรายงานว่าตามหลัง origin/<branch> อยู่กี่ commit.

    คืน ``{"behind": int, "subject": str, "branch": str}`` เมื่อเช็คสำเร็จ
    (behind=0 = ล่าสุดแล้ว), หรือ ``None`` เมื่อเช็คไม่ได้ (ไม่มี git, ออฟไลน์,
    ไม่ใช่ repo). ไม่โยน exception."""
    if not repo_dir:
        return None
    branch = current_branch(repo_dir)
    rc, _, err = _run_git(["fetch", "--quiet", "origin", branch],
                          cwd=repo_dir, timeout=30)
    if rc != 0:
        logger.debug("update check: fetch failed: %s", err)
        return None
    rc, out, _ = _run_git(["rev-list", "--count", f"HEAD..origin/{branch}"],
                          cwd=repo_dir)
    if rc != 0:
        return None
    try:
        behind = int(out)
    except ValueError:
        return None
    subject = ""
    if behind:
        rc2, out2, _ = _run_git(["log", "-1", "--format=%s", f"origin/{branch}"],
                                cwd=repo_dir)
        subject = out2 if rc2 == 0 else ""
    return {"behind": behind, "subject": subject, "branch": branch}


def apply_update(repo_dir):
    """Fast-forward local branch ไปที่ origin/<branch>. คืน (ok, message).

    ``--ff-only`` ปฏิเสธถ้า history แยกทาง (มี local commit / แก้ค้างชนกัน) แทนที่จะ
    สร้าง merge หรือทิ้ง tree ที่ conflict — เครื่องแล็บควรเป็นผู้บริโภคล้วน ถ้า
    fast-forward ไม่ได้แปลว่าต้องให้คนดู."""
    if not repo_dir:
        return False, "ไม่ใช่ git repository"
    branch = current_branch(repo_dir)
    rc, out, err = _run_git(["pull", "--ff-only", "origin", branch],
                            cwd=repo_dir, timeout=120)
    if rc == 0:
        return True, out or "Updated."
    return False, (err or out or "update failed")
