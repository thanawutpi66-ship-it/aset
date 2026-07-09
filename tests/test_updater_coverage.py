import pytest
from unittest.mock import patch
from aset_batt.services.updater import check_for_updates, apply_update, repo_root, current_branch, _git_env, _run_git

def test_updater_functions():
    with patch('aset_batt.services.updater._run_git') as mock_git:
        # Mock repo_root
        mock_git.return_value = (0, "/fake/repo", "")
        root = repo_root("/fake")
        assert root == "/fake/repo"
        
        # Mock current_branch
        mock_git.return_value = (0, "main", "")
        branch = current_branch("/fake/repo")
        assert branch == "main"
        
        # check_for_updates - success
        mock_git.side_effect = [
            (0, "main", ""),        # current_branch
            (0, "", ""),            # fetch
            (0, "2", ""),           # rev-list count
            (0, "Update MSG", "")   # log
        ]
        res = check_for_updates("/fake/repo")
        assert res is not None
        assert res["behind"] == 2
        assert res["subject"] == "Update MSG"
        
        # check_for_updates - fail fetch
        mock_git.side_effect = [
            (0, "main", ""),
            (-1, "", "Error")
        ]
        assert check_for_updates("/fake/repo") is None
        
        # apply_update - success
        mock_git.side_effect = [
            (0, "main", ""),
            (0, "Updated", "")
        ]
        ok, msg = apply_update("/fake/repo")
        assert ok is True
        assert msg == "Updated"
        
        # apply_update - fail
        mock_git.side_effect = [
            (0, "main", ""),
            (-1, "", "Conflict")
        ]
        ok, msg = apply_update("/fake/repo")
        assert ok is False

def test_run_git():
    with patch('aset_batt.services.updater.subprocess.run') as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "out"
        mock_run.return_value.stderr = "err"
        
        rc, out, err = _run_git(["status"], "/fake")
        assert rc == 0
        assert out == "out"
        
        env = _git_env()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
